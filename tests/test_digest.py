"""Each listing goes out as its own card, with everything known about it.

Two things were wrong. The digest queried the database directly on the old
0-100 score, skipping the profiles: a 58 m² one-bedroom in a town no home
search covers arrived as a "Top 5 vivienda". And the card for a home had
grown thin — location, price, size — so the buyer opened every listing to
find out the floor, the state, and whether the price had already been cut.
"""
from datetime import datetime, timedelta, timezone

import pytest

from alerts.telegram_bot import TelegramAlerter


@pytest.fixture
def alerter():
    return TelegramAlerter({})


def card(alerter, purpose="investment", listing=None, metrics=None):
    base_listing = {
        "id": "fotocasa_1", "portal": "fotocasa", "city": "mataro",
        "district": "cerdanyola", "price": 186000, "area_m2": 108, "rooms": 4,
        "bathrooms": 2, "floor": "3ª planta", "condition": "buen_estado",
        "description": "Presentamos este magnifico piso en venta ubicado en la zona "
                       "de Cerdanyola, Mataro. Con una superficie construida de 108 m2 "
                       "y util de 100 m2, esta propiedad se distribuye en cuatro "
                       "dormitorios y dos banos completos.",
        "url": "https://www.fotocasa.es/es/comprar/vivienda/mataro/1",
        "first_seen_at": datetime.now(timezone.utc) - timedelta(days=3),
        "price_history": [{"price": 199000, "date": "2026-08-01"}],
    }
    base_metrics = {
        "price_per_m2": 1722, "cash_needed": 38000, "cash_gap": 0,
        "monthly_payment": 720, "monthly_payment_total": 720,
        "estimated_monthly_rent": 1050, "monthly_cashflow": 180,
        "net_yield_pct": 6.4, "gross_yield_pct": 7.1, "payback_years": 15,
        "matched_profiles": "💰 Inversión", "matched_purpose": purpose,
    }
    base_listing.update(listing or {})
    base_metrics.update(metrics or {})
    return alerter._format_message(base_listing, base_metrics, 80)


# ── What the flat is ────────────────────────────────────────────────────────

def test_card_describes_the_property_physically(alerter):
    """Floor and condition decide a home as firmly as the price does."""
    text = card(alerter)
    for expected in ("186.000€", "108m²", "4 hab", "2 baños", "1.722€/m²",
                     "3ª planta", "buen estado", "fotocasa"):
        assert expected in text, expected


def test_card_quotes_the_sellers_own_description(alerter):
    assert "magnifico piso" in card(alerter)


def test_a_very_short_description_is_not_worth_a_line(alerter):
    assert "📝" not in card(alerter, listing={"description": "Piso."})


def test_long_descriptions_are_cut_on_a_word(alerter):
    text = card(alerter, listing={"description": "palabra " * 200})
    assert "…" in text
    assert "palabra palabra" in text


# ── History: the numbers the bot has been quietly accumulating ──────────────

def test_card_says_how_long_it_has_been_on_the_market(alerter):
    assert "hace 3 días" in card(alerter)


def test_a_listing_seen_today_says_so(alerter):
    text = card(alerter, listing={"first_seen_at": datetime.now(timezone.utc)})
    assert "Visto hoy" in text


def test_a_price_cut_is_surfaced(alerter):
    """The clearest signal a seller is negotiable."""
    text = card(alerter)
    assert "bajado" in text
    assert "199.000€" in text
    assert "-13.000€" in text


def test_a_price_rise_is_not_reported_as_a_cut(alerter):
    text = card(alerter, listing={"price_history": [{"price": 170000, "date": "x"}]})
    assert "subido" in text
    assert "bajado" not in text


def test_an_unchanged_price_says_nothing(alerter):
    assert "bajado" not in card(alerter, listing={"price_history": []})


def test_malformed_price_history_does_not_break_the_card(alerter):
    text = card(alerter, listing={"price_history": [{"date": "x"}]})
    assert "186.000€" in text


# ── Purpose decides which numbers belong ────────────────────────────────────

def test_investment_card_carries_the_rental_maths(alerter):
    text = card(alerter)
    for expected in ("6.4%", "7.1%", "1.050€/mes", "+180€/mes", "38.000€",
                     "720€/mes", "15 años"):
        assert expected in text, expected


def test_investment_card_sizes_the_town(alerter):
    """Town size is how long the flat takes to let and to sell again."""
    assert "129.000 habitantes" in card(alerter)


def test_home_card_hides_the_rental_maths(alerter):
    """Cashflow misreads the purchase of a flat someone will live in."""
    text = card(alerter, purpose="home", metrics={"rent_capped_zone": True})
    assert "Cashflow" not in text
    assert "Alquiler" not in text
    assert "tensionada" not in text
    assert "Entrada + gastos" in text
    assert "720€/mes" in text


def test_card_names_the_search_it_answers(alerter):
    assert "💰 Inversión" in card(alerter)


def test_gap_loan_is_broken_out(alerter):
    text = card(alerter, metrics={"cash_needed": 46200, "cash_gap": 6200,
                                  "gap_loan_payment": 120})
    assert "de tu bolsillo 40.000€" in text
    assert "crédito 6.200€" in text
    assert "Cuota crédito: 120€/mes" in text


# ── Caveats ────────────────────────────────────────────────────────────────

def test_unverified_condition_is_stated_not_hidden(alerter):
    assert "sin confirmar" in card(alerter, metrics={"condition_unknown": True})


def test_reform_cost_is_declared(alerter):
    assert "54.000€" in card(alerter, metrics={"reform_cost": 54000})


def test_capped_rent_is_flagged_on_an_investment(alerter):
    assert "tensionada" in card(alerter, metrics={"rent_capped_zone": True})


def test_money_uses_spanish_separators(alerter):
    """186.000€, not the 186,000€ that was going out."""
    assert alerter._money(186000) == "186.000"


# ── The daily note that frames the cards ───────────────────────────────────

def test_pending_matches_are_announced(alerter):
    assert "2</b> nueva" in alerter._digest_header_text(3, 2)


# ── Nothing new means nothing sent ─────────────────────────────────────────
#
# The digest header went out every three minutes saying "3 encajan, ya te las
# mandé todas" — a message whose entire content is that there is no message.
# A date guard was supposed to hold it to once a day and did not hold on the
# runner, so the guard is no longer what stands between the user and the
# noise: having something to say is.

class _Recorder:
    """Stands in for the parts of the orchestrator the digest touches."""

    def __init__(self, entries, already):
        self.entries, self.already = entries, already
        self.headers, self.cards = [], []

    # alerter surface
    def property_signature(self, listing):
        return listing["id"]

    def already_sent(self, listing_id, dedup_key=None):
        return listing_id in self.already

    def send_digest_header(self, total, pending):
        self.headers.append((total, pending))
        return True

    def send_alert(self, listing_id, listing, metrics, score, dedup_key=None):
        self.cards.append(listing_id)
        return True


def run_digest(entries, already):
    from scheduler.jobs import PipelineOrchestrator

    orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
    rec = _Recorder(entries, already)
    orch.alerter = rec
    orch.digest_entries = lambda *a, **k: entries
    return rec, PipelineOrchestrator.run_daily_digest(orch)


def _entry(listing_id):
    return {"listing": {"id": listing_id}, "metrics": {}, "is_new": True}


def test_all_matches_already_sent_sends_nothing():
    rec, ok = run_digest([_entry("a"), _entry("b")], already={"a", "b"})
    assert rec.headers == []
    assert rec.cards == []
    assert ok is False


def test_no_matches_at_all_sends_nothing():
    rec, ok = run_digest([], already=set())
    assert rec.headers == []
    assert ok is False


def test_something_new_is_announced_and_sent():
    rec, ok = run_digest([_entry("a"), _entry("b")], already={"a"})
    assert rec.headers == [(2, 1)]
    assert rec.cards == ["b"]
    assert ok is True
