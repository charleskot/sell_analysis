"""Servihabitat read directly: its cards, its possession field, its links."""
from types import SimpleNamespace

from bs4 import BeautifulSoup

from ingest.email_parsers import town_from_url
from scraper.servihabitat import advert_text, card_to_listing


def card(possession: str, extra: str = "[lift]", visible: str = "") -> BeautifulSoup:
    html = f'''<div class="product-item" data-id="60455489" data-category="catalog/sale/piso/barcelona/barcelones/badalona">
      <a href="/es/venta/vivienda-piso/barcelona-barcelones-badalona/60455489"><img src="https://img/1.jpg"></a>
      <span>productPrice: 115400.00 productM2: 72 productRoomNum: 3 productBathNum: 1
      productExtra: {extra} productWithoutPosesion: {possession} productName: x</span>
      <p>115.400 € Piso en venta en C. Calle Atlantida, 71-73, Badalona, Barcelona 72m 2 3 hab. {visible}</p>
    </div>'''
    return BeautifulSoup(html, "lxml").select_one("div.product-item")


def test_a_free_flat_reads_as_one():
    flat = card_to_listing(card("productEquipmentArray"), "badalona")
    assert (flat["external_id"], flat["price"], flat["area_m2"], flat["rooms"]) == ("60455489", 115400.0, 72.0, 3)
    assert flat["city"] == "badalona"
    assert flat["title"] == "Piso en venta en C. Calle Atlantida, 71-73, Badalona, Barcelona"
    assert flat["description"] == "con ascensor"


def test_an_occupied_or_tenanted_flat_says_so_in_words_the_bot_refuses():
    from ingest.email_parsers import _REJECT_RE

    occupied = card_to_listing(card("sinposesion"), "badalona")
    rented = card_to_listing(card("productEquipmentArray", visible="En rentabilidad"), "badalona")
    assert _REJECT_RE.search(occupied["description"])
    assert _REJECT_RE.search(rented["description"])


def test_the_hidden_form_heading_does_not_condemn_a_free_flat(monkeypatch):
    page = ('<p class="callme-h2">Vivienda sin posesión</p><span>productWithoutPosesion: </span>'
            '<p class="description">Superficie m 2 : +74%</p>'
            '<p class="description">Vivienda en Badalona, en una zona consolidada y bien comunicada, '
            'ideal para residencia habitual, con ascensor y balcón.</p>')
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(status_code=200, text=page))
    text = advert_text("https://www.servihabitat.com/x/1")
    assert "sin posesión" not in text and text.startswith("Vivienda en Badalona")
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(
        status_code=200, text=page.replace("productWithoutPosesion: ", "productWithoutPosesion: sinposesion")))
    assert advert_text("https://www.servihabitat.com/x/1").startswith("Inmueble sin posesión")


def test_links_name_barcelona_by_district_and_towns_with_a_trailing_article():
    base = "https://www.servihabitat.com/es/venta/vivienda-piso/barcelona-barcelones-{}/60000000"
    assert town_from_url("servihabitat", base.format("bcnnoubarris")) == "barcelona"
    assert town_from_url("servihabitat", base.format("hospitaletdellobregatl")) == "hospitalet de llobregat"
