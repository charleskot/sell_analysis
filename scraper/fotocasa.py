"""Fotocasa scraper - uses Selenium because listings are client-side rendered.
Requires Chrome/Chromium installed. Falls back gracefully if unavailable.
"""
import json
import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing
from scraper.http_client import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.fotocasa.es"
# Fotocasa blocks plain HTTP scrapers with PerimeterX.
# We use their /api/ prefix which serves the shell, then Selenium to get rendered listings.


class FotocasaScraper(BaseScraper):
    PORTAL_NAME = "fotocasa"

    def __init__(self, http_client: HttpClient, config: dict, respect_robots: bool = True):
        super().__init__(http_client, config, respect_robots)
        self._selenium_available: bool | None = None

    def _check_selenium(self) -> bool:
        if self._selenium_available is None:
            try:
                from scraper.selenium_driver import get_driver
                get_driver(headless=True)
                self._selenium_available = True
                logger.info("Fotocasa: Selenium available")
            except Exception as e:
                self._selenium_available = False
                logger.warning(f"Fotocasa: Selenium not available ({e}). "
                               "Install Chrome/ChromeDriver to scrape Fotocasa. Skipping portal.")
        return self._selenium_available

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        if not self._check_selenium():
            return

        from scraper.selenium_driver import get_page_html

        # Convert /es/ URL to /api/ prefix if not already
        api_url = search_url.replace("www.fotocasa.es/es/", "www.fotocasa.es/api/")
        if "/api/" not in api_url:
            api_url = search_url

        page = 1
        current_url = api_url

        while page <= max_pages:
            logger.info(f"Fotocasa (Selenium) page {page}: {current_url}")

            html = get_page_html(
                current_url,
                wait_selector="[class*='re-Card'], [class*='CardListing'], article[class*='listing']",
                wait_seconds=4.0,
            )

            if html is None:
                logger.warning("Fotocasa: Selenium returned None, stopping")
                break

            soup = BeautifulSoup(html, "lxml")
            listings = self._parse_listings_from_soup(soup)

            if not listings:
                # Try __NEXT_DATA__ as fallback
                listings = self._parse_next_data(html)

            if not listings:
                logger.warning(f"Fotocasa: No listings found on page {page}")
                break

            for listing in listings:
                yield listing

            # Check for next page
            next_url = self._get_next_page_url(soup, current_url, page)
            if not next_url:
                break
            current_url = next_url
            page += 1

    def _parse_listings_from_soup(self, soup: BeautifulSoup) -> list[RawListing]:
        """Parse listing cards from rendered HTML."""
        results = []

        # Fotocasa uses Tailwind CSS class names that change often - try multiple patterns
        card_selectors = [
            "[class*='re-Card']",
            "[data-testid*='card']",
            "[class*='CardListing']",
            "[class*='PropertyCard']",
            "article[class*='property']",
            "article[class*='listing']",
        ]

        cards = []
        for sel in card_selectors:
            cards = soup.select(sel)
            if cards:
                break

        for card in cards:
            listing = self._parse_card(card)
            if listing:
                results.append(listing)

        return results

    def _parse_card(self, card) -> RawListing | None:
        try:
            link = card.select_one("a[href*='/es/comprar/']") or card.select_one("a[href]")
            if not link:
                return None

            href = link.get("href", "")
            url = urljoin(BASE_URL, href) if not href.startswith("http") else href

            m = re.search(r"/(\d+)/?$", href)
            external_id = m.group(1) if m else self.http.hash_content(href)

            title = link.get("title", "") or link.get_text(strip=True)[:200]

            price_el = card.select_one("[class*='price'], [class*='Price']")
            price = self.parse_price(price_el.get_text()) if price_el else None

            area_el = card.select_one("[class*='area'], [class*='surface'], [class*='size']")
            area_m2 = self.parse_area(area_el.get_text()) if area_el else None

            rooms_el = card.select_one("[class*='room'], [class*='bedroom']")
            rooms = self.parse_rooms(rooms_el.get_text()) if rooms_el else None

            loc_el = card.select_one("[class*='location'], [class*='address'], [class*='ubication']")
            district = city = None
            if loc_el:
                loc_text = loc_el.get_text(strip=True)
                parts = [p.strip() for p in loc_text.split(",")]
                if len(parts) >= 2:
                    district = parts[0].lower()
                    city = parts[-1].lower()
                elif parts:
                    city = parts[0].lower()

            lat = lon = None
            for attr in ["data-latitude", "data-lat", "data-lng", "data-longitude"]:
                val = card.get(attr)
                if val:
                    try:
                        if "lat" in attr:
                            lat = float(val)
                        else:
                            lon = float(val)
                    except ValueError:
                        pass

            photos = []
            for img in card.select("img[src]")[:3]:
                src = img.get("src", "")
                if src and not src.endswith(".svg") and "placeholder" not in src:
                    photos.append(src)

            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=str(external_id),
                url=url,
                title=title,
                price=price,
                area_m2=area_m2,
                rooms=rooms,
                district=district,
                city=city or "desconocido",
                latitude=lat,
                longitude=lon,
                photo_urls=photos,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}{rooms}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Fotocasa card: {e}")
            return None

    def _parse_next_data(self, html: str) -> list[RawListing]:
        """Fallback: parse __NEXT_DATA__ JSON blob."""
        try:
            soup = BeautifulSoup(html, "lxml")
            nd = soup.find("script", id="__NEXT_DATA__")
            if not nd:
                return []
            data = json.loads(nd.string)
            items = (
                data.get("props", {}).get("pageProps", {}).get("initialSearch", {}).get("realEstates", [])
                or data.get("props", {}).get("pageProps", {}).get("realEstates", [])
                or []
            )
            return [self._item_from_json(item) for item in items if item]
        except Exception:
            return []

    def _item_from_json(self, item: dict) -> RawListing | None:
        try:
            external_id = str(item.get("id", ""))
            url = item.get("url", "")
            if not url.startswith("http"):
                url = urljoin(BASE_URL, url)

            transactions = item.get("transactions", [{}])
            price = None
            if transactions:
                vals = transactions[0].get("value", [{}])
                if vals:
                    price = float(vals[0].get("amount", 0) or 0) or None

            features = {f.get("key"): f.get("value") for f in item.get("features", [])}
            area_m2 = features.get("constructedArea") or features.get("usableArea")
            rooms = features.get("rooms") or features.get("bedrooms")

            address = item.get("address", {})
            district = (address.get("district") or address.get("neighborhood") or "").lower()
            city = (address.get("city") or address.get("municipality") or "").lower()
            lat = address.get("latitude") or (item.get("coordinates") or {}).get("latitude")
            lon = address.get("longitude") or (item.get("coordinates") or {}).get("longitude")

            photos = [
                m.get("url", "") for m in item.get("multimedia", {}).get("images", [])
                if m.get("url")
            ][:5]

            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=external_id,
                url=url,
                title=item.get("title", "")[:200],
                price=price,
                area_m2=float(area_m2) if area_m2 else None,
                rooms=int(rooms) if rooms else None,
                district=district or None,
                city=city or "desconocido",
                latitude=float(lat) if lat else None,
                longitude=float(lon) if lon else None,
                photo_urls=photos,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Fotocasa JSON item: {e}")
            return None

    def _get_next_page_url(self, soup: BeautifulSoup, current_url: str, page: int) -> str | None:
        next_el = soup.select_one("a[rel='next'], [class*='next'] a, [aria-label='Siguiente'] a")
        if next_el and next_el.get("href"):
            return urljoin(BASE_URL, next_el["href"])
        # Increment page in URL
        if "/l" in current_url:
            if re.search(r"/l\d+$", current_url):
                return re.sub(r"/l\d+$", f"/l{page + 1}", current_url)
            return current_url.rstrip("/") + f"/l{page + 1}"
        return None
