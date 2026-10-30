"""Bargains that miss one limit by a little go out, marked "Casi".

    "Si ves una oportunidad interesante, aunque se pase un poco de los
    límites, para abajo, para arriba o como sea, me la puedes enviar."

Tested against the real config, like the home search itself.
"""
import pytest
import yaml

from analysis.profiles import NEAR_MISS_PREFIX, Profile

# Navas: half the flats ask under 4.000 €/m², a quarter under 3.700 €/m²
NAVAS = {"n": 40, "scope": "navas", "p25": 3700, "median": 4000, "p75": 4400}


@pytest.fixture(scope="module")
def home():
    cfg = yaml.safe_load(open("config.yaml"))
    return Profile(next(p for p in cfg["search_profiles"] if p["name"] == "vivienda"))


def _flat(**kw):
    base = {"price": 240000, "area_m2": 75, "rooms": 3, "floor": "3ª planta",
            "city": "barcelona", "district": "navas",
            "title": "Piso con ascensor en venta en Navas",
            "description": "piso exterior reformado, listo para entrar a vivir"}
    base.update(kw)
    return base


def casi(home, zone=NAVAS, **kw):
    return home.near_miss(_flat(**kw), {}, zone)


def test_a_full_match_is_not_a_near_miss(home):
    assert casi(home) is None


def test_slightly_small_and_cheap_goes_out_saying_why(home):
    why = casi(home, area_m2=62, price=220000)          # 3.548 €/m², 11% under
    assert why.startswith(NEAR_MISS_PREFIX)
    assert "62 m²" in why and "11%" in why


def test_slightly_over_budget_and_cheap_per_metre_goes_out(home):
    why = casi(home, price=325000, area_m2=100,           # 3.250 €/m²
               description="piso exterior para entrar a vivir")
    assert why and "25.000 €" in why


def test_a_barri_nowhere_near_does_not(home):
    assert casi(home, district="ciutat meridiana", price=200000) is None
    assert casi(home, city="terrassa", district="centre", price=150000) is None


def test_missing_by_more_than_a_little_is_still_refused(home):
    assert casi(home, area_m2=58, price=190000) is None          # 7 m² short
    assert casi(home, price=340000, area_m2=110) is None         # 55.000 € over


def test_two_misses_is_not_a_near_miss(home):
    assert casi(home, area_m2=62, rooms=1, price=210000) is None


def test_hard_limits_are_never_eased(home):
    assert casi(home, area_m2=62, price=210000, floor="Bajo") is None
    assert casi(home, area_m2=62, price=210000, floor="4ª planta",
                description="piso exterior sin ascensor") is None
    assert casi(home, area_m2=62, price=210000,
                description="piso interior para entrar a vivir") is None


def test_not_cheap_for_its_zone_is_not_an_opportunity(home):
    assert casi(home, area_m2=62, price=265000) is None          # 3.955 €/m²


def test_suspiciously_cheap_is_a_warning_not_a_deal(home):
    assert casi(home, area_m2=62, price=150000) is None          # 44% under


def test_no_zone_figures_no_judgement(home):
    assert casi(home, zone=None, area_m2=62, price=220000) is None


def test_phone_says_casi_and_why():
    from alerts.telegram_bot import TelegramAlerter

    reason = f"{NEAR_MISS_PREFIX} 67 m² (pides 70) — pero 18% más barato por m² que lo normal en Navas"
    title, body = TelegramAlerter._push_text(_flat(area_m2=62), {"near_miss": True, "match_reason": reason})
    assert title.startswith("Casi · ")
    assert reason in body


def test_near_misses_have_their_own_small_budget():
    cfg = yaml.safe_load(open("config.yaml"))
    per_kind = cfg["alerts"]["telegram"]["limits"]["per_kind"]
    assert 0 < per_kind["casi"] <= 5


# ── "Buen piso a buen precio, cerca de precio objetivo, sin reforma" ───────

def test_close_to_the_zone_price_is_enough_now(home):
    # 319.000 € for 84 m² in a 4.000 €/m² zone: 5% under — not yet; 9% is
    assert casi(home, price=325000, area_m2=84) is None          # 3% under
    assert casi(home, price=325000, area_m2=90)                  # 10% under


def test_any_work_at_all_rules_a_near_miss_out(home):
    for words in ("piso para reformar", "necesita actualización", "ideal para dar tu toque",
                  "piso a actualizar, con posibilidades"):
        assert casi(home, area_m2=62, price=220000, description=words) is None


def test_once_read_the_advert_must_say_it_is_ready(home):
    flat = _flat(area_m2=62, price=220000, description="piso exterior con balcón")
    assert home.near_miss(flat, {}, NAVAS)
    assert home.near_miss(flat, {}, NAVAS, confirm_ready=True) is None
    ready = _flat(area_m2=62, price=220000, description="reformado, para entrar a vivir")
    why = home.near_miss(ready, {}, NAVAS, confirm_ready=True)
    assert why and why.endswith("para entrar a vivir")


def test_a_barri_is_not_its_district(home):
    assert casi(home, district="Carmel (Horta Gui", price=220000) is None
