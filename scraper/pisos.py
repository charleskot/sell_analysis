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

    # CSS selectors mapped from actual pisos.com HTML
    CARD_SEL = "div.ad-preview"
    LINK_SEL = "a.ad-preview__title"
    PRICE_SEL = "span.ad-preview__price"
    CHARS_SEL = "p.ad-preview__char"        # rooms, bathrooms, area, floor
    LOCATION_SEL = "p.ad-preview__subtitle"  # e.g. "Centro (Rivas-Vaciamadrid)"
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
            # External ID from card's own id attribute: "59204642590.100500"
            card_id = card.get("id", "")
            external_id = card_id.replace(".", "_") if card_id else None

            # Link and URL from the title anchor
            link = card.select_one(self.LINK_SEL)
            data_href = card.get("data-lnk-href", "")
            href = (link.get("href", "") if link else "") or data_href
            if not href:
                return None
            url = urljoin(BASE_URL, href) if not href.startswith("http") else href

            if not external_id:
                m = re.search(r"-(\d{6,})[_/]", href)
                external_id = m.group(1) if m else self.http.hash_content(href)

            # Title
            title = link.get_text(strip=True)[:200] if link else ""

            # Price
            price_el = card.select_one(self.PRICE_SEL)
            price = self.parse_price(price_el.get_text()) if price_el else None

            # Characteristics: rooms, bathrooms, area, floor
            # Each is a <p class="ad-preview__char p-sm"> with text like "4 habs.", "238 m²"
            area_m2 = rooms = bathrooms = floor = None
            for char_el in card.select(self.CHARS_SEL):
                text = char_el.get_text(strip=True)
                if "m²" in text or "m2" in text:
                    area_m2 = self.parse_area(text)
                elif "hab" in text.lower():
                    rooms = self.parse_rooms(text)
                elif "baño" in text.lower() or "aseo" in text.lower():
                    m = re.search(r"(\d+)", text)
                    bathrooms = int(m.group(1)) if m else None
                elif re.search(r"\d+[ªº]", text) or "planta" in text.lower():
                    floor = text

            # Location: "Centro (Rivas-Vaciamadrid)" or "Salamanca (Madrid)"
            district = city = zip_code = None
            loc_el = card.select_one(self.LOCATION_SEL)
            if loc_el:
                loc_text = loc_el.get_text(strip=True)
                zip_m = re.search(r"\b(\d{5})\b", loc_text)
                zip_code = zip_m.group(1) if zip_m else None

                # Pattern: "Barrio (Ciudad)" or "Ciudad"
                paren_m = re.search(r"^(.+?)\s*\((.+?)\)", loc_text)
                if paren_m:
                    district = paren_m.group(1).strip().lower()
                    city = paren_m.group(2).strip().lower()
                    # Remove postal code from city if present
                    city = re.sub(r"\d{5}", "", city).strip()
                else:
                    parts = [p.strip() for p in re.split(r"[,\-]", loc_text) if p.strip()]
                    if len(parts) >= 2:
                        district = parts[0].lower()
                        city = parts[-1].lower()
                    elif parts:
                        city = parts[0].lower()

            # Condition from description or tag
            desc_el = card.select_one(".ad-preview__description")
            condition = "desconocido"
            if desc_el:
                condition = self.normalize_condition(desc_el.get_text())

            # Photos
            photos = []
            for img in card.select("img[src]")[:3]:
                src = img.get("src", "")
                if src and not src.endswith(".svg") and "logo" not in src:
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
                floor=floor,
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
