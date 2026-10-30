"""Fotocasa, read through an Apify actor.

Fotocasa's alert emails, like Idealista's, are a sample rather than a feed,
and its pages refuse the runner. The memo23/fotocasa-scraper actor returns
each search's newest flats with the seller's whole description, the barri,
and Fotocasa's own flags for occupied, tenanted, bare ownership and auction;
in monitor mode only flats no earlier run has seen are delivered and billed.
"""
from __future__ import annotations

ACTOR = "memo23~fotocasa-scraper"


def to_raw(item: dict) -> dict | None:
    """One actor row into the listing shape every source shares.

    The id is Fotocasa's advert number, the one its emails link to, so a flat
    seen both ways is one listing. Fotocasa's flags are written into the
    description in the words the occupation screen already reads.

    The floor is left out: the field carries Fotocasa's internal code, not
    the floor — an Esplugues flat Habitaclia lists on the first came back as
    "6" — and a wrong floor would refuse good flats.
    """
    code = item.get("id") or item.get("propertyId")
    price = item.get("rawPrice")
    if not code or not price:
        return None
    address = item.get("address") or {}
    flags = item.get("flags") or {}
    amenities = item.get("amenities") or {}

    city = address.get("municipality") or address.get("city")
    district = address.get("neighborhood") or address.get("district")

    facts = []
    if flags.get("isOccupied"):
        facts.append("ocupado")
    if flags.get("isRentedWithTenants") or item.get("isRentedWithTenants"):
        facts.append("alquilado con inquilinos")
    if flags.get("isBareOwnership"):
        facts.append("nuda propiedad")
    if flags.get("isAuctioned") or item.get("isAuctioned"):
        facts.append("subasta")
    # Only a stated lift counts: every amenity reads False when the advert
    # simply does not list amenities, and silence is not "sin ascensor".
    if amenities.get("elevator") is True:
        facts.append("con ascensor")
    if amenities.get("terrace") is True:
        facts.append("terraza")
    if amenities.get("balcony") is True:
        facts.append("balcón")

    street = address.get("street") or item.get("street") or ""
    kind = {"Flat": "Piso", "Penthouse": "Ático", "Duplex": "Dúplex",
            "Studio": "Estudio"}.get(item.get("propertySubtype") or "", "Piso")
    heading = f"{kind} en venta" + (f" en {street}" if street else "")
    title = ", ".join(p for p in (heading, district, city) if p)
    description = " · ".join(facts) + "\n" + (item.get("description") or "")

    return {
        "portal": "fotocasa",
        "external_id": str(code),
        "url": item.get("url") or f"https://www.fotocasa.es/es/comprar/vivienda/x/x/{code}/d",
        "title": title,
        "price": float(price),
        "area_m2": float(item["surface"]) if item.get("surface") else None,
        "rooms": item.get("rooms"),
        "bathrooms": item.get("bathrooms"),
        "floor": None,
        "city": city.lower() if city else "desconocido",
        "district": district.lower() if district else None,
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
        "description": description.strip(),
        "photo_urls": (item.get("photos") or [])[:5],
        "description_complete": True,
        # When the advert went up: an actor's first look at a search calls
        # every flat on it new, however long it has been for sale.
        "published_at": item.get("publicationDate") or item.get("listedDate"),
    }
