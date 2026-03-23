"""Idealista scraper - uses Selenium with undetected-chromedriver to bypass DataDome.
Requires Chrome/Chromium + undetected-chromedriver installed.
Falls back gracefully if unavailable.

Install: pip install undetected-chromedriver
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


class IdealistaScraper(BaseScraper):
    PORTAL_NAME = "idealista"

    def __init__(self, http_client: HttpClient, config: dict, respect_robots: bool = True):
        # Override: Idealista's robots.txt itself is served behind DataDome.
        # We treat it as unavailable and proceed with our own rate limiting.
        super().__init__(http_client, config, respect_robots=False)
        self._selenium_available: bool | None = None

    def _check_selenium(self) -> bool:
        if self._selenium_available is None:
            try:
                from scraper.selenium_driver import get_driver
                get_driver(headless=True, use_undetected=True)
                self._selenium_available = True
                logger.info("Idealista: undetected-chromedriver available")
            except Exception as e:
                self._selenium_available = False
                logger.warning(
                    f"Idealista: Selenium/Chrome not available ({e}). "
                    "Install undetected-chromedriver + Chrome to scrape Idealista. Skipping."
                )
        return self._selenium_available

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        if not self._check_selenium():
            return

        from scraper.selenium_driver import get_page_html, get_driver

        page = 1
        current_url = search_url

        try:
            driver = get_driver(headless=True, use_undetected=True)
        except Exception:
            return

        while page <= max_pages:
            logger.info(f"Idealista (Selenium) page {page}: {current_url}")
            try:
                driver.get(current_url)
                time.sleep(random.uniform(3, 6))

                # Wait for listings to appear
                html = driver.page_source
            except Exception as e:
                logger.error(f"Idealista Selenium error: {e}")
                break

            soup = BeautifulSoup(html, "lxml")

            # Check for DataDome challenge
            if "captcha" in html.lower() or "just a moment" in html.lower():
                logger.warning("Idealista: Captcha/block detected. Try with visible browser or real proxy.")
                break

            items = soup.select("article.item")
            if not items:
                items = soup.select(".item-info-container")
            if not items:
                items = soup.select("[class*='item-info']")

            if not items:
                logger.warning(f"Idealista: No listings on page {page}")
                # Try JSON-LD
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
                loc_text = loc_el.get_text(strip=True)
                parts = [p.strip() for p in loc_text.split(",")]
                if len(parts) >= 2:
                    district = parts[-2].lower()
                    city = parts[-1].lower()
                elif parts:
                    city = parts[0].lower()

            condition_el = item.select_one("[class*='tag'], [class*='condition']")
            condition = self.normalize_condition(condition_el.get_text()) if condition_el else "desconocido"

            photos = []
            for img in item.select("img[src]")[:3]:
                src = img.get("src", "")
                if src and "logo" not in src:
                    photos.append(src)

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
