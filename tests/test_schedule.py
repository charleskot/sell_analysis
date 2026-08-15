"""The bot decides for itself when the daily digest is owed.

It used to have a dedicated 07:00 cron. Now it runs inside a loop that ticks
every few minutes, so the decision moved into the code.
"""
from datetime import datetime, timezone

import pytest

from scheduler.jobs import digest_due


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 8, day, hour, 0, tzinfo=timezone.utc)


def test_not_due_before_the_hour():
    assert digest_due(at(15, 6), "") is False


def test_due_at_the_hour_when_never_sent():
    assert digest_due(at(15, 7), "") is True


def test_not_due_again_the_same_day():
    assert digest_due(at(15, 9), "2026-08-15") is False


def test_due_again_the_next_day():
    assert digest_due(at(16, 7), "2026-08-15") is True


def test_late_is_better_than_never():
    """The loop restarts every few hours and may not be alive at 07:00."""
    assert digest_due(at(15, 23), "2026-08-14") is True


def test_a_missed_day_does_not_stack():
    """Two days offline still means one digest, not two."""
    assert digest_due(at(17, 8), "2026-08-14") is True


@pytest.mark.parametrize("hour", [0, 3, 6])
def test_early_hours_wait_for_the_configured_time(hour):
    assert digest_due(at(15, hour), "2026-08-14") is False


def test_repeat_state_write_does_not_touch_the_row(tmp_path):
    """Rewriting the same value must be a no-op.

    The bot saves its memory by committing a text dump of the database. If a
    no-op write moved updated_at, every cycle would produce a commit and the
    runs that actually found something would be lost in the noise.
    """
    import models.db as db

    db.init_engine(str(tmp_path / "t.db"))
    db.create_all_tables()

    db.set_telegram_state("last_update_id", "42")
    with db.session_scope() as conn:
        before = conn.execute(
            db.select(db.telegram_state).where(db.telegram_state.c.key == "last_update_id")
        ).fetchone().updated_at

    db.set_telegram_state("last_update_id", "42")
    with db.session_scope() as conn:
        after = conn.execute(
            db.select(db.telegram_state).where(db.telegram_state.c.key == "last_update_id")
        ).fetchone().updated_at
    assert after == before

    db.set_telegram_state("last_update_id", "43")
    with db.session_scope() as conn:
        row = conn.execute(
            db.select(db.telegram_state).where(db.telegram_state.c.key == "last_update_id")
        ).fetchone()
    assert row.value == "43"
    assert row.updated_at != before

    db._engine = None


# ── Heartbeat: telling "alive but quiet" from "dead" ────────────────────────

def test_heartbeat_reports_a_quiet_cycle(tmp_path, monkeypatch):
    """A quiet mailbox and a dead bot look identical without this."""
    from scheduler import heartbeat

    monkeypatch.setattr(heartbeat, "PATH", tmp_path / ".heartbeat")
    heartbeat.beat(cycle=12, emails=0, new=0)
    text = heartbeat.describe()
    assert "ciclo 12" in text
    assert "sin correo nuevo" in text
    assert "menos de un minuto" in text


def test_heartbeat_reports_what_arrived(tmp_path, monkeypatch):
    from scheduler import heartbeat

    monkeypatch.setattr(heartbeat, "PATH", tmp_path / ".heartbeat")
    heartbeat.beat(cycle=3, emails=2, new=7)
    assert "2 correos, 7 anuncios nuevos" in heartbeat.describe()


def test_heartbeat_surfaces_a_failed_cycle(tmp_path, monkeypatch):
    from scheduler import heartbeat

    monkeypatch.setattr(heartbeat, "PATH", tmp_path / ".heartbeat")
    heartbeat.beat(cycle=4, error="Gmail 401")
    assert "⚠️ falló: Gmail 401" in heartbeat.describe()


def test_heartbeat_absent_says_so_rather_than_lying(tmp_path, monkeypatch):
    from scheduler import heartbeat

    monkeypatch.setattr(heartbeat, "PATH", tmp_path / "nope")
    assert "aún no he completado un ciclo" in heartbeat.describe()


def test_heartbeat_never_raises_on_an_unwritable_path(tmp_path, monkeypatch):
    """Bookkeeping must not be able to kill a cycle."""
    from scheduler import heartbeat

    monkeypatch.setattr(heartbeat, "PATH", tmp_path / "a-file" / "x" / "y")
    (tmp_path / "a-file").write_text("not a directory")
    heartbeat.beat(cycle=1)


def test_corrupt_heartbeat_is_reported_not_raised(tmp_path, monkeypatch):
    from scheduler import heartbeat

    path = tmp_path / ".heartbeat"
    path.write_text("{not json")
    monkeypatch.setattr(heartbeat, "PATH", path)
    assert "aún no he completado un ciclo" in heartbeat.describe()


# ── Pulse: a line every half hour, so silence is never ambiguous ────────────

def test_pulse_is_due_when_never_sent(tmp_path, monkeypatch):
    from scheduler import heartbeat

    monkeypatch.setattr(heartbeat, "PULSE_PATH", tmp_path / ".pulse")
    assert heartbeat.pulse_due(30) is True


def test_pulse_is_not_due_again_immediately(tmp_path, monkeypatch):
    from scheduler import heartbeat

    monkeypatch.setattr(heartbeat, "PULSE_PATH", tmp_path / ".pulse")
    heartbeat.mark_pulse()
    assert heartbeat.pulse_due(30) is False


def test_pulse_is_due_once_the_interval_has_passed(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from scheduler import heartbeat

    path = tmp_path / ".pulse"
    path.write_text((datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat())
    monkeypatch.setattr(heartbeat, "PULSE_PATH", path)
    assert heartbeat.pulse_due(30) is True


def test_pulse_can_be_switched_off(tmp_path, monkeypatch):
    from scheduler import heartbeat

    monkeypatch.setattr(heartbeat, "PULSE_PATH", tmp_path / "absent")
    assert heartbeat.pulse_due(0) is False


def test_corrupt_pulse_file_errs_towards_reporting(tmp_path, monkeypatch):
    """Better one extra line than a silence nobody can explain."""
    from scheduler import heartbeat

    path = tmp_path / ".pulse"
    path.write_text("no es una fecha")
    monkeypatch.setattr(heartbeat, "PULSE_PATH", path)
    assert heartbeat.pulse_due(30) is True


def test_quiet_pulse_says_it_is_working():
    from alerts.telegram_bot import TelegramAlerter

    text = TelegramAlerter({})._pulse_text({"parsed": 0, "alerts_sent": 0}, None)
    assert "funcionando" in text
    assert "sin novedades" in text


def test_pulse_reports_listings_that_did_not_fit():
    """"Nothing arrived" and "plenty arrived, none good" are different facts."""
    from alerts.telegram_bot import TelegramAlerter

    text = TelegramAlerter({})._pulse_text({"parsed": 34, "alerts_sent": 0}, None)
    assert "34 anuncios revisados" in text
    assert "ninguno encaja" in text


def test_pulse_defers_to_the_alerts_it_just_sent():
    from alerts.telegram_bot import TelegramAlerter

    text = TelegramAlerter({})._pulse_text({"parsed": 9, "alerts_sent": 2}, None)
    assert "2 enviadas" in text


def test_broken_mailbox_is_reported_loudly():
    from alerts.telegram_bot import TelegramAlerter

    text = TelegramAlerter({})._pulse_text(None, "Gmail 401 invalid_grant")
    assert "🔴" in text
    assert "falla la lectura del correo" in text
    assert "invalid_grant" in text
