"""A flat already on the market is not news under a new id.

27 October 2026: the Sant Feliu house and the Molins flat seen on the 23rd
came back through a Habitaclia email under new numbers and went out as new.
"""
from datetime import datetime, timedelta, timezone

import pytest

import models.db as models_db


@pytest.fixture
def db(tmp_path):
    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


NOW = datetime.now(timezone.utc)
FOUR_DAYS_AGO = NOW - timedelta(days=4)


def _store(db, uid, first_seen=None, **kw):
    raw = {"portal": "habitaclia", "external_id": uid, "url": f"https://h/{uid}",
           "title": "Piso en Molins de Rei", "price": 245000.0, "area_m2": 73.0,
           "rooms": 3, "city": "molins de rei", "district": None, **kw}
    if first_seen:
        raw["first_seen_at"] = first_seen
    db.upsert_listing(raw)
    return raw


def _seen(db, raw):
    return db.was_flat_seen_before(raw, NOW - timedelta(hours=24))


def test_relisted_under_a_new_id_is_not_new(db):
    _store(db, "735c039d", FOUR_DAYS_AGO, district="molins de rei")
    again = _store(db, "3492002813310")
    assert _seen(db, again)


def test_town_named_as_district_does_not_hide_the_same_house(db):
    _store(db, "0cc6cfa9", FOUR_DAYS_AGO, title="Casa con balcón en venta en Centre - Can Nadal",
           city="sant feliu de llobregat", district="sant feliu de llobregat",
           price=250000.0, area_m2=275.0, rooms=2)
    again = _store(db, "3492002810310", title="Casa en Sant Feliu de Llobregat - Cen...",
                   city="sant feliu de llobregat", price=250000.0, area_m2=275.0, rooms=2)
    assert _seen(db, again)


def test_a_different_flat_is_still_new(db):
    _store(db, "old", FOUR_DAYS_AGO)
    assert not _seen(db, _store(db, "new", price=210000.0))
    assert not _seen(db, _store(db, "big", area_m2=110.0))


def test_the_same_flat_from_two_sources_today_is_left_to_the_send_dedup(db):
    _store(db, "today_email")
    assert not _seen(db, _store(db, "today_browser"))


def test_unknown_town_never_matches(db):
    assert not _seen(db, _store(db, "x", city="desconocido"))
