from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Float, Integer, String, Text, DateTime, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Auction(Base):
    __tablename__ = "auctions"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(String(1024))

    title: Mapped[Optional[str]] = mapped_column(String(512))
    address: Mapped[Optional[str]] = mapped_column(String(512))
    city: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    province: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(16))
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)

    property_type: Mapped[Optional[str]] = mapped_column(String(64))
    surface_m2: Mapped[Optional[float]] = mapped_column(Float)
    rooms: Mapped[Optional[int]] = mapped_column(Integer)
    year_built: Mapped[Optional[int]] = mapped_column(Integer)

    starting_price: Mapped[Optional[float]] = mapped_column(Float)
    appraised_value: Mapped[Optional[float]] = mapped_column(Float)
    minimum_deposit: Mapped[Optional[float]] = mapped_column(Float)

    auction_start: Mapped[Optional[datetime]] = mapped_column(DateTime)
    auction_end: Mapped[Optional[datetime]] = mapped_column(DateTime)

    has_charges: Mapped[Optional[bool]] = mapped_column(Boolean)
    charges_description: Mapped[Optional[str]] = mapped_column(Text)
    occupancy_status: Mapped[Optional[str]] = mapped_column(String(64))
    raw_description: Mapped[Optional[str]] = mapped_column(Text)

    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auction_id: Mapped[int] = mapped_column(Integer, index=True)
    model: Mapped[str] = mapped_column(String(64))

    opportunity_score: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    estimated_market_value: Mapped[Optional[float]] = mapped_column(Float)
    estimated_discount_pct: Mapped[Optional[float]] = mapped_column(Float)
    estimated_rental_yield_pct: Mapped[Optional[float]] = mapped_column(Float)
    estimated_flip_roi_pct: Mapped[Optional[float]] = mapped_column(Float)

    summary: Mapped[Optional[str]] = mapped_column(Text)
    pros: Mapped[Optional[str]] = mapped_column(Text)
    cons: Mapped[Optional[str]] = mapped_column(Text)
    legal_risks: Mapped[Optional[str]] = mapped_column(Text)
    recommendation: Mapped[Optional[str]] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    auctions_seen: Mapped[int] = mapped_column(Integer, default=0)
    new_auctions: Mapped[int] = mapped_column(Integer, default=0)
    analyzed: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
