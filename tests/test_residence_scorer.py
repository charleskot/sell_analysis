"""Unit tests for the residence match scorer."""
import pytest
from analysis.residence_scorer import ResidenceScorer, extract_features


MOCK_CONFIG = {
    "target_locations": [
        {"city": "molins de rei"},
        {"city": "sant cugat del vallès"},
        {
            "city": "castelldefels",
            "preferred_districts": ["montemar", "playa"],
            "excluded_districts": ["zona industrial"],
        },
        {
            "city": "barcelona",
            "preferred_districts": ["camp de l'arpa", "el guinardó"],
        },
    ],
    "residence_criteria": {
        "profile_new": {
            "label": "Obra nueva",
            "max_price": 360000,
            "min_rooms": 2,
            "require_condition": ["nuevo"],
        },
        "profile_recent": {
            "label": "Reciente",
            "max_price": 285000,
            "max_price_soft": 315000,
            "min_rooms": 3,
            "max_years_old": 20,
        },
    },
    "common_requirements": {
        "min_area_m2": 60,
        "require_elevator": True,
        "require_balcony": True,
    },
}


def _mk(**kw):
    base = {
        "city": "molins de rei",
        "district": None,
        "price": 250000,
        "area_m2": 80,
        "rooms": 3,
        "condition": "buen_estado",
        "title": "Piso con ascensor y balcón",
        "description": "año 2010",
    }
    base.update(kw)
    return base


def test_extract_features_elevator():
    assert extract_features({"title": "Con ascensor", "description": ""})["has_elevator"] is True
    assert extract_features({"title": "Sin ascensor", "description": ""})["has_elevator"] is False
    assert extract_features({"title": "Piso céntrico", "description": ""})["has_elevator"] is None


def test_extract_features_balcony():
    assert extract_features({"title": "Con balcón", "description": ""})["has_balcony"] is True
    assert extract_features({"title": "Con terraza", "description": ""})["has_balcony"] is True
    assert extract_features({"title": "Interior", "description": ""})["has_balcony"] is None


def test_extract_features_year():
    assert extract_features({"title": "año 2015", "description": ""})["year_built"] == 2015
    assert extract_features({"title": "construido en 2018", "description": ""})["year_built"] == 2018


def test_scorer_matches_profile_new():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(city="sant cugat del vallès", price=320000, condition="nuevo", rooms=2))
    assert bd.hard_filters_pass
    assert bd.matches_profile == "new"
    assert bd.total > 60


def test_scorer_matches_profile_recent():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(price=250000, rooms=3, condition="buen_estado"))
    assert bd.hard_filters_pass
    assert bd.matches_profile == "recent"


def test_scorer_rejects_over_budget_new():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(city="sant cugat del vallès", price=400000, condition="nuevo"))
    assert not bd.hard_filters_pass


def test_scorer_rejects_over_budget_recent_soft():
    scorer = ResidenceScorer(MOCK_CONFIG)
    # Above soft limit 315k → reject
    bd = scorer.score(_mk(price=350000, rooms=3))
    assert not bd.hard_filters_pass


def test_scorer_accepts_soft_range_recent():
    scorer = ResidenceScorer(MOCK_CONFIG)
    # Between hard 285k and soft 315k → accept (negotiable)
    bd = scorer.score(_mk(price=300000, rooms=3))
    assert bd.hard_filters_pass
    assert "negociable" in " ".join(bd.reasons_pass)


def test_scorer_rejects_too_old():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(title="ascensor balcón", description="construido en 1980"))
    assert not bd.hard_filters_pass


def test_scorer_rejects_no_elevator():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(title="Piso sin ascensor con balcón"))
    assert not bd.hard_filters_pass


def test_scorer_rejects_too_few_rooms():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(rooms=1))
    assert not bd.hard_filters_pass


def test_scorer_rejects_excluded_district():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(city="castelldefels", district="zona industrial",
                          price=250000, condition="nuevo", rooms=3))
    assert not bd.hard_filters_pass
    assert any("excluido" in r.lower() for r in bd.reasons_fail)


def test_scorer_accepts_preferred_district():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(city="castelldefels", district="montemar",
                          price=320000, condition="nuevo", rooms=3))
    assert bd.hard_filters_pass
    assert bd.location_score == 100


def test_scorer_rejects_non_preferred_when_whitelist():
    scorer = ResidenceScorer(MOCK_CONFIG)
    # City has preferred list → district not in list must fail
    bd = scorer.score(_mk(city="barcelona", district="sarrià",
                          price=280000, condition="nuevo", rooms=3))
    assert not bd.hard_filters_pass


def test_scorer_rejects_off_target_city():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(city="madrid"))
    assert not bd.hard_filters_pass


def test_scorer_rejects_too_small():
    scorer = ResidenceScorer(MOCK_CONFIG)
    bd = scorer.score(_mk(area_m2=40))
    assert not bd.hard_filters_pass


def test_scorer_rejects_rented():
    scorer = ResidenceScorer(MOCK_CONFIG)
    for text in [
        "Piso alquilado con inquilino actual",
        "Alquilada con contrato vigente",
        "Piso con inquilino, rentabilidad garantizada",
        "Renta antigua, arrendado desde 2015",
    ]:
        bd = scorer.score(_mk(title=text, description="ascensor balcón"))
        assert not bd.hard_filters_pass, f"Debería rechazar: {text}"
        assert any("alquil" in r.lower() or "inquilino" in r.lower() for r in bd.reasons_fail)


def test_scorer_rejects_occupied():
    scorer = ResidenceScorer(MOCK_CONFIG)
    for text in [
        "Piso ocupado ilegalmente",
        "Vivienda okupada por terceros",
        "Con okupas dentro",
        "Usurpación en curso",
    ]:
        bd = scorer.score(_mk(title=text, description="ascensor balcón"))
        assert not bd.hard_filters_pass, f"Debería rechazar: {text}"
        assert any("okup" in r.lower() or "ocupad" in r.lower() for r in bd.reasons_fail)


def test_scorer_accepts_normal_listing():
    scorer = ResidenceScorer(MOCK_CONFIG)
    # No mention of rented/occupied → should not reject on this basis
    bd = scorer.score(_mk(title="Piso en el centro con vistas",
                          description="Balcón, ascensor, 2010"))
    assert bd.hard_filters_pass
