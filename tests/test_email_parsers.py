"""Tests for portal alert email parsing."""
import pytest

from ingest.email_parsers import (
    detect_portal,
    parse_area,
    parse_email,
    parse_price,
    parse_rooms,
    strip_html,
    clean_url,
)


# ── Field extraction ─────────────────────────────────────────────────────

def test_parse_price_formats():
    assert parse_price("285.000 €") == 285000
    assert parse_price("285.000€") == 285000
    assert parse_price("Precio: 199.500 €") == 199500
    assert parse_price("1.250.000 €") == 1250000


def test_parse_price_rejects_out_of_range():
    assert parse_price("950 €") is None          # monthly rent, not a price
    assert parse_price("5.000.000 €") is None    # above sane ceiling
    assert parse_price("sin precio") is None


def test_parse_area():
    assert parse_area("78 m²") == 78
    assert parse_area("78m2") == 78
    assert parse_area("Superficie 105 m²") == 105
    assert parse_area("2 m²") is None            # below sane floor


def test_parse_rooms():
    assert parse_rooms("3 hab.") == 3
    assert parse_rooms("3 habitaciones") == 3
    assert parse_rooms("4 dormitorios") == 4
    assert parse_rooms("sin datos") is None


def test_strip_html_separates_blocks():
    html = "<div>285.000 €</div><div>78 m²</div>"
    text = strip_html(html)
    assert "285.000" in text and "78" in text
    assert "<div>" not in text


def test_clean_url_strips_tracking():
    url = "https://www.idealista.com/inmueble/12345678/?utm_source=alerta&xtor=AL-123"
    assert clean_url(url) == "https://www.idealista.com/inmueble/12345678/"


# ── Portal detection ─────────────────────────────────────────────────────

def test_detect_portal_by_sender():
    assert detect_portal("no-reply@idealista.com", "")["portal"] == "idealista"
    assert detect_portal("alertas@fotocasa.es", "")["portal"] == "fotocasa"
    assert detect_portal("info@habitaclia.com", "")["portal"] == "habitaclia"


def test_detect_portal_by_body_when_forwarded():
    body = 'Mira: <a href="https://www.idealista.com/inmueble/98765432/">piso</a>'
    assert detect_portal("charles@gmail.com", body)["portal"] == "idealista"


def test_detect_portal_unknown():
    assert detect_portal("newsletter@random.com", "hola") is None


# ── Full email parsing ───────────────────────────────────────────────────

IDEALISTA_EMAIL = """
<html><body>
  <h1>Nuevos inmuebles para tu búsqueda</h1>
  <div>
    <a href="https://www.idealista.com/inmueble/11111111/">Piso en Carrer Padilla, El Clot</a>
    <p>285.000 €</p><p>78 m²</p><p>3 hab.</p><p>2 baños</p><p>3ª planta</p>
  </div>
  <div>
    <a href="https://www.idealista.com/inmueble/22222222/">Piso en Navas</a>
    <p>240.000 €</p><p>65 m²</p><p>2 hab.</p><p>1 baño</p>
  </div>
</body></html>
"""


def test_parse_email_extracts_multiple_listings():
    listings = parse_email("no-reply@idealista.com", "2 nuevos inmuebles", IDEALISTA_EMAIL)
    assert len(listings) == 2

    first = listings[0]
    assert first.portal == "idealista"
    assert first.external_id == "11111111"
    assert first.price == 285000
    assert first.area_m2 == 78
    assert first.rooms == 3
    assert first.published_ago_hours == 0   # an alert email IS the new signal

    assert listings[1].external_id == "22222222"
    assert listings[1].price == 240000


def test_parse_email_rejects_occupied():
    html = """
    <a href="https://www.idealista.com/inmueble/33333333/">Piso</a>
    <p>150.000 €</p><p>70 m²</p><p>3 hab.</p>
    <p>Inmueble ocupado ilegalmente. Posesión no garantizada.</p>
    """
    assert parse_email("no-reply@idealista.com", "alerta", html) == []


def test_parse_email_rejects_tenanted():
    html = """
    <a href="https://www.idealista.com/inmueble/44444444/">Piso</a>
    <p>180.000 €</p><p>70 m²</p><p>3 hab.</p>
    <p>Se vende alquilado con inquilino, rentabilidad garantizada.</p>
    """
    assert parse_email("no-reply@idealista.com", "alerta", html) == []


def test_parse_email_rejects_auction_and_bare_ownership():
    for bad in ["Venta en subasta judicial", "Se vende la nuda propiedad"]:
        html = f"""
        <a href="https://www.idealista.com/inmueble/55555555/">Piso</a>
        <p>180.000 €</p><p>70 m²</p><p>3 hab.</p><p>{bad}</p>
        """
        assert parse_email("no-reply@idealista.com", "alerta", html) == [], bad


def test_parse_email_skips_blocks_without_price():
    html = """
    <a href="https://www.idealista.com/inmueble/66666666/">Ver todos los resultados</a>
    <p>Gestiona tus alertas</p>
    """
    assert parse_email("no-reply@idealista.com", "alerta", html) == []


def test_parse_email_unknown_sender_returns_empty():
    assert parse_email("spam@example.com", "oferta", "<p>300.000 €</p>") == []


def test_parse_email_empty_body():
    assert parse_email("no-reply@idealista.com", "vacío", "") == []


def test_parse_email_dedupes_repeated_urls():
    """Alert emails link the same listing from both image and title."""
    html = """
    <a href="https://www.idealista.com/inmueble/77777777/"><img src="x.jpg"></a>
    <a href="https://www.idealista.com/inmueble/77777777/">Piso en El Clot</a>
    <p>290.000 €</p><p>80 m²</p><p>3 hab.</p>
    """
    listings = parse_email("no-reply@idealista.com", "alerta", html)
    assert len(listings) == 1
    assert listings[0].external_id == "77777777"
