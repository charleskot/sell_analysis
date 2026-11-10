"""Pisos.com read directly: its cards, its places and its photos."""
from types import SimpleNamespace

from scraper.pisos import place_from_subtitle, scrape


def test_a_barcelona_barri_is_named_as_the_rest_of_the_bot_names_it():
    assert place_from_subtitle("El Congrés-Els Indians (Distrito Sant Andreu. Barcelona Capital)") \
        == ("el congrés-els indians", "barcelona")
    assert place_from_subtitle("Sant Josep (L'Hospitalet de Llobregat)") \
        == ("sant josep", "l'hospitalet de llobregat")
    assert place_from_subtitle("Esplugues de Llobregat") == (None, "esplugues de llobregat")
    assert place_from_subtitle("Barcelona Capital") == (None, "barcelona")


CARD = '''
<div class="ad-preview" id="6815013852.109700" data-lnk-href="/comprar/piso-x-6815013852_109700/">
  <a class="ad-preview__title" href="/comprar/piso-x-6815013852_109700/">Piso en Carrer de Mallorca</a>
  <span class="ad-preview__price">295.000 €</span>
  <p class="ad-preview__char">3 habs.</p><p class="ad-preview__char">74 m²</p>
  <p class="ad-preview__char">1ª planta</p>
  <p class="ad-preview__subtitle">La Sagrada Família (Distrito Eixample. Barcelona Capital)</p>
  <p class="ad-preview__description">Exterior y luminoso, con ascensor.</p>
</div>'''


def test_a_card_becomes_a_listing(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(status_code=200, text=CARD))
    flat, = scrape("https://www.pisos.com/venta/pisos-barcelona_capital/", max_pages=1)
    assert flat["portal"] == "pisos" and flat["external_id"] == "6815013852_109700"
    assert flat["url"] == "https://www.pisos.com/comprar/piso-x-6815013852_109700/"
    assert (flat["price"], flat["area_m2"], flat["rooms"], flat["floor"]) == (295000.0, 74.0, 3, "1ª planta")
    assert (flat["district"], flat["city"]) == ("la sagrada família", "barcelona")
    assert flat["description"] == "Exterior y luminoso, con ascensor."


def test_a_page_of_known_flats_ends_the_read(monkeypatch):
    import requests

    pages = []
    page = CARD * 25

    def fake_get(url, **k):
        pages.append(url)
        return SimpleNamespace(status_code=200, text=page)

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("time.sleep", lambda s: None)
    scrape("https://www.pisos.com/venta/pisos-badalona/", max_pages=3, known=lambda ids: True)
    assert pages == ["https://www.pisos.com/venta/pisos-badalona/"]
    scrape("https://www.pisos.com/venta/pisos-badalona/", max_pages=3, known=lambda ids: False)
    assert pages[-1] == "https://www.pisos.com/venta/pisos-badalona/3/"


def test_the_judge_sees_the_large_photos(monkeypatch):
    from analysis import taste_judge

    html = ('<img src="https://fotos.imghs.net/apps-wp/1097/680/77f003f5-0581-4787-a80d-89b036c048c7_2026.jpg">'
            '<img src="https://fotos.imghs.net/fch-wp/1097/680/77f003f5-0581-4787-a80d-89b036c048c7_2026.jpg">'
            '<img src="https://fotos.imghs.net/prof-wp/logos/Logo_992912_94778.jpg">')
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(text=html))
    assert taste_judge.photo_urls({"portal": "pisos", "url": "https://www.pisos.com/comprar/x/"}) == [
        "https://fotos.imghs.net/xl-wp/1097/680/77f003f5-0581-4787-a80d-89b036c048c7_2026.jpg"]


def test_the_daily_control_names_pisos_com_only_when_it_brought_something():
    from scheduler.jobs import PipelineOrchestrator

    report = {"day": "2026-11-10", "escaped": [], "portals": {
        "pisos": {"nuevos": 40, "cumplen": 3, "enviados": 2, "repetidos": 1,
                  "descartados": 0, "escapados": 0}}}
    assert "Pisos.com: 40 nuevos · 3 cumplían → 2 enviados, 1 repetidos" in \
        PipelineOrchestrator.daily_control_text(report)
    report["portals"] = {}
    assert "Pisos.com" not in PipelineOrchestrator.daily_control_text(report)
