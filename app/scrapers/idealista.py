from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.scrapers.base import AuctionItem, BaseScraper

log = logging.getLogger(__name__)

BASE_URL = "https://www.idealista.com"
PROVINCE_SLUGS = {
    "08": "barcelona-provincia",
    "17": "girona-provincia",
    "25": "lleida-provincia",
    "43": "tarragona-provincia",
}


def _parse_money(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", text).replace(".", "").replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


class IdealistaScraper(BaseScraper):
    """Scrape Idealista auction-flagged listings.

    NOTE: Idealista actively rate-limits and blocks bots with strict CAPTCHA.
    For production use, swap to their official API (paid) or proxy through
    Playwright with rotating residential IPs. The implementation below is a
    minimal best-effort that will likely be blocked without those measures.
    """

    name = "idealista"

    def fetch(self, provinces: Iterable[str]) -> List[AuctionItem]:
        items: List[AuctionItem] = []
        for code in provinces:
            slug = PROVINCE_SLUGS.get(code)
            if not slug:
                continue
            try:
                items.extend(self._fetch_province(slug))
            except Exception as e:
                log.warning("Idealista %s failed (likely blocked): %s", slug, e)
        return items

    def _fetch_province(self, slug: str) -> List[AuctionItem]:
        url = f"{BASE_URL}/venta-viviendas/{slug}/con-subasta/"
        log.info("Idealista listing %s", slug)
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        items: List[AuctionItem] = []
        for article in soup.select("article.item"):
            link = article.select_one("a.item-link")
            if not link:
                continue
            href = link.get("href")
            if not href:
                continue
            detail_url = urljoin(BASE_URL, href)
            external_id = re.search(r"/inmueble/(\d+)/", href)
            external_id = external_id.group(1) if external_id else detail_url

            title = link.get_text(" ", strip=True)
            price_el = article.select_one(".item-price, .price-row")
            price = _parse_money(price_el.get_text(" ", strip=True)) if price_el else None
            details = article.select_one(".item-detail-char")
            details_text = details.get_text(" ", strip=True) if details else ""
            surface_match = re.search(r"(\d+)\s*m²", details_text)
            rooms_match = re.search(r"(\d+)\s*hab", details_text)

            items.append(AuctionItem(
                source="idealista",
                external_id=external_id,
                url=detail_url,
                title=title,
                province=slug.replace("-provincia", "").title(),
                starting_price=price,
                surface_m2=float(surface_match.group(1)) if surface_match else None,
                rooms=int(rooms_match.group(1)) if rooms_match else None,
                raw_description=details_text,
            ))
        log.info("Idealista %s: %d items", slug, len(items))
        return items
