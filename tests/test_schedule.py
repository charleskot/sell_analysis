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
#
# The clock lives in the database rather than a file. A file looked cheaper,
# but the runner did not keep it between cycles, so every cycle believed it
# had never pulsed: a line every three minutes instead of every thirty.


@pytest.fixture
def db(tmp_path):
    import models.db as models_db

    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


def test_pulse_is_due_when_never_sent(db):
    from scheduler import heartbeat

    assert heartbeat.pulse_due(30) is True


def test_pulse_is_not_due_again_immediately(db):
    from scheduler import heartbeat

    heartbeat.mark_pulse()
    assert heartbeat.pulse_due(30) is False


def test_pulse_survives_between_cycles(db):
    """The whole point: a second process must see the first one's pulse."""
    from scheduler import heartbeat

    heartbeat.mark_pulse()
    stored = db.get_telegram_state(heartbeat.PULSE_KEY, "")
    assert stored
    assert heartbeat.pulse_due(30) is False


def test_pulse_is_due_once_the_interval_has_passed(db):
    from datetime import datetime, timedelta, timezone

    from scheduler import heartbeat

    db.set_telegram_state(
        heartbeat.PULSE_KEY,
        (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(),
    )
    assert heartbeat.pulse_due(30) is True


def test_pulse_can_be_switched_off(db):
    from scheduler import heartbeat

    assert heartbeat.pulse_due(0) is False


def test_corrupt_pulse_value_errs_towards_reporting(db):
    """Better one extra line than a silence nobody can explain."""
    from scheduler import heartbeat

    db.set_telegram_state(heartbeat.PULSE_KEY, "no es una fecha")
    assert heartbeat.pulse_due(30) is True


def test_naive_pulse_timestamp_does_not_crash_the_cycle(db):
    """An older value written without a timezone must not raise."""
    from datetime import datetime, timedelta

    from scheduler import heartbeat

    db.set_telegram_state(
        heartbeat.PULSE_KEY, (datetime.utcnow() - timedelta(minutes=31)).isoformat()
    )
    assert heartbeat.pulse_due(30) is True


def test_pulse_without_a_database_still_reports(monkeypatch):
    """Bookkeeping must never be the reason a cycle dies."""
    import models.db as models_db

    from scheduler import heartbeat

    monkeypatch.setattr(models_db, "_engine", None)
    assert heartbeat.pulse_due(30) is True
    heartbeat.mark_pulse()


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


# ── Quiet hours ────────────────────────────────────────────────────────────
#
# Suppressing a message is the easy half. Not losing the flat is the point:
# nothing is recorded as sent while quiet, so the first waking cycle sends it.

from datetime import datetime as _dt, timezone as _tz


def cfg(**over):
    quiet = {"from": 23, "to": 8}
    quiet.update(over)
    return {"alerts": {"telegram": {"quiet_hours": quiet}}}


def utc(hour, minute=0):
    return _dt(2026, 8, 15, hour, minute, tzinfo=_tz.utc)


def test_night_is_quiet():
    from scheduler.quiet_hours import is_quiet

    assert is_quiet(cfg(), utc(1)) is True     # 03:00 Madrid


def test_daytime_is_not_quiet():
    from scheduler.quiet_hours import is_quiet

    assert is_quiet(cfg(), utc(12)) is False   # 14:00 Madrid


def test_window_is_read_in_madrid_time_not_utc():
    """22:30 UTC is 00:30 in Madrid in summer — quiet, though UTC says 22."""
    from scheduler.quiet_hours import is_quiet

    assert is_quiet(cfg(), utc(22, 30)) is True


def test_just_before_the_window_still_sends():
    from scheduler.quiet_hours import is_quiet

    assert is_quiet(cfg(), utc(20, 30)) is False   # 22:30 Madrid


def test_first_hour_after_waking_sends():
    from scheduler.quiet_hours import is_quiet

    assert is_quiet(cfg(), utc(6, 30)) is False    # 08:30 Madrid


def test_a_daytime_window_does_not_wrap():
    from scheduler.quiet_hours import is_quiet

    assert is_quiet(cfg(**{"from": 1, "to": 6}), utc(2)) is True    # 04:00 Madrid
    # 22:00 Madrid is outside 1→6. A window that wrapped would call it quiet.
    assert is_quiet(cfg(**{"from": 1, "to": 6}), utc(20)) is False


def test_no_window_configured_means_never_quiet():
    from scheduler.quiet_hours import is_quiet

    assert is_quiet({}, utc(3)) is False
    assert is_quiet({"alerts": {"telegram": {}}}, utc(3)) is False


def test_equal_bounds_are_not_a_permanent_silence():
    """A misconfigured 8→8 must not mute the bot for ever."""
    from scheduler.quiet_hours import is_quiet

    assert is_quiet(cfg(**{"from": 8, "to": 8}), utc(3)) is False


# ── Never lose a listing found at night ────────────────────────────────────

def test_quiet_alert_is_not_recorded_as_sent(db, monkeypatch):
    """If it were recorded, the morning sweep would skip it for ever."""
    from alerts.telegram_bot import TelegramAlerter
    import scheduler.quiet_hours as quiet

    alerter = TelegramAlerter({"alerts": {"telegram": {
        "token": "t", "chat_id": "c", "quiet_hours": {"from": 23, "to": 8}}}})
    monkeypatch.setattr(quiet, "is_quiet", lambda *a, **k: True)

    assert alerter.send_alert("habitaclia_1", {"price": 1}, {}, 0) is False
    assert alerter.already_sent("habitaclia_1") is False


def test_ever_sent_outlives_the_cooldown_window(db):
    """The sweep re-examines stored listings, so 'recently' is the wrong
    question: every listing leaves a 24-hour window eventually, and the
    catalogue would go out again once a day, for ever."""
    from datetime import timedelta

    from alerts.telegram_bot import TelegramAlerter
    from models.schema import alerts_sent

    with db.session_scope() as conn:
        conn.execute(alerts_sent.insert().values(
            listing_id="habitaclia_9", alert_type="telegram", message_preview="",
            sent_at=_dt.now(_tz.utc) - timedelta(days=30),
        ))

    alerter = TelegramAlerter({"alerts": {"telegram": {"cooldown_hours": 24}}})
    assert db.was_alert_sent_recently("habitaclia_9", 24) is False
    assert alerter.already_sent("habitaclia_9") is True
