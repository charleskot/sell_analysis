"""Reading Habitaclia's search-result cards.

The card texts below are copied from the live search page for Navas,
24 October 2026 — one fact per line, as the browser's innerText gives it.
"""
from scraper.habitaclia_browser import parse_card

HREF = ("/comprar/planta-intermedia/cl-las-navas-de-tolosa/sant-andreu/"
        "barcelona-capital/c690d9f0-38b5-4860-b2bf-1c0b0d2dd6be/d")

FULL = """1/32
515.000 €
Ha bajado 10.000 €
Piso con ascensor en venta en CL LAS NAVAS DE TOLOSA
Sant Andreu, Barcelona Capital
70 m²
2 hab.
2 baños
7º"""


def test_a_full_card_gives_every_field():
    x = parse_card(FULL, HREF)
    assert x["external_id"] == "c690d9f0-38b5-4860-b2bf-1c0b0d2dd6be"
    assert x["url"].startswith("https://www.habitaclia.com/comprar/")
    assert x["price"] == 515000
    assert x["price_drop"] == 10000
    assert x["area_m2"] == 70
    assert x["rooms"] == 2
    assert x["bathrooms"] == 2
    assert x["floor"] == "7ª planta"
    assert x["district"] == "sant andreu"
    assert x["city"] == "barcelona"
    assert "en venta en" in x["title"]


def test_a_card_without_a_price_cut_has_none():
    text = FULL.replace("Ha bajado 10.000 €\n", "")
    assert parse_card(text, HREF)["price_drop"] is None


def test_a_photo_counter_is_not_mistaken_for_a_price():
    """"1/32" sits above the price on every card."""
    assert parse_card(FULL, HREF)["price"] == 515000


def test_a_card_with_no_id_in_its_link_is_skipped():
    """Promoted blocks and agency banners link elsewhere."""
    assert parse_card(FULL, "/inmobiliaria/tecnocasa-navas/") is None


def test_a_card_with_no_price_is_skipped():
    text = "\n".join(ln for ln in FULL.splitlines() if "€" not in ln)
    assert parse_card(text, HREF) is None


def test_missing_floor_and_baths_are_left_empty():
    text = "\n".join(ln for ln in FULL.splitlines()
                     if not ln.endswith("º") and "baños" not in ln)
    x = parse_card(text, HREF)
    assert x["floor"] is None and x["bathrooms"] is None
    assert x["rooms"] == 2


def test_a_town_card_reads_province_as_city_and_needs_the_search_to_say():
    """"Manresa, Barcelona" is town then province. The card alone files it
    under Barcelona — which is why each search declares its own city and
    the scraper overrides this."""
    text = FULL.replace("Sant Andreu, Barcelona Capital", "Manresa, Barcelona")
    x = parse_card(text, HREF)
    assert x["city"] == "barcelona"          # the trap
    assert x["district"] == "manresa"


def test_bajo_is_a_floor():
    x = parse_card(FULL.replace("7º", "Bajo"), HREF)
    assert x["floor"] == "bajo"
