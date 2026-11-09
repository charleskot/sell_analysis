"""Favourites: a 👍 keeps the flat, and its price is followed."""
from types import SimpleNamespace

import pytest

import models.db as models_db
from scheduler import favorites


@pytest.fixture
def db(tmp_path):
    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


FLAT = {"portal": "habitaclia", "external_id": "123", "url": "https://www.habitaclia.com/x/d",
        "title": "Piso en Clot", "price": 280000.0, "area_m2": 72.0, "rooms": 3,
        "district": "el clot", "city": "barcelona", "description": ""}


class Alerter:
    def __init__(self):
        self.changes, self.gone = [], []

    def send_favourite_change(self, listing_id, listing, old, new):
        self.changes.append((listing_id, old, new))
        return True

    def send_favourite_gone(self, listing_id, listing):
        self.gone.append(listing_id)
        return True


def page(monkeypatch, status=200, price=None):
    import requests

    text = f'x\\"price\\":{{\\"amount\\":{price},\\"currency\\":null}}' if price else "<html>"
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(status_code=status, text=text))


def test_any_change_of_price_is_news_and_told_once(db):
    db.upsert_listing(FLAT)
    favorites.add("habitaclia_123")
    alerter = Alerter()
    assert favorites.price_seen(alerter, "habitaclia_123", FLAT, 275000.0)
    assert favorites.price_seen(alerter, "habitaclia_123", FLAT, 275000.0)
    assert favorites.price_seen(alerter, "habitaclia_123", FLAT, 279000.0)    # a rise too
    assert alerter.changes == [("habitaclia_123", 280000.0, 275000.0),
                               ("habitaclia_123", 275000.0, 279000.0)]


def test_a_flat_not_liked_is_left_to_the_ordinary_path(db):
    assert favorites.price_seen(Alerter(), "habitaclia_999", FLAT, 1.0) is False


def test_the_advert_is_read_for_its_price(db, monkeypatch):
    db.upsert_listing(FLAT)
    favorites.add("habitaclia_123")
    page(monkeypatch, price=265000)
    alerter = Alerter()
    assert favorites.check(alerter, force=True) == 1
    assert alerter.changes == [("habitaclia_123", 280000.0, 265000.0)]
    from models.db import get_listing_price
    assert get_listing_price("habitaclia_123") == 265000.0
    # The morning control shows where it started
    line, = favorites.control_lines()
    assert line.startswith("⭐ 265.000 € (antes 280.000 €) · El Clot")


def test_a_vanished_advert_is_told_after_two_reads(db, monkeypatch):
    db.upsert_listing(FLAT)
    favorites.add("habitaclia_123")
    page(monkeypatch, status=404)
    alerter = Alerter()
    favorites.check(alerter, force=True)
    assert alerter.gone == []                       # one failed read proves nothing
    favorites.check(alerter, force=True)
    assert alerter.gone == ["habitaclia_123"]
    assert "ya no está anunciado" in favorites.control_lines()[0]


def test_a_dislike_after_a_like_drops_it(db):
    db.upsert_listing(FLAT)
    favorites.add("habitaclia_123")
    assert favorites.remove("habitaclia_123")
    assert favorites.all_favourites() == {}


def test_the_notification_says_up_or_down():
    from alerts.telegram_bot import TelegramAlerter

    listing = {**FLAT, "price": 270000.0}
    title, body = TelegramAlerter._push_text(listing, {"favourite_from": 280000.0})
    assert title == "⭐ Favorito · Baja · 270.000 € · El Clot, Barcelona"
    assert "Antes 280.000 € → ahora 270.000 € (−4%)" in body
    title, body = TelegramAlerter._push_text({**FLAT, "price": 290000.0}, {"favourite_from": 280000.0})
    assert title.startswith("⭐ Favorito · Sube")
    assert "(+4%)" in body
