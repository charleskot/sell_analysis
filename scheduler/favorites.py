"""Favourites: the flats he tapped 👍 on, and their price, followed.

"Importante que el botón cuando gusta haga que el piso se guarde como
especial y se le dé seguimiento al precio." A liked flat is kept here with
the price it had when he liked it. Every change of price is news on it —
up or down, by any amount, whether or not it still meets the search — and
so is its advert disappearing. Habitaclia and Fotocasa adverts are read
every few hours for their price, since the searches only re-read the
newest pages and a flat liked last week is no longer on them; Idealista
refuses those reads, so its favourites are followed through whatever the
Apify runs and alert emails bring.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_KEY = "favoritos"
_LAST_KEY = "favoritos_ultima_revision"
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
# Both portals state the asking price in the page's data, Habitaclia's escaped
_PRICE_RE = re.compile(r'\\?"price\\?":\{\\?"amount\\?":(\d{4,8})')
_READABLE = ("habitaclia", "fotocasa")
_GONE_AFTER = 2            # failed reads in a row before an advert counts as gone


def all_favourites() -> dict:
    from models.db import get_telegram_state

    try:
        data = json.loads(get_telegram_state(_KEY, "") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _save(data: dict) -> None:
    from models.db import set_telegram_state

    set_telegram_state(_KEY, json.dumps(data, ensure_ascii=False))


def is_favourite(listing_id: str) -> bool:
    return bool(listing_id) and listing_id in all_favourites()


def _row(listing_id: str) -> dict | None:
    from sqlalchemy import select
    from models.db import get_engine
    from models.schema import listings

    with get_engine().connect() as conn:
        row = conn.execute(select(listings).where(listings.c.id == listing_id)).fetchone()
    return dict(row._mapping) if row else None


def add(listing_id: str) -> bool:
    """Keep a liked flat, at the price it had when he liked it."""
    data = all_favourites()
    if listing_id in data:
        return False
    row = _row(listing_id) or {}
    data[listing_id] = {"p": row.get("price"),
                        "desde": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                        "fallos": 0}
    _save(data)
    logger.info(f"Favorito guardado: {listing_id}")
    return True


def remove(listing_id: str) -> bool:
    """A 👎 after a 👍: he changed his mind."""
    data = all_favourites()
    if data.pop(listing_id, None) is None:
        return False
    _save(data)
    return True


def read_price(url: str) -> tuple[int | None, bool]:
    """(price on the advert, whether the advert is gone)."""
    import requests

    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=20)
    except Exception as e:
        logger.info(f"favoritos: no pude leer {url[-60:]}: {e}")
        return None, False
    if resp.status_code in (404, 410):
        return None, True
    if resp.status_code >= 300:
        return None, False
    found = _PRICE_RE.search(resp.text)
    return (int(found.group(1)) if found else None), False


def _note_price(alerter, listing_id: str, fav: dict, listing: dict, new_price: float) -> bool:
    """Record a favourite's price, telling him when it moved. True if told."""
    old = fav.get("p")
    fav["fallos"] = 0
    fav.pop("retirado", None)
    if old and abs(new_price - old) >= 1:
        if alerter.send_favourite_change(listing_id, listing, old, new_price):
            fav["p"] = new_price
            return True
        return False
    if not old:
        fav["p"] = new_price
    return False


def price_seen(alerter, listing_id: str, listing: dict, new_price: float | None) -> bool:
    """Any source saw a favourite at a price: tell him if it changed.

    Returns True when the listing is a favourite and has been dealt with,
    so the caller does not also send it as an ordinary price drop.
    """
    data = all_favourites()
    fav = data.get(listing_id)
    if fav is None:
        return False
    if new_price:
        _note_price(alerter, listing_id, fav, listing, float(new_price))
        _save(data)
    return True


def check(alerter, interval_minutes: int = 180, force: bool = False) -> int:
    """Read each readable favourite's advert for its price. Returns changes sent."""
    from models.db import get_telegram_state, set_telegram_state, upsert_listing

    data = all_favourites()
    if not data:
        return 0
    now = datetime.now(timezone.utc)
    last = get_telegram_state(_LAST_KEY, "")
    try:
        if not force and last and now - datetime.fromisoformat(last) < timedelta(minutes=interval_minutes):
            return 0
    except ValueError:
        pass
    set_telegram_state(_LAST_KEY, now.isoformat())

    sent = 0
    for listing_id, fav in data.items():
        row = _row(listing_id)
        if not row or row.get("portal") not in _READABLE or fav.get("retirado"):
            continue
        price, gone = read_price(row.get("url") or "")
        if gone:
            fav["fallos"] = fav.get("fallos", 0) + 1
            if fav["fallos"] >= _GONE_AFTER and alerter.send_favourite_gone(listing_id, row):
                fav["retirado"] = now.isoformat(timespec="minutes")
            continue
        if price is None:
            continue
        if row.get("price") != price:
            upsert_listing({**row, "price": float(price)})
        if _note_price(alerter, listing_id, fav, {**row, "price": float(price)}, float(price)):
            sent += 1
    _save(data)
    if sent:
        logger.info(f"favoritos: {sent} cambios de precio avisados")
    return sent


def control_lines(limit: int = 10) -> list[str]:
    """The favourites as the morning control lists them."""
    data = all_favourites()
    lines = []
    for listing_id, fav in list(data.items())[-limit:]:
        row = _row(listing_id) or {}
        place = (row.get("district") or row.get("city") or "").title()
        now = row.get("price") or fav.get("p") or 0
        money = f"{now:,.0f}".replace(",", ".")
        history = row.get("price_history") or []
        first = history[0]["price"] if history and isinstance(history[0], dict) else None
        moved = ""
        if first and first != now:
            moved = f" (antes {first:,.0f} €)".replace(",", ".")
        if fav.get("retirado"):
            moved += " · ya no está anunciado"
        lines.append(f"⭐ {money} €{moved} · {place} · {row.get('url') or listing_id}")
    return lines
