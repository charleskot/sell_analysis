"""Only what is news reaches the chat.

After the rent table was corrected, the sweep that catches up on held-back
alerts found 601 never-sent matches from before 30 September and began
delivering them twenty a day: cards for flats advertised weeks earlier and
most likely gone. And when the mailbox came back after 23 days dead, its
first cycle read the whole backlog at once. The user's rule is plain —
only what is coming out at each moment — so both paths are bounded.
"""
from datetime import datetime, timedelta, timezone

import pytest


class _Alerter:
    def __init__(self):
        self.sent = []

    @staticmethod
    def property_signature(listing):
        return None

    def already_sent(self, listing_id, signature=None):
        return False

    def send_alert(self, listing_id, listing, metrics, score, dedup_key=None):
        self.sent.append(listing_id)
        return True


class _Sweep:
    """send_pending, with everything around it stubbed."""

    def __init__(self, entries, fresh_hours=24):
        from scheduler.jobs import PipelineOrchestrator

        self.send_pending = PipelineOrchestrator.send_pending.__get__(self)
        self.config = {"alerts": {"fresh_hours": fresh_hours}}
        self.alerter = _Alerter()
        self._entries = entries

    def digest_entries(self):
        return self._entries

    def verify(self, listing, metrics):
        return listing, metrics


def _entry(listing_id, hours_ago, naive=False):
    seen = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    if naive:
        seen = seen.replace(tzinfo=None)
    return {"listing": {"id": listing_id, "first_seen_at": seen},
            "metrics": {"investment_score": 0}}


def test_the_sweep_does_not_resurrect_old_listings():
    sweep = _Sweep([_entry("viejo", hours_ago=72)])
    assert sweep.send_pending() == 0
    assert sweep.alerter.sent == []


def test_the_sweep_still_delivers_what_quiet_hours_held_back():
    """Found at 23:30, sent at 08:00: eight and a half hours, still news."""
    sweep = _Sweep([_entry("de_anoche", hours_ago=9)])
    assert sweep.send_pending() == 1
    assert sweep.alerter.sent == ["de_anoche"]


def test_only_the_fresh_ones_go_out_of_a_mixed_queue():
    sweep = _Sweep([_entry("hace_3_semanas", 500), _entry("hoy", 2),
                    _entry("ayer_tarde", 20), _entry("hace_2_dias", 50)])
    sweep.send_pending()
    assert sweep.alerter.sent == ["hoy", "ayer_tarde"]


def test_the_window_is_configurable():
    sweep = _Sweep([_entry("hace_30h", 30)], fresh_hours=48)
    assert sweep.send_pending() == 1


def test_a_naive_timestamp_is_read_as_utc():
    """SQLite hands datetimes back without a zone."""
    sweep = _Sweep([_entry("viejo_naive", 72, naive=True),
                    _entry("nuevo_naive", 2, naive=True)])
    sweep.send_pending()
    assert sweep.alerter.sent == ["nuevo_naive"]


def test_a_string_timestamp_is_understood():
    seen = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    sweep = _Sweep([{"listing": {"id": "s", "first_seen_at": seen},
                     "metrics": {}}])
    assert sweep.send_pending() == 0


# ── A late email dates its listings to when it was sent ────────────────────

@pytest.fixture
def db(tmp_path):
    import models.db as models_db

    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


def test_a_backlog_listing_keeps_the_date_the_portal_sent_it(db):
    from sqlalchemy import select
    from models.schema import listings

    sent_on = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    db.upsert_listing({"portal": "habitaclia", "external_id": "1",
                       "url": "https://x.example/1", "price": 200000,
                       "first_seen_at": sent_on})
    with db.get_engine().connect() as conn:
        stored = conn.execute(select(listings.c.first_seen_at)).scalar()
    assert stored.replace(tzinfo=timezone.utc) == sent_on


def test_a_live_listing_is_dated_now(db):
    from sqlalchemy import select
    from models.schema import listings

    db.upsert_listing({"portal": "habitaclia", "external_id": "2",
                       "url": "https://x.example/2", "price": 200000})
    with db.get_engine().connect() as conn:
        stored = conn.execute(select(listings.c.first_seen_at)).scalar()
    age = datetime.now(timezone.utc) - stored.replace(tzinfo=timezone.utc)
    assert age < timedelta(minutes=1)


def test_an_email_without_a_date_is_treated_as_current():
    """IMAP mail carries no internalDate; it must not be mistaken for old."""
    from ingest.mailbox import RawEmail

    assert RawEmail(uid="1", sender="a@b.c", subject="s", html="", text="").received_ms == 0
