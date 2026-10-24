"""The same flat, sent once.

"Nunca nunca repetido." Every case below is a pair of cards actually sent,
taken from the alert history: pairs that were one flat and went out twice,
and pairs that were two flats a looser rule would have merged.
"""
import pytest

from models.db import same_flat


def L(title, price, area=None, rooms=None, city="barcelona", district=None):
    return {"title": title, "price": price, "area_m2": area, "rooms": rooms,
            "city": city, "district": district}


# ── One flat, sent twice before ────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    # Cross-portal, same day: Idealista with no surface, Habitaclia with it.
    (L("Ático en Calle de Biscaia, Navas, Barcelona", 325000, None, 4, district="navas"),
     L("Ático en Barcelona - Navas (Sant Andreu)", 325000, 76, 4, district="navas")),
    # The Navas flat of August, again in September without its m².
    (L("Piso en Navas, Barcelona", 299000, 65, 2, district="navas"),
     L("Piso en Navas, Barcelona", 299000, None, 2, district="navas")),
    # Re-listed after a small cut, two months apart.
    (L("Piso en Esplugues de Llobregat - Can ...", 290000, 91, 4, city="esplugues de llobregat"),
     L("Piso en Esplugues de Llobregat - Can ...", 295000, 90, 4, city="esplugues de llobregat")),
    # Built and useful area at one price, rooms counted differently.
    (L("Piso en Esplugues de Llobregat - Can ...", 270000, 70, 2, city="esplugues de llobregat"),
     L("Piso en Esplugues de Llobregat - Can ...", 270000, 85, 3, city="esplugues de llobregat")),
    # No room count at all, on both.
    (L("Piso en Vilanova i la Geltrú - Centre...", 75000, 83, None, city="vilanova i la geltrú"),
     L("Piso en Vilanova i la Geltru - Centre...", 75000, 83, None, city="vilanova i la geltru")),
])
def test_one_flat_is_recognised(a, b):
    assert same_flat(a, b) and same_flat(b, a)


# ── Two flats a looser rule would merge ────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    # Same barrio and price, no surface either side — but two streets.
    (L("Piso en Calle de Besalú, El Camp de l'Arpa del Clot, Barcelona", 325000, None, 3,
       district="el camp de l'arpa del clot"),
     L("Piso en Calle del Pistó, 2, El Camp de l'Arpa del Clot, Barcelona", 325000, None, 3,
       district="el camp de l'arpa del clot")),
    (L("Piso en Calle de Sèrbia, El Guinardó, Barcelona", 325000, None, 3, district="el guinardó"),
     L("Piso en Avenida de la Mare de Déu de Montserrat, El Guinardó, Barcelona", 325000, None, 3,
       district="el guinardó")),
    # A truncated barrio still disagrees with another barrio.
    (L("Piso en Sant Feliu de Llobregat - Ros...", 235000, 68, 3, city="sant feliu de llobregat"),
     L("Piso en Sant Feliu de Llobregat - Cen...", 239000, 75, 3, city="sant feliu de llobregat")),
    (L("Piso en Esplugues de Llobregat - Can ...", 270000, 70, 3, city="esplugues de llobregat"),
     L("Piso en Esplugues de Llobregat - Centre", 266000, 67, 3, city="esplugues de llobregat")),
    # Big city, two barrios, same price and size: coincidence, not a repeat.
    (L("Piso en Guinardó, Barcelona", 330000, 67, 3, district="el guinardó"),
     L("Piso en Sant Andreu, Barcelona", 330000, 65, 3, district="sant andreu")),
    # Price apart by more than 2%.
    (L("Piso en Navas, Barcelona", 300000, 70, 3, district="navas"),
     L("Piso en Navas, Barcelona", 285000, 70, 3, district="navas")),
    # Two towns.
    (L("Piso en Cornellà", 200000, 70, 3, city="cornellà de llobregat"),
     L("Piso en Esplugues", 200000, 70, 3, city="esplugues de llobregat")),
])
def test_two_flats_stay_two(a, b):
    assert not same_flat(a, b) and not same_flat(b, a)


def test_without_a_surface_the_price_must_agree_closely():
    """Thinner evidence asks for more: 1.5% apart and no m² is not enough."""
    a = L("Piso en Navas, Barcelona", 310000, None, 3, district="navas")
    b = L("Piso en Navas, Barcelona", 315000, 72, 3, district="navas")
    assert not same_flat(a, b)
