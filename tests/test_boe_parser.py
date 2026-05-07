from __future__ import annotations

from pathlib import Path

from app.scrapers.boe import BoeScraper, _parse_money, _parse_date


FIXTURE = Path(__file__).parent / "fixtures" / "boe_detail_sample.html"


def test_parse_money():
    assert _parse_money("120.000,00 €") == 120000.0
    assert _parse_money("1.234,56 €") == 1234.56
    assert _parse_money("") is None
    assert _parse_money(None) is None


def test_parse_date():
    d = _parse_date("15-06-2026 09:00:00 CET")
    assert d is not None
    assert d.year == 2026 and d.month == 6 and d.day == 15
    assert _parse_date("garbage") is None


def test_parse_detail_html():
    html = FIXTURE.read_text(encoding="utf-8")
    url = "https://subastas.boe.es/detalleSubasta.php?idSub=SUB-JA-2024-12345"
    item = BoeScraper.parse_detail_html(html, url)

    assert item is not None
    assert item.source == "boe"
    assert item.external_id == "SUB-JA-2024-12345"
    assert item.url == url
    assert item.address and "Calle Mayor 12" in item.address
    assert item.city == "Barcelona"
    assert item.province == "Barcelona"
    assert item.postal_code == "08001"
    assert item.property_type == "Vivienda"
    assert item.surface_m2 == 85.0
    assert item.starting_price == 120000.0
    assert item.appraised_value == 250000.0
    assert item.minimum_deposit == 6000.0
    assert item.auction_start is not None and item.auction_start.year == 2026
    assert item.auction_end is not None and item.auction_end.month == 7
    assert item.has_charges is True
    assert "Hipoteca" in (item.charges_description or "")
    assert "Ocupado" in (item.occupancy_status or "")
