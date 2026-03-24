"""Idealista scraper.

Strategy (in order):
1. Plain HTTP request with residential proxy → parse server-side rendered HTML.
   Includes session warm-up (homepage visit) to improve DataDome success rate.
   Works reliably when a good residential proxy is configured.
2. Selenium / undetected-chromedriver fallback.

Set `proxy` in config.yaml scraping section to enable HTTP mode.
"""
import json
import logging
import re
import time
import random
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing
from scraper.http_client import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.idealista.com"

_GOOGLE_REFERER = "https://www.google.es/search?q=idealista+pisos+venta+madrid"
_IDEALISTA_HEADERS = {
    "Referer": _GOOGLE_REFERER,
    "Sec-Fetch-Site": "cross-site",
}


class IdealistaScraper(BaseScraper):
    PORTAL_NAME = "idealista"

    def __init__(self, http_client: HttpClient, config: dict, respect_robots: bool = True):
        # Idealista's robots.txt is itself served behind DataDome — skip robots check.
        super().__init__(http_client, config, respect_robots=False)
        self._selenium_available: bool | None = None
        self._session_warmed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        page = 1
        current_url = search_url

        while page <= max_pages:
            logger.info(f"Idealista page {page}: {current_url}")
            listings: list[RawListing] = []

            # 1) Plain HTTP (works with residential proxy)
            if self.http.has_proxy:
                listings, next_http_url = self._fetch_via_http(current_url)
                if listings:
                    for listing in listings:
                        yield listing
                    if not next_http_url:
                        break
                    current_url = next_http_url
                    page += 1
                    time.sleep(random.uniform(2, 4))
                    continue

            # 2) Selenium fallback
            if not listings and self._check_selenium():
                for listing in self._fetch_via_selenium(search_url, max_pages):
                    yield listing
                return  # Selenium handles its own pagination loop

            if not listings:
                if not self.http.has_proxy:
                    logger.warning(
                        "Idealista: no proxy configured and Selenium unavailable. "
                        "Set scraping.proxy in config.yaml (residential proxy required)."
                    )
                else:
                    logger.warning(f"Idealista: no listings on page {page}, stopping.")
                break

    # ------------------------------------------------------------------
    # HTTP approach
    # ------------------------------------------------------------------

    def _warm_session(self) -> None:
        """Visit homepage to get cookies — significantly improves DataDome score."""
        if self._session_warmed:
            return
        logger.debug("Idealista: warming session on homepage")
        self.http.get(BASE_URL, extra_headers={"Sec-Fetch-Site": "none"})
        time.sleep(random.uniform(1.5, 3.0))
        self._session_warmed = True

    def _fetch_via_http(self, url: str) -> tuple[list[RawListing], str | None]:
        """Returns (listings, next_page_url)."""
        self._warm_session()

        resp = self.http.get(url, referer=_GOOGLE_REFERER, extra_headers=_IDEALISTA_HEADERS)
        if resp is None or len(resp.text) < 500:
            return [], None

        html = resp.text
        if "captcha" in html.lower() or "datadome" in html.lower():
            logger.warning("Idealista HTTP: bot challenge detected. Use a higher-quality residential proxy.")
            return [], None

        soup = BeautifulSoup(html, "lxml")
        items = (
            soup.select("article.item")
            or soup.select(".item-info-container")
            or soup.select("[class*='item-info']")
        )

        if not items:
            # Try JSON-LD as last resort
            listings = self._parse_json_ld_listings(soup)
            return listings, None

        listings = [self._parse_card(item) for item in items]
        listings = [l for l in listings if l]
        logger.info(f"Idealista HTTP: parsed {len(listings)} listings")

        next_url = None
        next_link = soup.select_one("a.icon-arrow-right-after, a[rel='next']")
        if next_link and next_link.get("href"):
            next_url = urljoin(BASE_URL, next_link["href"])

        return listings, next_url

    # ------------------------------------------------------------------
    # Selenium approach
    # ------------------------------------------------------------------

    def _check_selenium(self) -> bool:
        if self._selenium_available is None:
            try:
                from scraper.selenium_driver import get_driver
                get_driver(headless=True, use_undetected=True)
                self._selenium_available = True
                logger.info("Idealista: undetected-chromedriver available")
            except Exception as e:
                self._selenium_available = False
                logger.warning(f"Idealista: Selenium not available ({e}). Skipping Selenium fallback.")
        return self._selenium_available

    def _fetch_via_selenium(self, search_url: str, max_pages: int) -> Iterator[RawListing]:
        from scraper.selenium_driver import get_driver

        try:
            driver = get_driver(headless=True, use_undetected=True)
        except Exception:
            return

        page = 1
        current_url = search_url
        while page <= max_pages:
            logger.info(f"Idealista (Selenium) page {page}: {current_url}")
            try:
                driver.get(current_url)
                time.sleep(random.uniform(3, 6))
                html = driver.page_source
            except Exception as e:
                logger.error(f"Idealista Selenium error: {e}")
                break

            if "captcha" in html.lower() or "just a moment" in html.lower():
                logger.warning("Idealista Selenium: captcha detected, stopping.")
                break

            soup = BeautifulSoup(html, "lxml")
            items = (
                soup.select("article.item")
                or soup.select(".item-info-container")
                or soup.select("[class*='item-info']")
            )

            if not items:
                for listing in self._parse_json_ld_listings(soup):
                    yield listing
                break

            for item in items:
                listing = self._parse_card(item)
                if listing:
                    yield listing

            next_link = soup.select_one("a.icon-arrow-right-after, a[rel='next']")
            if next_link and next_link.get("href"):
                current_url = urljoin(BASE_URL, next_link["href"])
                page += 1
                time.sleep(random.uniform(2, 5))
            else:
                break

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_card(self, item) -> RawListing | None:
        try:
            link = item.select_one("a.item-link") or item.select_one("a[href*='/inmueble/']")
            if not link:
                return None

            href = link.get("href", "")
            url = urljoin(BASE_URL, href)
            m = re.search(r"/inmueble/(\d+)/", href)
            if not m:
                return None
            external_id = m.group(1)
            title = link.get_text(strip=True)[:200] or ""

            price_el = item.select_one(".item-price, [class*='price']")
            price = self.parse_price(price_el.get_text()) if price_el else None

            area_m2 = rooms = bathrooms = None
            for detail in item.select(".item-detail, [class*='detail']"):
                text = detail.get_text(strip=True)
                if "m²" in text or "m2" in text:
                    area_m2 = self.parse_area(text)
                elif "hab" in text.lower():
                    rooms = self.parse_rooms(text)
                elif "baño" in text.lower():
                    bm = re.search(r"(\d+)", text)
                    bathrooms = int(bm.group(1)) if bm else None

            loc_el = item.select_one(".item-location, [class*='location']")
            district = city = None
            if loc_el:
                parts = [p.strip() for p in loc_el.get_text(strip=True).split(",")]
                if len(parts) >= 2:
                    district = parts[-2].lower()
                    city = parts[-1].lower()
                elif parts:
                    city = parts[0].lower()

            condition_el = item.select_one("[class*='tag'], [class*='condition']")
            condition = self.normalize_condition(condition_el.get_text()) if condition_el else "desconocido"

            photos = [
                img.get("src", "") for img in item.select("img[src]")[:3]
                if img.get("src", "") and "logo" not in img["src"]
            ]

            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=external_id,
                url=url,
                title=title,
                price=price,
                area_m2=area_m2,
                rooms=rooms,
                bathrooms=bathrooms,
                condition=condition,
                district=district,
                city=city or "desconocido",
                photo_urls=photos,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}{rooms}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Idealista card: {e}")
            return None

    def _parse_json_ld_listings(self, soup: BeautifulSoup) -> list[RawListing]:
        results = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                if data.get("@type") in ("Apartment", "House", "SingleFamilyResidence", "Residence"):
                    listing = self._json_ld_to_listing(data)
                    if listing:
                        results.append(listing)
            except Exception:
                continue
        return results

    def _json_ld_to_listing(self, data: dict) -> RawListing | None:
        try:
            url = data.get("url", "")
            m = re.search(r"/inmueble/(\d+)/", url)
            if not m:
                return None
            price_spec = data.get("offers", {})
            price = float(price_spec.get("price", 0) or 0) or None
            area_m2 = float(data.get("floorSize", {}).get("value", 0) or 0) or None
            rooms = data.get("numberOfRooms")
            address = data.get("address", {})
            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=m.group(1),
                url=url,
                title=data.get("name", "")[:200],
                price=price,
                area_m2=area_m2,
                rooms=int(rooms) if rooms else None,
                city=address.get("addressLocality", "desconocido").lower(),
                district=address.get("addressRegion", "").lower() or None,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}"),
            )
        except Exception:
            return None
