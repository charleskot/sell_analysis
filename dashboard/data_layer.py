"""DB queries optimized for Streamlit dashboard."""
import logging
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)


def _get_engine():
    from models.db import get_engine
    return get_engine()


@st.cache_data(ttl=300)
def get_ranked_listings(
    cities: list | None = None,
    portals: list | None = None,
    price_min: float = 0,
    price_max: float = 2_000_000,
    min_yield_pct: float = 0,
    min_score: float = 0,
    min_m2: float = 0,
    max_m2: float = 500,
    min_rooms: int = 0,
    conditions: list | None = None,
) -> pd.DataFrame:
    from sqlalchemy import select, and_, or_
    from models.schema import listings, investment_metrics

    try:
        engine = _get_engine()
        with engine.connect() as conn:
            query = (
                select(
                    listings.c.id,
                    listings.c.portal,
                    listings.c.url,
                    listings.c.title,
                    listings.c.price,
                    listings.c.area_m2,
                    listings.c.price_per_m2,
                    listings.c.rooms,
                    listings.c.bathrooms,
                    listings.c.floor,
                    listings.c.condition,
                    listings.c.city,
                    listings.c.district,
                    listings.c.zip_code,
                    listings.c.latitude,
                    listings.c.longitude,
                    listings.c.photo_urls,
                    listings.c.first_seen_at,
                    listings.c.last_seen_at,
                    listings.c.price_history,
                    investment_metrics.c.gross_yield_pct,
                    investment_metrics.c.net_yield_pct,
                    investment_metrics.c.roi_pct,
                    investment_metrics.c.payback_years,
                    investment_metrics.c.estimated_monthly_rent,
                    investment_metrics.c.total_investment,
                    investment_metrics.c.investment_score,
                    investment_metrics.c.score_breakdown,
                )
                .join(investment_metrics, listings.c.id == investment_metrics.c.listing_id, isouter=True)
                .where(listings.c.is_active == True)
                .where(listings.c.price >= price_min)
                .where(listings.c.price <= price_max)
                .order_by(investment_metrics.c.investment_score.desc().nulls_last())
            )

            if cities:
                query = query.where(listings.c.city.in_([c.lower() for c in cities]))
            if portals:
                query = query.where(listings.c.portal.in_(portals))
            if min_yield_pct > 0:
                query = query.where(investment_metrics.c.gross_yield_pct >= min_yield_pct)
            if min_score > 0:
                query = query.where(investment_metrics.c.investment_score >= min_score)
            if min_m2 > 0:
                query = query.where(listings.c.area_m2 >= min_m2)
            if max_m2 < 500:
                query = query.where(listings.c.area_m2 <= max_m2)
            if min_rooms > 0:
                query = query.where(listings.c.rooms >= min_rooms)
            if conditions:
                query = query.where(listings.c.condition.in_(conditions))

            rows = conn.execute(query).fetchall()
            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=[
                "id", "portal", "url", "title", "price", "area_m2", "price_per_m2",
                "rooms", "bathrooms", "floor", "condition", "city", "district",
                "zip_code", "latitude", "longitude", "photo_urls",
                "first_seen_at", "last_seen_at", "price_history",
                "gross_yield_pct", "net_yield_pct", "roi_pct", "payback_years",
                "estimated_monthly_rent", "total_investment", "investment_score", "score_breakdown"
            ])
            return df

    except Exception as e:
        logger.error(f"Error fetching ranked listings: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=3600)
def get_available_cities() -> list[str]:
    try:
        from sqlalchemy import select, distinct
        from models.schema import listings
        engine = _get_engine()
        with engine.connect() as conn:
            rows = conn.execute(select(distinct(listings.c.city)).where(listings.c.city.isnot(None))).fetchall()
            return sorted([r[0].title() for r in rows if r[0]])
    except Exception:
        return []


@st.cache_data(ttl=3600)
def get_zone_summary() -> pd.DataFrame:
    try:
        from sqlalchemy import select, func
        from models.schema import listings, investment_metrics
        engine = _get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    listings.c.city,
                    listings.c.district,
                    func.count(listings.c.id).label("count"),
                    func.avg(listings.c.price_per_m2).label("avg_price_per_m2"),
                    func.avg(investment_metrics.c.gross_yield_pct).label("avg_yield"),
                    func.avg(investment_metrics.c.investment_score).label("avg_score"),
                )
                .join(investment_metrics, listings.c.id == investment_metrics.c.listing_id, isouter=True)
                .where(listings.c.is_active == True)
                .group_by(listings.c.city, listings.c.district)
            ).fetchall()
            if not rows:
                return pd.DataFrame()
            return pd.DataFrame(rows, columns=["city", "district", "count", "avg_price_per_m2", "avg_yield", "avg_score"])
    except Exception as e:
        logger.error(f"Error fetching zone summary: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def get_stats_summary() -> dict:
    try:
        from sqlalchemy import select, func
        from models.schema import listings, investment_metrics
        engine = _get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                select(
                    func.count(listings.c.id).label("total"),
                    func.avg(listings.c.price).label("avg_price"),
                    func.avg(investment_metrics.c.gross_yield_pct).label("avg_yield"),
                    func.avg(investment_metrics.c.investment_score).label("avg_score"),
                )
                .join(investment_metrics, listings.c.id == investment_metrics.c.listing_id, isouter=True)
                .where(listings.c.is_active == True)
            ).fetchone()
            if row:
                return {
                    "total": int(row[0] or 0),
                    "avg_price": float(row[1] or 0),
                    "avg_yield": float(row[2] or 0),
                    "avg_score": float(row[3] or 0),
                }
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
    return {"total": 0, "avg_price": 0, "avg_yield": 0, "avg_score": 0}
