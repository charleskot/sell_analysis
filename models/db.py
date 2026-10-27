"""Database engine, session management, and upsert helpers."""
import json
import re
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select, update, insert
from sqlalchemy.engine import Engine

from models.schema import (
    metadata, listings, investment_metrics, alerts_sent,
    rent_zone_averages, feedback, telegram_state,
)

logger = logging.getLogger(__name__)

_engine: Engine | None = None


def init_engine(db_path: str) -> Engine:
    global _engine
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{db_path}", echo=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Call init_engine() first")
    return _engine


def create_all_tables() -> None:
    metadata.create_all(get_engine())
    logger.info("Database tables ready")


@contextmanager
def session_scope():
    conn = get_engine().connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_updated_price_history(
    existing_price: float | None,
    existing_last_seen: datetime | None,
    existing_history: list,
    new_price: float | None,
    now: datetime | None = None,
) -> list:
    """Return updated price_history list, appending existing price when it changes."""
    history = list(existing_history or [])
    if existing_price is not None and existing_price != new_price:
        ts = existing_last_seen.isoformat() if existing_last_seen else (now or datetime.now(timezone.utc)).isoformat()
        history.append({"price": existing_price, "date": ts})
    return history


def upsert_listing(raw: dict) -> bool:
    """Insert or update a listing. Returns True if new, False if updated."""
    listing_id = f"{raw['portal']}_{raw['external_id']}"
    now = datetime.now(timezone.utc)

    with session_scope() as conn:
        existing = conn.execute(
            select(listings).where(listings.c.id == listing_id)
        ).fetchone()

        if existing:
            history = build_updated_price_history(
                existing.price, existing.last_seen_at, existing.price_history or [], raw.get("price"), now
            )

            area = raw.get("area_m2")
            price = raw.get("price")
            ppm2 = (price / area) if price and area and area > 0 else None

            conn.execute(
                update(listings).where(listings.c.id == listing_id).values(
                    # url and title are refreshed too: a listing already known
                    # may have been stored with a link that never worked, and
                    # without these the bad value survives every later pass.
                    url=raw.get("url"),
                    title=raw.get("title", ""),
                    price=price,
                    area_m2=area,
                    price_per_m2=ppm2,
                    rooms=raw.get("rooms"),
                    bathrooms=raw.get("bathrooms"),
                    floor=raw.get("floor"),
                    condition=raw.get("condition"),
                    latitude=raw.get("latitude"),
                    longitude=raw.get("longitude"),
                    district=raw.get("district"),
                    city=raw.get("city"),
                    zip_code=raw.get("zip_code"),
                    description=raw.get("description", ""),
                    photo_urls=raw.get("photo_urls", []),
                    raw_html_hash=raw.get("raw_html_hash"),
                    price_history=history,
                    last_seen_at=now,
                    is_active=True,
                )
            )
            return False
        else:
            area = raw.get("area_m2")
            price = raw.get("price")
            ppm2 = (price / area) if price and area and area > 0 else None

            conn.execute(
                insert(listings).values(
                    id=listing_id,
                    portal=raw["portal"],
                    external_id=raw["external_id"],
                    url=raw["url"],
                    title=raw.get("title", ""),
                    price=price,
                    area_m2=area,
                    price_per_m2=ppm2,
                    rooms=raw.get("rooms"),
                    bathrooms=raw.get("bathrooms"),
                    floor=raw.get("floor"),
                    condition=raw.get("condition"),
                    latitude=raw.get("latitude"),
                    longitude=raw.get("longitude"),
                    district=raw.get("district"),
                    city=raw.get("city"),
                    zip_code=raw.get("zip_code"),
                    description=raw.get("description", ""),
                    photo_urls=raw.get("photo_urls", []),
                    raw_html_hash=raw.get("raw_html_hash"),
                    price_history=[],
                    # An alert email read days late still dates the listing
                    # to when the portal announced it, not to when we caught
                    # up — otherwise a backlog looks brand new to every
                    # freshness check downstream.
                    first_seen_at=raw.get("first_seen_at") or now,
                    last_seen_at=now,
                    is_active=True,
                )
            )
            return True


def upsert_metrics(listing_id: str, metrics: dict) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as conn:
        existing = conn.execute(
            select(investment_metrics).where(investment_metrics.c.listing_id == listing_id)
        ).fetchone()

        data = {
            "gross_yield_pct": metrics.get("gross_yield_pct"),
            "net_yield_pct": metrics.get("net_yield_pct"),
            "roi_pct": metrics.get("roi_pct"),
            "payback_years": metrics.get("payback_years"),
            "estimated_monthly_rent": metrics.get("estimated_monthly_rent"),
            "total_investment": metrics.get("total_investment"),
            "investment_score": metrics.get("investment_score"),
            "score_breakdown": metrics.get("score_breakdown", {}),
            "computed_at": now,
        }
        if existing:
            conn.execute(update(investment_metrics).where(investment_metrics.c.listing_id == listing_id).values(**data))
        else:
            conn.execute(insert(investment_metrics).values(listing_id=listing_id, **data))


def was_alert_ever_sent(listing_id: str) -> bool:
    """Whether this flat has ever gone out, with no time limit.

    The cooldown window is the wrong question for anything that re-examines
    stored listings rather than freshly parsed ones: every listing falls out
    of a 24-hour window eventually, so a nightly sweep would re-send the
    whole catalogue once a day.
    """
    with session_scope() as conn:
        row = conn.execute(
            select(alerts_sent).where(alerts_sent.c.listing_id == listing_id)
        ).fetchone()
    return row is not None


def bump_daily(counter: str) -> None:
    """Count one funnel event for today. Never raises: bookkeeping.

    The user's recurring question is "where do the flats die", and until now
    only the logs knew. These counters make the bot able to answer it in
    the chat instead of the answer needing an engineer.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    key = "funnel_today"
    today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    try:
        raw = get_telegram_state(key, "")
        data = _json.loads(raw) if raw else {}
        if data.get("day") != today:
            data = {"day": today, "counts": {}}
        data["counts"][counter] = data["counts"].get(counter, 0) + 1
        set_telegram_state(key, _json.dumps(data))
    except Exception as e:
        logger.warning(f"No pude contar {counter}: {e}")


def daily_counters() -> dict:
    """Today's funnel tally, empty if none or unreadable."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    try:
        raw = get_telegram_state("funnel_today", "")
        data = _json.loads(raw) if raw else {}
        if data.get("day") == _dt.now(_tz.utc).strftime("%Y-%m-%d"):
            return dict(data.get("counts") or {})
    except Exception:
        pass
    return {}


def get_verify_verdict(listing_id: str) -> tuple[str, str] | None:
    """The stored page-verification verdict for a listing, if any.

    Returns (verdict, iso_timestamp): "rechazada" when the advert's own page
    disqualified the flat — final, the page said what it said — or
    "ilegible" when nothing could read the page, which is worth retrying
    once the reader has had a few hours to recover.
    """
    import json as _json

    if not listing_id:
        return None
    try:
        raw = get_telegram_state("verify_verdicts", "")
        data = _json.loads(raw) if raw else {}
        entry = data.get(listing_id)
        if entry:
            return entry.get("v", ""), entry.get("at", "")
    except Exception:
        pass
    return None


def set_verify_verdict(listing_id: str, verdict: str) -> None:
    """Remember why a listing failed verification. Never raises: bookkeeping.

    Without this, the pending sweep re-read the same rejected adverts every
    cycle: 68 listings × one paced reader fetch each × 480 cycles a day.
    The funnel counter reached 5.896 "occupied" in a day that saw perhaps a
    dozen distinct flats, and the reader time spent re-condemning them was
    time not spent verifying anything new.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    if not listing_id:
        return
    try:
        raw = get_telegram_state("verify_verdicts", "")
        data = _json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
        data[listing_id] = {"v": verdict,
                            "at": _dt.now(_tz.utc).isoformat()}
        if len(data) > 800:
            oldest_first = sorted(data.items(),
                                  key=lambda kv: kv[1].get("at", ""))
            data = dict(oldest_first[-800:])
        set_telegram_state("verify_verdicts", _json.dumps(data))
    except Exception as e:
        logger.warning(f"No pude guardar veredicto de {listing_id}: {e}")


_STREET_TYPES_RE = None


def _street_of(title: str | None) -> str:
    """The street a title names, normalised, or "".

    "Piso en Calle de Besalú, El Camp de l'Arpa..."  -> "besalu"
    "Piso con ascensor en venta en CL LAS NAVAS DE TOLOSA" -> "las navas de tolosa"
    "Piso en Barcelona - Navas (Sant Andreu)"        -> ""   (no street)
    """
    import re
    from analysis.profiles import normalise

    global _STREET_TYPES_RE
    if _STREET_TYPES_RE is None:
        _STREET_TYPES_RE = re.compile(
            r"^(?:calle|cl|c|carrer|avenida|avda|av|avinguda|paseo|passeig|pg|plaza|placa|"
            r"pza|pz|pl|ronda|rambla|travessera|trav|camino|cami|via|pasaje|passatge|ptge|"
            r"carretera|ctra|gran via|urbanizacion|urb)\s+(?:(?:de|del|dels|de la|de les|"
            r"d|la|el|les|los|las)\s+)?(.+)$")
    if not title:
        return ""
    m = re.search(r"\ben (?:venta en )?(.+)$", title, re.I)
    segment = (m.group(1) if m else title).split(",")[0]
    seg = normalise(segment)
    sm = _STREET_TYPES_RE.match(seg)
    if not sm:
        return ""
    street = re.sub(r"\s+\d.*$", "", sm.group(1)).strip()
    return street if len(street) >= 3 else ""


def _place_hint(listing: dict) -> str:
    """The barrio, or what is left of it in a truncated title.

    Habitaclia's emails cut "Esplugues de Llobregat - Can Vidalet" to
    "... - Can ...", so the stored district is empty; the fragment still
    tells "can" from "centre" and "el gall".
    """
    from analysis.profiles import normalise

    district = _own_district(listing)
    if district:
        return district
    title = listing.get("title") or ""
    # The browser's cards name the barrio whole, dashes and all: "Casa con
    # balcón en venta en Centre - Can Nadal" is in "Centre - Can Nadal",
    # which the email truncates to "Sant Feliu de Llobregat - Cen...".
    sale = re.search(r"\ben venta en (.+)$", title, re.I)
    if sale and " - " in sale.group(1):
        return normalise(sale.group(1))[:30]
    if " - " in title:
        return normalise(title.split(" - ", 1)[1].replace("...", " "))[:30]
    return ""


def _own_district(listing: dict) -> str:
    """The district, normalised, unless it only repeats the town.

    The browser search for a whole town files every card under the town's
    own name, which says nothing about where in it the flat is — and read
    as a barrio it contradicted "Sant Feliu de Llobregat - Cen..." for the
    same house.
    """
    from analysis.profiles import normalise

    district = normalise(listing.get("district"))
    return "" if district and district == normalise(listing.get("city")) else district


def _compatible(a: str, b: str) -> bool:
    """Neither says something the other contradicts: one is a prefix of,
    or contained in, the other. Truncated fragments are prefixes."""
    return a.startswith(b) or b.startswith(a) or a in b or b in a


def same_flat(a: dict, b: dict) -> bool:
    """Whether two listings are, in all likelihood, the same flat.

    The same flat reaches the user through several agencies and both
    portals, each measuring and pricing it its own way. Exact fingerprints
    missed it four ways, all seen in the cards actually sent:

      - no surface: one Camp de l'Arpa flat at 325.000 € went out four
        times through four Idealista agencies, none of which gave its m²;
      - no room count, which leaves no fingerprint at all;
      - a small price change: re-listed at 295.000 € after 299.000 €;
      - the town spelt two ways ("Vilanova i la Geltrú" / "...Geltru").

    The user's rule is "nunca nunca repetido", so this errs towards calling
    two listings the same. At an identical price: surface within 25%,
    rooms within one. With the price up to 2% apart: surface within 12%
    and the same rooms. Two genuinely different flats that close in the same place
    — identical units in one building — will show as one card, which is the
    accepted cost. Where the barrio is known on both sides and differs, they
    are different flats; in a city over 200.000 people the barrio must
    agree, because there a coincidence of price and size is common.
    Without a surface on both sides the evidence is thinner, so the price
    must agree within 1% and the barrio and rooms must match.
    """
    from analysis.profiles import normalise, _place_matches
    from analysis.municipalities import population_of

    ca, cb = normalise(a.get("city")), normalise(b.get("city"))
    if not ca or not cb or "desconocido" in (ca, cb):
        return False
    if not (ca == cb or ca.startswith(cb) or cb.startswith(ca)):
        return False

    pa, pb = a.get("price"), b.get("price")
    if not pa or not pb:
        return False
    dprice = abs(pa - pb) / max(pa, pb)
    if dprice > 0.02:
        return False
    same_price = dprice == 0

    # Agencies count rooms differently (the Esplugues flat went out as 2 and
    # 3 hab at one price), so at an identical price one apart is allowed.
    # When the price differs too, two differences at once are more likely
    # two flats, so the rooms must agree.
    ra, rb = a.get("rooms"), b.get("rooms")
    if ra is not None and rb is not None:
        if abs(ra - rb) > (1 if same_price else 0):
            return False

    # Two named streets that differ are two flats, whatever else agrees:
    # "Calle de Besalú" and "Calle del Pistó" both asked 325.000 € in Camp
    # de l'Arpa, neither gave its surface, and the barrio-and-price rule
    # alone made them one.
    sa, sb = _street_of(a.get("title")), _street_of(b.get("title"))
    if sa and sb and not _compatible(sa, sb):
        return False

    da, db_ = _own_district(a), _own_district(b)
    both_districts = bool(da) and bool(db_)
    same_place = both_districts and (_place_matches(da, db_) or _place_matches(db_, da))
    if both_districts and not same_place:
        return False

    # Where a district is missing, what the title still says about the
    # barrio has to agree: "Can ..." is not "Centre" or "El Gall".
    ha, hb = _place_hint(a), _place_hint(b)
    if ha and hb and not _compatible(ha, hb) and not same_place:
        return False
    if ha and hb and _compatible(ha, hb):
        same_place = True

    big_city = (population_of(a.get("city")) or 0) > 200_000
    if big_city and not same_place:
        return False

    # Built and useful area differ by up to a quarter — the Esplugues flat
    # went out as 70 m² and 85 m² at the same price — so an identical price
    # allows that much. A price that also moved allows less.
    aa, ab = a.get("area_m2"), b.get("area_m2")
    if aa and ab:
        return abs(aa - ab) / max(aa, ab) <= (0.25 if same_price else 0.12)

    # One side has no surface: thinner evidence, so ask for more of it.
    return (same_place and dprice <= 0.01
            and (ra is None or rb is None or ra == rb))


def was_similar_listing_sent(city: str | None, price: float | None,
                             area: float | None, district: str | None = None,
                             rooms: int | None = None, title: str | None = None) -> bool:
    """Whether a flat that is, in all likelihood, this one has gone out.

    See same_flat for what "the same" means. Candidates are narrowed by
    price in SQL — the one field every alert carries — and judged in Python,
    where town names can be normalised.
    """
    if not city or city == "desconocido" or not price:
        return False
    me = {"city": city, "price": price, "area_m2": area,
          "district": district, "rooms": rooms, "title": title}
    with session_scope() as conn:
        rows = conn.execute(
            select(listings.c.city, listings.c.district, listings.c.price,
                   listings.c.area_m2, listings.c.rooms, listings.c.title)
            .select_from(alerts_sent.join(listings, listings.c.id == alerts_sent.c.listing_id))
            .where(listings.c.price.between(price * 0.98, price * 1.02))
        ).fetchall()
    for c, d, p, a, r, t in rows:
        if same_flat(me, {"city": c, "district": d, "price": p, "area_m2": a,
                          "rooms": r, "title": t}):
            return True
    return False


def was_flat_seen_before(listing: dict, before: datetime) -> bool:
    """Whether this flat was already on the market before `before`.

    An agency re-listing a flat, or a second agency taking it on, gives it a
    new id, and to the id check it is news. It is not: the Sant Feliu house
    and the Molins flat seen on 23 October came back on the 27th through a
    Habitaclia email under new numbers and went out as new. Any listing of
    the same flat first seen before the freshness window makes this one old.
    """
    city, price = listing.get("city"), listing.get("price")
    if not city or city == "desconocido" or not price:
        return False
    own_id = f"{listing.get('portal')}_{listing.get('external_id')}"
    if before.tzinfo is not None:
        # Stored as naive UTC
        before = before.astimezone(timezone.utc).replace(tzinfo=None)
    with session_scope() as conn:
        rows = conn.execute(
            select(listings.c.city, listings.c.district, listings.c.price,
                   listings.c.area_m2, listings.c.rooms, listings.c.title)
            .where(listings.c.price.between(price * 0.98, price * 1.02),
                   listings.c.first_seen_at < before,
                   listings.c.id != own_id)
        ).fetchall()
    for c, d, p, a, r, t in rows:
        if same_flat(listing, {"city": c, "district": d, "price": p, "area_m2": a,
                               "rooms": r, "title": t}):
            return True
    return False


def was_alert_sent_recently(listing_id: str, cooldown_hours: int = 168) -> bool:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    with session_scope() as conn:
        row = conn.execute(
            select(alerts_sent).where(
                alerts_sent.c.listing_id == listing_id,
                alerts_sent.c.sent_at > cutoff,
            )
        ).fetchone()
    return row is not None


def record_alert_sent(listing_id: str, alert_type: str, message_preview: str,
                      chat_message_id: int | None = None) -> None:
    with session_scope() as conn:
        conn.execute(
            insert(alerts_sent).values(
                listing_id=listing_id,
                alert_type=alert_type,
                sent_at=datetime.now(timezone.utc),
                message_preview=message_preview[:500],
                chat_message_id=chat_message_id,
            )
        )


# ── Feedback ────────────────────────────────────────────────────────────

def record_feedback(listing_id: str, verdict: str, note: str = "") -> None:
    """verdict: 'yes' | 'no' | 'maybe'"""
    with session_scope() as conn:
        conn.execute(
            insert(feedback).values(
                listing_id=listing_id,
                verdict=verdict,
                note=note[:1000],
                created_at=datetime.now(timezone.utc),
            )
        )


def get_feedback_summary() -> dict:
    """Return counts by verdict + listings user liked/disliked."""
    from sqlalchemy import func
    with session_scope() as conn:
        counts = dict(conn.execute(
            select(feedback.c.verdict, func.count())
            .group_by(feedback.c.verdict)
        ).fetchall())
    return counts


def get_liked_listings(verdict: str = "yes", limit: int = 20) -> list[dict]:
    """Return listings the user liked/disliked, most recent first."""
    with session_scope() as conn:
        rows = conn.execute(
            select(
                feedback.c.verdict, feedback.c.note, feedback.c.created_at,
                listings.c.url, listings.c.city, listings.c.district,
                listings.c.price, listings.c.area_m2, listings.c.rooms,
                listings.c.title,
            )
            .select_from(feedback.join(listings, feedback.c.listing_id == listings.c.id))
            .where(feedback.c.verdict == verdict)
            .order_by(feedback.c.created_at.desc())
            .limit(limit)
        ).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Telegram bookkeeping ────────────────────────────────────────────────

def get_telegram_state(key: str, default: str = "") -> str:
    with session_scope() as conn:
        row = conn.execute(
            select(telegram_state).where(telegram_state.c.key == key)
        ).fetchone()
    return row.value if row else default


def set_telegram_state(key: str, value: str) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as conn:
        existing = conn.execute(
            select(telegram_state).where(telegram_state.c.key == key)
        ).fetchone()
        if existing:
            # Rewriting the same value would only move updated_at, which makes
            # the committed SQL dump differ on every single run. The bot saves
            # state by committing that dump, so a no-op write would produce a
            # commit every few minutes and hide the runs that did something.
            if existing.value == value:
                return
            conn.execute(
                update(telegram_state).where(telegram_state.c.key == key)
                .values(value=value, updated_at=now)
            )
        else:
            conn.execute(
                insert(telegram_state).values(key=key, value=value, updated_at=now)
            )


def update_rent_zone_average(city: str, district: str | None, avg_rent_per_m2: float, sample_size: int) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as conn:
        existing = conn.execute(
            select(rent_zone_averages).where(
                rent_zone_averages.c.city == city,
                rent_zone_averages.c.district == district,
            )
        ).fetchone()
        if existing:
            conn.execute(
                update(rent_zone_averages)
                .where(rent_zone_averages.c.id == existing.id)
                .values(avg_rent_per_m2=avg_rent_per_m2, sample_size=sample_size, updated_at=now)
            )
        else:
            conn.execute(
                insert(rent_zone_averages).values(
                    city=city, district=district,
                    avg_rent_per_m2=avg_rent_per_m2,
                    sample_size=sample_size,
                    updated_at=now,
                )
            )


def get_rent_zone_average(city: str, district: str | None) -> float | None:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    with session_scope() as conn:
        row = conn.execute(
            select(rent_zone_averages).where(
                rent_zone_averages.c.city == city,
                rent_zone_averages.c.district == district,
                rent_zone_averages.c.updated_at > cutoff,
            )
        ).fetchone()
        if row:
            return row.avg_rent_per_m2
        # fallback: city-level (no district filter)
        row = conn.execute(
            select(rent_zone_averages).where(
                rent_zone_averages.c.city == city,
                rent_zone_averages.c.updated_at > cutoff,
            )
        ).fetchone()
        return row.avg_rent_per_m2 if row else None
