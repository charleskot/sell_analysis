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
