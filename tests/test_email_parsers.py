"""Tests for portal alert email parsing.

The parser is now the critical path for acquisition, so these cover the
shapes real alert emails take: listing linked twice (thumbnail + title),
several properties per email, tracking params, and the reject patterns.
"""
import pytest

from ingest.email_parsers import (
    clean_url,
    detect_portal,
    parse_area,
    parse_email,
    parse_floor,
    parse_price,
    parse_rooms,
    strip_html,
)


# ── Field parsing ────────────────────────────────────────────────────────

def test_parse_price_spanish_thousands():
    assert parse_price("285.000 €") == 285000
    assert parse_price("1.250.000 €") == 1250000
    assert parse_price("Precio: 199.500 €") == 199500


def test_parse_price_rejects_out_of_range():
    assert parse_price("1.200 €") is None        # monthly rent, not a sale price
    assert parse_price("9.000.000 €") is None    # above sane ceiling
    assert parse_price("sin precio") is None


def test_parse_area():
    assert parse_area("78 m²") == 78
    assert parse_area("120 m2") == 120
    assert parse_area("5 m²") is None            # implausible


def test_parse_rooms():
    assert parse_rooms("3 hab.") == 3
    assert parse_rooms("4 habitaciones") == 4
    assert parse_rooms("2 dormitorios") == 2


def test_parse_floor_numeric_and_words():
    assert parse_floor("3ª planta") == "3"
    assert parse_floor("planta 5ª") == "5"
    assert parse_floor("Bajo") == "Bajo"
    assert parse_floor("Ático") == "Ático"
    assert parse_floor("sin datos") is None


def test_strip_html_separates_blocks():
    out = strip_html("<p>285.000 €</p><p>78 m²</p>")
    assert "285.000" in out and "78" in out
    # Values must not run together into "285.000 €78 m²"
    assert "€78" not in out.replace(" ", "€78") or "|" in out


def test_clean_url_strips_tracking():
    assert clean_url(
        "https://www.idealista.com/inmueble/12345/?utm_source=alerta&xtor=AD-1"
    ) == "https://www.idealista.com/inmueble/12345/"


# ── Portal detection ─────────────────────────────────────────────────────

def test_detect_portal_by_sender():
    assert detect_portal("no-reply@idealista.com", "")["portal"] == "idealista"
    assert detect_portal("alertas@fotocasa.es", "")["portal"] == "fotocasa"


def test_detect_portal_by_body_when_forwarded():
    # A forwarded alert loses the original sender
    body = 'ver <a href="https://www.habitaclia.com/comprar-piso-clot-i123456.htm">aquí</a>'
    assert detect_portal("charles@gmail.com", body)["portal"] == "habitaclia"


def test_detect_portal_unknown():
    assert detect_portal("newsletter@random.com", "hola") is None


# ── Full email parsing ───────────────────────────────────────────────────

IDEALISTA_EMAIL = """
<html><body>
  <h1>3 inmuebles nuevos para tu búsqueda</h1>

  <a href="https://www.idealista.com/inmueble/11111111/"><img src="foto1.jpg"></a>
  <a href="https://www.idealista.com/inmueble/11111111/">Piso en Carrer Padilla, El Clot</a>
  <div>285.000 €</div><div>78 m²</div><div>3 hab.</div><div>3ª planta</div>

  <a href="https://www.idealista.com/inmueble/22222222/"><img src="foto2.jpg"></a>
  <a href="https://www.idealista.com/inmueble/22222222/">Ático en Navas</a>
  <div>320.000 €</div><div>90 m²</div><div>4 hab.</div><div>Ático</div>

  <a href="https://www.idealista.com/inmueble/33333333/">Piso en Sagrera</a>
  <div>210.000 €</div><div>65 m²</div><div>2 hab.</div><div>Bajo</div>
</body></html>
"""


def test_parse_email_multiple_listings():
    listings = parse_email("no-reply@idealista.com", "3 inmuebles nuevos", IDEALISTA_EMAIL)
    assert len(listings) == 3
    assert [l.external_id for l in listings] == ["11111111", "22222222", "33333333"]


def test_parse_email_fields_belong_to_right_listing():
    """The listing linked twice (thumbnail + title) must not steal the
    neighbour's price — this is the bug the block-splitting guards against."""
    listings = parse_email("no-reply@idealista.com", "alerta", IDEALISTA_EMAIL)
    by_id = {l.external_id: l for l in listings}

    assert by_id["11111111"].price == 285000
    assert by_id["11111111"].area_m2 == 78
    assert by_id["11111111"].rooms == 3

    assert by_id["22222222"].price == 320000
    assert by_id["22222222"].area_m2 == 90
    assert by_id["22222222"].rooms == 4

    assert by_id["33333333"].price == 210000
    assert by_id["33333333"].rooms == 2


def test_parse_email_marks_as_fresh():
    listings = parse_email("no-reply@idealista.com", "alerta", IDEALISTA_EMAIL)
    # An alert email IS the new-listing signal
    assert all(l.published_ago_hours == 0 for l in listings)


def test_parse_email_unknown_sender_returns_empty():
    assert parse_email("newsletter@zara.com", "Rebajas", "<p>50 €</p>") == []


def test_parse_email_empty_body():
    assert parse_email("no-reply@idealista.com", "vacío", "") == []


def test_parse_email_skips_blocks_without_price():
    body = """
    <a href="https://www.idealista.com/inmueble/44444444/">Consulta tu búsqueda</a>
    <p>Gestiona tus alertas</p>
    """
    assert parse_email("no-reply@idealista.com", "alerta", body) == []


# ── Rejection rules ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_text", [
    "Piso alquilado con inquilino",
    "Vivienda ocupada ilegalmente",
    "Inmueble con okupas",
    "Se vende en nuda propiedad",
    "Inmueble en subasta judicial",
    "Posesión no garantizada",
])
def test_parse_email_rejects_bad_listings(bad_text):
    body = f"""
    <a href="https://www.idealista.com/inmueble/55555555/">Piso</a>
    <div>200.000 €</div><div>70 m²</div><div>3 hab.</div>
    <div>{bad_text}</div>
    """
    assert parse_email("no-reply@idealista.com", "alerta", body) == []


def test_parse_email_keeps_clean_listing():
    body = """
    <a href="https://www.idealista.com/inmueble/66666666/">Piso reformado en El Clot</a>
    <div>250.000 €</div><div>75 m²</div><div>3 hab.</div>
    <div>Listo para entrar a vivir, libre de cargas</div>
    """
    listings = parse_email("no-reply@idealista.com", "alerta", body)
    assert len(listings) == 1
    assert listings[0].price == 250000


# ── Other portals ────────────────────────────────────────────────────────

def test_parse_habitaclia_email():
    body = """
    <a href="https://www.habitaclia.com/comprar-piso-el_clot-i9876543.htm">Piso Clot</a>
    <div>240.000 €</div><div>72 m²</div><div>3 hab.</div>
    """
    listings = parse_email("alertas@habitaclia.com", "alerta", body)
    assert len(listings) == 1
    assert listings[0].portal == "habitaclia"
    assert listings[0].external_id == "9876543"


def test_parse_fotocasa_email():
    body = """
    <a href="https://www.fotocasa.es/es/comprar/vivienda/barcelona/1234567/d">Piso</a>
    <div>265.000 €</div><div>80 m²</div><div>3 hab.</div>
    """
    listings = parse_email("noreply@fotocasa.es", "alerta", body)
    assert len(listings) == 1
    assert listings[0].portal == "fotocasa"


# ── Click-tracking wrappers ──────────────────────────────────────────────

def test_unwrap_tracking_exposes_wrapped_url():
    from ingest.email_parsers import unwrap_tracking
    wrapped = "https://links.idealista.com/r/?u=https%3A%2F%2Fwww.idealista.com%2Finmueble%2F999%2F"
    out = unwrap_tracking(wrapped)
    assert "https://www.idealista.com/inmueble/999/" in out
    # Original must be preserved so offsets into it stay valid
    assert out.startswith(wrapped)


def test_unwrap_tracking_handles_double_encoding():
    from ingest.email_parsers import unwrap_tracking
    wrapped = "https://t.co/x?u=https%253A%252F%252Fwww.idealista.com%252Finmueble%252F777%252F"
    assert "https://www.idealista.com/inmueble/777/" in unwrap_tracking(wrapped)


def test_unwrap_tracking_noop_when_nothing_encoded():
    from ingest.email_parsers import unwrap_tracking
    plain = "https://www.idealista.com/inmueble/123/"
    assert unwrap_tracking(plain) == plain


def test_parse_email_with_tracking_wrapped_links():
    """Portals route alert links through tracking domains — the listing URL
    only becomes visible after percent-decoding."""
    body = """
    <a href="https://links.idealista.com/r/?u=https%3A%2F%2Fwww.idealista.com%2Finmueble%2F88888888%2F">
      Piso en El Clot
    </a>
    <div>275.000 €</div><div>76 m²</div><div>3 hab.</div>
    """
    listings = parse_email("noresponder@idealista.com", "Nuevo inmueble", body)
    assert len(listings) == 1
    assert listings[0].external_id == "88888888"
    assert listings[0].price == 275000
    assert listings[0].area_m2 == 76


def test_parse_email_tracking_does_not_break_multi_listing():
    body = """
    <a href="https://links.idealista.com/r/?u=https%3A%2F%2Fwww.idealista.com%2Finmueble%2F1111%2F">A</a>
    <div>200.000 €</div><div>60 m²</div><div>2 hab.</div>
    <a href="https://links.idealista.com/r/?u=https%3A%2F%2Fwww.idealista.com%2Finmueble%2F2222%2F">B</a>
    <div>300.000 €</div><div>90 m²</div><div>4 hab.</div>
    """
    listings = parse_email("noresponder@idealista.com", "alerta", body)
    by_id = {l.external_id: l for l in listings}
    assert len(listings) == 2
    assert by_id["1111"].price == 200000
    assert by_id["2222"].price == 300000


# ── Title and location extraction ────────────────────────────────────────
# Shapes below are taken from real Habitaclia alert emails.

def test_extract_title_skips_url_and_spec_noise():
    from ingest.email_parsers import extract_title
    block = (
        'https://www.habitaclia.com/i123/x.htm" target="_blank" style="none"> '
        '| | | 220.000 € | | Piso en Barcelona - El Poble Sec - Pau | 52m 2 | 2 hab.'
    )
    assert extract_title(block) == "Piso en Barcelona - El Poble Sec - Pau"


def test_extract_title_falls_back_to_subject():
    from ingest.email_parsers import extract_title
    assert extract_title("| | 100 € |", "Asunto de la alerta") == "Asunto de la alerta"


def test_extract_location_city_and_district():
    from ingest.email_parsers import extract_location
    assert extract_location("Piso en Barcelona - El Poble Sec - Paral·lel") == (
        "barcelona", "el poble sec",
    )
    assert extract_location("Ático en Sant Adrià de Besòs - Port Forum") == (
        "sant adrià de besòs", "port forum",
    )


def test_extract_location_city_only():
    from ingest.email_parsers import extract_location
    assert extract_location("Casa en Sabadell") == ("sabadell", None)


def test_extract_location_drops_truncated_district():
    """Habitaclia truncates long titles; a '...' fragment is not a district."""
    from ingest.email_parsers import extract_location
    city, district = extract_location("Piso en Barcelona - Pa...")
    assert city == "barcelona"
    assert district is None


def test_extract_location_unparseable():
    from ingest.email_parsers import extract_location
    assert extract_location("Oportunidad Anuncio nuevo") == (None, None)


# ── Habitaclia alert-redirect URLs ───────────────────────────────────────

def test_parse_habitaclia_alert_redirect_url():
    """Alert emails link through /i{id}/... redirects, not canonical URLs."""
    body = (
        '<a href="https://www.habitaclia.com/i36598004379142/28112891/'
        'express28112891/alertas/email/lo-34/20260813-e_nuevo_img.htm">x</a>'
        "<div>220.000 €</div><div>52m 2</div><div>2 hab.</div>"
        "<div>Piso en Barcelona - El Poble Sec - Pau</div>"
    )
    listings = parse_email("alertas@email.habitaclia.com", "novedades", body)
    assert len(listings) == 1
    got = listings[0]
    assert got.external_id == "36598004379142"
    assert got.price == 220000
    assert got.area_m2 == 52
    assert got.city == "barcelona"


def test_parse_area_accepts_spaced_superscript():
    """Habitaclia renders m² as 'm 2' once the markup is flattened."""
    assert parse_area("52m 2") == 52
    assert parse_area("109 m 2") == 109


def test_price_range_filter_ignores_mortgage_quota():
    """Alert emails advertise 'CUOTA DE HIPOTECA DESDE 719,80€' next to the
    price — a quota must never be mistaken for a sale price."""
    assert parse_price("719,80€") is None
    assert parse_price("1.050€") is None      # monthly rent


# ── Property signature (cross-agency dedup) ──────────────────────────────

def test_property_signature_stable_across_listing_ids():
    """The same flat marketed by two agencies has two listing ids but must
    produce one signature, so the user gets one alert."""
    from alerts.telegram_bot import TelegramAlerter
    a = {"price": 199000, "area_m2": 91, "rooms": 3, "city": "gavà"}
    b = {"price": 199000.0, "area_m2": 91.0, "rooms": 3, "city": "Gavà "}
    assert TelegramAlerter.property_signature(a) == TelegramAlerter.property_signature(b)


def test_property_signature_differs_for_different_flats():
    from alerts.telegram_bot import TelegramAlerter
    a = {"price": 199000, "area_m2": 91, "rooms": 3, "city": "gavà"}
    b = {"price": 199000, "area_m2": 91, "rooms": 4, "city": "gavà"}
    assert TelegramAlerter.property_signature(a) != TelegramAlerter.property_signature(b)


@pytest.mark.parametrize("incomplete", [
    {"price": None, "area_m2": 91, "rooms": 3, "city": "gavà"},
    {"price": 199000, "area_m2": None, "rooms": 3, "city": "gavà"},
    {"price": 199000, "area_m2": 91, "rooms": None, "city": "gavà"},
    {"price": 199000, "area_m2": 91, "rooms": 3, "city": "desconocido"},
    {"price": 199000, "area_m2": 91, "rooms": 3, "city": ""},
])
def test_property_signature_none_when_incomplete(incomplete):
    """Never dedup on partial data — two unrelated flats would collide."""
    from alerts.telegram_bot import TelegramAlerter
    assert TelegramAlerter.property_signature(incomplete) is None
