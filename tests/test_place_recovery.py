"""Recovering where a flat is when the alert email leaves it out.

Both cases come from the mailbox on 25 October 2026: an Idealista flat in El
Guinardó whose town was cut to "Bar...", and Fotocasa listings that name only
the street.
"""
import yaml

from analysis.municipalities import city_from_barri
from analysis.profiles import ProfileMatcher
from ingest.email_parsers import extract_location
from ingest.enrich import district_from_page
from scheduler import jobs as jobs_mod


# ── A town cut down to "Bar..." ────────────────────────────────────────────

def test_cut_town_with_a_barcelona_barri_is_barcelona():
    city, district = extract_location(
        "Piso en Avenida de la Mare de Déu de Montserrat, 186, El Guinardó, Bar...")
    assert city == "barcelona" and district == "el guinardó"


def test_barri_names_match_whatever_the_articles():
    assert city_from_barri("Bar...", "El Camp de l'Arpa del Clot") == "barcelona"
    assert city_from_barri("Bar...", "Vilapicina i la Torre Llobeta") == "barcelona"


def test_a_barri_badalona_shares_needs_more_of_the_town():
    assert city_from_barri("B...", "La Salut") is None
    assert city_from_barri("Bar...", "La Salut") == "barcelona"


def test_no_guess_when_the_fragment_is_another_town_or_the_barri_unknown():
    assert city_from_barri("Sab...", "Centre") is None
    assert city_from_barri("Bar...", "La Romànica") is None


def test_whole_towns_are_untouched():
    assert extract_location("Piso en Calle del Prat, Bellvitge, Hospitalet de Llobregat") == (
        "hospitalet de llobregat", "bellvitge")


# ── Fotocasa: the barri is on the advert, not in the email ─────────────────

_PAGE = ("3 habs.\n1 baño\n80 m²\n7ª Planta\n"
         "Piso en venta en Carrer D'antonio Machado, 10, Canyelles\n\nBarcelona Capital\n\n"
         "AGENDE SU VISITA\n…\nPisos y casas en venta en Puerta de Hierro, Madrid\n")


def test_barri_read_from_the_advert_header():
    assert district_from_page(_PAGE) == "Canyelles"


def test_header_without_a_number():
    page = "Piso en venta en Calle Provençals, Sant Martí de Provençals\n\nBarcelona Capital\n"
    assert district_from_page(page) == "Sant Martí de Provençals"


def test_no_header_no_barri():
    assert district_from_page("Pisos en venta en Madrid\nPisos en venta Barcelona\n") is None
    assert district_from_page(None) is None


def _matcher():
    return ProfileMatcher(yaml.safe_load(open("config.yaml")))


def _fotocasa(**kw):
    base = {"portal": "fotocasa", "external_id": "1", "url": "https://www.fotocasa.es/x/1/d",
            "price": 250000, "area_m2": 78, "rooms": 3, "floor": "3ª planta",
            "city": "barcelona", "district": "carrer d'antonio machado 10",
            "title": "piso · Carrer d'Antonio Machado 10, Barcelona",
            "description": "piso exterior con ascensor, listo para entrar a vivir"}
    base.update(kw)
    return base


def test_only_the_missing_barri_is_worth_a_read():
    m = _matcher()
    assert m.needs_district(_fotocasa(), {})
    assert not m.needs_district(_fotocasa(price=400000), {})
    assert not m.needs_district(_fotocasa(city="girona"), {})


class _Orch:
    _STREET_ONLY_PORTALS = jobs_mod.PipelineOrchestrator._STREET_ONLY_PORTALS
    _locate_from_page = jobs_mod.PipelineOrchestrator._locate_from_page

    def __init__(self):
        self.profile_matcher = _matcher()


def _read_as(monkeypatch, page):
    import ingest.enrich as enrich
    import models.db as db

    monkeypatch.setattr(enrich, "fetch_page_text_fallback", lambda url: page)
    monkeypatch.setattr(db, "upsert_listing", lambda raw: False)


def test_flat_in_a_wanted_barri_is_found_through_its_page(monkeypatch):
    _read_as(monkeypatch, "Piso en venta en Carrer de Felip II, 20, Navas\n\nBarcelona Capital\n")
    flat = _fotocasa()
    assert _Orch()._locate_from_page(flat, {})
    assert flat["district"] == "Navas"


def test_flat_outside_the_zones_stays_out(monkeypatch):
    _read_as(monkeypatch, _PAGE)
    assert _Orch()._locate_from_page(_fotocasa(), {}) == []


def test_other_portals_are_not_read(monkeypatch):
    _read_as(monkeypatch, "Piso en venta en X, Navas\n")
    assert _Orch()._locate_from_page(_fotocasa(portal="idealista"), {}) == []


def test_header_with_only_the_barri():
    page = ("2ª Planta\nPiso en venta en La Prosperitat\n\nBarcelona Capital\n\nJTO. VIA JULIA\n"
            "Pisos en venta en Madrid\nPisos en venta en Valencia\n")
    assert district_from_page(page) == "La Prosperitat"


def test_footer_links_are_not_a_header():
    page = "Pisos en venta en Madrid\nPisos y casas en venta en Puerta de Hierro, Madrid\nViviendas\n"
    assert district_from_page(page) is None


def test_floor_read_from_above_the_header():
    from ingest.enrich import floor_from_page

    assert floor_from_page(_PAGE) == "7ª Planta"
    assert floor_from_page("75 m²\nSótano\nPiso en venta en Vilapicina i la Torre Llobeta\n\n"
                           "Barcelona Capital\n") == "Sótano"
    assert floor_from_page("75 m²\nPiso en venta en Porta\n\nBarcelona Capital\n") is None


def test_basement_found_on_the_page_is_refused(monkeypatch):
    _read_as(monkeypatch, "75 m²\nSótano\nPiso en venta en Vilapicina i la Torre Llobeta\n\n"
                          "Barcelona Capital\n")
    flat = _fotocasa(floor=None)      # the email never says
    assert _Orch()._locate_from_page(flat, {}) == []
    assert flat["floor"] == "Sótano"
