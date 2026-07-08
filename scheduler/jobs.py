"""Pipeline orchestration and APScheduler job definitions."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """Coordinates: scrape → DB → analysis → alerts."""

    def __init__(self, config: dict):
        from scraper.http_client import HttpClient
        from analysis.rent_estimator import RentEstimator
        from analysis.scorer import InvestmentScorer
        from analysis.residence_scorer import ResidenceScorer
        from alerts.telegram_bot import TelegramAlerter

        self.config = config
        self.mode = config.get("search_mode", "investment")
        self.http = HttpClient(config.get("scraping", {}))
        self.rent_estimator = RentEstimator(config)
        self.scorer = InvestmentScorer(config)
        self.residence_scorer = ResidenceScorer(config)
        self.alerter = TelegramAlerter(config)
        self._scrapers = self._init_scrapers()
        logger.info(f"Pipeline initialised in '{self.mode}' mode")

    def _init_scrapers(self) -> dict:
        from scraper.idealista import IdealistaScraper
        from scraper.fotocasa import FotocasaScraper
        from scraper.pisos import PisosScraper
        from scraper.habitaclia import HabitacliaScraper
        from scraper.solvia import SolviaScraper
        from scraper.servihabitat import ServihabitatScraper

        respect_robots = self.config.get("scraping", {}).get("respect_robots", True)
        return {
            "idealista": IdealistaScraper(self.http, self.config, respect_robots),
            "fotocasa": FotocasaScraper(self.http, self.config, respect_robots),
            "pisos": PisosScraper(self.http, self.config, respect_robots),
            "habitaclia": HabitacliaScraper(self.http, self.config, respect_robots),
            "solvia": SolviaScraper(self.http, self.config, respect_robots),
            "servihabitat": ServihabitatScraper(self.http, self.config, respect_robots),
        }

    def run_rental_scrape(self) -> dict:
        """Scrape rental listings, compute median rent/m² per zone, update DB."""
        from analysis.rent_aggregator import aggregate_and_store

        rental_cfg = self.config.get("rental_scraping", {})
        portals_cfg = rental_cfg.get("portals", {})

        all_listings: list[dict] = []

        for portal_name, portal_cfg in portals_cfg.items():
            if not portal_cfg.get("enabled", False):
                logger.info(f"Rental portal {portal_name} disabled, skipping")
                continue

            scraper = self._scrapers.get(portal_name)
            if not scraper:
                logger.warning(f"No scraper found for rental portal {portal_name}")
                continue

            max_pages = portal_cfg.get("max_pages", 3)
            for url in portal_cfg.get("urls", []):
                logger.info(f"Rental scrape {portal_name}: {url}")
                try:
                    for raw in scraper.search_listings(url, max_pages):
                        all_listings.append(raw.to_dict())
                except Exception as e:
                    logger.error(f"Rental scrape error {portal_name} {url}: {e}")

        stats = aggregate_and_store(all_listings)
        logger.info(f"Rental scrape complete: {stats}")
        return stats

    def run_purchase_scrape(self) -> dict:
        """Main scraping job. Returns summary stats."""
        from models.db import upsert_listing, upsert_metrics

        portals_cfg = self.config.get("portals", {})
        stats = {"new": 0, "updated": 0, "errors": 0, "total": 0, "alerts_sent": 0}

        for portal_name, portal_cfg in portals_cfg.items():
            if not portal_cfg.get("enabled", False):
                logger.info(f"Portal {portal_name} disabled, skipping")
                continue

            scraper = self._scrapers.get(portal_name)
            if not scraper:
                logger.warning(f"No scraper for {portal_name}")
                continue

            search_urls = portal_cfg.get("search_urls", [])
            max_pages = portal_cfg.get("max_pages", 3)

            for search_url in search_urls:
                logger.info(f"Scraping {portal_name}: {search_url}")
                try:
                    for raw in scraper.search_listings(search_url, max_pages):
                        stats["total"] += 1
                        try:
                            raw_dict = raw.to_dict()
                            is_new = upsert_listing(raw_dict)
                            if is_new:
                                stats["new"] += 1
                            else:
                                stats["updated"] += 1

                            # Compute metrics
                            metrics = self._compute_metrics(raw_dict)
                            if metrics:
                                listing_id = f"{raw.portal}_{raw.external_id}"
                                upsert_metrics(listing_id, metrics)

                                # Alert only for NEW listings above threshold
                                score = metrics.get("investment_score", 0) or 0
                                if is_new and self.alerter.send_alert(listing_id, raw_dict, metrics, score):
                                    stats["alerts_sent"] += 1

                        except Exception as e:
                            logger.error(f"Error processing listing {raw.url}: {e}")
                            stats["errors"] += 1

                except Exception as e:
                    logger.error(f"Error scraping {portal_name} {search_url}: {e}")
                    stats["errors"] += 1

        logger.info(f"Scrape complete: {stats}")
        return stats

    def run_daily_digest(self) -> None:
        """Send daily digest of top opportunities via Telegram."""
        logger.info("Sending daily digest...")
        self.alerter.send_daily_digest()

    def _compute_metrics(self, listing: dict) -> dict | None:
        if self.mode == "residence":
            return self._compute_residence_metrics(listing)
        return self._compute_investment_metrics(listing)

    def _compute_residence_metrics(self, listing: dict) -> dict | None:
        """Residence-mode: score against personal-home criteria."""
        bd = self.residence_scorer.score(listing)
        if not bd.hard_filters_pass:
            # Doesn't pass filters — no metrics, no alert
            return None
        return {
            "investment_score": bd.total,
            "score_breakdown": bd.to_dict(),
            "match_profile": bd.matches_profile,
        }

    def _compute_investment_metrics(self, listing: dict) -> dict | None:
        from analysis.metrics import compute_all_metrics

        price = listing.get("price")
        area = listing.get("area_m2")
        city = listing.get("city", "")
        district = listing.get("district")
        condition = listing.get("condition", "desconocido")

        if not price or not area:
            return None

        # Sanity checks: skip unrealistic values
        if price < 20_000:
            logger.debug(f"Skipping unrealistic price {price}€")
            return None
        if area < 20 or area > 2_000:
            logger.debug(f"Skipping unrealistic area {area}m²")
            return None

        # Get purchase costs for this city/region
        costs_cfg = self.config.get("analysis", {}).get("purchase_costs_pct", {})
        city_key = city.lower()
        purchase_costs_pct = costs_cfg.get(city_key, costs_cfg.get("default", 0.10))
        expense_ratio = self.config.get("analysis", {}).get("assumed_expenses_pct", 0.25)
        growth_pct = self.config.get("analysis", {}).get("capital_growth_annual_pct", 2.5)

        # Estimate rent
        monthly_rent = self.rent_estimator.estimate_monthly_rent(city, district, area)

        metrics = compute_all_metrics(
            price=price,
            area_m2=area,
            estimated_monthly_rent=monthly_rent,
            purchase_costs_pct=purchase_costs_pct,
            expense_ratio=expense_ratio,
            capital_growth_pct=growth_pct,
        )

        # Compute investment score
        zone_avg_ppm2 = self.scorer.get_zone_avg_price_per_m2(city, district)
        breakdown = self.scorer.score(
            city=city,
            district=district,
            condition=condition,
            gross_yield_pct=metrics.get("gross_yield_pct"),
            net_yield_pct=metrics.get("net_yield_pct"),
            listing_price_per_m2=metrics.get("price_per_m2"),
            zone_avg_price_per_m2=zone_avg_ppm2,
        )

        metrics["investment_score"] = breakdown.total
        metrics["score_breakdown"] = breakdown.to_dict()

        return metrics


def create_scheduler(config: dict):
    """Create and configure APScheduler with all jobs."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    orchestrator = PipelineOrchestrator(config)
    scheduler = BlockingScheduler(timezone="Europe/Madrid")

    scrape_hours = config.get("scheduler", {}).get("scrape_interval_hours", 12)
    rental_days = config.get("scheduler", {}).get("rental_scrape_interval_days", 7)

    scheduler.add_job(
        func=orchestrator.run_purchase_scrape,
        trigger="interval",
        hours=scrape_hours,
        id="purchase_scrape",
        max_instances=1,
        coalesce=True,
        name="Purchase listings scrape",
    )

    scheduler.add_job(
        func=orchestrator.run_rental_scrape,
        trigger="interval",
        days=rental_days,
        id="rental_scrape",
        max_instances=1,
        coalesce=True,
        name="Rental listings scrape",
    )

    # Daily digest: every morning at 9:00 (Spain time)
    scheduler.add_job(
        func=orchestrator.run_daily_digest,
        trigger="cron",
        hour=9,
        minute=0,
        id="daily_digest",
        max_instances=1,
        name="Daily Telegram digest",
    )

    logger.info(f"Scheduler configured: purchase every {scrape_hours}h, rental every {rental_days}d, digest daily at 09:00")
    return scheduler, orchestrator
