"""Database engine, session management, and upsert helpers."""
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select, update, insert
from sqlalchemy.engine import Engine

from models.schema import metadata, listings, investment_metrics, alerts_sent, rent_zone_averages

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


def upsert_listing(raw: dict) -> bool:
    """Insert or update a listing. Returns True if new, False if updated."""
    listing_id = f"{raw['portal']}_{raw['external_id']}"
    now = datetime.now(timezone.utc)

    with session_scope() as conn:
        existing = conn.execute(
            select(listings).where(listings.c.id == listing_id)
        ).fetchone()

        if existing:
            history = existing.price_history or []
            if existing.price != raw.get("price") and existing.price is not None:
                history.append({"price": existing.price, "date": existing.last_seen_at.isoformat() if existing.last_seen_at else now.isoformat()})

            area = raw.get("area_m2")
            price = raw.get("price")
            ppm2 = (price / area) if price and area and area > 0 else None

            conn.execute(
                update(listings).where(listings.c.id == listing_id).values(
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


def record_alert_sent(listing_id: str, alert_type: str, message_preview: str) -> None:
    with session_scope() as conn:
        conn.execute(
            insert(alerts_sent).values(
                listing_id=listing_id,
                alert_type=alert_type,
                sent_at=datetime.now(timezone.utc),
                message_preview=message_preview[:500],
            )
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
