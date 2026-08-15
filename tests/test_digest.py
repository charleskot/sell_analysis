"""The daily digest has to carry the analysis, not just a ranking.

It used to query the database directly on the old 0-100 score, skipping the
profiles entirely: a 58 m² one-bedroom in a town no home search covers was
sent as a "Top 5 vivienda", with nothing but a star rating to justify it.
"""
from types import SimpleNamespace

import pytest

from alerts.telegram_bot import TelegramAlerter


@pytest.fixture
def alerter():
    return TelegramAlerter({})


def entry(purpose="investment", **over):
    listing = {"city": "mataro", "district": "cerdanyola", "price": 186000,
               "area_m2": 108, "rooms": 4, "condition": "buen_estado",
               "url": "https://www.fotocasa.es/es/comprar/vivienda/mataro/1"}
    metrics = {"price_per_m2": 1722, "cash_needed": 38000, "cash_gap": 0,
               "monthly_payment": 720, "monthly_payment_total": 720,
               "estimated_monthly_rent": 1050, "monthly_cashflow": 180,
               "net_yield_pct": 6.4, "gross_yield_pct": 7.1,
               "matched_profiles": "💰 Inversión", "matched_purpose": purpose}
    listing.update(over.pop("listing", {}))
    metrics.update(over.pop("metrics", {}))
    return {"listing": listing, "metrics": metrics, "reason": "encaja",
            "profile": SimpleNamespace(purpose=purpose, label="💰 Inversión"),
            "is_new": True, **over}


def test_empty_digest_says_so_instead_of_going_silent(alerter):
    """Silence reads as a broken bot, which is exactly the complaint."""
    text = alerter._format_digest([])
    assert "Nada encaja" in text


def test_investment_entry_carries_the_numbers_that_decide_it(alerter):
    text = alerter._format_digest([entry()])
    for expected in ("6.4%", "7.1%", "1.050€/mes", "+180€/mes", "38.000€", "720€/mes"):
        assert expected in text, expected


def test_investment_entry_describes_the_zone(alerter):
    text = alerter._format_digest([entry(metrics={"rent_capped_zone": True})])
    assert "zona tensionada" in text
    assert "129.000 hab" in text  # Mataró, from the municipality table


def test_home_entry_hides_rental_maths(alerter):
    """Cashflow and rent caps misread the purchase of a home to live in."""
    text = alerter._format_digest([entry(purpose="home",
                                         metrics={"rent_capped_zone": True})])
    assert "Cashflow" not in text
    assert "Alquiler" not in text
    assert "tensionada" not in text
    assert "hab" in text  # rooms still shown
    assert "38.000€" in text  # but the entry and the payment still are


def test_says_which_search_the_listing_answers(alerter):
    text = alerter._format_digest([entry()])
    assert "💰 Inversión" in text


def test_money_uses_spanish_separators(alerter):
    """186.000€, not the 186,000€ that was going out."""
    assert alerter._money(186000) == "186.000"
    assert "186.000€" in alerter._format_digest([entry()])


def test_unverified_condition_is_stated_not_hidden(alerter):
    text = alerter._format_digest([entry(metrics={"condition_unknown": True})])
    assert "sin confirmar" in text


def test_reform_cost_is_declared(alerter):
    text = alerter._format_digest([entry(metrics={"reform_cost": 54000})])
    assert "54.000€" in text


def test_new_listings_are_marked_and_counted(alerter):
    text = alerter._format_digest([entry(), entry(is_new=False)])
    assert "🆕" in text
    assert "2 encajan · 1 nuevas en 24h" in text


def test_long_lists_are_capped_and_say_so(alerter):
    text = alerter._format_digest([entry() for _ in range(8)])
    assert "y 3 más" in text
