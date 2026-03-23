"""Idealista scraper."""
import json
import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing
from scraper.http_client import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.idealista.com"


class IdealistaScraper(BaseScraper):
    PORTAL_NAME = "idealista"

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        # Warm up: visit homepage first to get cookies
        home_html = self._get_html(BASE_URL)
        if home_html is None:
            logger.warning("Could not load Idealista homepage - may be blocked")

        current_url = search_url
        page = 1

        while current_url and page <= max_pages:
            logger.info(f"Idealista page {page}: {current_url}")
            html = self._get_html(current_url, referer=BASE_URL if page == 1 else search_url)

            if html is None:
                logger.warning(f"Failed to get page {page} from Idealista")
                break

            soup = BeautifulSoup(html, "lxml")
            items = soup.select("article.item")

            if not items:
                items = soup.select(".item-info-container")

            if not items:
                logger.warning(f"No listings found on Idealista page {page}")
                break

            for item in items:
                listing = self._parse_card(item, search_url)
                if listing:
                    yield listing

            next_link = soup.select_one("a.icon-arrow-right-after")
            if next_link and next_link.get("href"):
                current_url = urljoin(BASE_URL, next_link["href"])
                page += 1
            else:
                break

    def _parse_card(self, item, referer: str) -> RawListing | None:
        try:
            link = item.select_one("a.item-link") or item.select_one("a[href*='/inmueble/']")
            if not link:
                return None

            href = link.get("href", "")
            url = urljoin(BASE_URL, href)
            external_id = re.search(r"/inmueble/(\d+)/", href)
            if not external_id:
                return None
            external_id = external_id.group(1)

            title = link.get_text(strip=True) or ""

            price_el = item.select_one(".item-price") or item.select_one("[class*='price']")
            price = self.parse_price(price_el.get_text()) if price_el else None

            details = item.select(".item-detail")
            area_m2 = rooms = bathrooms = None
            for detail in details:
                text = detail.get_text(strip=True)
                if "m²" in text or "m2" in text:
                    area_m2 = self.parse_area(text)
                elif "hab" in text.lower():
                    rooms = self.parse_rooms(text)

            location_el = item.select_one(".item-location") or item.select_one("[class*='location']")
            district = city = zip_code = None
            if location_el:
                loc_text = location_el.get_text(strip=True)
                parts = [p.strip() for p in loc_text.split(",")]
                if len(parts) >= 2:
                    district = parts[-2].lower()
                    city = parts[-1].lower()
                elif parts:
                    city = parts[0].lower()

            condition_el = item.select_one("[class*='condition']") or item.select_one("[class*='tag']")
            condition = self.normalize_condition(condition_el.get_text()) if condition_el else "desconocido"

            photo_el = item.select_one("img[src]")
            photos = [photo_el["src"]] if photo_el else []

            content_hash = self.http.hash_content(f"{price}{area_m2}{rooms}")

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
                zip_code=zip_code,
                photo_urls=photos,
                raw_html_hash=content_hash,
            )
        except Exception as e:
            logger.debug(f"Error parsing Idealista card: {e}")
            return None

    def _parse_json_ld(self, html: str) -> dict:
        """Extract structured data from JSON-LD script tags."""
        soup = BeautifulSoup(html, "lxml")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") in ("Residence", "SingleFamilyResidence", "Apartment"):
                    return data
            except Exception:
                continue
        return {}
