"""Habitaclia scraper."""
import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing

logger = logging.getLogger(__name__)

BASE_URL = "https://www.habitaclia.com"


class HabitacliaScraper(BaseScraper):
    PORTAL_NAME = "habitaclia"

    CARD_SEL = "article.list-item, .list-item"
    NEXT_SEL = "a.next, [rel='next']"

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        current_url = search_url
        page = 1

        while current_url and page <= max_pages:
            logger.info(f"Habitaclia page {page}: {current_url}")
            html = self._get_html(current_url, referer=BASE_URL if page == 1 else search_url)

            if html is None:
                break

            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(self.CARD_SEL)

            if not cards:
                logger.warning(f"No listings on Habitaclia page {page}")
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
            link = card.select_one("a[href]")
            if not link:
                return None

            href = link.get("href", "")
            url = urljoin(BASE_URL, href) if not href.startswith("http") else href

            m = re.search(r"-(\d{6,})\.htm", href)
            external_id = m.group(1) if m else self.http.hash_content(href)

            price_el = card.select_one("[class*='price'], .price")
            price = self.parse_price(price_el.get_text()) if price_el else None

            area_el = card.select_one("[class*='surface'], [class*='area']")
            area_m2 = self.parse_area(area_el.get_text()) if area_el else None

            rooms_el = card.select_one("[class*='room'], [class*='bedroom']")
            rooms = self.parse_rooms(rooms_el.get_text()) if rooms_el else None

            loc_el = card.select_one("[class*='location'], [class*='address'], .location")
            district = city = None
            if loc_el:
                loc_text = loc_el.get_text(strip=True)
                parts = [p.strip() for p in loc_text.split(",")]
                if len(parts) >= 2:
                    district = parts[0].lower()
                    city = parts[-1].lower()
                elif parts:
                    city = parts[0].lower()

            title_el = card.select_one("h2, h3, [class*='title']")
            title = title_el.get_text(strip=True)[:200] if title_el else ""

            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=external_id,
                url=url,
                title=title,
                price=price,
                area_m2=area_m2,
                rooms=rooms,
                district=district,
                city=city or "desconocido",
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Habitaclia card: {e}")
            return None
