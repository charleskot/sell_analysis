from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.scrapers.base import AuctionItem, BaseScraper

log = logging.getLogger(__name__)

BASE_URL = "https://subastas.boe.es"
SEARCH_PATH = "/subastas_ava.php"


def _parse_money(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", text).replace(".", "").replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _parse_date(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d-%m-%Y %H:%M:%S CET", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class BoeScraper(BaseScraper):
    """Scrape https://subastas.boe.es for property auctions in target provinces.

    Strategy:
      1. POST/GET the advanced search with filters (active, immovable, province).
      2. Parse the listing table to get per-auction identifiers + detail URLs.
      3. Fetch each detail page and parse fields from the labelled tables.

    BOE's HTML structure is reasonably stable (it's a government site), but
    selectors below fall back gracefully — missing fields stay None.
    """

    name = "boe"

    def fetch(self, provinces: Iterable[str]) -> List[AuctionItem]:
        items: List[AuctionItem] = []
        for prov_code in provinces:
            try:
                items.extend(self._fetch_province(prov_code))
            except Exception as e:
                log.exception("BOE province %s failed: %s", prov_code, e)
        return items

    def _fetch_province(self, province_code: str) -> List[AuctionItem]:
        params = {
            "accion": "Buscar",
            "tipoSubasta": "BIEN",
            "campo[0]": "SUBASTA.ESTADO",
            "dato[0]": "EJ",  # En plazo
            "campo[1]": "BIEN.TIPO",
            "dato[1]": "I",   # Inmuebles
            "campo[3]": "BIEN.PROVINCIA.CODIGO",
            "dato[3]": province_code,
            "page_hits": "50",
        }
        url = urljoin(BASE_URL, SEARCH_PATH)
        log.info("BOE listing %s", province_code)
        r = self.get(url, params=params)
        soup = BeautifulSoup(r.text, "lxml")

        detail_urls = []
        for a in soup.select("a[href*='detalleSubasta.php']"):
            href = a.get("href")
            if href:
                detail_urls.append(urljoin(BASE_URL, href))
        # Dedupe while preserving order
        seen = set()
        detail_urls = [u for u in detail_urls if not (u in seen or seen.add(u))]
        log.info("BOE %s: %d detail URLs", province_code, len(detail_urls))

        items: List[AuctionItem] = []
        for url in detail_urls:
            try:
                item = self._parse_detail(url)
                if item:
                    items.append(item)
            except Exception as e:
                log.warning("BOE detail %s failed: %s", url, e)
        return items

    def _parse_detail(self, url: str) -> Optional[AuctionItem]:
        r = self.get(url)
        return self.parse_detail_html(r.text, url)

    @classmethod
    def parse_detail_html(cls, html: str, url: str) -> Optional[AuctionItem]:
        """Pure parsing function for testability."""
        soup = BeautifulSoup(html, "lxml")

        # External id from URL: ?idSub=SUB-JA-2024-12345
        m = re.search(r"idSub=([A-Za-z0-9\-]+)", url)
        external_id = m.group(1) if m else url

        fields = {}
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    label = cells[0].get_text(" ", strip=True).rstrip(":").lower()
                    value = cells[1].get_text(" ", strip=True)
                    if label and value:
                        fields[label] = value

        def find(*keys: str) -> Optional[str]:
            for k in keys:
                for label, value in fields.items():
                    if k in label:
                        return value
            return None

        title = soup.find("h1") or soup.find("h2")
        title_text = title.get_text(" ", strip=True) if title else None

        addr = find("dirección", "direccion", "localización")
        city = find("localidad", "municipio", "población")
        province = find("provincia")
        postal_code = find("código postal", "codigo postal", "cp")

        prop_type = find("tipo de bien", "tipo")
        surface = _parse_int(find("superficie"))

        starting = _parse_money(find("valor de subasta", "tipo de subasta", "valor inicial"))
        appraised = _parse_money(find("valor del bien", "valor de tasación", "tasación"))
        deposit = _parse_money(find("depósito", "deposito", "tramos"))

        start_dt = _parse_date(find("fecha de inicio", "inicio de subasta"))
        end_dt = _parse_date(find("fecha de conclusión", "fin de subasta", "conclusión"))

        charges_text = find("cargas")
        has_charges = None
        if charges_text:
            has_charges = "no" not in charges_text.lower()[:10]

        occupancy = find("situación posesoria", "ocupación", "ocupacion")

        return AuctionItem(
            source="boe",
            external_id=external_id,
            url=url,
            title=title_text,
            address=addr,
            city=city,
            province=province,
            postal_code=postal_code,
            property_type=prop_type,
            surface_m2=float(surface) if surface else None,
            starting_price=starting,
            appraised_value=appraised,
            minimum_deposit=deposit,
            auction_start=start_dt,
            auction_end=end_dt,
            has_charges=has_charges,
            charges_description=charges_text,
            occupancy_status=occupancy,
            raw_description=" | ".join(f"{k}: {v}" for k, v in fields.items())[:5000],
        )
