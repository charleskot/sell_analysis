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
        from analysis.profiles import ProfileMatcher
        from alerts.telegram_bot import TelegramAlerter

        self.config = config
        self.mode = config.get("search_mode", "investment")
        self.http = HttpClient(config.get("scraping", {}))
        self.rent_estimator = RentEstimator(config)
        self.scorer = InvestmentScorer(config)
        self.residence_scorer = ResidenceScorer(config)
        self.profile_matcher = ProfileMatcher(config)
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
        from models.db import upsert_listing, upsert_metrics, get_engine
        from models.schema import listings as listings_tbl
        from sqlalchemy import select, func

        portals_cfg = self.config.get("portals", {})
        max_age_hours = self.config.get("common_requirements", {}).get("max_published_ago_hours", 48)

        stats = {"new": 0, "updated": 0, "errors": 0, "total": 0, "alerts_sent": 0,
                 "too_old": 0, "by_portal": {}}

        # Cold start detection: if DB is empty at the start of this scrape,
        # DON'T send individual alerts — the whole catalogue looks 'new' but
        # isn't. Just index everything. Next scrape (12h later) will alert
        # only listings that are actually new since this run.
        with get_engine().connect() as conn:
            initial_count = conn.execute(select(func.count()).select_from(listings_tbl)).scalar() or 0
        cold_start = initial_count == 0
        if cold_start:
            logger.warning(
                "COLD START: DB is empty. Indexing only, alerts suppressed for this run. "
                "Next scrape (12h) will alert on truly new listings."
            )
        stats["cold_start"] = cold_start

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

            portal_stats = stats["by_portal"].setdefault(portal_name, {"scraped": 0, "kept": 0, "too_old": 0})

            for search_url in search_urls:
                logger.info(f"Scraping {portal_name}: {search_url}")
                try:
                    for raw in scraper.search_listings(search_url, max_pages):
                        stats["total"] += 1
                        portal_stats["scraped"] += 1
                        try:
                            raw_dict = raw.to_dict()

                            # ── Filter by publication age ──────────────────
                            age_h = raw_dict.get("published_ago_hours")
                            if age_h is not None and age_h > max_age_hours:
                                stats["too_old"] += 1
                                portal_stats["too_old"] += 1
                                continue

                            portal_stats["kept"] += 1
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

                                # Alert only for NEW listings above threshold.
                                # In cold-start mode (empty DB at scrape start), suppress all alerts.
                                score = metrics.get("investment_score", 0) or 0
                                if not cold_start and is_new and self.alerter.send_alert(listing_id, raw_dict, metrics, score):
                                    stats["alerts_sent"] += 1

                        except Exception as e:
                            logger.error(f"Error processing listing {raw.url}: {e}")
                            stats["errors"] += 1

                except Exception as e:
                    logger.error(f"Error scraping {portal_name} {search_url}: {e}")
                    stats["errors"] += 1

        logger.info(f"Scrape complete: {stats}")
        # Explicit per-portal summary — spot dead portals immediately
        for pname, pstats in stats["by_portal"].items():
            status = "✅" if pstats["scraped"] > 0 else "❌"
            logger.info(
                f"  {status} {pname}: scraped={pstats['scraped']}, "
                f"kept={pstats['kept']}, filtered_too_old={pstats['too_old']}"
            )

        # After cold start, send a single friendly Telegram message summarising
        # what was indexed. From next scrape onwards, individual alerts fire.
        if cold_start and stats["new"] > 0 and self.alerter.enabled:
            try:
                por_portal = " · ".join(
                    f"{p}={s['kept']}" for p, s in stats["by_portal"].items() if s["scraped"] > 0
                )
                msg = (
                    f"🏠 *Bot inicializado*\n\n"
                    f"Indexadas *{stats['new']}* viviendas de últimas 48h.\n"
                    f"Portales: {por_portal}\n"
                    f"Descartadas por antigüedad: {stats['too_old']}\n\n"
                    f"A partir del próximo scrape (12h) solo te aviso de "
                    f"viviendas *realmente nuevas* que cumplan filtros."
                )
                self.alerter._send_sync(msg)
            except Exception as e:
                logger.warning(f"Cold-start summary send failed: {e}")

        return stats

    def run_email_ingest(self) -> dict:
        """Read portal alert emails, score them, alert on the good ones.

        This replaces scraping as the primary acquisition path: alert emails
        arrive pushed, carry the same fields, and cannot be blocked.
        """
        from ingest.email_parsers import parse_email
        from models.db import upsert_listing, upsert_metrics

        stats = {"emails": 0, "parsed": 0, "new": 0, "updated": 0,
                 "alerts_sent": 0, "filtered_out": 0, "errors": 0,
                 "by_portal": {}, "by_profile": {}}

        reader = self._get_mail_reader()
        if reader is None:
            return stats

        try:
            messages = reader.fetch_new() if hasattr(reader, "fetch_new") else reader.fetch_unseen()
        except Exception as e:
            logger.error(f"Email fetch failed: {e}")
            stats["errors"] += 1
            return stats

        stats["emails"] = len(messages)

        for msg in messages:
            try:
                listings = parse_email(msg.sender, msg.subject, msg.html, msg.text)
            except Exception as e:
                logger.error(f"Parse failed for {msg.subject[:50]!r}: {e}")
                stats["errors"] += 1
                continue

            for raw in listings:
                stats["parsed"] += 1
                portal_stats = stats["by_portal"].setdefault(
                    raw.portal, {"parsed": 0, "alerts": 0}
                )
                portal_stats["parsed"] += 1

                try:
                    raw_dict = raw.to_dict()
                    is_new = upsert_listing(raw_dict)
                    stats["new" if is_new else "updated"] += 1

                    metrics = self._compute_metrics(raw_dict)
                    if not metrics:
                        continue

                    listing_id = f"{raw.portal}_{raw.external_id}"
                    upsert_metrics(listing_id, metrics)

                    if not is_new:
                        continue

                    hits = self.profile_matcher.match(raw_dict, metrics)
                    if not hits:
                        stats["filtered_out"] += 1
                        continue

                    # A listing can satisfy more than one search; name them all
                    # so the alert says why it is being shown.
                    labels = " + ".join(p.label for p, _ in hits)
                    reason = hits[0][1]
                    for profile, _ in hits:
                        stats["by_profile"][profile.name] = (
                            stats["by_profile"].get(profile.name, 0) + 1
                        )

                    metrics = {**metrics, "matched_profiles": labels}

                    score = metrics.get("investment_score", 0) or 0
                    dedup_key = self.alerter.property_signature(raw_dict)
                    if self.alerter.send_alert(
                        listing_id, raw_dict, metrics, score, dedup_key=dedup_key
                    ):
                        stats["alerts_sent"] += 1
                        portal_stats["alerts"] += 1
                        logger.info(f"Alerta {listing_id} [{labels}]: {reason}")

                except Exception as e:
                    logger.error(f"Error processing {raw.url}: {e}")
                    stats["errors"] += 1

        logger.info(f"Email ingest complete: {stats}")
        for portal, pstats in stats["by_portal"].items():
            logger.info(f"  📧 {portal}: {pstats['parsed']} anuncios, {pstats['alerts']} alertas")

        return stats

    def _get_mail_reader(self):
        """Pick the configured mail backend. Cached after first call."""
        if hasattr(self, "_mail_reader"):
            return self._mail_reader

        backend = (self.config.get("email_ingest", {}) or {}).get("backend", "gmail_api")

        if backend == "imap":
            from ingest.mailbox import Mailbox
            reader = Mailbox(self.config)
        else:
            from ingest.gmail_api import GmailReader
            reader = GmailReader(self.config)

        self._mail_reader = reader if reader.enabled else None
        return self._mail_reader

    def run_daily_digest(self) -> None:
        """Send daily digest of top opportunities via Telegram."""
        logger.info("Sending daily digest...")
        self.alerter.send_daily_digest()

    def run_feedback_poll(self) -> None:
        """Poll Telegram for button clicks / text replies from the user."""
        try:
            self.alerter.poll_feedback()
        except Exception as e:
            logger.error(f"Feedback poll failed: {e}")

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

        # €/m² sanity band. Rent is estimated as €/m² × area, so an area that
        # is actually the plot (masías, casas con terreno) or a parsing slip
        # produces a fictitious rent and a spectacular fake yield — exactly
        # the listings that would float to the top of the ranking.
        #
        # A genuine home in the Barcelona metro area does not trade below
        # ~800 €/m². Anything under that means the area is not the built area,
        # so the rent estimate cannot be trusted and neither can the yield.
        sanity = self.config.get("analysis", {}).get("sanity", {})
        min_ppm2 = sanity.get("min_price_per_m2", 800)
        max_ppm2 = sanity.get("max_price_per_m2", 15_000)
        ppm2 = price / area
        if ppm2 < min_ppm2 or ppm2 > max_ppm2:
            logger.info(
                f"Skipping implausible €/m² ({ppm2:,.0f}) for {price:,.0f}€ / {area:.0f}m² "
                f"— area is probably the plot, not the built area"
            )
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
            financing=self.config.get("financing"),
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

    sched_cfg = config.get("scheduler", {})
    scrape_hours = sched_cfg.get("scrape_interval_hours", 12)
    rental_days = sched_cfg.get("rental_scrape_interval_days", 7)
    mail_minutes = sched_cfg.get("email_poll_minutes", 10)

    # Primary acquisition path: portal alert emails. Runs frequently because
    # a good deal in Barcelona is gone in hours, and polling email is cheap.
    if config.get("email_ingest", {}).get("enabled", True):
        scheduler.add_job(
            func=orchestrator.run_email_ingest,
            trigger="interval",
            minutes=mail_minutes,
            id="email_ingest",
            max_instances=1,
            coalesce=True,
            name="Portal alert email ingest",
        )

    # Secondary: scraping, for whatever portals still let us in.
    if config.get("scraping", {}).get("enabled", True):
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

    # Feedback polling: every 2 minutes (records button clicks and replies)
    scheduler.add_job(
        func=orchestrator.run_feedback_poll,
        trigger="interval",
        minutes=2,
        id="feedback_poll",
        max_instances=1,
        coalesce=True,
        name="Telegram feedback poll",
    )

    logger.info(
        f"Scheduler configured: purchase every {scrape_hours}h, rental every {rental_days}d, "
        f"digest daily at 09:00, feedback poll every 2min"
    )
    return scheduler, orchestrator
