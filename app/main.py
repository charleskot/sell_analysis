from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from sqlalchemy import select, desc
from starlette.responses import HTMLResponse

from app import models
from app.db import init_db, session_scope
from app.notifier import render_email
from app.pipeline import run_daily
from app.scheduler import build_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = build_scheduler()
    scheduler.start()
    log.info("App iniciada con scheduler")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        log.info("App detenida")


app = FastAPI(title="Auction Analyzer", lifespan=lifespan)


@app.get("/")
def root():
    return {
        "service": "auction-analyzer",
        "endpoints": [
            "GET /health",
            "GET /auctions",
            "GET /auctions/{id}",
            "GET /reports/latest",
            "POST /run",
        ],
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/auctions")
def list_auctions(
    source: Optional[str] = None,
    province: Optional[str] = None,
    min_score: Optional[int] = Query(None, ge=0, le=100),
    limit: int = Query(50, le=500),
):
    with session_scope() as s:
        q = select(models.Auction)
        if source:
            q = q.where(models.Auction.source == source)
        if province:
            q = q.where(models.Auction.province == province)
        q = q.order_by(desc(models.Auction.discovered_at)).limit(limit)
        rows = s.scalars(q).all()

        out = []
        for a in rows:
            analysis = s.scalar(
                select(models.Analysis)
                .where(models.Analysis.auction_id == a.id)
                .order_by(desc(models.Analysis.created_at))
                .limit(1)
            )
            if min_score and (not analysis or (analysis.opportunity_score or 0) < min_score):
                continue
            out.append({
                "id": a.id,
                "source": a.source,
                "url": a.url,
                "title": a.title,
                "city": a.city,
                "province": a.province,
                "starting_price": a.starting_price,
                "score": analysis.opportunity_score if analysis else None,
                "recommendation": analysis.recommendation if analysis else None,
                "summary": analysis.summary if analysis else None,
            })
        return out


@app.get("/auctions/{auction_id}")
def get_auction(auction_id: int):
    with session_scope() as s:
        a = s.get(models.Auction, auction_id)
        if not a:
            raise HTTPException(404)
        analysis = s.scalar(
            select(models.Analysis)
            .where(models.Analysis.auction_id == a.id)
            .order_by(desc(models.Analysis.created_at))
            .limit(1)
        )
        return {
            "auction": {c.name: getattr(a, c.name) for c in a.__table__.columns},
            "analysis": {c.name: getattr(analysis, c.name) for c in analysis.__table__.columns} if analysis else None,
        }


@app.get("/reports/latest")
def latest_report():
    with session_scope() as s:
        report = s.scalar(select(models.DailyReport).order_by(desc(models.DailyReport.run_date)).limit(1))
        if not report:
            return {"message": "No reports yet"}
        return {c.name: getattr(report, c.name) for c in report.__table__.columns}


@app.get("/preview", response_class=HTMLResponse)
def preview_email(min_score: int = Query(0, ge=0, le=100), limit: int = Query(20, le=100)):
    """Render the email template against the most recent analyses for a quick visual check."""
    with session_scope() as s:
        analyses = s.scalars(
            select(models.Analysis)
            .where(models.Analysis.opportunity_score >= min_score)
            .order_by(desc(models.Analysis.created_at))
            .limit(limit)
        ).all()

        opps = []
        from app.scrapers.base import AuctionItem
        from app.analyzer import OpportunityAnalysis

        for an in analyses:
            a = s.get(models.Auction, an.auction_id)
            if not a:
                continue
            opps.append({
                "auction": AuctionItem(
                    source=a.source, external_id=a.external_id, url=a.url,
                    title=a.title, address=a.address, city=a.city, province=a.province,
                    starting_price=a.starting_price, surface_m2=a.surface_m2,
                    auction_end=a.auction_end,
                ),
                "analysis": OpportunityAnalysis(
                    opportunity_score=an.opportunity_score or 0,
                    estimated_market_value_eur=an.estimated_market_value,
                    estimated_discount_pct=an.estimated_discount_pct,
                    estimated_rental_yield_pct=an.estimated_rental_yield_pct,
                    estimated_flip_roi_pct=an.estimated_flip_roi_pct,
                    summary=an.summary or "",
                    pros=(an.pros or "").split("\n") if an.pros else [],
                    cons=(an.cons or "").split("\n") if an.cons else [],
                    legal_risks=(an.legal_risks or "").split("\n") if an.legal_risks else [],
                    recommendation=an.recommendation or "investigar",
                ),
            })

    from datetime import date
    return render_email(opportunities=opps, stats={"scraped": "?", "new_count": "?", "analyzed": len(opps)}, run_date=date.today().isoformat())


@app.post("/run", status_code=202)
def trigger_run(background_tasks: BackgroundTasks):
    """Trigger the daily pipeline manually. Runs in the background."""
    background_tasks.add_task(run_daily)
    return {"status": "scheduled", "message": "Daily pipeline launched"}
