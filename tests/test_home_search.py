"""The home search as the buyer set it on 24 October 2026.

Tested against the real config file, so the code and the configured
profile are checked together.

    "Piso con al menos 70 m², con ascensor, y si es sin ascensor pues en
    primera o segunda planta. Con luz, con reforma o para entrar a vivir,
    poca reforma si es necesario. Menos de 285.000 €; si fuera a más es
    porque estamos viendo un piso muy bueno que amerita pagarlo."
"""
import pytest
import yaml

from analysis.profiles import Profile


@pytest.fixture(scope="module")
def home():
    cfg = yaml.safe_load(open("config.yaml"))
    spec = next(p for p in cfg["search_profiles"] if p["name"] == "vivienda")
    return Profile(spec)


def _flat(**kw):
    base = {"price": 270000, "area_m2": 75, "rooms": 3, "floor": "3ª planta",
            "city": "barcelona", "district": "navas",
            "title": "Piso con ascensor en venta en Navas",
            "description": "piso exterior reformado, listo para entrar a vivir"}
    base.update(kw)
    return base


def ok(home, **kw):
    return home.match(_flat(**kw), {})


# ── Price: a budget, and a ceiling he will break for the right flat ────────

def test_within_budget_passes(home):
    assert ok(home)[0]


def test_over_budget_without_a_reason_is_refused(home):
    good, why = ok(home, price=300000, area_m2=72,
                   description="piso para entrar a vivir")
    assert not good and "justifique" in why


def test_over_budget_with_terrace_and_size_passes_and_says_so(home):
    good, why = ok(home, price=300000, area_m2=92,
                   description="gran terraza, listo para entrar a vivir")
    assert good
    assert "por encima" in why and "15.000" in why


def test_over_budget_with_balcony_and_light_passes(home):
    assert ok(home, price=310000, area_m2=74,
              description="balcón a la calle, muy luminoso")[0]


def test_above_the_stretch_ceiling_nothing_passes(home):
    assert not ok(home, price=335000, area_m2=110,
                  description="terraza enorme, luminoso, reformado")[0]


# ── Size ──────────────────────────────────────────────────────────────────

def test_under_70_square_metres_is_refused(home):
    assert not ok(home, area_m2=65)[0]


# ── Lift, or a low enough walk-up ─────────────────────────────────────────

@pytest.mark.parametrize("floor", ["1ª planta", "2ª planta"])
def test_no_lift_is_fine_on_the_first_two_floors(home, floor):
    assert ok(home, floor=floor, title="Piso en venta en Navas",
              description="piso sin ascensor, exterior")[0]


def test_no_lift_on_the_third_floor_is_refused(home):
    good, why = ok(home, floor="3ª planta", title="Piso en venta en Navas",
                   description="piso sin ascensor, exterior")
    assert not good and "sin ascensor" in why


def test_no_lift_and_no_floor_stated_is_refused(home):
    assert not ok(home, floor=None, title="Piso en venta en Navas",
                  description="finca sin ascensor")[0]


def test_silence_about_the_lift_is_not_a_reason_to_refuse(home):
    assert ok(home, floor="5ª planta", title="Piso en venta en Navas",
              description="piso exterior para entrar a vivir")[0]


def test_ground_floors_are_refused(home):
    assert not ok(home, floor=None, description="bajo con patio")[0]


# ── Condition: light work yes, a gutting no ───────────────────────────────

@pytest.mark.parametrize("text", [
    "piso para reformar integralmente",
    "necesita reforma integral",
    "a rehabilitar",
    "ideal para reformar por completo",
])
def test_a_flat_that_needs_gutting_is_refused(home, text):
    good, why = ok(home, description=text)
    assert not good and "reforma integral" in why


@pytest.mark.parametrize("text", [
    "piso con reforma integral, a estrenar",
    "necesita pequeña reforma en la cocina",
    "para actualizar a tu gusto",
])
def test_light_work_or_a_done_renovation_passes(home, text):
    assert ok(home, description=text)[0]


# ── Light ─────────────────────────────────────────────────────────────────

def test_an_interior_flat_is_refused(home):
    assert not ok(home, description="piso interior muy tranquilo")[0]


def test_a_courtyard_is_not_an_interior_flat(home):
    assert ok(home, description="exterior con patio interior de luces")[0]


# ── Zones ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("city,district", [
    ("barcelona", "el turó de la peira"),
    ("barcelona", "vilapicina i la torre llobeta"),
    ("barcelona", "el congrés i els indians"),
    ("barcelona", "baix guinardó (ho"),
    ("barcelona", "la marina del port"),
    ("badalona", "centre"),
    ("badalona", "bufalà (bonavista"),
    ("badalona", "sant crist (montig"),
    ("santa coloma de gramenet", "centre"),
    ("hospitalet de llobregat", "centre"),
    ("cornellà de llobregat", "almeda"),
    ("molins de rei", None),
])
def test_wanted_zones_pass(home, city, district):
    assert ok(home, city=city, district=district)[0], (city, district)


@pytest.mark.parametrize("city,district", [
    ("barcelona", "la sagrada familia"),    # dropped from the new search
    ("barcelona", "sant andreu de palomar"),
    ("badalona", "llefià (artigues"),
    ("badalona", "congrés (gorg"),           # Barcelona's Congrés, not this one
    ("santa coloma de gramenet", "fondo"),
    ("hospitalet de llobregat", "collblanc"),
    ("mataró", "centre"),
])
def test_other_zones_are_refused(home, city, district):
    assert not ok(home, city=city, district=district)[0], (city, district)
