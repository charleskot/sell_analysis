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


def test_home_reason_leads_with_entry_and_payment():
    _, reason = Profile(VIVIENDA_HOME).match(
        _home(), {"cash_needed": 66000, "monthly_payment_total": 1250}
    )
    assert "entrada" in reason and "cuota" in reason


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
