from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.scrapers.base import AuctionItem, BaseScraper

log = logging.getLogger(__name__)

BASE_URL = "https://www.addmeet.com"

# Provinces in Catalonia mapped to addmeet's geographic slugs
PROVINCE_SLUGS = {
    "08": "barcelona",
    "17": "girona",
    "25": "lleida",
    "43": "tarragona",
}


def _parse_money(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", text).replace(".", "").replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


class AddmeetScraper(BaseScraper):
    """Scrape addmeet.com auction listings.

    Addmeet exposes auctions at /subasta/<province>/<type>/...
    Selectors below are best-effort; expect to maintain them against the
    live site. Failures degrade gracefully (returns whatever was parsed).
    """

    name = "addmeet"

    def fetch(self, provinces: Iterable[str]) -> List[AuctionItem]:
        items: List[AuctionItem] = []
        for code in provinces:
            slug = PROVINCE_SLUGS.get(code)
            if not slug:
                continue
            try:
                items.extend(self._fetch_province(slug))
            except Exception as e:
                log.warning("Addmeet %s failed: %s", slug, e)
        return items

    def _fetch_province(self, slug: str) -> List[AuctionItem]:
        url = f"{BASE_URL}/subasta/{slug}/vivienda/"
        log.info("Addmeet listing %s", slug)
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        detail_urls = []
        for a in soup.select("a[href*='/subasta/']"):
            href = a.get("href")
            if href and "/subasta/" in href and href.endswith((".html", "/")) is False:
                detail_urls.append(urljoin(BASE_URL, href))
        seen = set()
        detail_urls = [u for u in detail_urls if not (u in seen or seen.add(u))]
        log.info("Addmeet %s: %d detail URLs", slug, len(detail_urls))

        items: List[AuctionItem] = []
        for url in detail_urls[:50]:
            try:
                items.append(self._parse_detail(url))
            except Exception as e:
                log.debug("Addmeet detail %s skipped: %s", url, e)
        return [i for i in items if i is not None]

    def _parse_detail(self, url: str) -> Optional[AuctionItem]:
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        m = re.search(r"/subasta/[^/]+/[^/]+/([A-Za-z0-9\-]+)", url)
        external_id = m.group(1) if m else url

        title = soup.find("h1")
        title_text = title.get_text(" ", strip=True) if title else None

        body_text = soup.get_text("\n", strip=True)
        starting = _parse_money(self._find_after(body_text, ["Precio de salida", "Precio salida", "Tipo de subasta"]))
        appraised = _parse_money(self._find_after(body_text, ["Valor de tasación", "Tasación"]))

        return AuctionItem(
            source="addmeet",
            external_id=external_id,
            url=url,
            title=title_text,
            starting_price=starting,
            appraised_value=appraised,
            raw_description=body_text[:5000],
        )

    @staticmethod
    def _find_after(text: str, labels) -> Optional[str]:
        for label in labels:
            m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([0-9\.,]+\s*€?)", text, re.IGNORECASE)
            if m:
                return m.group(1)
        return None
