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
    "purpose": "investment",
    "min_net_yield_pct": 6.0,
    "min_monthly_cashflow": 0,
    "max_cash_needed": 30000,
    "max_cash_gap": 0,
}


def _inv_metrics(**kw):
    base = {"net_yield_pct": 7.0, "monthly_cashflow": 300.0,
            "cash_needed": 25000, "cash_gap": 0}
    base.update(kw)
    return base


def test_investment_accepts_good_yield_within_budget():
    ok, reason = Profile(INVERSION).match({"price": 130000, "city": "badalona"}, _inv_metrics())
    assert ok
    assert "rentabilidad" in reason


def test_investment_rejects_low_yield():
    ok, reason = Profile(INVERSION).match(
        {"price": 130000, "city": "badalona"}, _inv_metrics(net_yield_pct=4.0)
    )
    assert not ok
    assert "rentabilidad" in reason


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


# ── Refreshing a known listing ───────────────────────────────────────────

def test_upsert_refreshes_url_and_title():
    """A listing stored with a broken link must be repairable by re-reading
    the email. Without this the bad URL survives every later pass, and the
    user keeps clicking through to a 404."""
    from models.db import upsert_listing, get_engine, init_engine, create_all_tables
    from models.schema import listings
    from sqlalchemy import select

    init_engine(":memory:")
    create_all_tables()

    base = {
        "portal": "habitaclia", "external_id": "999999",
        "url": "https://www.habitaclia.com/i999999/",     # truncated, 404
        "title": "viejo", "price": 200000, "area_m2": 70, "rooms": 3,
        "city": "barcelona",
    }
    assert upsert_listing(base) is True

    fixed = {**base,
             "url": "https://www.habitaclia.com/i999999/123/alertas/email/x.htm",
             "title": "Piso en Barcelona - El Clot"}
    assert upsert_listing(fixed) is False     # known listing, not new

    with get_engine().connect() as conn:
        row = conn.execute(
            select(listings.c.url, listings.c.title)
            .where(listings.c.id == "habitaclia_999999")
        ).first()

    assert row.url.endswith("x.htm")
    assert row.title == "Piso en Barcelona - El Clot"


# ── The two purposes read the same numbers differently ───────────────────
# A home is lived in: there is no rent, so yield and cashflow say nothing,
# and stretching for it is an accepted cost. An investment must fund itself.

VIVIENDA_HOME = {**VIVIENDA, "purpose": "home"}


def test_home_accepts_a_flat_that_would_lose_money_as_a_rental():
    """The reason a home is bought has nothing to do with rental return."""
    ok, reason = Profile(VIVIENDA_HOME).match(
        _home(price=330000),
        {"net_yield_pct": 1.8, "monthly_cashflow": -924, "cash_needed": 72600,
         "monthly_payment_total": 1500},
    )
    assert ok
    assert "cashflow" not in reason


def test_home_reason_names_the_criteria_cleared():
    """It used to restate the entry cash and the payment.

    Both are shown in full a few lines above in the alert, so the reason
    said nothing new while leaving out the thing the reader cannot derive:
    on what grounds this flat answers the search.
    """
    _, reason = Profile(VIVIENDA_HOME).match(
        _home(), {"cash_needed": 66000, "monthly_payment_total": 1250}
    )
    assert "hab (pides ≥2)" in reason
    assert "El Clot está en tu lista" in reason
    assert "entrada" not in reason


def test_home_accepts_needing_a_loan_for_the_entry():
    """Borrowing to complete the entry is acceptable for a home."""
    ok, _ = Profile(VIVIENDA_HOME).match(
        _home(price=300000),
        {"cash_needed": 66000, "cash_gap": 36000, "monthly_payment_total": 1400},
    )
    assert ok


def test_investment_rejects_needing_a_loan_for_the_entry():
    """For an investment the same borrowing means it is not funding itself."""
    ok, reason = Profile(INVERSION).match(
        {"price": 200000, "city": "mataró"},
        _inv_metrics(cash_needed=29000, cash_gap=14000),
    )
    assert not ok
    assert "prestados" in reason


def test_investment_rejects_negative_cashflow_despite_good_yield():
    """Both bars must clear: a good yield that still loses money monthly is
    not paying its own way."""
    ok, reason = Profile(INVERSION).match(
        {"price": 130000, "city": "badalona"},
        _inv_metrics(net_yield_pct=7.5, monthly_cashflow=-120),
    )
    assert not ok
    assert "cashflow" in reason


def test_investment_rejects_entry_above_available_cash():
    ok, reason = Profile(INVERSION).match(
        {"price": 200000, "city": "sabadell"}, _inv_metrics(cash_needed=44000)
    )
    assert not ok
    assert "necesita" in reason


# ── Town size as a services proxy ────────────────────────────────────────
# Size stands in for supermarket, health centre, school and train — what
# decides whether a flat lets quickly and sells again without a long wait.

from analysis.municipalities import population_of

SERVICIOS = {
    "name": "inversion", "label": "💰", "purpose": "investment",
    "min_population": 20000, "reject_unknown_population": True,
}


def test_population_lookup_handles_portal_spellings():
    assert population_of("Barcelona") == population_of("barcelona")
    assert population_of("Hospitalet de Llobregat (L')") is not None
    assert population_of("Premià de Mar") == population_of("premia de mar")


def test_population_unknown_town_returns_none():
    """Unknown must stay distinguishable from small."""
    assert population_of("Villarriba del Inventado") is None
    assert population_of(None) is None


def test_rejects_village_below_services_threshold():
    ok, reason = Profile(SERVICIOS).match(
        {"price": 100000, "city": "Collbató"}, {"net_yield_pct": 9.0}
    )
    assert not ok
    assert "hab" in reason


def test_accepts_town_with_services():
    ok, _ = Profile(SERVICIOS).match(
        {"price": 100000, "city": "Mataró"}, {"net_yield_pct": 9.0}
    )
    assert ok


def test_rejects_unknown_town_when_configured():
    ok, reason = Profile(SERVICIOS).match(
        {"price": 100000, "city": "Aldea Perdida"}, {"net_yield_pct": 9.0}
    )
    assert not ok
    assert "desconocida" in reason


def test_unknown_town_allowed_when_not_configured():
    profile = Profile({**SERVICIOS, "reject_unknown_population": False})
    ok, _ = profile.match({"price": 100000, "city": "Aldea Perdida"}, {"net_yield_pct": 9.0})
    assert ok


def test_no_population_filter_when_unset():
    profile = Profile({"name": "x", "label": "x", "purpose": "investment"})
    ok, _ = profile.match({"price": 100000, "city": "Collbató"}, {})
    assert ok


# ── Zones ruled out by hand ──────────────────────────────────────────────

def test_excluded_zone_rejects_by_district():
    profile = Profile({**SERVICIOS, "excluded_zones": ["sant roc"]})
    ok, reason = Profile({**SERVICIOS, "excluded_zones": ["sant roc"]}).match(
        {"price": 90000, "city": "Badalona", "district": "Sant Roc (Artigues)"},
        {"net_yield_pct": 9.0},
    )
    assert not ok
    assert "excluida" in reason


def test_excluded_zone_leaves_other_districts_alone():
    ok, _ = Profile({**SERVICIOS, "excluded_zones": ["sant roc"]}).match(
        {"price": 90000, "city": "Badalona", "district": "Canyadó"},
        {"net_yield_pct": 9.0},
    )
    assert ok


# ── Rent-capped zones ────────────────────────────────────────────────────
# In a declared zone the lawful rent is set by the Generalitat's index, so
# a yield built on market averages is an upper bound there, not a forecast.

from analysis.municipalities import is_tensioned


def test_metro_municipalities_are_flagged_as_capped():
    for city in ("Barcelona", "Badalona", "Sabadell", "Mataró",
                 "L'Hospitalet de Llobregat", "Terrassa"):
        assert is_tensioned(city), city


def test_small_towns_outside_the_declaration_are_not_flagged():
    for city in ("Collbató", "Cànoves i Samalús", "Castellví de Rosanes"):
        assert not is_tensioned(city)


def test_tensioned_lookup_handles_missing_and_odd_spellings():
    assert is_tensioned(None) is False
    assert is_tensioned("") is False
    assert is_tensioned("badalona") == is_tensioned("Badalona")


# ── Condition the advert never stated ────────────────────────────────────
# Alert emails often carry price, m² and rooms and nothing else. Silence
# about the state of a flat is not evidence that it is fine.

def _orch():
    import yaml
    from models.db import init_engine, create_all_tables
    from scheduler.jobs import PipelineOrchestrator
    init_engine(":memory:")
    create_all_tables()
    return PipelineOrchestrator(yaml.safe_load(open("config.yaml")))


def _listing(**kw):
    base = {"price": 133500, "area_m2": 75, "rooms": 4, "city": "sabadell",
            "title": "133.500 € Piso en Sabadell - Espronceda", "description": ""}
    base.update(kw)
    return base


def test_condition_marked_unknown_when_advert_says_nothing():
    m = _orch()._compute_metrics(_listing())
    assert m["condition_unknown"] is True
    assert m["needs_reform"] is False


def test_condition_known_when_advert_describes_it():
    m = _orch()._compute_metrics(
        _listing(description="Totalmente reformado, listo para entrar a vivir")
    )
    assert m["condition_unknown"] is False


def test_reform_cost_is_added_to_the_entry_and_lowers_the_yield():
    """Works are not mortgageable, so they land on the buyer and belong in
    the entry. Costing them at zero describes a flat that does not exist."""
    orch = _orch()
    plain = orch._compute_metrics(_listing(description="Piso reformado, buen estado"))
    works = orch._compute_metrics(_listing(description="Oportunidad para reformar a su gusto"))

    assert works["reform_cost"] > 0
    assert works["cash_needed"] > plain["cash_needed"]
    assert works["net_yield_pct"] < plain["net_yield_pct"]


# ── Disqualifying wording the emails never carried ───────────────────────
# Recovered by reading the listing pages: three of the four best-yielding
# flats were occupied or explicitly unmortgageable, and the yield was the
# premium for exactly that.

@pytest.mark.parametrize("page_text", [
    "badalona (barcelona). okupado (no se puede visitar ni hipotecar)..",
    'sin posesión "ocupado" (no se puede visitar ni hipotecar)',
    "CONDICIONES ESPECIALES DE COMPRA: 1º.- No permite hipoteca sobre el inmueble",
    "Vivienda no hipotecable, solo compra al contado",
    "Sin posibilidad de hipoteca",
])
def test_reject_rules_catch_page_wording(page_text):
    from ingest.email_parsers import _REJECT_RE
    assert _REJECT_RE.search(page_text) is not None


@pytest.mark.parametrize("page_text", [
    "oportunidad para reformar a su gusto. Vivienda amplia y luminosa",
    "precioso piso reformado a estrenar con ascensor en sant andreu",
    "La Casa Agency tiene el placer de presentar esta magnífica vivienda",
])
def test_reject_rules_leave_ordinary_listings_alone(page_text):
    from ingest.email_parsers import _REJECT_RE
    assert _REJECT_RE.search(page_text) is None


# ── Size and floor, for a home ───────────────────────────────────────────

CASA = {**VIVIENDA, "purpose": "home", "min_area_m2": 65, "min_floor": 1}


def test_home_rejects_small_flat():
    ok, reason = Profile(CASA).match(_home(area_m2=52), {})
    assert not ok
    assert "m²" in reason


def test_home_accepts_at_the_size_limit():
    ok, _ = Profile(CASA).match(_home(area_m2=65), {})
    assert ok


def test_home_rejects_ground_floor():
    ok, reason = Profile(CASA).match(_home(floor="Bajo"), {})
    assert not ok
    assert "planta" in reason


def test_home_rejects_ground_floor_named_only_in_the_description():
    """Portals often leave the floor field empty and say it in the text."""
    ok, reason = Profile(CASA).match(
        _home(floor=None, description="Bonito bajo con patio, reformado"), {}
    )
    assert not ok
    assert "bajo" in reason


@pytest.mark.parametrize("text", ["planta baja", "entresuelo", "semisótano"])
def test_home_rejects_ground_floor_synonyms(text):
    ok, _ = Profile(CASA).match(_home(floor=None, description=f"Piso en {text}"), {})
    assert not ok


def test_home_accepts_first_floor_and_above():
    for floor in ("1ª", "3", "Ático"):
        ok, _ = Profile(CASA).match(_home(floor=floor), {})
        assert ok, floor


def test_home_keeps_listing_when_floor_is_simply_unstated():
    """Silence about the floor is a parsing gap, not a ground floor."""
    ok, _ = Profile(CASA).match(_home(floor=None, description="Piso reformado"), {})
    assert ok


# ── Typed instructions ───────────────────────────────────────────────────
# This is a chat, not a CLI. Being told "unknown command" for writing
# "que tenemos?" instead of "/top" is how a tool ends up unused.

def _pipeline():
    import yaml
    from models.db import init_engine, create_all_tables
    from scheduler.jobs import PipelineOrchestrator
    init_engine(":memory:")
    create_all_tables()
    return PipelineOrchestrator(yaml.safe_load(open("config.yaml")))


@pytest.mark.parametrize("phrase", [
    "que tenemos",
    "qué tenemos?",
    "dime lo que tenemos",
    "TOP",
    "resumen",
    "que hay",
    "lista",
])
def test_asking_what_we_have_is_understood_however_it_is_written(phrase):
    reply = _pipeline().handle_command(phrase)
    assert reply
    assert "No he entendido" not in reply


@pytest.mark.parametrize("phrase", ["estado", "¿funciona?", "sigues vivo"])
def test_health_question_is_understood(phrase):
    reply = _pipeline().handle_command(phrase)
    assert "Funcionando" in reply


def test_help_lists_what_can_be_asked():
    reply = _pipeline().handle_command("ayuda")
    assert "qué tenemos" in reply and "estado" in reply


def test_unrelated_message_gets_a_useful_nudge():
    reply = _pipeline().handle_command("cuanto vale el bitcoin")
    assert "No he entendido" in reply
    assert "qué tenemos" in reply


def test_empty_message_is_ignored():
    assert _pipeline().handle_command("   ") is None


def test_summary_says_so_plainly_when_nothing_matches():
    """An empty result must read as an answer, not as a failure."""
    reply = _pipeline()._summary_text()
    assert "Nada encaja" in reply


# ── Comfortable money vs money that exists ───────────────────────────────
# The buyer holds 40.000 € but would rather not go past 30.000 €. Spending
# the difference is allowed and worth being told about.

def test_purchase_within_comfort_uses_no_reserve():
    from analysis.metrics import compute_leverage
    m = compute_leverage(price=120000, monthly_rent=800, purchase_costs_pct=0.12,
                         expense_ratio=0.25, ltv_pct=90, annual_rate_pct=3.0, years=30,
                         available_cash=40000, comfortable_cash=30000,
                         gap_loan_rate_pct=5.5)
    assert m["cash_needed"] < 30000
    assert m["reserve_used"] == 0
    assert m["cash_gap"] == 0


def test_purchase_above_comfort_reports_the_reserve_it_eats():
    from analysis.metrics import compute_leverage
    m = compute_leverage(price=160000, monthly_rent=900, purchase_costs_pct=0.12,
                         expense_ratio=0.25, ltv_pct=90, annual_rate_pct=3.0, years=30,
                         available_cash=40000, comfortable_cash=30000,
                         gap_loan_rate_pct=5.5)
    assert 30000 < m["cash_needed"] <= 40000
    assert m["reserve_used"] == pytest.approx(m["cash_needed"] - 30000, abs=1)
    assert m["cash_gap"] == 0        # still his own money, no loan


def test_purchase_above_everything_becomes_a_loan():
    from analysis.metrics import compute_leverage
    m = compute_leverage(price=250000, monthly_rent=1200, purchase_costs_pct=0.12,
                         expense_ratio=0.25, ltv_pct=90, annual_rate_pct=3.0, years=30,
                         available_cash=40000, comfortable_cash=30000,
                         gap_loan_rate_pct=5.5)
    assert m["cash_gap"] > 0
    assert m["gap_loan_payment"] > 0
    assert m["reserve_used"] == 10000     # the whole buffer, then borrowing


# ── Unverifiable listings must not be sent as investments ────────────────

def test_investment_rejects_listing_whose_page_could_not_be_read():
    """The cheapest flats are cheap because they are occupied or
    unmortgageable, and the email never says so. Silence is not neutral."""
    profile = Profile({**INVERSION, "require_verified_condition": True})
    ok, reason = profile.match(
        {"price": 90000, "city": "badalona"},
        {**_inv_metrics(), "condition_unknown": True},
    )
    assert not ok
    assert "verificar" in reason


def test_investment_accepts_once_the_page_has_been_read():
    profile = Profile({**INVERSION, "require_verified_condition": True})
    ok, _ = profile.match(
        {"price": 90000, "city": "badalona"},
        {**_inv_metrics(), "condition_unknown": False},
    )
    assert ok


def test_home_does_not_require_verification():
    """A home is chosen by visiting it; the advert is a starting point."""
    ok, _ = Profile(VIVIENDA).match(_home(), {"condition_unknown": True})
    assert ok


# ── Further out, but bigger ──────────────────────────────────────────────

FUERA = {
    "name": "vivienda_fuera", "label": "🏠 fuera", "purpose": "home",
    "max_price": 330000, "min_rooms": 3, "min_area_m2": 80, "min_floor": 2,
    "require_any_of": ["balcon", "terraza", "patio"],
    "areas": [{"city": "premia de mar"}, {"city": "castelldefels"},
              {"city": "gava"}, {"city": "mataro"}],
}


def _outer(**kw):
    base = {"price": 280000, "area_m2": 95, "rooms": 3, "city": "mataró",
            "district": "centre", "floor": "3ª",
            "title": "Piso amplio", "description": "con balcón y mucha luz"}
    base.update(kw)
    return base


def test_outer_home_accepts_bigger_flat_further_out():
    ok, _ = Profile(FUERA).match(_outer(), {})
    assert ok


def test_outer_home_rejects_small_flat():
    """Leaving the city has to buy square metres, or it buys nothing."""
    ok, reason = Profile(FUERA).match(_outer(area_m2=72), {})
    assert not ok
    assert "m²" in reason


def test_outer_home_rejects_two_rooms():
    ok, _ = Profile(FUERA).match(_outer(rooms=2), {})
    assert not ok


def test_outer_home_rejects_low_floor():
    ok, _ = Profile(FUERA).match(_outer(floor="1ª"), {})
    assert not ok


def test_outer_home_requires_outdoor_space():
    ok, reason = Profile(FUERA).match(
        _outer(description="piso reformado, muy luminoso"), {}
    )
    assert not ok
    assert "menciona" in reason


@pytest.mark.parametrize("word", ["balcón", "terraza", "patio", "BALCON"])
def test_outer_home_accepts_any_outdoor_wording(word):
    ok, _ = Profile(FUERA).match(_outer(description=f"Piso con {word}"), {})
    assert ok


def test_outer_home_stays_in_its_towns():
    ok, reason = Profile(FUERA).match(_outer(city="terrassa"), {})
    assert not ok
    assert "ciudad" in reason


# ── Knowing the condition, and knowing that you do not ─────────────────────
#
# A 130 m² flat in Sabadell at 176.026 €, sold by "transmisión directa",
# reached the user as a verified investment. Habitaclia labels every card
# "Anuncio nuevo"; the condition detector matched the word "nuevo" in that
# label and concluded it knew the state of the flat. Knowing it meant not
# reading the page, and not reading the page meant require_verified_condition
# had nothing to object to.

import pytest as _pytest

from scheduler.jobs import CONDITION_WORDS


@_pytest.mark.parametrize("chrome", [
    "Anuncio nuevo",                        # Habitaclia's own card label
    "Nuevo piso en tu búsqueda: barcelona",  # Idealista's subject line
    "12 novedades en comarca Barcelonès",
    "Nueva búsqueda guardada",
])
def test_email_furniture_is_not_a_statement_about_the_flat(chrome):
    assert CONDITION_WORDS.search(chrome) is None


@_pytest.mark.parametrize("described", [
    "Piso REFORMADO A ESTRENAR con ascensor",
    "vivienda en buen estado, para entrar a vivir",
    "obra nueva, entrega 2027",
    "piso nuevo de nueva construcción",
    "seminuevo, impecable",
    "para reformar integral",
    "piso para actualizar",
])
def test_a_real_description_is_recognised(described):
    assert CONDITION_WORDS.search(described) is not None


@_pytest.mark.parametrize("wording", [
    "Transmisión directa del inmueble",
    "venta directa del banco",
    "sin derecho a visita",
    "no se garantiza la posesión",
    "entrega de llaves no garantizada",
    "consultar estado posesorio",
])
def test_bank_wording_is_rejected(wording):
    """Reads as neutral sales language and is not: the servicer cannot show
    the flat, usually because somebody is living in it."""
    from ingest.email_parsers import _REJECT_RE

    assert _REJECT_RE.search(wording) is not None


def test_ordinary_sales_language_still_passes():
    """A reject list that eats normal adverts is worse than none."""
    from ingest.email_parsers import _REJECT_RE

    for ok in ("Venta directa del propietario, sin comisiones",
               "Piso reformado con terraza y ascensor",
               "Oportunidad de inversión inmobiliaria en el centro"):
        assert _REJECT_RE.search(ok) is None, ok


# ── Reading the advert before sending ──────────────────────────────────────
#
# A flat in Rubí went out at 99.000 € for 76 m² whose advert opens with
# "POSIBLE OCUPACION DEL INMUEBLE, NO SE PUEDE VISITAR NI FINANCIAR". Every
# one of those phrases was already in the reject list, and the page reads
# fine. It went out because it left by a path that never read it: the check
# lived inline in the ingest, and the sweep added later walked straight past.

class _Orchestrator:
    """The orchestrator's verify(), with its collaborators stubbed."""

    def __init__(self, page=None, matches=True):
        from analysis.profiles import ProfileMatcher
        from scheduler.jobs import PipelineOrchestrator

        self.verify = PipelineOrchestrator.verify.__get__(self)
        self._page = page
        self.profile_matcher = ProfileMatcher({"search_profiles": [INVERSION]})
        self._matches = matches
        self.enriched_calls = 0

    def _enrich(self, listing):
        self.enriched_calls += 1
        if self._page is None:
            return None
        return {**listing, "description": self._page}

    def _compute_metrics(self, listing):
        return _inv_metrics() if self._matches else {"net_yield_pct": 0.1}


LISTING = {"id": "habitaclia_1", "price": 99000, "area_m2": 76,
           "rooms": 3, "city": "rubi", "title": "Piso en Rubí"}


def test_a_known_condition_is_not_re_read():
    """Fetching a page costs a request the portal may refuse; only do it
    when the answer is actually missing."""
    orch = _Orchestrator()
    listing, metrics = orch.verify(LISTING, {"condition_unknown": False})
    assert orch.enriched_calls == 0
    assert listing is LISTING


def test_an_advert_that_disqualifies_stops_the_send():
    orch = _Orchestrator(page="¡¡¡ POSIBLE OCUPACION DEL INMUEBLE, "
                              "NO SE PUEDE VISITAR NI FINANCIAR !!!")
    assert orch.verify(LISTING, {"condition_unknown": True}) is None


def test_a_page_that_cannot_be_read_is_sent_still_marked():
    """Unreadable is not the same as disqualifying. The card says so and
    names it as a reason to look closer."""
    orch = _Orchestrator(page=None)
    result = orch.verify(LISTING, {"condition_unknown": True})
    assert result is not None
    assert result[1]["condition_unknown"] is True


def test_a_clean_advert_carries_its_description_forward():
    orch = _Orchestrator(page="Piso reformado, luminoso, con ascensor")
    listing, metrics = orch.verify(LISTING, {"condition_unknown": True})
    assert "reformado" in listing["description"]
    assert metrics["matched_profiles"]


def test_a_listing_that_stops_matching_once_read_is_dropped():
    """The page can change the numbers enough to disqualify a match that
    looked fine from the email alone."""
    orch = _Orchestrator(page="Piso normal y corriente", matches=False)
    assert orch.verify(LISTING, {"condition_unknown": True}) is None


# ── A loan is not a flat ───────────────────────────────────────────────────
#
# "npl piso ... NPL - Non Performing Loan ... esta operación NO supone la
# compra directa del inmueble" reached the user as a 12% investment: the
# email title said "npl piso", which meant nothing to the filter, and the
# page could not be read from the runner, so the page-level reject — which
# would have caught "venta sin posesión" — never saw it.

@pytest.mark.parametrize("wording", [
    "npl piso en Can Tintorer",
    "Oportunidad NPL con garantía inmobiliaria",
    "non-performing loan",
    "cesión de remate",
    "el comprador adquiere la posición acreedora del préstamo",
    "adquisición de un crédito hipotecario con garantía",
])
def test_loan_sales_are_rejected(wording):
    from ingest.email_parsers import _REJECT_RE

    assert _REJECT_RE.search(wording) is not None, wording


def test_ordinary_investment_marketing_still_passes():
    """"Oportunidad de inversión" is what every seller writes on a normal
    rentable flat; rejecting it would eat half the honest catalogue."""
    from ingest.email_parsers import _REJECT_RE

    assert _REJECT_RE.search("Gran oportunidad de inversión inmobiliaria "
                             "en el centro, piso reformado y luminoso") is None


def test_unreadable_plus_extreme_discount_is_dropped():
    """Nothing legitimate sits 55% under its whole zone while hiding its
    advert. Below the bar, unreadable listings still go out marked."""
    orch = _Orchestrator(page=None)
    dropped = orch.verify(LISTING, {"condition_unknown": True,
                                    "suspicion": ["61% por debajo de la zona (890 vs 2.256€/m²)"]})
    assert dropped is None

    kept = orch.verify(LISTING, {"condition_unknown": True,
                                 "suspicion": ["40% por debajo de la zona (1.203 vs 2.003€/m²)"]})
    assert kept is not None


# ── A shop dressed as a flat ───────────────────────────────────────────────
#
# Sold as "piso", legally still a local comercial: no housing mortgage, no
# cédula de habitabilidad, and the resale fails for the same reason.

@pytest.mark.parametrize("wording", [
    "antiguo local comercial reformado",
    "local convertido en vivienda con cocina equipada",
    "local comercial habilitado como piso",
    "local transformado a loft de diseño",
    "actualmente de uso comercial, ideal para cambio de uso",
    "en trámite de cambio de uso a vivienda",
    "se vende sin cédula de habitabilidad",
    "pendiente de cédula",
    "sin división horizontal",
])
def test_converted_shops_are_rejected(wording):
    from ingest.email_parsers import _REJECT_RE

    assert _REJECT_RE.search(wording) is not None, wording


@pytest.mark.parametrize("wording", [
    "piso luminoso cerca de todos los locales y comercios de la zona",
    "bajos comerciales en la finca, portero",
    "vivienda con cédula de habitabilidad vigente",
])
def test_flats_that_merely_mention_shops_still_pass(wording):
    """Bare "local" appears in half the honest catalogue."""
    from ingest.email_parsers import _REJECT_RE

    assert _REJECT_RE.search(wording) is None, wording
