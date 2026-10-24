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


LIMITS = {"total_per_day": 25, "per_kind": {"piso": 20, "fallo": 2, "resumen": 1, "respuesta": 25}}


def alerter(**telegram):
    cfg = {"token": "123:abc", "chat_id": "42", "limits": LIMITS, **telegram}
    return TelegramAlerter({"alerts": {"telegram": cfg}})


def test_the_notification_says_what_decides_a_flat():
    title, body = TelegramAlerter._push_text(FLAT, {})
    assert "289.000 €" in title and "Navas, Barcelona" in title
    assert "🏠" not in title        # the "house" tag already adds one
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


def test_a_card_telegram_refused_still_reaches_the_phone(live, monkeypatch):
    """The phone is what the user reads. Telegram failing must not cost it."""
    a, posts = live
    monkeypatch.setattr(a, "_send_sync", lambda *args, **kw: None)
    assert _send(a, "habitaclia_2", FLAT)
    assert len(posts) == 1


def test_a_flat_that_reached_only_the_phone_is_still_never_repeated(live, monkeypatch):
    a, posts = live
    monkeypatch.setattr(a, "_send_sync", lambda *args, **kw: None)
    _send(a, "habitaclia_3", FLAT)
    assert not _send(a, "idealista_3", {**FLAT, "price": 288000})
    assert len(posts) == 1


def test_an_exhausted_budget_silences_both_channels(live, monkeypatch):
    from alerts import budget

    a, posts = live
    monkeypatch.setattr(budget, "check", lambda *args, **kw: (False, "tope"))
    assert not _send(a, "habitaclia_4", FLAT)
    assert posts == []


# ── Telegram switched off: everything to the phone ─────────────────────────

def test_telegram_off_sends_flats_only_to_the_phone(monkeypatch, db, posts):
    import scheduler.quiet_hours as qh

    monkeypatch.setenv("NTFY_TOPIC", "secreto")
    monkeypatch.setattr(qh, "is_quiet", lambda *a, **k: False)
    a = alerter(enabled=False)
    tg = []
    monkeypatch.setattr(a, "_send_sync", lambda *args, **kw: tg.append(1) or 1)
    assert _send(a, "habitaclia_5", FLAT)
    assert tg == [] and len(posts) == 1


def test_telegram_off_without_a_phone_channel_keeps_telegram(monkeypatch):
    """Switching the chat off with no push configured would leave the bot
    mute — the way a dead mailbox once went unheard for three weeks."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert alerter(enabled=False).enabled


def test_a_failure_notice_reaches_the_phone_urgently(monkeypatch, db, posts):
    monkeypatch.setenv("NTFY_TOPIC", "secreto")
    a = alerter(enabled=False)
    assert a.send_pulse(error="no pude autenticarme con Gmail")
    (_, payload), = posts
    assert payload["priority"] == 5
    assert "<" not in payload["title"] + payload["message"]


# ── The card names the lift, one of the two things he filters on ───────────

@pytest.mark.parametrize("text,label", [
    ("Piso con ascensor en venta en Navas", "🛗 con ascensor"),
    ("Piso sin ascensor, segunda planta", "🚶 sin ascensor"),
    ("Finca que no tiene ascensor", "🚶 sin ascensor"),
    ("Piso exterior en Navas", "ascensor: no consta"),
])
def test_the_card_says_whether_there_is_a_lift(text, label):
    assert TelegramAlerter._lift_label({"title": text}, "home") == label


def test_the_card_shows_the_lift_next_to_the_floor():
    card = alerter()._format_message({**FLAT, "title": "Piso sin ascensor en Navas",
                                      "floor": "2ª planta"},
                                     {"matched_purpose": "home"}, 0)
    line = next(l for l in card.splitlines() if l.startswith("🏗"))
    assert "2ª planta" in line and "sin ascensor" in line


def test_a_generic_barrio_names_its_town():
    """"269.660 € · Centre" reached the phone: Centre of where?"""
    title, _ = TelegramAlerter._push_text({**FLAT, "district": "centre", "city": "badalona"}, {})
    assert "Centre, Badalona" in title


def test_a_town_without_barrio_is_just_the_town():
    title, _ = TelegramAlerter._push_text({**FLAT, "district": None, "city": "molins de rei"}, {})
    assert title.endswith("Molins De Rei")


def test_an_unverified_flat_says_so_on_the_phone():
    _, body = TelegramAlerter._push_text(FLAT, {"unverified": True})
    assert "sin verificar" in body
