"""Habitaclia scraper."""
import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing, parse_ago_to_hours

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

            # If HTTP returns block page ('Pardon Our Interruption'), try Selenium
            if html and ("Pardon Our Interruption" in html or len(html) < 20000):
                logger.warning(f"Habitaclia HTTP blocked (page {page}), trying Selenium")
                html = self._selenium_get(current_url)

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

    def _selenium_get(self, url: str) -> str | None:
        try:
            from scraper.selenium_driver import get_page_html
            return get_page_html(url, wait_selector="article.list-item, .list-item", wait_seconds=3.0, use_undetected=True)
        except Exception as e:
            logger.warning(f"Habitaclia Selenium failed: {e}")
            return None

    def _parse_card(self, card) -> RawListing | None:
        try:
            link = card.select_one("h3.list-item-title a, h2 a, a[itemprop='name'], a[href]")
            if not link:
                return None

            href = link.get("href", "")
            url = urljoin(BASE_URL, href) if not href.startswith("http") else href

            # ID: -iXXXXXX.htm at end of URL
            m = re.search(r"-i(\d{6,})\.htm", href)
            external_id = m.group(1) if m else self.http.hash_content(href)

            # Title from link title attr or text
            title = (link.get("title") or link.get_text(strip=True))[:200]

            # Price: extract FIRST euro amount only (some cards show
            # "349.000 € ha bajado 1.000 €" — must not concatenate)
            price = None
            price_el = card.select_one(".list-item-price, [class*='item-price'], [itemprop='price']")
            if price_el:
                price_text = price_el.get_text(" ", strip=True)
                pm = re.search(r"([\d\.]+)\s*€", price_text)
                if pm:
                    try:
                        price = float(pm.group(1).replace(".", ""))
                    except ValueError:
                        pass
            if price is None:
                pm = re.search(r"([\d\.]{5,})\s*€", card.get_text(" ", strip=True))
                if pm:
                    try:
                        price = float(pm.group(1).replace(".", ""))
                    except ValueError:
                        pass

            # Feature line: "386m2 - 6 habitaciones - 4 baños - 4.119€/m2"
            feat_el = card.select_one("p.list-item-feature, .list-item-feature")
            area_m2 = rooms = bathrooms = None
            if feat_el:
                feat_text = feat_el.get_text(" ", strip=True)
                am = re.search(r"(\d[\d\.,]*)\s*m(?:²|\d)?\b(?!\s*/)", feat_text)
                if am:
                    try:
                        area_m2 = float(am.group(1).replace(".", "").replace(",", "."))
                    except ValueError:
                        pass
                hm = re.search(r"(\d+)\s+habitacion", feat_text, re.I)
                if hm:
                    rooms = int(hm.group(1))
                bm = re.search(r"(\d+)\s+ba[ñn]o", feat_text, re.I)
                if bm:
                    bathrooms = int(bm.group(1))

            # Location: "Sant Cugat del Vallès - Eixample"
            loc_el = card.select_one("p.list-item-location, [class*='location']")
            district = city = None
            if loc_el:
                loc_text = loc_el.get_text(" ", strip=True)
                parts = [p.strip() for p in re.split(r"\s+[-–]\s+", loc_text)]
                if len(parts) >= 2:
                    city = parts[0].lower()
                    district = parts[1].lower()
                elif parts:
                    city = parts[0].lower()

            # Description helps with year/features detection
            desc_el = card.select_one(".list-item-description, [itemprop='description']")
            description = desc_el.get_text(" ", strip=True) if desc_el else ""

            # Publication age: '.list-item-date' contains 'actualizado hace X horas/días'
            date_el = card.select_one(".list-item-date, span.list-item-date")
            published_ago_hours = parse_ago_to_hours(date_el.get_text(strip=True)) if date_el else None

            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=external_id,
                url=url,
                title=title,
                price=price,
                area_m2=area_m2,
                rooms=rooms,
                bathrooms=bathrooms,
                district=district,
                city=city or "desconocido",
                description=description[:500],
                published_ago_hours=published_ago_hours,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Habitaclia card: {e}")
            return None
