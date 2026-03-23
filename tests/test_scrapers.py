"""Tests for scraper parsers using fixture HTML."""
import pytest
from unittest.mock import MagicMock, patch
from scraper.base import BaseScraper, RawListing
from scraper.pisos import PisosScraper
from scraper.http_client import HttpClient


def make_http_client():
    config = {"delay_min_seconds": 0, "delay_max_seconds": 0, "max_retries": 1, "proxy_list": []}
    return HttpClient(config)


def test_parse_price_basic():
    http = make_http_client()
    scraper = PisosScraper(http, {}, respect_robots=False)
    assert scraper.parse_price("180.000 €") == 180_000.0
    assert scraper.parse_price("1.250.000€") == 1_250_000.0
    assert scraper.parse_price("85.000 €") == 85_000.0


def test_parse_area():
    http = make_http_client()
    scraper = PisosScraper(http, {}, respect_robots=False)
    assert scraper.parse_area("85 m²") == 85.0
    assert scraper.parse_area("120m2") == 120.0
    assert scraper.parse_area("65,5 m²") == 65.5


def test_parse_rooms():
    http = make_http_client()
    scraper = PisosScraper(http, {}, respect_robots=False)
    assert scraper.parse_rooms("3 habitaciones") == 3
    assert scraper.parse_rooms("2 hab") == 2
    assert scraper.parse_rooms("sin datos") is None


def test_normalize_condition():
    http = make_http_client()
    scraper = PisosScraper(http, {}, respect_robots=False)
    assert scraper.normalize_condition("Obra nueva") == "nuevo"
    assert scraper.normalize_condition("Para reformar") == "reformar"
    assert scraper.normalize_condition("Buen estado") == "buen_estado"
    assert scraper.normalize_condition("cualquier cosa") == "desconocido"


def test_raw_listing_to_dict():
    listing = RawListing(
        portal="pisos",
        external_id="12345",
        url="https://pisos.com/12345",
        price=150_000,
        area_m2=70,
        rooms=3,
        city="madrid",
    )
    d = listing.to_dict()
    assert d["portal"] == "pisos"
    assert d["price"] == 150_000
    assert d["city"] == "madrid"
