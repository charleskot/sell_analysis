"""Rent estimator: DB lookup → fallback table."""
import logging
import re

logger = logging.getLogger(__name__)


class RentEstimator:
    def __init__(self, config: dict):
        self._fallback: dict[str, float] = config.get("fallback_rent_per_m2", {})
        self._default = self._fallback.get("default", 10.0)

    def estimate_monthly_rent(self, city: str, district: str | None, area_m2: float) -> float:
        """Returns estimated monthly rent based on zone averages."""
        rent_per_m2 = self._get_rent_per_m2(city, district)
        return round(rent_per_m2 * area_m2, 2)

    def _get_rent_per_m2(self, city: str, district: str | None) -> float:
        """Lookup rent per m²: DB first, then fallback table."""
        # Try DB lookup
        db_val = self._db_lookup(city, district)
        if db_val:
            return db_val

        # Fallback table: try city-level
        city_key = self._normalize_city(city)
        if city_key in self._fallback:
            return self._fallback[city_key]

        logger.debug(f"No rent data for {city}/{district}, using default {self._default}")
        return self._default

    def _db_lookup(self, city: str, district: str | None) -> float | None:
        try:
            from models.db import get_rent_zone_average
            return get_rent_zone_average(city, district)
        except Exception:
            return None

    def _normalize_city(self, city: str) -> str:
        city = city.lower().strip()

        # Servihabitat format: "distrito <name>. <city> capital" or "distrito <name>. <city>"
        m = re.match(r"distrito\s+.+\.\s+(.+?)(?:\s+capital)?$", city)
        if m:
            city = m.group(1).strip()

        # Strip trailing " capital" / " - capital"
        city = re.sub(r"\s*[-–]?\s*capital$", "", city).strip()

        # Canonical name map
        return {
            "madrid": "madrid",
            "barcelona": "barcelona",
            "barcelona-capital": "barcelona",
            "valència": "valencia",
            "valencia": "valencia",
            "sevilla": "sevilla",
            "seville": "sevilla",
            "zaragoza": "zaragoza",
            "málaga": "malaga",
            "malaga": "malaga",
            "bilbao": "bilbao",
            "bilbo": "bilbao",
            "palma": "palma",
            "palma de mallorca": "palma",
            "alicante": "alicante",
            "alacant": "alicante",
            "granada": "granada",
        }.get(city, city)
