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


# ── The live search the bot reads every half hour ───────────────────────

_WEB_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
_BCN_DISTRICTS = {"bcnsantmarti": "sant martí", "bcnhortaguinardo": "horta-guinardó",
                  "bcnsantandreu": "sant andreu", "bcneixample": "eixample",
                  "bcnnoubarris": "nou barris", "bcngracia": "gràcia",
                  "bcnsantsmontjuic": "sants-montjuïc", "bcnciutatvella": "ciutat vella",
                  "bcnlescorts": "les corts", "bcnsarriasantgervasi": "sarrià-sant gervasi"}


def card_to_listing(card, town: str | None) -> dict | None:
    """One result card as a listing dict.

    Each card carries its facts as data ("productPrice: 115400.00
    productM2: 72 productRoomNum: 3 productExtra: [lift]") beside the text a
    buyer sees ("Piso en venta en C. Calle Atlantida, 71-73, Badalona … En
    rentabilidad"). "En rentabilidad" is Servihabitat's word for a flat sold
    with its tenant, which the bot's own wording check would not know.
    """
    from urllib.parse import urljoin

    ext_id = card.get("data-id")
    link = card.find("a", href=re.compile(r"/\d{6,}$"))
    if not ext_id or not link:
        return None
    text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))

    def fact(name: str):
        found = re.search(rf"{name}:\s*([\w.\[\],]+)", text)
        return found.group(1) if found else None

    try:
        price = float(fact("productPrice") or 0) or None
    except ValueError:
        price = None
    if not price:
        return None
    area = fact("productM2")
    rooms = fact("productRoomNum")
    baths = fact("productBathNum")
    extras = (fact("productExtra") or "").strip("[]").split(",")
    heading = re.search(r"((?:Piso|Casa|Dúplex|Ático|Planta baja|Apartamento|Estudio|Chalet|Vivienda)"
                        r"[^€]*? en venta en [^€]+?)(?=\s+\d+\s*m\s*2|\s+\d+\s*hab|$)", text)
    title = heading.group(1).strip() if heading else f"Vivienda en venta en {town or ''}".strip()
    url = urljoin("https://www.servihabitat.com", link["href"])
    kind = (card.get("data-category") or "").split("/")
    floor = "Bajo" if "planta-baja" in kind or "vivienda-plantabaja" in url else None

    notes = []
    # "productWithoutPosesion: sinposesion" marks a flat sold occupied; on a
    # free one the field is empty and the next field's name follows
    if re.search(r"productWithoutPosesion:\s*sinposesion", text):
        notes.append("inmueble sin posesión (ocupado)")
    if "lift" in extras:
        notes.append("con ascensor")
    if re.search(r"\ben rentabilidad\b", text, re.I):
        notes.append("inmueble alquilado (en rentabilidad)")
    district = None
    slug = re.search(r"/venta/[^/]+/[a-z]+-[a-z]+-([a-z]+)/\d+", url)
    if slug and slug.group(1) in _BCN_DISTRICTS:
        district = _BCN_DISTRICTS[slug.group(1)]
    return {
        "portal": "servihabitat", "external_id": str(ext_id), "url": url,
        "title": title, "price": price,
        "area_m2": float(area) if area and area.replace(".", "").isdigit() else None,
        "rooms": int(rooms) if rooms and rooms.isdigit() else None,
        "bathrooms": int(baths) if baths and baths.isdigit() else None,
        "floor": floor, "city": town or "desconocido", "district": district,
        "description": " · ".join(notes),
        "photo_urls": [img["src"] for img in card.select("img[src]")
                       if img["src"].startswith("http") and "logo" not in img["src"]][:10],
    }


def scrape_recent(slug: str, town: str | None) -> list[dict]:
    """The newest flats of one Servihabitat search (`slug` as in its links,
    e.g. "barcelona-barcelones-badalona"), only its own results — the page
    goes on with "También te puede interesar", flats from anywhere."""
    import requests
    from bs4 import BeautifulSoup

    url = f"https://www.servihabitat.com/es/venta/vivienda/{slug}?o=2"    # o=2: newest first
    resp = requests.get(url, headers={"User-Agent": _WEB_UA, "Accept-Language": "es-ES"}, timeout=30)
    if resp.status_code != 200:
        logger.info(f"Servihabitat: {resp.status_code} en {url}")
        return []
    own = resp.text.split('id="suggested-message"')[0]
    cards = BeautifulSoup(own, "lxml").select("div.product-list div.product-item")
    return [x for x in (card_to_listing(c, town) for c in cards) if x]


def advert_text(url: str | None) -> str | None:
    """What a Servihabitat advert says about the flat, or None if unreadable.

    Not the whole page: every one of its pages carries the heading "Vivienda
    sin posesión" in a hidden contact form, so scanning the page for
    occupation condemned every flat, free or not. Whether a flat is sold
    occupied is the page's possession field; the rest is the seller's
    description.
    """
    import requests
    from bs4 import BeautifulSoup

    if not url:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": _WEB_UA, "Accept-Language": "es-ES"}, timeout=30)
    except Exception as e:
        logger.info(f"Servihabitat: no pude leer {url[-50:]}: {e}")
        return None
    if resp.status_code != 200:
        return None
    parts = []
    if re.search(r"productWithoutPosesion:\s*sinposesion", resp.text):
        parts.append("Inmueble sin posesión (ocupado).")
    soup = BeautifulSoup(resp.text, "lxml")
    for el in soup.select("p.description"):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(text) > 80 and not text.startswith("Un agente contactará"):
            parts.append(text)
    return " ".join(parts)
