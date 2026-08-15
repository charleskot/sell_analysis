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


# ── Fotocasa location format ─────────────────────────────────────────────

def test_extract_location_fotocasa_comma_format():
    """Fotocasa writes '<type> · <street>, <City>' instead of '<type> en <City>'."""
    from ingest.email_parsers import extract_location
    assert extract_location("apartamento · D'Aribau, Barcelona") == ("barcelona", "d'aribau")
    assert extract_location("apartamento , Badalona") == ("badalona", None)


def test_extract_location_prefers_en_format_when_both_present():
    from ingest.email_parsers import extract_location
    city, _ = extract_location("Piso en Terrassa - Ca n'Aurell, algo")
    assert city == "terrassa"


def test_extract_location_keeps_partial_but_meaningful_district():
    """A long truncated fragment still identifies the zone; a 2-letter one doesn't."""
    from ingest.email_parsers import extract_location
    assert extract_location("Apartamento en Badalona - Llefià (Art...")[1] == "llefià (art"
    assert extract_location("Piso en Barcelona - Pa...")[1] is None


def test_card_block_spans_whole_card_regardless_of_link_order():
    """Habitaclia puts the data after the title link, Fotocasa puts the
    'Ver anuncio' link after the price. Anchoring on the first occurrence of
    each listing covers both."""
    body = (
        '<a href="https://www.fotocasa.es/es/comprar/vivienda/bcn/111111/d">img</a>'
        "<div>200.000 €</div><div>60 m²</div><div>2 hab.</div>"
        '<a href="https://www.fotocasa.es/es/comprar/vivienda/bcn/111111/d">Ver anuncio</a>'
        '<a href="https://www.fotocasa.es/es/comprar/vivienda/bcn/222222/d">img</a>'
        "<div>300.000 €</div><div>90 m²</div><div>4 hab.</div>"
        '<a href="https://www.fotocasa.es/es/comprar/vivienda/bcn/222222/d">Ver anuncio</a>'
    )
    listings = parse_email("enviosfotocasa@fotocasa.es", "3 anuncios", body)
    by_id = {l.external_id: l for l in listings}
    assert len(listings) == 2
    assert by_id["111111"].price == 200000
    assert by_id["222222"].price == 300000


# ── Idealista location format ────────────────────────────────────────────
# Idealista and Fotocasa order narrow-to-broad (street, district, town);
# Habitaclia orders broad-to-narrow (town - district - area).

def test_extract_location_idealista_narrow_to_broad():
    from ingest.email_parsers import extract_location
    assert extract_location(
        "Casa o chalet independiente en Paseo Mirador, Bellamar, Castelldefels"
    ) == ("castelldefels", "bellamar")


def test_extract_location_skips_house_number_as_district():
    from ingest.email_parsers import extract_location
    city, district = extract_location(
        "Piso en Rambla de la Marina, 260, Bellvitge, Hospitalet de Llobregat"
    )
    assert city == "hospitalet de llobregat"
    assert district == "bellvitge"


def test_extract_location_allows_words_between_type_and_place():
    """'Casa o chalet independiente en X' — qualifiers sit between the
    property type and the location."""
    from ingest.email_parsers import extract_location
    assert extract_location("Casa o chalet adosado en Sitges")[0] == "sitges"


def test_extract_title_prefers_the_property_line():
    """Without this the title became 'Ver 46 fotos y visita 360', which also
    carries no location to parse."""
    from ingest.email_parsers import extract_title
    block = "Ver 46 fotos y visita 360 | Piso en Carrer Padilla, El Clot, Barcelona | 285.000 €"
    assert extract_title(block).startswith("Piso en Carrer Padilla")


def test_extract_title_falls_back_when_no_property_line():
    from ingest.email_parsers import extract_title
    assert extract_title("Ver 46 fotos y visita 360 | 285.000 €") == "Ver 46 fotos y visita 360"


# ── Not mortgageable ─────────────────────────────────────────────────────
# The buyer needs financing, so these are not opportunities at any price:
# a bank will not lend on them.

@pytest.mark.parametrize("phrase", [
    "Solo inversores",
    "Sólo inversores, no se puede visitar",
    "Apto inversores",
    "Ideal inversor",
    "Producto de inversión",
    "No visitable",
    "Sin posibilidad de visita",
    "Posesión no garantizada",
    "Inmueble con cargas",
    "Pendiente de lanzamiento",
    "Se vende en nuda propiedad",
    "Usufructo vitalicio",
    "Multipropiedad",
    "Suelo urbanizable",
])
def test_rejects_listings_no_bank_will_finance(phrase):
    body = f"""
    <a href="https://www.idealista.com/inmueble/77777777/">Piso</a>
    <div>120.000 €</div><div>70 m²</div><div>3 hab.</div>
    <div>{phrase}</div>
    """
    assert parse_email("no-reply@idealista.com", "alerta", body) == []


def test_keeps_ordinary_listing_mentioning_investment_generically():
    """'Inversión' alone is marketing filler and must not disqualify a flat
    that is otherwise normal — only the phrases meaning 'cash buyers only'."""
    body = """
    <a href="https://www.idealista.com/inmueble/88888888/">Piso</a>
    <div>150.000 €</div><div>70 m²</div><div>3 hab.</div>
    <div>Buena inversión de futuro, listo para entrar a vivir</div>
    """
    assert len(parse_email("no-reply@idealista.com", "alerta", body)) == 1


# ── Truncated alert emails ───────────────────────────────────────────────

def test_warns_when_the_email_is_a_sample_not_a_feed(caplog):
    """Portals cap an alert at ~30 listings and state the real count in the
    subject. A wide search then delivers 4% of what exists, and the gap is
    invisible — it looks exactly like a quiet day."""
    import logging
    body = "".join(
        f'<a href="https://www.habitaclia.com/i1000000000000{i}/x/y.htm">P</a>'
        f"<div>200.000 €</div><div>70 m²</div><div>3 hab.</div>"
        for i in range(5)
    )
    with caplog.at_level(logging.WARNING):
        listings = parse_email(
            "alertas@email.habitaclia.com",
            "689 novedades en comarca Barcelonès en comprar vivienda",
            body,
        )
    assert len(listings) == 5
    assert any("demasiado amplia" in r.message for r in caplog.records)


def test_no_warning_when_the_email_carries_what_it_claims(caplog):
    import logging
    body = "".join(
        f'<a href="https://www.habitaclia.com/i2000000000000{i}/x/y.htm">P</a>'
        f"<div>200.000 €</div><div>70 m²</div><div>3 hab.</div>"
        for i in range(5)
    )
    with caplog.at_level(logging.WARNING):
        parse_email("alertas@email.habitaclia.com", "5 novedades en comarca X", body)
    assert not any("demasiado amplia" in r.message for r in caplog.records)


# ── Bank portals ───────────────────────────────────────────────────────────
#
# Added without ever having seen one of their emails. Their patterns ended at
# the id, the same mistake that made Habitaclia links 404: the match is what
# gets stored and opened, so anything after the id was thrown away.

import pytest

from ingest.email_parsers import PORTAL_SPECS, parse_email


def spec_for(portal):
    return next(s for s in PORTAL_SPECS if s["portal"] == portal)


@pytest.mark.parametrize("portal,url,ext_id", [
    ("servihabitat",
     "https://www.servihabitat.com/es/inmueble/12345678/piso-en-mataro-barcelona",
     "12345678"),
    ("solvia",
     "https://www.solvia.es/es/inmuebles/vivienda/9876543/piso-sabadell?utm=alerta",
     "9876543"),
    ("altamira",
     "https://www.altamirainmuebles.com/venta/vivienda/1234567/piso-en-reus",
     "1234567"),
    ("aliseda",
     "https://www.aliseda.es/inmueble/7654321/piso-en-lleida",
     "7654321"),
])
def test_bank_url_is_captured_whole(portal, url, ext_id):
    """A truncated link is a link that 404s when he taps it."""
    m = spec_for(portal)["url_re"].search(f'<a href="{url}">Ver</a>')
    assert m is not None
    assert m.group(0) == url
    assert ext_id in m.groups()


def test_bank_alert_with_prices_but_no_links_is_reported():
    """Otherwise it looks exactly like a portal with nothing to send."""
    problems = []
    body = '<html><p>Piso en Mataró</p><p>186.000 €</p><a href="https://otro.com/x">Ver</a></html>'
    listings = parse_email("alertas@servihabitat.com", "Novedades", body, problems=problems)
    assert listings == []
    assert problems == [("servihabitat", "Novedades")]


def test_transactional_mail_is_not_reported_as_a_failure():
    """Warning on every account email trains the reader to ignore warnings."""
    problems = []
    body = "<html><p>Bienvenido a Servihabitat, confirma tu cuenta.</p></html>"
    parse_email("no-reply@servihabitat.com", "Bienvenido", body, problems=problems)
    assert problems == []


def test_warning_names_the_portals_and_asks_for_the_email():
    from alerts.telegram_bot import TelegramAlerter

    text = TelegramAlerter({})._unreadable_text(
        [("servihabitat", "12 novedades"), ("solvia", "Tus alertas")]
    )
    assert "servihabitat, solvia" in text
    assert "12 novedades" in text
    assert "Reenvíame ese email" in text


def test_nothing_to_warn_about_sends_nothing():
    from alerts.telegram_bot import TelegramAlerter

    assert TelegramAlerter({}).send_unreadable_warning([]) is False
