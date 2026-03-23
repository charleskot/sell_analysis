"""Unit tests for financial metrics calculations."""
import pytest
from analysis.metrics import (
    price_per_m2,
    gross_rental_yield,
    net_rental_yield,
    roi,
    payback_period_years,
    total_investment,
    compute_all_metrics,
)


def test_price_per_m2_basic():
    assert price_per_m2(200_000, 100) == 2000.0


def test_price_per_m2_none_price():
    assert price_per_m2(None, 100) is None


def test_price_per_m2_zero_area():
    assert price_per_m2(100_000, 0) is None


def test_total_investment():
    # 10% costs
    assert total_investment(200_000, 0.10) == 220_000.0


def test_gross_yield():
    # 180k price, 10% costs -> 198k total investment
    # 900€/month = 10800/year
    # yield = 10800/198000 = 5.45%
    result = gross_rental_yield(10_800, 180_000, 0.10)
    assert result == pytest.approx(5.45, rel=0.01)


def test_gross_yield_none_inputs():
    assert gross_rental_yield(None, 180_000, 0.10) is None
    assert gross_rental_yield(10_800, 0, 0.10) is None


def test_net_yield():
    # gross rent 10800, 25% expenses -> 8100 net
    # total inv 198000
    # net yield = 8100/198000 = 4.09%
    result = net_rental_yield(10_800, 180_000, 0.10, 0.25)
    assert result == pytest.approx(4.09, rel=0.01)


def test_payback_period():
    # 220k inv, 8100/year net income -> 27.16 years
    result = payback_period_years(220_000, 8_100)
    assert result == pytest.approx(27.2, rel=0.01)


def test_payback_zero_income():
    assert payback_period_years(200_000, 0) is None


def test_roi():
    result = roi(8_100, 220_000)
    assert result == pytest.approx(3.68, rel=0.01)


def test_compute_all_metrics_full():
    metrics = compute_all_metrics(
        price=180_000,
        area_m2=80,
        estimated_monthly_rent=900,
        purchase_costs_pct=0.10,
        expense_ratio=0.25,
    )
    assert "gross_yield_pct" in metrics
    assert "net_yield_pct" in metrics
    assert "payback_years" in metrics
    assert "investment_score" not in metrics  # score added by scorer, not metrics
    assert metrics["gross_yield_pct"] > 0
    assert metrics["net_yield_pct"] < metrics["gross_yield_pct"]
    assert metrics["price_per_m2"] == 2250.0
