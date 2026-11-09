"""The flats that meet the search, for the map page.

"¿Esto no puede estar en la app, que yo acceda por ahí y se actualice
constantemente?" A page published from a chat cannot be refreshed by the
bot, which runs on GitHub, but a file in this repository can: the bot writes
docs/pisos.json every half hour and GitHub Pages serves it beside the map in
docs/index.html, so the map is never older than the last pass.

What goes on it is what the notifications stand for: still on a portal in
the last few days, meeting the search, not refused by its advert, one point
per flat however many agencies list it. Habitaclia's cards carry no
coordinates; its advert page does, so it is read once per flat — the same
read screens it for occupation — and the position is stored.
"""
from __future__ import annotations

import json
import logging
import re
import time
import zlib
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_LAT_RE = re.compile(r'latitude\\?"?:\\?"?(-?\d+\.\d+)')
_LNG_RE = re.compile(r'longitude\\?"?:\\?"?(-?\d+\.\d+)')
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_TOWNS = {"barcelona": "Barcelona", "badalona": "Badalona",
          "hospitalet de llobregat": "L'Hospitalet", "l'hospitalet de llobregat": "L'Hospitalet",
          "santa coloma de gramenet": "Santa Coloma", "cornellà de llobregat": "Cornellà",
          "cornella de llobregat": "Cornellà", "esplugues de llobregat": "Esplugues",
          "sant feliu de llobregat": "Sant Feliu", "molins de rei": "Molins de Rei",
          "sant just desvern": "Sant Just"}


def _barri(listing: dict) -> str:
    district = (listing.get("district") or "").split("(")[0].strip()
    if not district or district.lower() == (listing.get("city") or "").lower():
        return ""
    small = {"de", "del", "la", "el", "els", "les", "i", "d'en"}
    return " ".join(w if w in small else w.capitalize() for w in district.lower().split())


def read_position(url: str) -> tuple[float | None, float | None, str | None]:
    """Latitude, longitude and any disqualifying wording from an advert page."""
    import requests
    from ingest.email_parsers import _REJECT_RE

    try:
        page = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=20).text
    except Exception as e:
        logger.info(f"mapa: no pude leer {url[-60:]}: {e}")
        return None, None, None
    lat, lng = _LAT_RE.search(page), _LNG_RE.search(page)
    trap = _REJECT_RE.search(re.sub(r"<[^>]+>", " ", page))
    return (float(lat.group(1)) if lat else None, float(lng.group(1)) if lng else None,
            trap.group(0) if trap else None)


def collect(orchestrator, listed_within_days: int = 3, max_reads: int = 40) -> list[dict]:
    """Every flat for the map, newest advert per flat, positions filled in."""
    from sqlalchemy import select, update
    from ingest.email_parsers import _REJECT_RE
    from models.db import (get_engine, get_verify_verdict, same_flat, session_scope,
                           set_verify_verdict)
    from models.schema import alerts_sent, listings

    since = (datetime.now(timezone.utc) - timedelta(days=listed_within_days)).replace(tzinfo=None)
    with get_engine().connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(
            select(listings).where(listings.c.last_seen_at >= since)
            .order_by(listings.c.first_seen_at.desc()))]
        sent_rows = [dict(r._mapping) for r in conn.execute(
            select(listings).select_from(alerts_sent.join(listings, listings.c.id == alerts_sent.c.listing_id)))]
    sent_ids = {r["id"] for r in sent_rows}

    flats: list[dict] = []
    for listing in rows:
        metrics = orchestrator._compute_metrics(listing) or orchestrator._minimal_metrics(listing)
        if not orchestrator.profile_matcher.match(listing, metrics):
            continue
        if _REJECT_RE.search(f"{listing.get('title') or ''} {listing.get('description') or ''}"):
            continue
        verdict = get_verify_verdict(listing["id"])
        if verdict and verdict[0] == "rechazada":
            continue
        twin = next((f for f in flats if same_flat(listing, f)), None)
        if twin is not None:
            if listing.get("latitude") and not twin.get("latitude"):
                flats[flats.index(twin)] = listing
            continue
        flats.append(listing)

    # His favourites stay on the map whatever their price does now
    from scheduler.favorites import all_favourites

    favs = all_favourites()
    on_map = {f["id"] for f in flats}
    missing = [i for i, fav in favs.items() if i not in on_map and not fav.get("retirado")]
    if missing:
        with get_engine().connect() as conn:
            flats += [dict(r._mapping) for r in conn.execute(
                select(listings).where(listings.c.id.in_(missing)))]

    reads = 0
    kept = []
    for listing in flats:
        url = listing.get("url") or ""
        if (not listing.get("latitude") and listing.get("portal") == "habitaclia"
                and "/alertas/" not in url and reads < max_reads):
            reads += 1
            lat, lng, trap = read_position(url)
            time.sleep(0.8)
            if trap:
                set_verify_verdict(listing["id"], "rechazada")
                continue
            if lat and lng:
                listing["latitude"], listing["longitude"] = lat, lng
                with session_scope() as conn:
                    conn.execute(update(listings).where(listings.c.id == listing["id"])
                                 .values(latitude=lat, longitude=lng))
        listing["_sent"] = listing["id"] in sent_ids or any(same_flat(listing, s) for s in sent_rows)
        listing["_fav"] = listing["id"] in favs
        kept.append(listing)
    if reads:
        logger.info(f"mapa: {reads} fichas leídas para situar pisos")
    return kept


def to_points(flats: list[dict]) -> list[dict]:
    """The JSON the page draws. A flat with no position of its own sits at
    the middle of the others in its barri, or its town, marked approximate."""
    import statistics

    groups: dict = {}
    for f in flats:
        if f.get("latitude"):
            for key in ((f.get("city") or "", _barri(f).lower()), (f.get("city") or "", "")):
                groups.setdefault(key, []).append((f["latitude"], f["longitude"]))
    points = []
    for f in flats:
        lat, lng, approx = f.get("latitude"), f.get("longitude"), False
        if not lat:
            near = groups.get((f.get("city") or "", _barri(f).lower())) or groups.get((f.get("city") or "", ""))
            if not near:
                continue
            # Spread the stand-ins a little so they do not stack on one spot
            spread = (zlib.crc32(f["id"].encode()) % 21 - 10) / 6000
            lat = statistics.median(p[0] for p in near) + spread
            lng = statistics.median(p[1] for p in near) - spread
            approx = True
        area = f.get("area_m2")
        if not area or area > 300:
            continue
        points.append({
            "p": int(f["price"]), "a": int(area), "r": f.get("rooms"), "f": f.get("floor") or "",
            "u": f.get("url"), "src": f.get("portal"),
            "t": _TOWNS.get((f.get("city") or "").lower(), (f.get("city") or "").title()),
            "b": _barri(f), "lat": round(lat, 5), "lng": round(lng, 5),
            "s": bool(f.get("_sent")), "ap": approx, "fv": bool(f.get("_fav")),
            "d": str(f.get("first_seen_at"))[:10],
        })
    return points


def write(points: list[dict], path: str) -> None:
    from pathlib import Path

    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "flats": points}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
