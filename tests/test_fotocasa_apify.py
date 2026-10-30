"""Fotocasa through Apify: actor rows into listings.

The rows below follow memo23/fotocasa-scraper's shape as returned on
30 October 2026 (advertiser details left out).
"""
from scraper.fotocasa_apify import to_raw

ROW = {
    "id": "191243353", "url": "https://www.fotocasa.es/es/comprar/vivienda/esplugues-de-llobregat/can-vidalet/191243353/d",
    "rawPrice": 207000, "surface": 72, "rooms": 2, "bathrooms": 1, "floor": "6",
    "propertySubtype": "Flat",
    "description": "Vivienda en muy buen estado, ubicada en la primera planta.",
    "address": {"street": None, "neighborhood": None, "district": "Can Vidalet",
                "municipality": "Esplugues de Llobregat"},
    "amenities": {"elevator": False, "terrace": False, "balcony": False},
    "flags": {"isOccupied": False, "isRentedWithTenants": False,
              "isBareOwnership": False, "isAuctioned": False},
}


def test_a_row_becomes_a_listing_under_the_id_the_emails_use():
    raw = to_raw(ROW)
    assert raw["portal"] == "fotocasa" and raw["external_id"] == "191243353"
    assert raw["city"] == "esplugues de llobregat" and raw["district"] == "can vidalet"
    assert raw["price"] == 207000 and raw["area_m2"] == 72 and raw["rooms"] == 2
    assert raw["description_complete"]


def test_the_floor_code_is_not_taken_for_a_floor():
    assert to_raw(ROW)["floor"] is None


def test_an_unlisted_lift_is_not_read_as_no_lift():
    assert "ascensor" not in to_raw(ROW)["description"]
    with_lift = {**ROW, "amenities": {**ROW["amenities"], "elevator": True}}
    assert "con ascensor" in to_raw(with_lift)["description"]


def test_fotocasas_own_flags_reach_the_occupation_screen():
    from ingest.email_parsers import _REJECT_RE

    for flag in ("isOccupied", "isRentedWithTenants", "isBareOwnership", "isAuctioned"):
        raw = to_raw({**ROW, "flags": {**ROW["flags"], flag: True}})
        assert _REJECT_RE.search(raw["description"]), flag


def test_a_row_without_id_or_price_is_skipped():
    assert to_raw({"rawPrice": 1}) is None
    assert to_raw({"id": "5"}) is None


# ── The pass ──────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone

import pytest

import scraper.idealista_apify as ia
from scheduler.jobs import _published_before


def test_only_a_stated_old_date_is_old():
    cutoff = datetime(2026, 10, 30, tzinfo=timezone.utc)
    assert _published_before("2026-10-20T10:00:00.000Z", cutoff)
    assert not _published_before("2026-10-30T09:00:00.000Z", cutoff)
    assert not _published_before(None, cutoff)
    assert not _published_before("ayer", cutoff)


class _Alerter:
    def __init__(self):
        self.sent = []

    @staticmethod
    def property_signature(listing):
        return None

    def send_alert(self, listing_id, listing, metrics, score, dedup_key=None):
        self.sent.append(listing_id)
        return True


class _Profile:
    name, label, purpose = "vivienda", "🏠", "home"


class _Matcher:
    def match(self, listing, metrics):
        return [(_Profile(), "encaja")]


class _Orch:
    def __init__(self):
        from scheduler.jobs import PipelineOrchestrator

        self.run_fotocasa_apify = PipelineOrchestrator.run_fotocasa_apify.__get__(self)
        self._run_apify = PipelineOrchestrator._run_apify.__get__(self)
        self._ingest_listing = PipelineOrchestrator._ingest_listing.__get__(self)
        self._APIFY_SOURCES = PipelineOrchestrator._APIFY_SOURCES
        self.config = {"alerts": {"fresh_hours": 24},
                       "apify": {"fotocasa": {"enabled": True, "per_search": 10,
                                              "input": {"startUrls": ["u1", "u2"]}}}}
        self.alerter = _Alerter()
        self.profile_matcher = _Matcher()

    def _compute_metrics(self, listing):
        return None

    def _minimal_metrics(self, listing):
        return {}

    def verify(self, listing, metrics):
        return listing, metrics


@pytest.fixture
def db(tmp_path):
    import models.db as models_db

    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


def test_each_search_is_read_apart_and_old_adverts_are_not_news(db, monkeypatch):
    now = datetime.now(timezone.utc)
    calls = []
    rows = {"next": []}

    def fake_each(token, base, urls, per_search, timeout_s=600, actor=None, parallel=4):
        calls.append((urls, per_search, base.get("monitorMode")))
        return [dict(r) for r in rows["next"]]

    monkeypatch.setattr(ia, "run_each", fake_each)
    monkeypatch.setenv("APIFY_TOKEN", "t")
    o = _Orch()
    rows["next"] = [{**ROW, "id": "1"}]
    o.run_fotocasa_apify(force=True)                      # the seed
    assert calls[0] == (["u1", "u2"], 10, True)
    assert o.alerter.sent == []

    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(days=9)).isoformat()
    rows["next"] = [{**ROW, "id": "2", "rawPrice": 150000, "publicationDate": old},
                    {**ROW, "id": "3", "rawPrice": 250000, "surface": 90, "publicationDate": fresh}]
    o.run_fotocasa_apify(force=True)
    assert o.alerter.sent == ["fotocasa_3"]
