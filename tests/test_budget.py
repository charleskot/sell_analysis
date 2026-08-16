"""A hard daily ceiling on everything the bot sends.

Twice in one day a bug turned a scheduled message into a message every
three minutes. Both times the fix reasoned about the code path that caused
it, which defends against the mistake already made rather than the next
one. This counts at the single point every message leaves through, and the
ceiling is absolute.
"""
import json

import pytest

from alerts import budget


CONFIG = {"alerts": {"telegram": {"limits": {
    "total_per_day": 5,
    "per_kind": {"piso": 3, "fallo": 1, "respuesta": 5, "otro": 0},
}}}}


@pytest.fixture
def db(tmp_path):
    import models.db as models_db

    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


def test_a_fresh_day_allows_the_first_message(db):
    assert budget.check(CONFIG, "piso")[0] is True


def test_spending_a_kind_closes_it(db):
    for _ in range(3):
        assert budget.check(CONFIG, "piso")[0] is True
        budget.record("piso")

    allowed, why = budget.check(CONFIG, "piso")
    assert allowed is False
    assert "tope de 'piso' agotado" in why


def test_one_kind_running_out_does_not_close_another(db):
    budget.record("fallo")
    assert budget.check(CONFIG, "fallo")[0] is False
    assert budget.check(CONFIG, "piso")[0] is True


def test_the_total_closes_everything(db):
    """Even a kind with headroom stops once the day's total is spent."""
    for _ in range(3):
        budget.record("piso")
    budget.record("fallo")
    budget.record("respuesta")

    allowed, why = budget.check(CONFIG, "respuesta")
    assert allowed is False
    assert "tope diario agotado" in why


def test_an_undeclared_kind_is_refused(db):
    """A future code path that forgets to say what it is goes silent."""
    allowed, why = budget.check(CONFIG, "loquesea")
    assert allowed is False
    assert "no declarado" in why


def test_an_unlabelled_message_is_refused(db):
    """Zero is a real cap, not a missing one."""
    assert budget.check(CONFIG, budget.UNLABELLED)[0] is False


def test_yesterdays_tally_does_not_count_against_today(db):
    from models.db import set_telegram_state

    set_telegram_state(budget.KEY, json.dumps(
        {"day": "2020-01-01", "counts": {"piso": 99}}
    ))
    assert budget.check(CONFIG, "piso")[0] is True


def test_a_corrupt_tally_does_not_lock_the_bot_out(db):
    """Erring towards silence here would be indistinguishable from broken."""
    from models.db import set_telegram_state

    set_telegram_state(budget.KEY, "{no es json")
    assert budget.check(CONFIG, "piso")[0] is True


def test_recording_never_raises(monkeypatch):
    import models.db as models_db

    monkeypatch.setattr(models_db, "_engine", None)
    budget.record("piso")


def test_status_reports_the_spend(db):
    budget.record("piso")
    budget.record("piso")
    text = budget.describe(CONFIG)
    assert "2 de 5 hoy" in text
    assert "2 piso" in text


def test_status_on_a_quiet_day(db):
    assert budget.describe(CONFIG) == "0 de 5 hoy"


# ── The gate is in the send path, not in the callers ───────────────────────

def test_send_is_refused_once_the_budget_is_spent(db, monkeypatch):
    """The point of the chokepoint: no caller can talk its way past it."""
    from alerts.telegram_bot import TelegramAlerter

    cfg = {"alerts": {"telegram": {
        "token": "t", "chat_id": "c",
        "limits": {"total_per_day": 9, "per_kind": {"piso": 1}},
    }}}
    alerter = TelegramAlerter(cfg)

    calls = []
    monkeypatch.setattr(budget, "check", lambda c, k: (False, "sin presupuesto"))
    monkeypatch.setattr("requests.post", lambda *a, **k: calls.append(1))

    assert alerter._send_sync("hola", kind="piso") is False
    assert calls == []


def test_an_unlabelled_send_is_refused_by_default(db):
    """_send_sync defaults to the unlabelled kind, which is capped at zero."""
    from alerts.telegram_bot import TelegramAlerter

    alerter = TelegramAlerter({"alerts": {"telegram": {
        "token": "t", "chat_id": "c",
        "limits": {"total_per_day": 9, "per_kind": {"piso": 5}},
    }}})
    assert alerter._send_sync("hola") is False
