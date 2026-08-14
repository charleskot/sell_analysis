"""Tests for the search profiles that gate alerts.

Three searches run over the same feed with contradictory criteria, so a
listing is checked against each. These cover the conditions that decide
whether the user hears about a flat at all.
"""
import pytest

from analysis.profiles import Profile, ProfileMatcher, normalise


# ── Place-name normalisation ─────────────────────────────────────────────

def test_normalise_strips_accents_and_punctuation():
    assert normalise("Sagrada Família") == "sagrada familia"
    assert normalise("Camp de l'Arpa") == "camp de l arpa"
    assert normalise("L´Hospitalet") == "l hospitalet"


def test_normalise_handles_none_and_empty():
    assert normalise(None) == ""
    assert normalise("  ") == ""


# ── Home profile ─────────────────────────────────────────────────────────

VIVIENDA = {
    "name": "vivienda",
    "label": "🏠 Para vivir",
    "max_price": 330000,
    "min_rooms": 2,
    "max_rooms": 4,
    "areas": [
        {"city": "barcelona", "districts": ["maragall", "el clot", "navas", "sant andreu"]},
        {"city": "sant feliu de llobregat"},
    ],
    "condition_in": ["nuevo", "buen_estado"],
    "needs_work_below_price": 240000,
}


def _home(**kw):
    base = {
        "price": 300000, "area_m2": 75, "rooms": 3,
        "city": "barcelona", "district": "el clot",
        "title": "Piso reformado", "description": "listo para entrar a vivir",
    }
    base.update(kw)
    return base


def test_home_accepts_target_district():
    ok, _ = Profile(VIVIENDA).match(_home(), {})
    assert ok


def test_home_rejects_above_budget():
    ok, reason = Profile(VIVIENDA).match(_home(price=400000), {})
    assert not ok
    assert "precio" in reason


def test_home_rejects_other_barcelona_district():
    ok, reason = Profile(VIVIENDA).match(_home(district="sarrià"), {})
    assert not ok
    assert "barrio" in reason


def test_home_rejects_other_city():
    ok, reason = Profile(VIVIENDA).match(_home(city="terrassa", district="centre"), {})
    assert not ok
    assert "ciudad" in reason


def test_home_accepts_whole_town_entry():
    """An area with no districts admits the entire town."""
    ok, _ = Profile(VIVIENDA).match(
        _home(city="sant feliu de llobregat", district="algun barrio"), {}
    )
    assert ok


def test_home_street_name_does_not_match_district_in_another_town():
    """'Passeig Maragall' is a street in Gavà and a neighbourhood in
    Barcelona. Scoping districts to their town keeps them apart."""
    ok, reason = Profile(VIVIENDA).match(
        _home(city="gavà", district="passeig maragall"), {}
    )
    assert not ok
    assert "ciudad" in reason


def test_home_matches_district_ignoring_accents():
    ok, _ = Profile(VIVIENDA).match(_home(district="Sant Andreu de Palomar"), {})
    assert ok


# ── Condition requirement and its discount exception ─────────────────────

def test_home_rejects_needs_work_at_full_price():
    ok, reason = Profile(VIVIENDA).match(
        _home(price=300000, description="piso para reformar integral"), {}
    )
    assert not ok
    assert "reforma" in reason


def test_home_accepts_needs_work_when_cheap_enough():
    """Below the discount threshold a refurbishment is part of the deal."""
    ok, _ = Profile(VIVIENDA).match(
        _home(price=200000, description="piso para reformar"), {}
    )
    assert ok


def test_home_accepts_move_in_ready():
    ok, _ = Profile(VIVIENDA).match(
        _home(description="totalmente reformado, entrar a vivir"), {}
    )
    assert ok


# ── Investment profile ───────────────────────────────────────────────────

INVERSION = {
    "name": "inversion",
    "label": "💰 Inversión",
    "min_net_yield_pct": 6.0,
    "max_cash_needed": 50000,
}


def _inv_metrics(**kw):
    base = {"net_yield_pct": 7.0, "monthly_cashflow": 300.0, "cash_needed": 40000}
    base.update(kw)
    return base


def test_investment_accepts_good_yield_within_budget():
    ok, reason = Profile(INVERSION).match({"price": 130000, "city": "badalona"}, _inv_metrics())
    assert ok
    assert "yield" in reason


def test_investment_rejects_low_yield():
    ok, reason = Profile(INVERSION).match(
        {"price": 130000, "city": "badalona"}, _inv_metrics(net_yield_pct=4.0)
    )
    assert not ok
    assert "yield" in reason


def test_investment_rejects_when_cash_needed_exceeds_budget():
    """No yield makes an unaffordable deal affordable."""
    ok, reason = Profile(INVERSION).match(
        {"price": 400000, "city": "badalona"}, _inv_metrics(cash_needed=120000)
    )
    assert not ok
    assert "necesita" in reason


def test_investment_ignores_zone():
    ok, _ = Profile(INVERSION).match({"price": 120000, "city": "lleida"}, _inv_metrics())
    assert ok


# ── Matcher across profiles ──────────────────────────────────────────────

def test_matcher_skips_disabled_profiles():
    matcher = ProfileMatcher({"search_profiles": [
        {**VIVIENDA, "enabled": False},
        {**INVERSION, "enabled": True},
    ]})
    assert [p.name for p in matcher.profiles] == ["inversion"]


def test_matcher_reports_every_profile_a_listing_satisfies():
    matcher = ProfileMatcher({"search_profiles": [VIVIENDA, INVERSION]})
    hits = matcher.match(_home(price=200000), _inv_metrics())
    assert {p.name for p, _ in hits} == {"vivienda", "inversion"}


def test_matcher_returns_nothing_when_no_profile_matches():
    matcher = ProfileMatcher({"search_profiles": [VIVIENDA, INVERSION]})
    hits = matcher.match(
        {"price": 900000, "city": "sitges", "rooms": 5},
        {"net_yield_pct": 1.0, "cash_needed": 300000},
    )
    assert hits == []


def test_matcher_with_no_profiles_alerts_nothing():
    """Better silent than alerting on everything when misconfigured."""
    assert ProfileMatcher({}).match(_home(), {}) == []


# ── Missing data ─────────────────────────────────────────────────────────

def test_unknown_rooms_does_not_reject():
    """A parsing gap must not discard a listing that otherwise fits."""
    ok, _ = Profile(VIVIENDA).match(_home(rooms=None), {})
    assert ok


def test_investment_without_metrics_is_rejected():
    """No numbers means no way to tell it is an investment."""
    ok, _ = Profile(INVERSION).match({"price": 100000, "city": "reus"}, {})
    assert not ok
