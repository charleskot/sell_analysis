"""Idealista, read through an Apify actor.

Idealista's alert emails cannot carry a feed: they arrive in a morning
burst and a daily summary capped at twenty flats, with no "immediate"
setting. On 27 October an ático in Molins de Rei at 255.000 € — inside the
search, matching every rule — was on the app at 22:09 and in no email at
all. Its pages refuse a plain or headless read, so the bot cannot fetch it.

The memo23/idealista-scraper actor can, and returns more than the email
ever did: the seller's whole description, floor, lift, exterior, the
portal's own condition and, through the search filter, only flats free to
move into. Billed per row delivered, its monitoring mode delivers only the
flats not seen by an earlier run, so a pass every hour or two costs cents.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

ACTOR = "memo23~idealista-scraper"
API = "https://api.apify.com/v2"


def run(token: str, actor_input: dict, timeout_s: int = 600) -> list[dict]:
    """Start a run, wait for it, return its rows.

    Started and polled rather than called synchronously: a request held
    open for minutes is cut by proxies along the way, and the run itself
    goes on regardless.
    """
    import requests

    started = requests.post(f"{API}/acts/{ACTOR}/runs",
                            params={"token": token, "timeout": timeout_s},
                            json=actor_input, timeout=60)
    started.raise_for_status()
    data = started.json()["data"]
    run_id, dataset = data["id"], data["defaultDatasetId"]

    deadline = time.time() + timeout_s + 60
    status = data.get("status")
    while status in ("READY", "RUNNING") and time.time() < deadline:
        time.sleep(10)
        try:
            polled = requests.get(f"{API}/actor-runs/{run_id}", params={"token": token}, timeout=30)
            status = polled.json()["data"]["status"]
        except Exception as e:
            logger.info(f"Apify: no pude consultar la ejecución {run_id}: {e}")
    if status != "SUCCEEDED":
        # A run that timed out may still have delivered rows worth keeping
        logger.warning(f"Apify: la ejecución {run_id} terminó en {status}")

    items = requests.get(f"{API}/datasets/{dataset}/items",
                         params={"token": token, "clean": 1}, timeout=60)
    items.raise_for_status()
    return [i for i in items.json() if isinstance(i, dict)]


def _place(ubication: dict) -> tuple[str | None, str | None]:
    """Town and barrio from Idealista's own location levels.

    Level 2 is the town ("Molins de Rei", "Barcelona"); below it, district
    and barrio when the town has them ("Distrito Roses - Castellbell").
    """
    city = ubication.get("administrativeAreaLevel2")
    district = ubication.get("administrativeAreaLevel4") or ubication.get("administrativeAreaLevel3")
    if district:
        district = district.removeprefix("Distrito ").removeprefix("Barrio ").strip()
    return (city.lower() if city else None), (district.lower() if district else None)


def to_raw(item: dict) -> dict | None:
    """One actor row into the listing shape every source shares.

    The id is Idealista's own advert number, the one its emails link to, so
    a flat seen both ways is one listing. What the structured fields say —
    lift, exterior, condition, auction — is written into the description in
    the words the profile already reads, so no rule needs a second path.
    """
    code = item.get("adid") or item.get("propertyCode")
    price = item.get("price")
    if not code or not price:
        return None
    more = item.get("moreCharacteristics") or {}
    ubication = item.get("ubication") or {}
    city, district = _place(ubication)

    facts = []
    if more.get("lift") is True:
        facts.append("con ascensor")
    elif more.get("lift") is False:
        facts.append("sin ascensor")
    # In the words the profile's light check reads: "piso interior" is
    # refused, a bare "interior" passed it (Esplugues, 207.000 €).
    if more.get("exterior") is True:
        facts.append("piso exterior")
    elif more.get("exterior") is False:
        facts.append("piso interior")
    status = (more.get("status") or "").lower()
    if status == "renew":
        facts.append("para reformar")
    elif status == "good":
        facts.append("buen estado")
    elif status == "newdevelopment":
        facts.append("obra nueva")
    if more.get("isAuction"):
        facts.append("subasta")

    street = ubication.get("title") or ""
    heading = (item.get("suggestedTexts") or {}).get("title") or f"Piso en venta en {street}"
    title = ", ".join(p for p in (heading, district and district.title(), city and city.title()) if p)
    description = " · ".join(facts) + "\n" + (item.get("propertyComment") or "")

    floor = more.get("floor")
    if more.get("isPenthouse") and floor:
        floor = f"{floor}ª planta (ático)"
    elif floor and str(floor).isdigit():
        floor = f"{floor}ª planta"

    return {
        "portal": "idealista",
        "external_id": str(code),
        "url": f"https://www.idealista.com/inmueble/{code}/",
        "title": title,
        "price": float(price),
        "area_m2": float(more["constructedArea"]) if more.get("constructedArea") else None,
        "rooms": more.get("roomNumber"),
        "bathrooms": more.get("bathNumber"),
        "floor": floor,
        "city": city or "desconocido",
        "district": district,
        "latitude": ubication.get("latitude"),
        "longitude": ubication.get("longitude"),
        "description": description.strip(),
        "photo_urls": [item["thumbnail"]] if item.get("thumbnail") else [],
        # The seller's own words arrived whole: there is no page left to read
        "description_complete": True,
    }
