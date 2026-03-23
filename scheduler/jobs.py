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
        from alerts.telegram_bot import TelegramAlerter

        self.config = config
        self.http = HttpClient(config.get("scraping", {}))
        self.rent_estimator = RentEstimator(config)
        self.scorer = InvestmentScorer(config)
        self.alerter = TelegramAlerter(config)
        self._scrapers = self._init_scrapers()

    def _init_scrapers(self) -> dict:
        from scraper.idealista import IdealistaScraper
        from scraper.fotocasa import FotocasaScraper
        from scraper.pisos import PisosScraper
        from scraper.habitaclia import HabitacliaScraper
        from scraper.solvia import SolviaScraper

        respect_robots = self.config.get("scraping", {}).get("respect_robots", True)
        return {
            "idealista": IdealistaScraper(self.http, self.config, respect_robots),
            "fotocasa": FotocasaScraper(self.http, self.config, respect_robots),
            "pisos": PisosScraper(self.http, self.config, respect_robots),
            "habitaclia": HabitacliaScraper(self.http, self.config, respect_robots),
            "solvia": SolviaScraper(self.http, self.config, respect_robots),
        }

    def run_purchase_scrape(self) -> dict:
        """Main scraping job. Returns summary stats."""
        from models.db import upsert_listing, upsert_metrics

        portals_cfg = self.config.get("portals", {})
        stats = {"new": 0, "updated": 0, "errors": 0, "total": 0}

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

                                # Check alert threshold
                                score = metrics.get("investment_score", 0) or 0
                                self.alerter.send_alert(listing_id, raw_dict, metrics, score)

                        except Exception as e:
                            logger.error(f"Error processing listing {raw.url}: {e}")
                            stats["errors"] += 1

                except Exception as e:
                    logger.error(f"Error scraping {portal_name} {search_url}: {e}")
                    stats["errors"] += 1

        logger.info(f"Scrape complete: {stats}")
        return stats

    def _compute_metrics(self, listing: dict) -> dict | None:
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

    logger.info(f"Scheduler configured: purchase scrape every {scrape_hours}h")
    return scheduler, orchestrator
