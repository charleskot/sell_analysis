"""Fotocasa scraper - parses window.__INITIAL_STATE__ JSON blob."""
import json
import logging
import re
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper.base import BaseScraper, RawListing
from scraper.http_client import HttpClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.fotocasa.es"


class FotocasaScraper(BaseScraper):
    PORTAL_NAME = "fotocasa"

    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        page = 1
        current_url = search_url

        while page <= max_pages:
            logger.info(f"Fotocasa page {page}: {current_url}")
            html = self._get_html(current_url, referer=BASE_URL if page == 1 else search_url)

            if html is None:
                break

            # Try JSON blob first (more reliable)
            listings = self._parse_initial_state(html, current_url)

            if not listings:
                # Fallback: HTML parsing
                listings = self._parse_html_cards(html)

            for listing in listings:
                yield listing

            if not listings:
                break

            # Next page: Fotocasa appends /l{page+1} or uses ?page=
            if "?page=" in current_url:
                current_url = re.sub(r"page=\d+", f"page={page + 1}", current_url)
            elif re.search(r"/l\d+$", current_url):
                current_url = re.sub(r"/l\d+$", f"/l{page + 1}", current_url)
            else:
                current_url = current_url.rstrip("/") + f"/l{page + 1}"

            page += 1

    def _parse_initial_state(self, html: str, search_url: str) -> list[RawListing]:
        """Extract listings from the __INITIAL_STATE__ embedded JSON."""
        m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\});?\s*</script>", html, re.DOTALL)
        if not m:
            return []

        try:
            state = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        results = []
        # Navigate the state tree - Fotocasa structure may vary
        try:
            items = (
                state.get("results", {}).get("items", [])
                or state.get("search", {}).get("result", {}).get("realEstates", [])
                or []
            )
            for item in items:
                listing = self._item_to_raw(item)
                if listing:
                    results.append(listing)
        except Exception as e:
            logger.debug(f"Error parsing Fotocasa state: {e}")

        return results

    def _item_to_raw(self, item: dict) -> RawListing | None:
        try:
            external_id = str(item.get("id", ""))
            if not external_id:
                return None

            url = item.get("detail", {}).get("url", "") or item.get("url", "")
            if not url.startswith("http"):
                url = urljoin(BASE_URL, url)

            price_obj = item.get("transactions", [{}])[0] if item.get("transactions") else {}
            price = None
            if price_obj:
                price = float(price_obj.get("value", [{}])[0].get("amount", 0) or 0) or None

            features = {f.get("key"): f.get("value") for f in item.get("features", [])}
            area_m2 = features.get("constructedArea") or features.get("usableArea")
            rooms = features.get("rooms") or features.get("bedrooms")
            bathrooms = features.get("bathrooms")

            location = item.get("address", {})
            district = (location.get("district") or location.get("neighborhood") or "").lower()
            city = (location.get("city") or location.get("municipality") or "").lower()
            zip_code = location.get("zipCode")
            lat = location.get("latitude") or (item.get("coordinates") or {}).get("latitude")
            lon = location.get("longitude") or (item.get("coordinates") or {}).get("longitude")

            title = item.get("title") or item.get("description", "")[:80]
            description = item.get("description", "")

            condition_raw = features.get("condition") or item.get("typology", "")
            condition = self.normalize_condition(str(condition_raw))

            photos = [m.get("url", "") for m in item.get("multimedia", {}).get("images", []) if m.get("url")]

            content_hash = self.http.hash_content(f"{price}{area_m2}{rooms}")

            return RawListing(
                portal=self.PORTAL_NAME,
                external_id=external_id,
                url=url,
                title=title[:200],
                price=price,
                area_m2=float(area_m2) if area_m2 else None,
                rooms=int(rooms) if rooms else None,
                bathrooms=int(bathrooms) if bathrooms else None,
                condition=condition,
                description=description[:1000],
                district=district or None,
                city=city or "desconocido",
                zip_code=zip_code,
                latitude=float(lat) if lat else None,
                longitude=float(lon) if lon else None,
                photo_urls=photos[:5],
                raw_html_hash=content_hash,
            )
        except Exception as e:
            logger.debug(f"Error parsing Fotocasa item: {e}")
            return None

    def _parse_html_cards(self, html: str) -> list[RawListing]:
        """Fallback HTML parsing when JSON blob is unavailable."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        cards = soup.select("article[data-test-id='card']") or soup.select(".re-Card")
        for card in cards:
            try:
                link = card.select_one("a[href*='/es/comprar/']") or card.select_one("a[href]")
                if not link:
                    continue

                href = link.get("href", "")
                url = urljoin(BASE_URL, href)

                m = re.search(r"/(\d+)/?$", href)
                external_id = m.group(1) if m else self.http.hash_content(href)

                price_el = card.select_one("[class*='price']")
                price = self.parse_price(price_el.get_text()) if price_el else None

                area_el = card.select_one("[class*='area']") or card.select_one("[class*='surface']")
                area_m2 = self.parse_area(area_el.get_text()) if area_el else None

                rooms_el = card.select_one("[class*='room']") or card.select_one("[class*='bedroom']")
                rooms = self.parse_rooms(rooms_el.get_text()) if rooms_el else None

                loc_el = card.select_one("[class*='location']") or card.select_one("[class*='address']")
                district = city = None
                if loc_el:
                    loc_text = loc_el.get_text(strip=True)
                    parts = [p.strip() for p in loc_text.split(",")]
                    if len(parts) >= 2:
                        district = parts[-2].lower()
                        city = parts[-1].lower()
                    elif parts:
                        city = parts[0].lower()

                results.append(RawListing(
                    portal=self.PORTAL_NAME,
                    external_id=external_id,
                    url=url,
                    price=price,
                    area_m2=area_m2,
                    rooms=rooms,
                    district=district,
                    city=city or "desconocido",
                    raw_html_hash=self.http.hash_content(f"{price}{area_m2}"),
                ))
            except Exception as e:
                logger.debug(f"Error parsing Fotocasa card: {e}")

        return results
