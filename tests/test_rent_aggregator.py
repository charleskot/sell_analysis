"""Unit tests for the rental data aggregator."""
import pytest
from unittest.mock import patch, call
from analysis.rent_aggregator import aggregate_and_store, _trimmed_median


# ── _trimmed_median ──────────────────────────────────────────────────────────

def test_trimmed_median_small_sample():
    assert _trimmed_median([10.0, 12.0, 14.0]) == 12.0


def test_trimmed_median_removes_outliers():
    # Without trimming, a high outlier would skew the result
    samples = [10.0] * 8 + [100.0, 200.0]
    result = _trimmed_median(samples)
    assert result == pytest.approx(10.0)


def test_trimmed_median_single():
    assert _trimmed_median([15.0]) == 15.0


# ── aggregate_and_store ──────────────────────────────────────────────────────

def _make_listing(price, area, city, district=None):
    return {"price": price, "area_m2": area, "city": city, "district": district}


def test_aggregate_basic():
    listings = [
        _make_listing(1200, 80, "madrid", "salamanca"),
        _make_listing(1400, 80, "madrid", "salamanca"),
        _make_listing(1000, 80, "madrid", "salamanca"),
        _make_listing(900, 60, "madrid", "carabanchel"),
        _make_listing(950, 60, "madrid", "carabanchel"),
        _make_listing(880, 60, "madrid", "carabanchel"),
        # city-level only (no district)
        _make_listing(800, 50, "madrid"),
        _make_listing(850, 50, "madrid"),
        _make_listing(820, 50, "madrid"),
        _make_listing(810, 50, "madrid"),
        _make_listing(830, 50, "madrid"),
    ]
    with patch("models.db.update_rent_zone_average") as mock_update:
        stats = aggregate_and_store(listings)

    assert stats["valid"] == 11
    assert stats["skipped"] == 0
    assert stats["zones_updated"] >= 2  # salamanca + carabanchel + city-level
    mock_update.assert_called()


def test_aggregate_filters_bad_data():
    listings = [
        _make_listing(None, 80, "madrid"),          # no price
        _make_listing(1000, None, "madrid"),         # no area
        _make_listing(1000, 80, ""),                 # no city
        _make_listing(1000, 80, "desconocido"),      # unknown city
        _make_listing(10, 80, "madrid"),             # 0.12 €/m² — too low
        _make_listing(100_000, 80, "madrid"),        # 1250 €/m² — too high
    ]
    with patch("models.db.update_rent_zone_average") as mock_update:
        stats = aggregate_and_store(listings)

    assert stats["valid"] == 0
    assert stats["skipped"] == 6
    mock_update.assert_not_called()


def test_aggregate_requires_minimum_samples():
    # Only 2 samples per district — below _MIN_SAMPLES_DISTRICT=3, should not save district
    listings = [
        _make_listing(1200, 80, "barcelona", "eixample"),
        _make_listing(1100, 80, "barcelona", "eixample"),
    ]
    with patch("models.db.update_rent_zone_average") as mock_update:
        stats = aggregate_and_store(listings)

    # District not saved (< 3 samples), city also not saved (< 5 samples)
    assert stats["zones_updated"] == 0
    mock_update.assert_not_called()


def test_aggregate_city_level_saved():
    listings = [_make_listing(900 + i * 10, 60, "valencia") for i in range(5)]
    with patch("models.db.update_rent_zone_average") as mock_update:
        stats = aggregate_and_store(listings)

    assert stats["zones_updated"] == 1
    # Should have been called with district=None for city-level
    calls = mock_update.call_args_list
    assert any(c.args[1] is None for c in calls)


def test_aggregate_empty_input():
    with patch("models.db.update_rent_zone_average") as mock_update:
        stats = aggregate_and_store([])
    assert stats == {"valid": 0, "skipped": 0, "zones_updated": 0}
    mock_update.assert_not_called()
