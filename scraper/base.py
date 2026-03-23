"""Base scraper ABC and RawListing dataclass."""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator

from scraper.http_client import HttpClient
from scraper.robots import RobotsChecker

logger = logging.getLogger(__name__)


@dataclass
class RawListing:
    portal: str
    external_id: str
    url: str
    title: str = ""
    price: float | None = None
    area_m2: float | None = None
    rooms: int | None = None
    bathrooms: int | None = None
    floor: str | None = None
    condition: str | None = None      # nuevo, buen_estado, reformar, desconocido
    description: str = ""
    district: str | None = None
    city: str = ""
    zip_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    photo_urls: list[str] = field(default_factory=list)
    raw_html_hash: str | None = None

    def to_dict(self) -> dict:
        return {
            "portal": self.portal,
            "external_id": self.external_id,
            "url": self.url,
            "title": self.title,
            "price": self.price,
            "area_m2": self.area_m2,
            "rooms": self.rooms,
            "bathrooms": self.bathrooms,
            "floor": self.floor,
            "condition": self.condition,
            "description": self.description,
            "district": self.district,
            "city": self.city,
            "zip_code": self.zip_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "photo_urls": self.photo_urls,
            "raw_html_hash": self.raw_html_hash,
        }


class BaseScraper(ABC):
    PORTAL_NAME: str = "base"

    def __init__(self, http_client: HttpClient, config: dict, respect_robots: bool = True):
        self.http = http_client
        self.config = config
        self._robots = RobotsChecker() if respect_robots else None

    def _get_html(self, url: str, referer: str | None = None) -> str | None:
        if self._robots and not self._robots.is_allowed(url):
            logger.info(f"robots.txt disallows {url}, skipping")
            return None

        resp = self.http.get(url, referer=referer)
        if resp is None:
            return None

        if resp.status_code == 200:
            return resp.text
        logger.warning(f"HTTP {resp.status_code} for {url}")
        return None

    @abstractmethod
    def search_listings(self, search_url: str, max_pages: int = 5) -> Iterator[RawListing]:
        """Yield RawListing objects from a paginated search URL."""

    def parse_price(self, text: str) -> float | None:
        """Parse Spanish price strings like '180.000 €' or '1.250.000€'."""
        import re
        cleaned = re.sub(r"[^\d,.]", "", text.strip())
        cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def parse_area(self, text: str) -> float | None:
        """Parse area strings like '85 m²' or '85m2'."""
        import re
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m", text, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", "."))
        return None

    def parse_rooms(self, text: str) -> int | None:
        import re
        m = re.search(r"(\d+)\s*(?:hab|dormitorio|room)", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    def normalize_condition(self, text: str) -> str:
        text_lower = text.lower()
        if any(w in text_lower for w in ["nuevo", "obra nueva", "a estrenar"]):
            return "nuevo"
        if any(w in text_lower for w in ["reformar", "reforma", "rehabilitar", "a reformar"]):
            return "reformar"
        if any(w in text_lower for w in ["buen estado", "buenas condiciones", "bien conservado"]):
            return "buen_estado"
        return "desconocido"
