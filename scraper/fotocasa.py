"""Fotocasa scraper.

Strategy (in order):
1. Plain HTTP request with residential proxy → parse __NEXT_DATA__ JSON or HTML.
   Works reliably when a residential proxy is configured (bypasses PerimeterX IP check).
2. Selenium fallback (requires Chrome/ChromeDriver in the environment).

Set `proxy` in config.yaml scraping section to enable HTTP mode.
"""
import json
import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing, parse_ago_to_hours
from scraper.http_client import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.fotocasa.es"

_FOTOCASA_HEADERS = {
    "Referer": "https://www.google.es/",
    "Sec-Fetch-Site": "cross-site",
}


class FotocasaScraper(BaseScraper):
    PORTAL_NAME = "fotocasa"

    def __init__(self, http_client: HttpClient, config: dict, respect_robots: bool = True):
        super().__init__(http_client, config, respect_robots)
        self._selenium_available: bool | None = None
        self._session_warmed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        page = 1
        current_url = search_url

        while page <= max_pages:
            logger.info(f"Fotocasa page {page}: {current_url}")
            listings: list[RawListing] = []

            # 1) Try plain HTTP always — works with residential IP OR proxy
            listings = self._fetch_via_http(current_url)

            # 2) Selenium fallback if HTTP failed
            if not listings and self._check_selenium():
                listings, next_url_selenium = self._fetch_via_selenium(current_url)
                if listings:
                    for listing in listings:
                        yield listing
                    if not next_url_selenium:
                        break
                    current_url = next_url_selenium
                    page += 1
                    continue

            if not listings:
                logger.warning(f"Fotocasa: no listings on page {page}, stopping.")
                break

            for listing in listings:
                yield listing

            next_url = self._next_page_url(current_url, page)
            if not next_url:
                break
            current_url = next_url
            page += 1

    # ------------------------------------------------------------------
    # HTTP approach
    # ------------------------------------------------------------------

    def _warm_session(self) -> None:
        """Visit homepage once to acquire cookies (helps PerimeterX scoring)."""
        if self._session_warmed:
            return
        self.http.get(BASE_URL, extra_headers=_FOTOCASA_HEADERS)
        self._session_warmed = True

    def _fetch_via_http(self, url: str) -> list[RawListing]:
        self._warm_session()
        resp = self.http.get(url, referer="https://www.google.es/", extra_headers=_FOTOCASA_HEADERS)
        if resp is None or len(resp.text) < 500:
            return []

        # Best source: __NEXT_DATA__ JSON embedded in the page
        listings = self._parse_next_data(resp.text)
        if listings:
            logger.info(f"Fotocasa HTTP: parsed {len(listings)} listings from __NEXT_DATA__")
            return listings

        # Fallback: parse rendered HTML cards
        soup = BeautifulSoup(resp.text, "lxml")
        listings = self._parse_listings_from_soup(soup)
        if listings:
            logger.info(f"Fotocasa HTTP: parsed {len(listings)} listings from HTML")
        return listings

    # ------------------------------------------------------------------
    # Selenium approach
    # ------------------------------------------------------------------

    def _check_selenium(self) -> bool:
        if self._selenium_available is None:
            try:
                from scraper.selenium_driver import get_driver
                get_driver(headless=True, use_undetected=True)
                self._selenium_available = True
                logger.info("Fotocasa: Selenium (undetected) available")
            except Exception as e:
                self._selenium_available = False
                logger.warning(f"Fotocasa: Selenium not available ({e}). Skipping Selenium fallback.")
        return self._selenium_available

    def _fetch_via_selenium(self, url: str) -> tuple[list[RawListing], str | None]:
        """Returns (listings, next_page_url)."""
        from scraper.selenium_driver import get_page_html

        html = get_page_html(
            url,
            wait_selector="[class*='re-Card'], [class*='CardListing'], article[class*='listing']",
            wait_seconds=4.0,
            use_undetected=True,
        )
        if html is None:
            return [], None

        listings = self._parse_next_data(html)
        if not listings:
            soup = BeautifulSoup(html, "lxml")
            listings = self._parse_listings_from_soup(soup)

        soup = BeautifulSoup(html, "lxml") if listings else None
        next_url = self._get_next_page_url_from_soup(soup, url, 1) if soup else None
        return listings, next_url

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_next_data(self, html: str) -> list[RawListing]:
        """Parse __NEXT_DATA__ JSON blob embedded in the page."""
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
            results = [self._item_from_json(item) for item in items if item]
            return [r for r in results if r]
        except Exception:
            return []

    def _parse_listings_from_soup(self, soup: BeautifulSoup) -> list[RawListing]:
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

        results = [self._parse_card(card) for card in cards]
        return [r for r in results if r]

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
                parts = [p.strip() for p in loc_el.get_text(strip=True).split(",")]
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

            photos = [
                img.get("src", "") for img in card.select("img[src]")[:3]
                if img.get("src", "") and not img["src"].endswith(".svg") and "placeholder" not in img["src"]
            ]

            # Publication age from card text (searches 'hace X días', 'nuevo', etc)
            published_ago_hours = parse_ago_to_hours(card.get_text(" ", strip=True))

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
                published_ago_hours=published_ago_hours,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}{rooms}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Fotocasa card: {e}")
            return None

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

            # Publication date from JSON
            published_ago_hours = None
            pub_date = item.get("publicationDate") or item.get("updatedDate")
            if pub_date:
                try:
                    from datetime import datetime, timezone
                    from dateutil import parser as _dp
                    dt = _dp.parse(pub_date) if isinstance(pub_date, str) else datetime.fromtimestamp(pub_date / 1000, tz=timezone.utc)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    delta = datetime.now(timezone.utc) - dt
                    published_ago_hours = int(delta.total_seconds() // 3600)
                except Exception:
                    pass

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
                published_ago_hours=published_ago_hours,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Fotocasa JSON item: {e}")
            return None

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _next_page_url(self, current_url: str, page: int) -> str | None:
        if "/l" in current_url:
            if re.search(r"/l\d+$", current_url):
                return re.sub(r"/l\d+$", f"/l{page + 1}", current_url)
            return current_url.rstrip("/") + f"/l{page + 1}"
        return None

    def _get_next_page_url_from_soup(self, soup: BeautifulSoup, current_url: str, page: int) -> str | None:
        next_el = soup.select_one("a[rel='next'], [class*='next'] a, [aria-label='Siguiente'] a")
        if next_el and next_el.get("href"):
            return urljoin(BASE_URL, next_el["href"])
        return self._next_page_url(current_url, page)
