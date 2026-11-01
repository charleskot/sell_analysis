"""Price drops on known flats, and flats on sale that were never sent.

"Si hay actualizaciones de pisos ya publicados, como bajadas de precio,
también se notifica inmediato. En cualquier plataforma."
"""
from datetime import datetime, timedelta, timezone

import pytest

from alerts.telegram_bot import TelegramAlerter


@pytest.fixture
def db(tmp_path):
    import models.db as models_db

    models_db.init_engine(str(tmp_path / "t.db"))
    models_db.create_all_tables()
    yield models_db
    models_db._engine = None


def _flat(uid="a1", price=310000):
    return {"portal": "habitaclia", "external_id": uid, "url": f"https://h/{uid}/d",
            "title": "Piso con ascensor en venta en Navas", "price": float(price),
            "area_m2": 80.0, "rooms": 3, "city": "barcelona", "district": "navas",
            "description": "piso exterior reformado"}


class _Alerter:
    def __init__(self):
        self.drops, self.sent = [], []

    @staticmethod
    def property_signature(listing):
        return None

    def send_price_drop(self, listing_id, listing, metrics, old_price):
        self.drops.append((listing_id, old_price, listing["price"]))
        return True

    def send_alert(self, listing_id, listing, metrics, score, dedup_key=None):
        self.sent.append(listing_id)
        return True


class _Orch:
    def __init__(self, matches=lambda l: l["price"] <= 300000):
        from scheduler.jobs import PipelineOrchestrator

        self._ingest_listing = PipelineOrchestrator._ingest_listing.__get__(self)
        self._price_drop = PipelineOrchestrator._price_drop.__get__(self)
        self.config = {"alerts": {"fresh_hours": 24}}
        self.alerter = _Alerter()
        outer = self

        class _P:
            name, label, purpose = "vivienda", "🏠", "home"

        class _M:
            def match(self, listing, metrics):
                return [(_P(), "encaja")] if matches(listing) else []

        self.profile_matcher = _M()

    def _compute_metrics(self, listing):
        return None

    def _minimal_metrics(self, listing):
        return {}

    def _near_miss(self, listing, metrics, confirm_ready=False):
        return []

    def verify(self, listing, metrics):
        return listing, metrics


def _stats():
    return {"new": 0, "updated": 0, "filtered_out": 0, "rejected_after_enrich": 0,
            "enriched": 0, "alerts_sent": 0, "by_profile": {}}


def test_a_cut_that_brings_a_flat_within_budget_is_sent(db):
    o = _Orch()
    o._ingest_listing(_flat(price=310000), _stats(), {"alerts": 0}, alert=False)
    o._ingest_listing(_flat(price=295000), _stats(), {"alerts": 0})
    assert o.alerter.drops == [("habitaclia_a1", 310000.0, 295000.0)]


def test_a_cut_on_a_flat_still_outside_the_search_is_not(db):
    o = _Orch()
    o._ingest_listing(_flat(price=360000), _stats(), {"alerts": 0}, alert=False)
    o._ingest_listing(_flat(price=340000), _stats(), {"alerts": 0})
    assert o.alerter.drops == []


def test_a_rise_or_the_same_price_is_not_a_drop(db):
    o = _Orch()
    o._ingest_listing(_flat(price=280000), _stats(), {"alerts": 0}, alert=False)
    o._ingest_listing(_flat(price=280000), _stats(), {"alerts": 0})
    o._ingest_listing(_flat(price=290000), _stats(), {"alerts": 0})
    assert o.alerter.drops == []


def test_a_seed_pass_never_announces_a_drop(db):
    o = _Orch()
    o._ingest_listing(_flat(price=310000), _stats(), {"alerts": 0}, alert=False)
    o._ingest_listing(_flat(price=295000), _stats(), {"alerts": 0}, alert=False)
    assert o.alerter.drops == []


def test_the_phone_says_bajada_and_by_how_much():
    title, body = TelegramAlerter._push_text(_flat(price=285000), {"price_drop_from": 300000})
    assert title.startswith("Bajada · 285.000 €")
    assert "Antes 300.000 € → ahora 285.000 € (−5%)" in body


def test_a_flat_never_sent_says_it_was_already_on_sale():
    title, _ = TelegramAlerter._push_text(_flat(price=250000), {"backlog": True})
    assert title.startswith("Ya en venta · 250.000 €")


def test_each_drop_goes_out_once(db, monkeypatch):
    import alerts.telegram_bot as tb

    monkeypatch.setenv("NTFY_TOPIC", "t")
    pushes = []
    alerter = TelegramAlerter.__new__(TelegramAlerter)
    alerter._config = {"alerts": {"telegram": {"limits": {"per_kind": {"bajada": 5}}}}}
    monkeypatch.setattr(alerter, "_push", lambda listing, metrics: pushes.append(1) or True,
                        raising=False)
    flat = {**_flat(price=285000)}
    assert alerter.send_price_drop("habitaclia_a1", flat, {}, 300000)
    assert not alerter.send_price_drop("habitaclia_a1", flat, {}, 300000)
    assert len(pushes) == 1


# ── Flats on sale that never went out ─────────────────────────────────────

class _Backlog:
    def __init__(self, entries, trap=()):
        from scheduler.jobs import PipelineOrchestrator

        self.send_backlog = PipelineOrchestrator.send_backlog.__get__(self)
        self.config = {"alerts": {"backlog": {"enabled": True, "per_run": 2},
                                  "telegram": {"limits": {"per_kind": {"stock": 20}}}}}
        self._entries, self._trap = entries, set(trap)
        self.alerter = _Alerter()
        self.alerter.already_sent = lambda lid, sig=None: lid in self.alerter.sent

    def digest_entries(self):
        return self._entries

    def verify(self, listing, metrics):
        return None if listing["id"] in self._trap else (listing, metrics)


def _entry(uid, price, days_since_seen=0):
    seen = datetime.now(timezone.utc) - timedelta(days=days_since_seen)
    return {"listing": {**_flat(uid, price), "id": f"habitaclia_{uid}", "last_seen_at": seen},
            "metrics": {}}


def test_the_backlog_goes_out_cheapest_first_a_few_at_a_time(db):
    o = _Backlog([_entry("c", 290000), _entry("a", 210000), _entry("b", 250000)])
    assert o.send_backlog() == 2
    assert o.alerter.sent == ["habitaclia_a", "habitaclia_b"]


def test_traps_and_flats_no_longer_listed_are_skipped(db):
    o = _Backlog([_entry("gone", 200000, days_since_seen=10), _entry("trap", 210000),
                  _entry("ok", 260000)], trap={"habitaclia_trap"})
    o.send_backlog()
    assert o.alerter.sent == ["habitaclia_ok"]
