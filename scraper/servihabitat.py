"""Servihabitat (CaixaBank) property scraper.
Plain HTML scraping - no anti-bot detected. Data extracted from GTM spans.
"""
import logging
import re
from typing import Iterator
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing
from scraper.http_client import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.servihabitat.com"
CARD_SEL = "[data-id]"


class ServihabitatScraper(BaseScraper):
    PORTAL_NAME = "servihabitat"

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        page = 1
        while page <= max_pages:
            url = self._build_page_url(search_url, page)
            logger.info(f"Servihabitat page {page}: {url}")
            html = self._get_html(url)
            if not html:
                break

            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(CARD_SEL)
            if not cards:
                logger.info(f"Servihabitat: no cards on page {page}")
                break

            for card in cards:
                listing = self._parse_card(card)
                if listing:
                    yield listing

            page += 1

    def _build_page_url(self, base_url: str, page: int) -> str:
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        if page > 1:
            params["pagina"] = [str(page)]
        elif "pagina" in params:
            del params["pagina"]
        new_query = urlencode({k: v[0] for k, v in params.items()})
        return urlunparse(parsed._replace(query=new_query))

    def _parse_card(self, card) -> RawListing | None:
        try:
            external_id = card.get("data-id")
            if not external_id:
                return None

            # All data comes from GTM spans
            def gtm(key: str) -> str | None:
                el = card.select_one(f'span[gtm="{key}"]')
                return el.get("gtm-value", "").strip() if el else None

            price_str = gtm("product-price")
            price = float(price_str) if price_str else None

            area_str = gtm("product-m2")
            area_m2 = float(area_str) if area_str else None

            rooms_str = gtm("product-room-num")
            rooms = int(rooms_str) if rooms_str else None

            baths_str = gtm("product-bath-num")
            bathrooms = int(baths_str) if baths_str else None

            town = (gtm("location-town") or "").lower()
            area_name = (gtm("location-area") or "").lower()
            province = (gtm("location-province") or "").lower()

            # Build URL from link
            link = card.select_one("a[href*='/es/venta/']")
            if not link:
                return None
            href = link.get("href", "")
            url = urljoin(BASE_URL, href) if not href.startswith("http") else href

            # Title from link text or card text
            title_el = card.select_one("h2, h3, .product-title, [class*='title']")
            if title_el:
                title = re.sub(r"\s+", " ", title_el.get_text(strip=True))
            else:
                title = card.get("data-name", f"Vivienda {external_id}")

            # Photo
            img = card.select_one("img[src]")
            photos = [img["src"]] if img and "no-foto" not in img.get("src", "") else []

            # Condition - check for "nueva construcción", "segunda mano" tags
            condition = "desconocido"
            card_text = card.get_text(" ").lower()
            if "nueva" in card_text or "obra nueva" in card_text or "estrenar" in card_text:
                condition = "nuevo"
            elif "segunda mano" in card_text or "buen estado" in card_text:
                condition = "buen_estado"
            elif "reformar" in card_text or "a reformar" in card_text:
                condition = "reformar"

            # Discount indicator from prev_price
            description = ""
            prev_price_el = card.select_one("[class*='old-price'], [class*='precio-anterior'], s")
            if prev_price_el:
                prev_price = self.parse_price(prev_price_el.get_text())
                if prev_price and price and prev_price > price:
                    discount = (prev_price - price) / prev_price * 100
                    description = f"Descuento {discount:.0f}% (antes {prev_price:,.0f}€)"

            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=str(external_id),
                url=url,
                title=title[:200],
                price=price,
                area_m2=area_m2,
                rooms=rooms,
                bathrooms=bathrooms,
                condition=condition,
                description=description,
                district=area_name or None,
                # Use town as city for specific rent lookup;
                # province stored in district as fallback for unknown towns.
                city=town or province or "desconocido",
                photo_urls=photos,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}{rooms}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Servihabitat card: {e}")
            return None
