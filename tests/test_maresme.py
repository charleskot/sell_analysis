"""The Maresme from Montgat to Cabrera de Mar: whole towns, 350.000 €, his zone."""
import yaml

from analysis.profiles import ProfileMatcher, in_focus

CONFIG = yaml.safe_load(open("config.yaml"))
FLAT = {"portal": "habitaclia", "external_id": "1", "price": 340000.0, "area_m2": 78.0,
        "rooms": 3, "floor": "3ª planta", "title": "Piso exterior con ascensor",
        "description": "Piso exterior, luminoso, con ascensor. Para entrar a vivir."}


def matches(**place):
    matcher = ProfileMatcher(CONFIG)
    flat = {**FLAT, **place}
    return bool(matcher.match(flat, {"price_per_m2": flat["price"] / flat["area_m2"]}))


def test_every_town_from_montgat_to_cabrera_is_searched_up_to_350000():
    for town in ("Premià de Mar", "El Masnou", "Masnou", "Vilassar de Mar", "Montgat",
                 "Cabrera de Mar", "Premià de Dalt", "Vilassar de Dalt", "Teià", "Alella", "Cabrils"):
        assert matches(city=town), town


def test_mataro_stays_out():
    assert not matches(city="Mataró")


def test_above_350000_is_out():
    assert not matches(city="Premià de Mar", price=360000.0)


def test_the_maresme_is_his_zone_and_the_barcelona_barris_still_are():
    assert in_focus({"city": "Premià de Mar", "district": "centre"}, CONFIG)
    assert in_focus({"city": "El Masnou", "district": None}, CONFIG)
    assert in_focus({"city": "barcelona", "district": "el clot"}, CONFIG)
    assert not in_focus({"city": "barcelona", "district": "turo de la peira"}, CONFIG)
    assert not in_focus({"city": "Mataró", "district": "centre"}, CONFIG)
