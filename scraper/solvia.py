"""Solvia scraper - bank-owned properties (Sabadell).
Solvia renders Angular SSR HTML with az-element-mosaic cards accessible via plain HTTP.
~16,000 properties in Spain including many below-market bank-owned assets.
"""
import logging
import re
from typing import Iterator
from urllib.parse import urljoin, urlencode, urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing
from scraper.http_client import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.solvia.es"
ITEMS_PER_PAGE = 20


class SolviaScraper(BaseScraper):
    PORTAL_NAME = "solvia"

    # Selectors for Angular SSR-rendered cards
    CARD_SEL = "az-element-mosaic"
    LINK_SEL = "a[href*='/es/propiedades/comprar/']"
    TITLE_SEL = "h3.build-name"
    ADDR_SEL = "div.build-address"
    PRICE_SEL = "span.final-price"
    PREV_PRICE_SEL = "span.previous-price"
    TAGS_SEL = "ul.build-tags li"
    LABELS_SEL = "ul.build-etiquetas li"
    IMG_SEL = "img.az-element-mosaic__image"

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        # Warm up with homepage (not strictly needed but polite)
        base_html = self._get_html(BASE_URL)

        for page in range(1, max_pages + 1):
            page_url = self._build_page_url(search_url, page)
            logger.info(f"Solvia page {page}: {page_url}")

            html = self._get_html(page_url, referer=BASE_URL if page == 1 else search_url)
            if html is None:
                logger.warning(f"Solvia: Failed to load page {page}")
                break

            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(self.CARD_SEL)

            if not cards:
                logger.info(f"Solvia: No more listings at page {page}")
                break

            for card in cards:
                listing = self._parse_card(card)
                if listing:
                    yield listing

            # Check if last page
            total_text = self._get_total_count(soup)
            if total_text and page * ITEMS_PER_PAGE >= total_text:
                break

    def _build_page_url(self, base_url: str, page: int) -> str:
        """Add/update numeroPagina query param."""
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["numeroPagina"] = [str(page - 1)]  # Solvia is 0-indexed
        new_query = "&".join(f"{k}={v[0]}" for k, v in params.items())
        return parsed._replace(query=new_query).geturl()

    def _get_total_count(self, soup: BeautifulSoup) -> int | None:
        try:
            title_el = soup.find("solvia-title-section")
            if title_el:
                m = re.search(r"(\d[\d.]*)", title_el.get_text().replace(".", ""))
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        return None

    # Non-residential keywords to skip
    _SKIP_TYPES = (
        "suelo", "solar", "terreno", "local", "garaje", "trastero",
        "nave", "oficina", "parking", "parcela", "finca rústica",
    )

    def _is_residential(self, title: str) -> bool:
        title_lower = title.lower()
        return not any(kw in title_lower for kw in self._SKIP_TYPES)

    def _parse_card(self, card) -> RawListing | None:
        try:
            # URL and external ID
            link = card.select_one(self.LINK_SEL)
            if not link:
                return None

            url = link.get("href", "")
            if not url.startswith("http"):
                url = urljoin(BASE_URL, url)

            # ID from URL: /es/propiedades/comprar/slug-PROPID-REFID
            m = re.search(r"-(\d+)-(\d+)/?$", url)
            if m:
                external_id = f"{m.group(1)}_{m.group(2)}"
            else:
                external_id = self.http.hash_content(url)

            # Title
            title_el = card.select_one(self.TITLE_SEL)
            title = title_el.get_text(strip=True)[:200] if title_el else ""

            # Skip non-residential properties
            if not self._is_residential(title):
                return None

            # Location: "Province, City - Street"
            addr_el = card.select_one(self.ADDR_SEL)
            province = city = street = None
            if addr_el:
                spans = [s.get_text(strip=True) for s in addr_el.find_all("span") if s.get_text(strip=True)]
                # Typically: [empty?, province, ", city", "- street"]
                clean = [s.lstrip(",- ").strip() for s in spans if s.strip() not in ("", ",")]
                if len(clean) >= 2:
                    province = clean[0].lower()
                    city = clean[1].lower()
                    street = " ".join(clean[2:]).lstrip("- ") if len(clean) > 2 else None
                elif clean:
                    city = clean[0].lower()

            # Price (final price after discount)
            price_el = card.select_one(self.PRICE_SEL)
            price = None
            if price_el:
                raw = price_el.get_text(strip=True)
                price = self.parse_price(raw)

            # Previous price (original before discount)
            prev_price_el = card.select_one(self.PREV_PRICE_SEL)
            prev_price = None
            if prev_price_el:
                prev_price = self.parse_price(prev_price_el.get_text(strip=True))

            # Characteristics from build-tags using icon classes:
            # icon-medidas=area, icon-dormitorio=rooms, icon-bano=bathrooms
            area_m2 = rooms = bathrooms = None
            floor = None
            for tag_el in card.select(self.TAGS_SEL):
                icon_span = tag_el.find("span")
                icon_classes = icon_span.get("class", []) if icon_span else []

                if "icon-medidas" in icon_classes:
                    # Parse area number before "m"; handle both decimal dot and thousands dot
                    raw_text = tag_el.get_text(separator=" ", strip=True)
                    am = re.search(r"([\d.,]+)\s*m", raw_text)
                    if am:
                        num_str = am.group(1)
                        # Try direct parse (dot as decimal: "85.94" → 85.94)
                        candidate = None
                        try:
                            candidate = float(num_str.replace(",", "."))
                        except ValueError:
                            pass
                        if candidate is None or not (15 <= candidate <= 2000):
                            # Try treating dot as thousands sep ("1.840" → 1840)
                            try:
                                candidate = float(num_str.replace(".", "").replace(",", "."))
                            except ValueError:
                                candidate = None
                        if candidate is not None and 15 <= candidate <= 2000:
                            area_m2 = candidate

                elif "icon-dormitorio" in icon_classes:
                    bm = re.search(r"(\d+)", tag_el.get_text())
                    if bm:
                        rooms = int(bm.group(1))

                elif "icon-bano" in icon_classes:
                    bm = re.search(r"(\d+)", tag_el.get_text())
                    if bm:
                        bathrooms = int(bm.group(1))

                elif re.search(r"\d+\s*planta|bajo|ático", tag_el.get_text(), re.I):
                    floor = tag_el.get_text(strip=True)

            # Labels: "EN SITUACIÓN ESPECIAL", condition, etc.
            labels = [el.get_text(strip=True) for el in card.select(self.LABELS_SEL)]
            # Also include last-item tag text (condition like "A estrenar")
            last_item = card.select_one("li.last-item")
            if last_item:
                labels.append(last_item.get_text(strip=True))
            condition = "desconocido"
            for label in labels:
                if "nueva" in label.lower() or "nuevo" in label.lower() or "estrenar" in label.lower():
                    condition = "nuevo"
                    break
                if "reforma" in label.lower():
                    condition = "reformar"
                    break
                if "buen" in label.lower():
                    condition = "buen_estado"
                    break

            # Solvia "situación especial" = bank repossession, potentially negotiable
            is_special = any("situaci" in l.lower() for l in labels)

            # Photo
            img_el = card.select_one(self.IMG_SEL)
            photos = []
            if img_el:
                src = img_el.get("src", "")
                if src and "no-foto" not in src:
                    photos = [src]

            # Solvia note: "desde" prefix means negotiable/minimum price
            is_desde = bool(card.select_one("span.from-price"))

            # Description enrichment
            desc_parts = []
            if is_special:
                desc_parts.append("EN SITUACIÓN ESPECIAL (inmueble bancario)")
            if is_desde:
                desc_parts.append(f"Precio desde {price}€ (negociable)")
            if prev_price and price and prev_price > price:
                discount_pct = (prev_price - price) / prev_price * 100
                desc_parts.append(f"Descuento del {discount_pct:.0f}% (antes {prev_price:,.0f}€)")
            if street:
                desc_parts.append(f"Dirección: {street}")
            description = " | ".join(desc_parts)

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
                description=description,
                district=province,    # Solvia shows province, not district
                city=city or "desconocido",
                zip_code=None,
                photo_urls=photos,
                raw_html_hash=self.http.hash_content(f"{price}{area_m2}{url}"),
            )
        except Exception as e:
            logger.debug(f"Error parsing Solvia card: {e}")
            return None
