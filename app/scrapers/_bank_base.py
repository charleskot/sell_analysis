from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.scrapers.base import AuctionItem, BaseScraper

log = logging.getLogger(__name__)


def parse_money(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", text).replace(".", "").replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


class BankPortalScraper(BaseScraper):
    """Generic best-effort scraper for bank-owned property portals.

    These portals are JavaScript-heavy single-page apps. Static HTTP scraping
    will only work for the SSR-rendered subset. For full coverage, swap the
    `get` call for a Playwright session. The base implementation returns the
    items it can parse and logs the rest.
    """

    name = "bank"
    list_url_template: str = ""
    province_filter_param: str = "provincia"
    province_codes: dict = {"08": "BARCELONA", "17": "GIRONA", "25": "LLEIDA", "43": "TARRAGONA"}

    def fetch(self, provinces: Iterable[str]) -> List[AuctionItem]:
        items: List[AuctionItem] = []
        for code in provinces:
            province_value = self.province_codes.get(code)
            if not province_value:
                continue
            try:
                items.extend(self._fetch_province(province_value))
            except Exception as e:
                log.warning("%s %s failed: %s", self.name, province_value, e)
        return items

    def _fetch_province(self, province_value: str) -> List[AuctionItem]:
        url = self.list_url_template.format(province=province_value)
        log.info("%s listing %s", self.name, province_value)
        r = self.get(url)
        soup = BeautifulSoup(r.text, "lxml")

        items: List[AuctionItem] = []
        cards = soup.select("[class*=property], [class*=card], article, [class*=result-item]")
        for card in cards[:60]:
            link = card.find("a", href=True)
            if not link:
                continue
            detail_url = urljoin(url, link["href"])

            external_id = re.search(r"(\d{4,})", detail_url)
            external_id = external_id.group(1) if external_id else detail_url

            title_el = card.find(["h2", "h3", "h4"])
            title = title_el.get_text(" ", strip=True) if title_el else link.get_text(" ", strip=True)

            text = card.get_text(" ", strip=True)
            price_match = re.search(r"([\d\.,]+)\s*€", text)
            price = parse_money(price_match.group(0)) if price_match else None
            surface_match = re.search(r"(\d+)\s*m²", text)

            items.append(AuctionItem(
                source=self.name,
                external_id=external_id,
                url=detail_url,
                title=title,
                province=province_value.title(),
                starting_price=price,
                surface_m2=float(surface_match.group(1)) if surface_match else None,
                raw_description=text[:2000],
            ))
        return items
