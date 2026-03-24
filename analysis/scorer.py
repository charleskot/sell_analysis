"""Investment scoring engine: 0-100 weighted score."""
import logging
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# Gross yield normalization thresholds (%)
YIELD_SCALE = [(0, 0), (3, 10), (4, 30), (5, 50), (6, 65), (7, 80), (8, 90), (9, 100)]

# price/m² discount vs zone average normalization
DISCOUNT_SCALE = [(-0.30, 100), (-0.20, 85), (-0.10, 70), (0, 50), (0.10, 30), (0.20, 15), (0.30, 0)]

CONDITION_SCORES = {
    "nuevo": 100,
    "buen_estado": 70,
    "desconocido": 50,
    "reformar": 20,
}


def _interpolate(value: float, scale: list[tuple]) -> float:
    """Linear interpolation along a scale of (value, score) pairs."""
    if value <= scale[0][0]:
        return float(scale[0][1])
    if value >= scale[-1][0]:
        return float(scale[-1][1])
    for i in range(len(scale) - 1):
        x0, y0 = scale[i]
        x1, y1 = scale[i + 1]
        if x0 <= value <= x1:
            t = (value - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0), 1)
    return 50.0


@dataclass
class ScoreBreakdown:
    gross_yield_score: float = 0.0
    price_per_m2_score: float = 0.0
    location_score: float = 0.0
    condition_score: float = 0.0
    roi_score: float = 0.0
    total: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class InvestmentScorer:
    def __init__(self, config: dict):
        weights = config.get("analysis", {}).get("scoring_weights", {})
        self._w_yield = weights.get("gross_yield", 0.35)
        self._w_price = weights.get("price_per_m2", 0.25)
        self._w_location = weights.get("location_score", 0.20)
        self._w_condition = weights.get("condition_score", 0.10)
        self._w_roi = weights.get("roi_net", 0.10)
        self._location_scores: dict = config.get("location_scores", {})

    def score(
        self,
        city: str,
        district: str | None,
        condition: str | None,
        gross_yield_pct: float | None,
        net_yield_pct: float | None,
        listing_price_per_m2: float | None,
        zone_avg_price_per_m2: float | None,
    ) -> ScoreBreakdown:

        # 1. Gross yield score
        gross_yield_score = _interpolate(gross_yield_pct or 0, YIELD_SCALE)

        # 2. Price per m² vs zone average
        if listing_price_per_m2 and zone_avg_price_per_m2 and zone_avg_price_per_m2 > 0:
            discount = (listing_price_per_m2 - zone_avg_price_per_m2) / zone_avg_price_per_m2
            price_per_m2_score = _interpolate(discount, DISCOUNT_SCALE)
        else:
            price_per_m2_score = 50.0  # neutral when no comparison data

        # 3. Location score
        city_key = self._normalize_city(city)
        city_map = self._location_scores.get(city_key, {})
        district_key = district.lower().strip() if district else ""
        location_score = float(city_map.get(district_key, 50) if district_key else 50)

        # 4. Condition score
        condition_score = float(CONDITION_SCORES.get(condition or "desconocido", 50))

        # 5. ROI score (same normalization as yield)
        roi_score = _interpolate(net_yield_pct or 0, YIELD_SCALE)

        total = round(
            gross_yield_score * self._w_yield
            + price_per_m2_score * self._w_price
            + location_score * self._w_location
            + condition_score * self._w_condition
            + roi_score * self._w_roi,
            1,
        )

        return ScoreBreakdown(
            gross_yield_score=gross_yield_score,
            price_per_m2_score=price_per_m2_score,
            location_score=location_score,
            condition_score=condition_score,
            roi_score=roi_score,
            total=total,
        )

    def _normalize_city(self, city: str) -> str:
        city = city.lower().strip()
        return {
            "barcelona-capital": "barcelona",
            "valència": "valencia",
            "madrid capital": "madrid",
            "sevilla": "sevilla",
            "seville": "sevilla",
            "málaga": "malaga",
            "málaga capital": "malaga",
        }.get(city, city)

    def get_zone_avg_price_per_m2(self, city: str, district: str | None) -> float | None:
        """Compute zone average from DB listings."""
        try:
            from sqlalchemy import select, func, and_
            from models.db import get_engine
            from models.schema import listings

            with get_engine().connect() as conn:
                conditions = [listings.c.city == city, listings.c.price_per_m2.isnot(None)]
                if district:
                    conditions.append(listings.c.district == district)

                row = conn.execute(
                    select(func.avg(listings.c.price_per_m2))
                    .where(and_(*conditions))
                ).fetchone()

                return float(row[0]) if row and row[0] else None
        except Exception:
            return None
