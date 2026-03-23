"""Pisos.com scraper - straightforward HTML."""
import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing

logger = logging.getLogger(__name__)

BASE_URL = "https://www.pisos.com"


class PisosScraper(BaseScraper):
    PORTAL_NAME = "pisos"

    # CSS selectors (update if site structure changes)
    CARD_SEL = "div.ad-preview"
    LINK_SEL = "a.ad-preview__image, a[href*='/pisos/']"
    PRICE_SEL = ".ad-preview__price"
    AREA_SEL = ".ad-preview__char--surface"
    ROOMS_SEL = ".ad-preview__char--rooms"
    LOCATION_SEL = ".ad-preview__location"
    NEXT_SEL = "a[rel='next'], .pagination__next a"

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        current_url = search_url
        page = 1

        while current_url and page <= max_pages:
            logger.info(f"Pisos.com page {page}: {current_url}")
            html = self._get_html(current_url, referer=BASE_URL if page == 1 else search_url)

            if html is None:
                break

            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(self.CARD_SEL)

            if not cards:
                # Try alternative structure
                cards = soup.select("[class*='ad-preview']")

            if not cards:
                logger.warning(f"No listings found on Pisos.com page {page}")
                break

            for card in cards:
                listing = self._parse_card(card)
                if listing:
                    yield listing

            next_el = soup.select_one(self.NEXT_SEL)
            if next_el and next_el.get("href"):
                current_url = urljoin(BASE_URL, next_el["href"])
                page += 1
            else:
                break

    def _parse_card(self, card) -> RawListing | None:
        try:
            link = card.select_one(self.LINK_SEL) or card.select_one("a[href]")
            if not link:
                return None

            href = link.get("href", "")
            if not href:
                return None

            url = urljoin(BASE_URL, href) if not href.startswith("http") else href

            # Extract ID from URL like /pisos/madrid-capital/venta/12345678/
            m = re.search(r"/(\d{6,})/", href)
            external_id = m.group(1) if m else self.http.hash_content(href)

            price_el = card.select_one(self.PRICE_SEL)
            price = self.parse_price(price_el.get_text()) if price_el else None

            area_el = card.select_one(self.AREA_SEL)
            area_m2 = self.parse_area(area_el.get_text()) if area_el else None

            rooms_el = card.select_one(self.ROOMS_SEL)
            rooms = self.parse_rooms(rooms_el.get_text()) if rooms_el else None

            loc_el = card.select_one(self.LOCATION_SEL)
            district = city = zip_code = None
            if loc_el:
                loc_text = loc_el.get_text(strip=True)
                parts = [p.strip() for p in re.split(r"[,\-]", loc_text)]
                if len(parts) >= 2:
                    district = parts[0].lower().strip()
                    city = parts[-1].lower().strip()
                elif parts:
                    city = parts[0].lower().strip()

                zip_m = re.search(r"\b(\d{5})\b", loc_text)
                zip_code = zip_m.group(1) if zip_m else None

            title_el = card.select_one("[class*='title']") or link
            title = title_el.get_text(strip=True)[:200] if title_el else ""

            condition_el = card.select_one("[class*='condition']") or card.select_one("[class*='tag']")
            condition = self.normalize_condition(condition_el.get_text()) if condition_el else "desconocido"

            photo_el = card.select_one("img[src]")
            photos = []
            if photo_el:
                src = photo_el.get("src") or photo_el.get("data-src", "")
                if src:
                    photos = [src]

            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=external_id,
                url=url,
                title=title,
                price=price,
                area_m2=area_m2,
                rooms=rooms,
                condition=condition,
                district=district,
                city=city or "desconocido",
                zip_code=zip_code,
                photo_urls=photos,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}{rooms}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Pisos.com card: {e}")
            return None
