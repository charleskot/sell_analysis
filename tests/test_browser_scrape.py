"""The browser pass: seed silently, then alert only what is new.

The first read of a search sees every flat on it as new — under ids the
emails never used — and would alert dozens at once. 530 across nine
searches, on the first run.
"""
from datetime import datetime, timedelta, timezone

import pytest

import scraper.habitaclia_browser as hb

URL_A = "https://www.habitaclia.com/comprar/viviendas/x/navas/s?maxPrice=330000"
URL_B = "https://www.habitaclia.com/comprar/viviendas/x/manresa/s?maxPrice=130000"


def _flat(uid, price=300000):
    return {"portal": "habitaclia", "external_id": uid,
            "url": f"https://www.habitaclia.com/comprar/p/x/{uid}/d",
            "title": "Piso en venta en X", "price": float(price), "area_m2": 70.0,
            "rooms": 3, "city": "barcelona", "district": "navas",
            "description": "piso reformado"}


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
    def __init__(self, searches, **cfg):
        from scheduler.jobs import PipelineOrchestrator

        self.run_browser_scrape = PipelineOrchestrator.run_browser_scrape.__get__(self)
        self._ingest_listing = PipelineOrchestrator._ingest_listing.__get__(self)
        self.config = {"alerts": {"fresh_hours": 24},
                       "browser_scraping": {"habitaclia": {
                           "enabled": True, "max_pages": 1, "interval_minutes": 30,
                           "searches": searches, **cfg}}}
        self.alerter = _Alerter()
        self.profile_matcher = _Matcher()

    def _compute_metrics(self, listing):
        return None

    def _minimal_metrics(self, listing):
        return {"condition_unknown": False}

    def verify(self, listing, metrics):
        return listing, metrics


@pytest.fixture
def db(tmp_path):
    import models.db as models_db

    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


@pytest.fixture
def pages(monkeypatch):
    """What each search URL returns; a value may be an exception to raise."""
    data, calls = {}, []

    def fake(url, max_pages=3, city=None, district=None, **_):
        calls.append(url)
        result = data.get(url, [])
        if isinstance(result, Exception):
            raise result
        return [dict(x) for x in result]

    monkeypatch.setattr(hb, "scrape", fake)
    return data, calls


def test_the_first_pass_stores_everything_and_alerts_nothing(db, pages):
    data, _ = pages
    data[URL_A] = [_flat("a1"), _flat("a2"), _flat("a3")]
    o = _Orch([{"url": URL_A, "city": "barcelona", "district": "navas"}])

    st = o.run_browser_scrape(force=True)

    assert st["new"] == 3 and st["seeded"] == 1
    assert o.alerter.sent == []


def test_seeded_flats_are_dated_out_of_the_sweeps_reach(db, pages):
    """Otherwise the sweep finds them "fresh" and sends them anyway."""
    from sqlalchemy import select
    from models.schema import listings

    data, _ = pages
    data[URL_A] = [_flat("a1")]
    _Orch([{"url": URL_A}]).run_browser_scrape(force=True)

    with db.get_engine().connect() as conn:
        seen = conn.execute(select(listings.c.first_seen_at)).scalar()
    age = datetime.now(timezone.utc) - seen.replace(tzinfo=timezone.utc)
    assert age > timedelta(hours=24)


def test_after_the_seed_only_a_new_flat_is_alerted(db, pages):
    data, _ = pages
    data[URL_A] = [_flat("a1"), _flat("a2")]
    o = _Orch([{"url": URL_A}])
    o.run_browser_scrape(force=True)

    data[URL_A] = [_flat("a1"), _flat("a2"), _flat("nuevo")]
    st = o.run_browser_scrape(force=True)

    assert o.alerter.sent == ["habitaclia_nuevo"]
    assert st["new"] == 1 and st["updated"] == 2


def test_it_waits_out_the_interval(db, pages):
    data, calls = pages
    data[URL_A] = [_flat("a1")]
    o = _Orch([{"url": URL_A}])
    o.run_browser_scrape(force=True)
    calls.clear()

    o.run_browser_scrape()           # a tick three minutes later
    assert calls == []


def test_a_failed_search_is_not_seeded_and_does_not_stop_the_others(db, pages):
    data, _ = pages
    data[URL_A] = RuntimeError("blocked")
    data[URL_B] = [_flat("b1")]
    o = _Orch([{"url": URL_A}, {"url": URL_B}])

    st = o.run_browser_scrape(force=True)
    assert st["errors"] == 1 and st["seeded"] == 1

    # Unblocked later: still a first read, still silent.
    data[URL_A] = [_flat("a1"), _flat("a2")]
    o.run_browser_scrape(force=True)
    assert o.alerter.sent == []


def test_an_empty_page_does_not_count_as_the_seed(db, pages):
    """Blocked or re-skinned pages come back empty; seeding on that would
    let the first real read afterwards alert everything on it."""
    data, _ = pages
    data[URL_A] = []
    o = _Orch([{"url": URL_A}])
    o.run_browser_scrape(force=True)

    data[URL_A] = [_flat("a1"), _flat("a2"), _flat("a3")]
    o.run_browser_scrape(force=True)
    assert o.alerter.sent == []


def test_a_search_added_later_is_seeded_on_its_own(db, pages):
    data, _ = pages
    data[URL_A] = [_flat("a1")]
    o = _Orch([{"url": URL_A}])
    o.run_browser_scrape(force=True)

    data[URL_B] = [_flat("b1"), _flat("b2")]
    o.config["browser_scraping"]["habitaclia"]["searches"].append({"url": URL_B})
    o.run_browser_scrape(force=True)
    assert o.alerter.sent == []


def test_disabled_does_nothing(db, pages):
    _, calls = pages
    o = _Orch([{"url": URL_A}], enabled=False)
    assert o.run_browser_scrape(force=True)["searches"] == 0
    assert calls == []
