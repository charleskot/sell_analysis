from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import List

from sqlalchemy import select

from app import models
from app import telegram
from app.analyzer import OpportunityAnalysis, analyze
from app.config import settings
from app.db import session_scope, init_db
from app.scrapers import get_scrapers
from app.scrapers.base import AuctionItem

log = logging.getLogger(__name__)


@dataclass
class Opportunity:
    auction: AuctionItem
    analysis: OpportunityAnalysis


def scrape_all() -> List[AuctionItem]:
    items: List[AuctionItem] = []
    for scraper in get_scrapers(settings.enabled_scrapers):
        log.info("Ejecutando scraper: %s", scraper.name)
        try:
            with scraper:
                source_items = scraper.fetch(settings.target_provinces)
                log.info("%s: %d items", scraper.name, len(source_items))
                items.extend(source_items)
        except Exception as e:
            log.exception("Scraper %s falló: %s", scraper.name, e)
    return items


def upsert_auctions(items: List[AuctionItem]) -> tuple[List[models.Auction], int]:
    """Insert new auctions, refresh `last_seen_at` on known ones.
    Returns (newly_inserted_records, total_seen)."""
    new_records: List[models.Auction] = []
    with session_scope() as s:
        for item in items:
            existing = s.scalar(
                select(models.Auction).where(
                    models.Auction.source == item.source,
                    models.Auction.external_id == item.external_id,
                )
            )
            if existing:
                existing.last_seen_at = datetime.utcnow()
                continue

            row = models.Auction(
                source=item.source,
                external_id=item.external_id,
                url=item.url,
                title=item.title,
                address=item.address,
                city=item.city,
                province=item.province,
                postal_code=item.postal_code,
                latitude=item.latitude,
                longitude=item.longitude,
                property_type=item.property_type,
                surface_m2=item.surface_m2,
                rooms=item.rooms,
                year_built=item.year_built,
                starting_price=item.starting_price,
                appraised_value=item.appraised_value,
                minimum_deposit=item.minimum_deposit,
                auction_start=item.auction_start,
                auction_end=item.auction_end,
                has_charges=item.has_charges,
                charges_description=item.charges_description,
                occupancy_status=item.occupancy_status,
                raw_description=item.raw_description,
            )
            s.add(row)
            new_records.append(row)
        s.flush()
    return new_records, len(items)


def analyze_new(items: List[AuctionItem]) -> List[Opportunity]:
    """Run LLM analysis on the items that look worth it (have a price)."""
    opportunities: List[Opportunity] = []
    with session_scope() as s:
        for item in items:
            if not item.starting_price:
                continue
            if settings.max_price_eur and item.starting_price > settings.max_price_eur:
                continue
            try:
                analysis = analyze(item)
            except Exception as e:
                log.warning("Análisis falló para %s/%s: %s", item.source, item.external_id, e)
                continue

            auction_row = s.scalar(
                select(models.Auction).where(
                    models.Auction.source == item.source,
                    models.Auction.external_id == item.external_id,
                )
            )
            if not auction_row:
                continue

            row = models.Analysis(
                auction_id=auction_row.id,
                model=settings.analysis_model,
                opportunity_score=analysis.opportunity_score,
                estimated_market_value=analysis.estimated_market_value_eur,
                estimated_discount_pct=analysis.estimated_discount_pct,
                estimated_rental_yield_pct=analysis.estimated_rental_yield_pct,
                estimated_flip_roi_pct=analysis.estimated_flip_roi_pct,
                summary=analysis.summary,
                pros="\n".join(analysis.pros),
                cons="\n".join(analysis.cons),
                legal_risks="\n".join(analysis.legal_risks),
                recommendation=analysis.recommendation,
            )
            s.add(row)
            opportunities.append(Opportunity(auction=item, analysis=analysis))
    return opportunities


def run_daily() -> dict:
    """End-to-end daily pipeline: scrape → upsert → analyze new → email."""
    init_db()
    log.info("Inicio pipeline diario")

    items = scrape_all()
    new_records, total_seen = upsert_auctions(items)

    new_external_ids = {(r.source, r.external_id) for r in new_records}
    new_items = [it for it in items if (it.source, it.external_id) in new_external_ids]
    log.info("Nuevas subastas para analizar: %d / %d", len(new_items), len(items))

    opportunities = analyze_new(new_items)
    opportunities = [o for o in opportunities if o.analysis.opportunity_score >= settings.min_opportunity_score]
    opportunities.sort(key=lambda o: o.analysis.opportunity_score, reverse=True)

    stats = {"scraped": total_seen, "new_count": len(new_records), "analyzed": len(opportunities)}
    run_date = date.today().isoformat()

    payload = [{"auction": o.auction, "analysis": o.analysis} for o in opportunities]
    sent_ok = True
    error_msg = None
    try:
        telegram.notify(opportunities=payload, stats=stats, run_date=run_date)
    except Exception as e:
        log.exception("Telegram falló: %s", e)
        sent_ok = False
        error_msg = str(e)

    with session_scope() as s:
        s.add(models.DailyReport(
            run_date=datetime.utcnow(),
            auctions_seen=total_seen,
            new_auctions=len(new_records),
            analyzed=len(opportunities),
            sent=sent_ok,
            error=error_msg,
        ))

    log.info("Pipeline diario terminado: %s", stats)
    return {**stats, "sent": sent_ok}
