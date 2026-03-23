"""Unit tests for investment scorer."""
import pytest
from analysis.scorer import InvestmentScorer, _interpolate, YIELD_SCALE, DISCOUNT_SCALE


MOCK_CONFIG = {
    "analysis": {
        "scoring_weights": {
            "gross_yield": 0.35,
            "price_per_m2": 0.25,
            "location_score": 0.20,
            "condition_score": 0.10,
            "roi_net": 0.10,
        }
    },
    "location_scores": {
        "madrid": {
            "salamanca": 90,
            "carabanchel": 58,
        }
    },
}


def test_interpolate_below_range():
    assert _interpolate(-1, YIELD_SCALE) == 0.0


def test_interpolate_above_range():
    assert _interpolate(10, YIELD_SCALE) == 100.0


def test_interpolate_midpoint():
    # Between (5, 50) and (6, 65): at 5.5 -> 57.5
    result = _interpolate(5.5, YIELD_SCALE)
    assert result == pytest.approx(57.5, rel=0.01)


def test_scorer_high_yield():
    scorer = InvestmentScorer(MOCK_CONFIG)
    bd = scorer.score(
        city="madrid",
        district="carabanchel",
        condition="buen_estado",
        gross_yield_pct=8.0,
        net_yield_pct=6.0,
        listing_price_per_m2=2000,
        zone_avg_price_per_m2=2500,  # 20% below average -> good
    )
    assert bd.total > 60
    assert bd.gross_yield_score >= 90


def test_scorer_low_yield():
    scorer = InvestmentScorer(MOCK_CONFIG)
    bd = scorer.score(
        city="madrid",
        district="salamanca",
        condition="nuevo",
        gross_yield_pct=2.0,
        net_yield_pct=1.5,
        listing_price_per_m2=6000,
        zone_avg_price_per_m2=5000,  # 20% above average -> bad
    )
    assert bd.gross_yield_score < 30


def test_scorer_condition():
    scorer = InvestmentScorer(MOCK_CONFIG)
    bd_nuevo = scorer.score("madrid", None, "nuevo", 6.0, 4.5, 3000, 3000)
    bd_reformar = scorer.score("madrid", None, "reformar", 6.0, 4.5, 3000, 3000)
    assert bd_nuevo.condition_score > bd_reformar.condition_score


def test_scorer_unknown_city():
    scorer = InvestmentScorer(MOCK_CONFIG)
    # Should not raise, should return neutral location score (50)
    bd = scorer.score("segovia", "centro", "buen_estado", 5.0, 3.5, 1500, 1600)
    assert bd.location_score == 50.0
    assert bd.total > 0
