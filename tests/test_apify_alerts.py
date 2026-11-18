"""A refused Apify token, or a source gone quiet, is told to the phone."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import models.db as models_db


@pytest.fixture
def db(tmp_path):
    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


class Orchestrator:
    def __init__(self):
        from scheduler.jobs import PipelineOrchestrator

        self.config = {"apify": {"idealista": {"enabled": True, "input": {"locations": ["Badalona"]}}}}
        self.pulses = []
        self.alerter = SimpleNamespace(send_pulse=lambda error=None, stats=None: self.pulses.append(error) or True)
        self._run_apify = PipelineOrchestrator._run_apify.__get__(self)
        self._warn_once = PipelineOrchestrator._warn_once.__get__(self)
        self._APIFY_SOURCES = PipelineOrchestrator._APIFY_SOURCES
        self._ingest_listing = lambda *a, **k: None


def test_a_refused_token_is_told_once_a_day(db, monkeypatch):
    import requests
    monkeypatch.setenv("APIFY_TOKEN", "viejo")
    monkeypatch.setattr(requests, "post", lambda *a, **k: SimpleNamespace(
        status_code=401, text="token revoked", raise_for_status=lambda: None))
    o = Orchestrator()
    o._run_apify("idealista", force=True)
    o._run_apify("idealista", force=True)
    assert len(o.pulses) == 1 and "APIFY_TOKEN" in o.pulses[0]


def test_a_source_bringing_nothing_for_hours_is_told(db, monkeypatch):
    import scraper.idealista_apify as apify
    monkeypatch.setenv("APIFY_TOKEN", "bueno")
    monkeypatch.setattr(apify, "run", lambda *a, **k: [])
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    db.set_telegram_state("apify_idealista_last_items_at", long_ago)
    db.set_telegram_state("apify_idealista_seeded", '["Badalona"]')
    o = Orchestrator()
    o._run_apify("idealista", force=True)
    assert len(o.pulses) == 1 and "no trae ningún piso desde hace 30 horas" in o.pulses[0]
