"""SQLAlchemy Core table definitions."""
from sqlalchemy import (
    MetaData, Table, Column,
    String, Float, Integer, Boolean, DateTime, Text, JSON
)
from datetime import datetime, timezone

metadata = MetaData()


listings = Table(
    "listings",
    metadata,
    Column("id", String, primary_key=True),           # portal_externalid
    Column("portal", String, nullable=False),
    Column("external_id", String, nullable=False),
    Column("url", String, nullable=False),
    Column("title", Text, default=""),
    Column("price", Float, nullable=True),
    Column("area_m2", Float, nullable=True),
    Column("price_per_m2", Float, nullable=True),
    Column("rooms", Integer, nullable=True),
    Column("bathrooms", Integer, nullable=True),
    Column("floor", String, nullable=True),
    Column("condition", String, nullable=True),        # nuevo, buen_estado, reformar
    Column("latitude", Float, nullable=True),
    Column("longitude", Float, nullable=True),
    Column("district", String, nullable=True),
    Column("city", String, nullable=True),
    Column("zip_code", String, nullable=True),
    Column("description", Text, default=""),
    Column("photo_urls", JSON, default=list),
    Column("raw_html_hash", String, nullable=True),
    Column("price_history", JSON, default=list),       # [{price, date}, ...]
    Column("first_seen_at", DateTime, default=lambda: datetime.now(timezone.utc)),
    Column("last_seen_at", DateTime, default=lambda: datetime.now(timezone.utc)),
    Column("is_active", Boolean, default=True),
)

rent_zone_averages = Table(
    "rent_zone_averages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("district", String, nullable=True),
    Column("city", String, nullable=False),
    Column("avg_rent_per_m2", Float, nullable=False),
    Column("sample_size", Integer, default=0),
    Column("updated_at", DateTime, default=lambda: datetime.now(timezone.utc)),
)

investment_metrics = Table(
    "investment_metrics",
    metadata,
    Column("listing_id", String, primary_key=True),
    Column("gross_yield_pct", Float, nullable=True),
    Column("net_yield_pct", Float, nullable=True),
    Column("roi_pct", Float, nullable=True),
    Column("payback_years", Float, nullable=True),
    Column("estimated_monthly_rent", Float, nullable=True),
    Column("total_investment", Float, nullable=True),
    Column("investment_score", Float, nullable=True),
    Column("score_breakdown", JSON, default=dict),
    Column("computed_at", DateTime, default=lambda: datetime.now(timezone.utc)),
)

alerts_sent = Table(
    "alerts_sent",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("listing_id", String, nullable=False),
    Column("alert_type", String, default="telegram"),
    Column("sent_at", DateTime, default=lambda: datetime.now(timezone.utc)),
    Column("message_preview", Text, default=""),
)
