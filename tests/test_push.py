"""A phone notification for every card, and only for cards.

The user asked for a notification per flat beyond Telegram, and never a
repeat. The push is sent from the one place a card is recorded as sent,
so it inherits every gate the card passed.
"""
import pytest

import alerts.telegram_bot as tb
from alerts.telegram_bot import TelegramAlerter

FLAT = {"price": 289000, "district": "navas", "city": "barcelona", "area_m2": 75,
        "rooms": 3, "floor": "3ª planta", "url": "https://www.habitaclia.com/comprar/x/d",
        "title": "Piso con ascensor en venta en Navas", "description": ""}


def alerter():
    return TelegramAlerter({"alerts": {"telegram": {"token": "123:abc", "chat_id": "42"}}})


def test_the_notification_says_what_decides_a_flat():
    title, body = TelegramAlerter._push_text(FLAT, {})
    assert "289.000 €" in title and "Navas" in title
    assert "75 m²" in body and "3 hab" in body and "3ª planta" in body
    assert "ascensor" in body


def test_a_stated_walkup_is_named_as_such():
    _, body = TelegramAlerter._push_text({**FLAT, "title": "Piso sin ascensor"}, {})
    assert "sin ascensor" in body


def test_an_over_budget_flat_says_so_on_the_phone_too():
    reason = "⚠️ 15.000€ por encima de tus 285.000€ — te lo paso por: 92m² con exterior · 92m²"
    _, body = TelegramAlerter._push_text(FLAT, {"match_reason": reason})
    assert "por encima" in body


@pytest.fixture
def posts(monkeypatch):
    sent = []

    class R:
        status_code = 200

    def fake_post(url, json=None, timeout=None):
        sent.append((url, json))
        return R()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    return sent


def test_without_a_topic_nothing_is_sent(monkeypatch, posts):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    alerter()._push(FLAT, {})
    assert posts == []


def test_with_a_topic_the_flat_goes_to_the_phone(monkeypatch, posts):
    monkeypatch.setenv("NTFY_TOPIC", "secreto")
    alerter()._push(FLAT, {})
    (url, payload), = posts
    assert url == "https://ntfy.sh/"
    assert payload["topic"] == "secreto"
    assert payload["click"] == FLAT["url"]
    assert payload["actions"][0]["url"] == FLAT["url"]


def test_a_dead_notification_service_costs_nothing(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "secreto")
    import requests

    def boom(*a, **k):
        raise ConnectionError("ntfy caído")

    monkeypatch.setattr(requests, "post", boom)
    alerter()._push(FLAT, {})          # must not raise


# ── Only for a card that actually went out ─────────────────────────────────

@pytest.fixture
def db(tmp_path):
    import models.db as models_db

    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


@pytest.fixture
def live(monkeypatch, db, posts):
    import scheduler.quiet_hours as qh

    monkeypatch.setenv("NTFY_TOPIC", "secreto")
    monkeypatch.setattr(qh, "is_quiet", lambda *a, **k: False)
    a = alerter()
    monkeypatch.setattr(a, "_send_sync", lambda *args, **kw: 777)
    monkeypatch.setattr(a, "_format_message", lambda *args, **kw: "tarjeta")
    return a, posts


def _send(a, listing_id, flat):
    """As the pipeline does it: the listing is stored, then alerted."""
    from models.db import upsert_listing

    portal, ext = listing_id.split("_", 1)
    upsert_listing({**flat, "portal": portal, "external_id": ext})
    return a.send_alert(listing_id, flat, {}, 0)


def test_a_sent_card_is_mirrored_once(live):
    a, posts = live
    assert _send(a, "habitaclia_1", FLAT)
    assert len(posts) == 1


def test_a_repeat_sends_neither_card_nor_notification(live):
    a, posts = live
    _send(a, "habitaclia_1", FLAT)
    # The same flat again, through another agency at a 1% lower price.
    again = {**FLAT, "price": 286500, "url": "https://www.idealista.com/inmueble/9/"}
    assert not _send(a, "idealista_9", again)
    assert len(posts) == 1


def test_a_card_telegram_refused_is_not_mirrored(live, monkeypatch):
    a, posts = live
    monkeypatch.setattr(a, "_send_sync", lambda *args, **kw: None)
    assert not _send(a, "habitaclia_2", FLAT)
    assert posts == []
