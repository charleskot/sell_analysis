"""Database engine, session management, and upsert helpers."""
import json
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
                    first_seen_at=now,
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


def was_similar_listing_sent(city: str | None, price: float | None,
                             area: float | None) -> bool:
    """Whether a flat this similar has already gone out.

    The exact fingerprint (city:price:area:rooms) misses the commonest
    duplicate: the same flat listed by several agencies, each measuring it
    their own way. Esplugues at 270.000 € went out as 70 m²/2 hab, then
    85 m²/3 hab, then 85 m²/2 hab — one flat, three cards. Identical asking
    price in the same town with area within 25% is that flat again; two
    genuinely different flats colliding on all three at once is rare enough
    to be worth the trade.
    """
    if not city or city == "desconocido" or not price or not area:
        return False
    with session_scope() as conn:
        row = conn.execute(
            select(alerts_sent.c.id)
            .select_from(alerts_sent.join(listings, listings.c.id == alerts_sent.c.listing_id))
            .where(
                listings.c.city == city,
                listings.c.price == price,
                listings.c.area_m2.between(area * 0.75, area * 1.25),
            )
            .limit(1)
        ).fetchone()
    return row is not None


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
