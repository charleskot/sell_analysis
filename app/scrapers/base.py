from __future__ import annotations

import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Iterable, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class AuctionItem:
    """Normalized auction record produced by every scraper."""
    source: str
    external_id: str
    url: str

    title: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    property_type: Optional[str] = None
    surface_m2: Optional[float] = None
    rooms: Optional[int] = None
    year_built: Optional[int] = None

    starting_price: Optional[float] = None
    appraised_value: Optional[float] = None
    minimum_deposit: Optional[float] = None

    auction_start: Optional[datetime] = None
    auction_end: Optional[datetime] = None

    has_charges: Optional[bool] = None
    charges_description: Optional[str] = None
    occupancy_status: Optional[str] = None
    raw_description: Optional[str] = None

    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("auction_start", "auction_end"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


class BaseScraper:
    """Subclass and override `fetch()` to return AuctionItem instances.

    Scrapers should be defensive — return whatever they can parse and skip
    listings that cannot be normalized. Real-estate sites change frequently;
    failures should not break the pipeline for other sources.
    """
    name: str = ""

    def __init__(self):
        self.client = httpx.Client(
            headers={"User-Agent": settings.http_user_agent, "Accept-Language": "es-ES,es;q=0.9"},
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get(self, url: str, **kwargs) -> httpx.Response:
        r = self.client.get(url, **kwargs)
        r.raise_for_status()
        return r

    def fetch(self, provinces: Iterable[str]) -> List[AuctionItem]:
        raise NotImplementedError
