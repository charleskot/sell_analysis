"""Idealista through Apify: rows into listings, a seed, then only news.

The row below is the shape memo23/idealista-scraper returned on 27 October
2026 for the Molins de Rei ático that no Idealista email ever carried
(advertiser details left out).
"""
import pytest

import scraper.idealista_apify as ia
from scraper.idealista_apify import to_raw

ATICO = {
    "adid": 112909156, "price": 255000, "homeType": "penthouse",
    "propertyComment": "Piso de 72 m² en Molins de Rei ... agradable terraza exterior con "
                       "vistas despejadas ... lista para entrar a vivir.",
    "ubication": {"title": "Calle del Puig d'Olorda", "latitude": 41.4087, "longitude": 2.0263,
                  "administrativeAreaLevel2": "Molins de Rei",
                  "administrativeAreaLevel1": "Baix Llobregat Sud, Barcelona"},
    "moreCharacteristics": {"roomNumber": 2, "bathNumber": 1, "exterior": True, "lift": True,
                            "isPenthouse": True, "constructedArea": 72, "floor": "5",
                            "status": "good", "isAuction": False},
    "suggestedTexts": {"title": "Ático en venta en Calle del Puig d'Olorda"},
    "thumbnail": "https://img/1.jpg",
}

GUINARDO = {
    "adid": 1, "price": 230000,
    "propertyComment": "Piso reformado.",
    "ubication": {"title": "Calle X", "administrativeAreaLevel2": "Barcelona",
                  "administrativeAreaLevel3": "Distrito Horta-Guinardó",
                  "administrativeAreaLevel4": "El Guinardó"},
    "moreCharacteristics": {"roomNumber": 3, "constructedArea": 75, "floor": "3",
                            "lift": False, "exterior": False, "status": "renew"},
}


def test_the_atico_in_molins_becomes_a_listing():
    raw = to_raw(ATICO)
    assert raw["portal"] == "idealista" and raw["external_id"] == "112909156"
    assert raw["url"] == "https://www.idealista.com/inmueble/112909156/"   # the id the emails use
    assert raw["city"] == "molins de rei" and raw["district"] is None
    assert raw["price"] == 255000 and raw["area_m2"] == 72 and raw["rooms"] == 2
    assert raw["floor"].startswith("5")
    assert "con ascensor" in raw["description"] and "exterior" in raw["description"]
    assert "terraza" in raw["description"]
    assert raw["description_complete"]


def test_the_atico_matches_the_home_search():
    import yaml
    from analysis.profiles import ProfileMatcher

    matcher = ProfileMatcher(yaml.safe_load(open("config.yaml")))
    assert matcher.match(to_raw(ATICO), {})


def test_barcelona_rows_carry_the_barri_and_what_the_fields_say():
    raw = to_raw(GUINARDO)
    assert raw["city"] == "barcelona" and raw["district"] == "el guinardó"
    for words in ("sin ascensor", "interior", "para reformar"):
        assert words in raw["description"]


def test_a_row_without_id_or_price_is_skipped():
    assert to_raw({"price": 1}) is None
    assert to_raw({"adid": 5}) is None


# ── The pass ──────────────────────────────────────────────────────────────

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
    def __init__(self, **cfg):
        from scheduler.jobs import PipelineOrchestrator

        self.run_idealista_apify = PipelineOrchestrator.run_idealista_apify.__get__(self)
        self._ingest_listing = PipelineOrchestrator._ingest_listing.__get__(self)
        self.config = {"alerts": {"fresh_hours": 24},
                       "apify": {"idealista": {"enabled": True, "interval_minutes": 90,
                                               "input": {"locations": ["Molins de Rei"]}, **cfg}}}
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


@pytest.fixture
def actor(monkeypatch):
    """What the next run returns, and the inputs it was given."""
    state = {"rows": [], "inputs": []}

    def fake(token, actor_input, timeout_s=600):
        state["inputs"].append(actor_input)
        return [dict(r) for r in state["rows"]]

    monkeypatch.setattr(ia, "run", fake)
    monkeypatch.setenv("APIFY_TOKEN", "t")
    return state


def test_no_token_no_run(db, actor, monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN")
    _Orch().run_idealista_apify(force=True)
    assert actor["inputs"] == []


def test_the_first_run_seeds_and_the_next_alerts_only_news(db, actor):
    actor["rows"] = [ATICO]
    o = _Orch()
    first = o.run_idealista_apify(force=True)
    assert first["seeded"] == 1 and o.alerter.sent == []

    actor["rows"] = [{**GUINARDO, "adid": 777, "price": 199000}]
    o.run_idealista_apify(force=True)
    assert o.alerter.sent == ["idealista_777"]


def test_runs_ask_only_for_flats_no_earlier_run_saw(db, actor):
    _Orch().run_idealista_apify(force=True)
    assert actor["inputs"][0]["monitoringMode"] is True


def test_waits_its_interval_between_runs(db, actor):
    o = _Orch()
    o.run_idealista_apify(force=True)
    o.run_idealista_apify()
    assert len(actor["inputs"]) == 1


def test_a_failing_actor_is_not_retried_every_tick(db, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("blocked")

    monkeypatch.setattr(ia, "run", boom)
    monkeypatch.setenv("APIFY_TOKEN", "t")
    o = _Orch()
    assert o.run_idealista_apify(force=True)["errors"] == 1
    o.run_idealista_apify()
    assert len(calls) == 1


# ── A whole description needs no page read ────────────────────────────────

class _Verifier:
    def __init__(self):
        from scheduler.jobs import PipelineOrchestrator

        self.verify = PipelineOrchestrator.verify.__get__(self)
        self.config = {}
        self.profile_matcher = _Matcher()

    def _near_miss(self, listing, metrics, confirm_ready=False):
        return []

    def _enrich(self, listing):                     # pragma: no cover - must not run
        raise AssertionError("no page to read")


def test_a_whole_description_is_screened_without_reading_the_page(db):
    raw = {**to_raw(ATICO), "id": "idealista_112909156"}
    assert _Verifier().verify(raw, {}) == (raw, {})


def test_a_whole_description_that_says_tenanted_is_refused(db):
    raw = {**to_raw(ATICO), "id": "idealista_2",
           "description": "Oportunidad: piso alquilado, ideal inversores"}
    assert _Verifier().verify(raw, {}) is None


def test_an_interior_flat_is_refused_by_the_home_search():
    import yaml
    from analysis.profiles import ProfileMatcher

    matcher = ProfileMatcher(yaml.safe_load(open("config.yaml")))
    inner = {**ATICO, "moreCharacteristics": {**ATICO["moreCharacteristics"], "exterior": False}}
    assert not matcher.match(to_raw(inner), {})
