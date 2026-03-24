"""Aggregates raw rental listings into per-zone median rent figures stored in DB.

Flow:
  rental scrape → list[RawListing.to_dict()] → aggregate_and_store()
                → update_rent_zone_average() per (city, district) and per city
                → RentEstimator picks them up on next run
"""
import logging
import statistics
from collections import defaultdict

logger = logging.getLogger(__name__)

# Hard bounds for €/m²/month — anything outside is bad data
_MIN_RENT_PER_M2 = 3.0
_MAX_RENT_PER_M2 = 60.0
# Minimum samples before we trust a zone's median
_MIN_SAMPLES_DISTRICT = 3
_MIN_SAMPLES_CITY = 5


def aggregate_and_store(raw_listings: list[dict]) -> dict:
    """Compute median rent/m² per zone and persist to DB.

    Args:
        raw_listings: list of dicts from RawListing.to_dict() where `price`
                      is the monthly rent in €.
    Returns:
        stats dict with keys: valid, skipped, zones_updated.
    """
    from models.db import update_rent_zone_average

    district_samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    city_samples: dict[str, list[float]] = defaultdict(list)

    valid = skipped = 0

    for item in raw_listings:
        price = item.get("price")        # monthly rent €
        area = item.get("area_m2")
        city = (item.get("city") or "").lower().strip()
        district = (item.get("district") or "").lower().strip() or None

        if not price or not area or area <= 0 or not city or city == "desconocido":
            skipped += 1
            continue

        rent_per_m2 = price / area
        if not (_MIN_RENT_PER_M2 <= rent_per_m2 <= _MAX_RENT_PER_M2):
            skipped += 1
            continue

        valid += 1
        city_samples[city].append(rent_per_m2)
        if district:
            district_samples[(city, district)].append(rent_per_m2)

    zones_updated = 0

    for (city, district), samples in district_samples.items():
        if len(samples) >= _MIN_SAMPLES_DISTRICT:
            median = _trimmed_median(samples)
            update_rent_zone_average(city, district, median, len(samples))
            zones_updated += 1
            logger.debug(f"  {city}/{district}: {median:.2f} €/m² ({len(samples)} samples)")

    for city, samples in city_samples.items():
        if len(samples) >= _MIN_SAMPLES_CITY:
            median = _trimmed_median(samples)
            update_rent_zone_average(city, None, median, len(samples))
            zones_updated += 1
            logger.debug(f"  {city}: {median:.2f} €/m² ({len(samples)} samples)")

    logger.info(
        f"Rent aggregation: {valid} valid, {skipped} skipped, {zones_updated} zones updated"
    )
    return {"valid": valid, "skipped": skipped, "zones_updated": zones_updated}


def _trimmed_median(samples: list[float]) -> float:
    """Median after removing top and bottom 10% (reduces outlier impact)."""
    if len(samples) < 5:
        return statistics.median(samples)
    n = len(samples)
    trim = max(1, n // 10)
    trimmed = sorted(samples)[trim:-trim]
    return statistics.median(trimmed)
