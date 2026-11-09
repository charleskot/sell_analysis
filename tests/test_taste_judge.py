"""The taste judge: photos and advert read against his taste, and learning
from his 👍/👎. No call here reaches the API: the client is a stand-in."""
import json
from types import SimpleNamespace

import pytest

import models.db as models_db
from analysis import taste_judge
from analysis.taste_judge import TasteJudge


@pytest.fixture
def db(tmp_path):
    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


FLAT = {"portal": "habitaclia", "external_id": "123", "id": "habitaclia_123",
        "url": "https://www.habitaclia.com/comprar/x/d", "title": "Piso en Clot",
        "price": 280000.0, "area_m2": 72.0, "rooms": 3, "floor": "2ª planta",
        "district": "el clot", "city": "barcelona", "description": "Piso reformado y luminoso."}

CONFIG = {"juez": {"enabled": True, "min_score": 5, "max_per_day": 2},
          "alerts": {"ntfy": {"server": "https://ntfy.sh"}}}


class FakeClient:
    """Answers like the API with structured output, and counts the calls."""

    def __init__(self, answer, stop_reason="end_turn"):
        self.answer, self.stop_reason, self.calls = answer, stop_reason, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.answer))])


def judge_with(answer, monkeypatch, stop_reason="end_turn", config=CONFIG):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(taste_judge, "photo_urls", lambda listing, n=8: [])
    judge = TasteJudge(config)
    judge._client = FakeClient(answer, stop_reason)
    return judge


UGLY = {"puntuacion": 3, "estado": "reforma importante", "calle": "ruidosa",
        "resumen": "Cocina de los 80 y baño antiguo, da a la Meridiana", "pegas": ["reforma"]}
NICE = {"puntuacion": 8, "estado": "para entrar a vivir", "calle": "tranquila",
        "resumen": "Reformado y luminoso, calle tranquila", "pegas": []}


# ── Photos ────────────────────────────────────────────────────────────────

def test_the_gallery_is_spread_not_the_first_eight():
    photos = [f"p{i}" for i in range(30)]
    picked = taste_judge._spread(photos, 8)
    assert len(picked) == 8 and picked[:2] == ["p0", "p1"]
    assert picked[-1] in photos[-5:]               # the end of the gallery too


def test_habitaclia_photos_come_from_the_advert(monkeypatch):
    uuid = "31c45162-1c7c-411b-9ed2-05879634c37f"
    page = (f'<img src="https://static.fotocasa.es/images/ads/{uuid}?rule=web_482x269_ar">'
            f'"url\\":\\"https://static.fotocasa.es/images/ads/{uuid}?rule=original')
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(text=page))
    assert taste_judge.photo_urls(FLAT) == [f"https://static.fotocasa.es/images/ads/{uuid}?rule=original"]


def test_stored_photos_are_used_when_there_are_enough():
    flat = {**FLAT, "portal": "fotocasa", "photo_urls": [f"https://x/{i}.jpg" for i in range(5)]}
    assert taste_judge.photo_urls(flat) == [f"https://x/{i}.jpg" for i in range(5)]


def test_idealista_keeps_the_whole_gallery():
    from scraper.idealista_apify import to_raw

    item = {"adid": 1, "price": 250000, "ubication": {"title": "Calle X"},
            "moreCharacteristics": {"constructedArea": 70},
            "multimedia": {"images": [{"url": "https://img/1.jpg"}, {"url": "https://img/2.jpg"}]},
            "thumbnail": "https://img/thumb.jpg"}
    raw = to_raw(item)
    if raw:  # to_raw may refuse a sample without a town; the gallery is what matters
        assert raw["photo_urls"] == ["https://img/1.jpg", "https://img/2.jpg"]


# ── Judging ───────────────────────────────────────────────────────────────

def test_without_a_key_nothing_is_judged(db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert TasteJudge(CONFIG).judge(FLAT) is None


def test_an_ugly_flat_is_held_and_remembered(db, monkeypatch):
    judge = judge_with(UGLY, monkeypatch)
    verdict = judge.judge(FLAT)
    assert verdict["s"] == 3 and verdict["ok"] is False
    assert verdict["calle"] == "ruidosa"
    # Judged once: the next pass reads the stored verdict, no second call
    assert judge.judge(FLAT)["s"] == 3
    assert len(judge._client.calls) == 1
    assert taste_judge.get_verdict("habitaclia_123")["r"].startswith("Cocina")


def test_the_request_carries_his_taste_and_asks_for_structure(db, monkeypatch):
    judge = judge_with(NICE, monkeypatch)
    assert judge.judge(FLAT)["ok"] is True
    call = judge._client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "calles" in call["system"] and "reforma" in call["system"]
    assert "Piso reformado y luminoso." in call["messages"][0]["content"][-1]["text"]


def test_a_refusal_or_failure_sends_the_flat_unjudged(db, monkeypatch):
    judge = judge_with(UGLY, monkeypatch, stop_reason="refusal")
    assert judge.judge(FLAT) is None
    assert taste_judge.get_verdict("habitaclia_123") is None


def test_the_daily_cap_stops_the_calls(db, monkeypatch):
    judge = judge_with(NICE, monkeypatch)
    for i in range(4):
        judge.judge({**FLAT, "id": f"habitaclia_{i}"})
    assert len(judge._client.calls) == 2          # max_per_day


# ── In the pipeline ──────────────────────────────────────────────────────

class _Pipeline:
    def __init__(self, judge, sent=False):
        from scheduler.jobs import PipelineOrchestrator

        self.verify = PipelineOrchestrator.verify.__get__(self)
        self._read_advert = lambda listing, metrics: (listing, metrics)
        self.judge = judge
        self.alerter = SimpleNamespace(already_sent=lambda *a: sent,
                                       property_signature=lambda listing: None)


def test_verify_holds_what_the_judge_turns_down(db, monkeypatch):
    assert _Pipeline(judge_with(UGLY, monkeypatch)).verify(FLAT, {}) is None


def test_verify_passes_the_verdict_on_to_the_notification(db, monkeypatch):
    listing, metrics = _Pipeline(judge_with(NICE, monkeypatch)).verify(FLAT, {})
    assert metrics["juez"]["s"] == 8
    from alerts.telegram_bot import TelegramAlerter
    _, body = TelegramAlerter._push_text(listing, metrics)
    assert "🧐 8/10 · Reformado y luminoso, calle tranquila" in body


def test_a_flat_already_sent_is_not_judged_again(db, monkeypatch):
    judge = judge_with(UGLY, monkeypatch)
    assert _Pipeline(judge, sent=True).verify(FLAT, {}) is not None
    assert judge._client.calls == []


def test_the_control_check_does_not_judge(db, monkeypatch):
    judge = judge_with(UGLY, monkeypatch)
    assert _Pipeline(judge).verify(FLAT, {}, judge=False) is not None
    assert judge._client.calls == []


def test_the_daily_control_lists_what_the_judge_held_back():
    from scheduler.jobs import PipelineOrchestrator

    held = {**FLAT, "juez": {"s": 3, "r": "Cocina de los 80"}}
    report = {"day": "2026-11-09", "escaped": [], "judged_out": [held], "portals": {
        "habitaclia": {"nuevos": 10, "cumplen": 2, "enviados": 1, "repetidos": 0,
                       "descartados": 0, "juez": 1, "escapados": 0}},
        "ideal": "Para entrar a vivir, calle tranquila."}
    out = PipelineOrchestrator.daily_control_text(report)
    assert "1 no te gustarían" in out
    assert "🧐 3/10 · 280.000 € · El Clot · Cocina de los 80 · https://www.habitaclia.com/comprar/x/d" in out
    assert "Para entrar a vivir, calle tranquila." in out


# ── Learning from him ────────────────────────────────────────────────────

def test_taps_and_notes_are_collected(db, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "secreto")
    db.upsert_listing(FLAT)
    lines = "\n".join(json.dumps(m) for m in [
        {"event": "message", "id": "a1", "message": "si habitaclia_123"},
        {"event": "message", "id": "a2", "message": "No me gustan las fincas sin portero"},
    ])
    seen = {}
    import requests

    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return SimpleNamespace(text=lines, raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "get", fake_get)
    assert taste_judge.poll_ratings(CONFIG) == (1, 1)
    assert seen["url"] == "https://ntfy.sh/secreto-gustos/json"
    assert db.get_telegram_state("juez_gustos_desde") == "a2"
    rated = taste_judge.rated_flats()
    assert [(r["listing_id"], r["verdict"]) for r in rated] == [("habitaclia_123", "yes")]


def test_his_ratings_reach_the_judge_and_rewrite_the_ideal(db, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "secreto")
    db.upsert_listing(FLAT)
    db.record_feedback("habitaclia_123", "no", "ntfy")
    taste_judge._store_verdict("habitaclia_123", {"r": "Oscuro y para reformar", "at": "x"})
    judge = judge_with({"piso_ideal": "Luminoso, reformado, calle tranquila."}, monkeypatch,
                       config={"juez": {**CONFIG["juez"], "relearn_every": 1}})
    assert "👎 280.000 € · 72 m² · 3 hab · 2ª planta · El Clot, Barcelona — Oscuro y para reformar" \
        in judge._system()
    assert judge.learn() is True
    assert judge.ideal_profile() == "Luminoso, reformado, calle tranquila."
    assert "Luminoso, reformado" in judge._system()
    # Shown once in the daily control, then not again until it changes
    assert judge.ideal_to_show() == "Luminoso, reformado, calle tranquila."
    assert judge.ideal_to_show() == ""


def test_buttons_need_the_secret_topic(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert taste_judge.rating_actions(CONFIG, "habitaclia_1") == []
