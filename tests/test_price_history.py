"""Tests for price history accumulation logic."""
from datetime import datetime, timezone

import pytest

from models.db import build_updated_price_history

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_no_change_no_entry():
    history = build_updated_price_history(150_000, EARLIER, [], 150_000, NOW)
    assert history == []


def test_price_decrease_appends_old_price():
    history = build_updated_price_history(160_000, EARLIER, [], 150_000, NOW)
    assert len(history) == 1
    assert history[0]["price"] == 160_000
    assert history[0]["date"] == EARLIER.isoformat()


def test_price_increase_appends_old_price():
    history = build_updated_price_history(140_000, EARLIER, [], 150_000, NOW)
    assert len(history) == 1
    assert history[0]["price"] == 140_000


def test_accumulates_multiple_changes():
    prior_history = [{"price": 180_000, "date": "2025-10-01T00:00:00+00:00"}]
    history = build_updated_price_history(170_000, EARLIER, prior_history, 160_000, NOW)
    assert len(history) == 2
    assert history[0]["price"] == 180_000
    assert history[1]["price"] == 170_000


def test_none_existing_price_no_entry():
    # Listing had no price before — nothing to record
    history = build_updated_price_history(None, EARLIER, [], 150_000, NOW)
    assert history == []


def test_none_new_price_still_records_old():
    # Price disappears (listing temporarily unparseable) — still record old
    history = build_updated_price_history(150_000, EARLIER, [], None, NOW)
    assert len(history) == 1
    assert history[0]["price"] == 150_000


def test_does_not_mutate_input_list():
    original = [{"price": 100_000, "date": "2025-01-01T00:00:00+00:00"}]
    original_copy = list(original)
    build_updated_price_history(120_000, EARLIER, original, 110_000, NOW)
    assert original == original_copy


def test_fallback_date_when_last_seen_none():
    history = build_updated_price_history(150_000, None, [], 140_000, NOW)
    assert len(history) == 1
    assert NOW.isoformat() in history[0]["date"]
