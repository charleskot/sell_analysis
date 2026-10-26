"""Pipeline orchestration and APScheduler job definitions."""
import logging
from datetime import datetime, timezone

from analysis.profiles import NEAR_MISS_PREFIX

logger = logging.getLogger(__name__)

DIGEST_HOUR_UTC = 7  # 09:00 Madrid in summer, 08:00 in winter

# Does the advert say anything at all about the state of the flat?
#
# Bare "nuevo" is deliberately absent. Habitaclia labels every card "Anuncio
# nuevo" and Idealista's subjects read "Nuevo piso en tu búsqueda", so the
# word appears on listings nobody described at all. A false "I know the
# condition" is worse than silence: it skips reading the page, and then
# require_verified_condition passes something that was never verified. A
# 130 m² flat in Sabadell at 176.026 €, sold by "transmisión directa",
# reached the user exactly that way.
import re as _re_mod

CONDITION_WORDS = _re_mod.compile(
    r"\breformad\w*\b|\breformar\b|\breforma\b|\bseminuev\w*\b|"
    r"\ba\s+estrenar\b|\bbuen\s+estado\b|\bentrar\s+a\s+vivir\b|"
    r"\bimpecable\b|\bpara\s+actualizar\b|\bobra\s+nueva\b|"
    r"\bnueva\s+construcci[oó]n\b|\b(?:piso|vivienda|casa)\s+nuev[oa]\b",
    _re_mod.I,
)


# What the seller says about light and orientation. Never inferred from
# absence: "no dice nada de luz" is not "oscuro".
BRIGHT_RE = _re_mod.compile(
    r"\bluminos[oa]\b|\bmucha\s+luz\b|\bsoleado\b|\bexterior\b|"
    r"\borientaci[oó]n\s+(?:sur|este|sureste|suroeste)\b|\btodo\s+exterior\b",
    _re_mod.I,
)
INTERIOR_RE = _re_mod.compile(r"\binterior\b", _re_mod.I)


def _verdict_stale(iso_timestamp: str, hours: int) -> bool:
    """Whether a stored "unreadable" verdict is old enough to retry.

    "Rejected" is final — the page said what it said — but "unreadable"
    only describes the reader on one afternoon, so it expires. An
    unparseable timestamp counts as stale: retrying one page too early is
    cheaper than never retrying it.
    """
    from datetime import datetime, timedelta, timezone

    try:
        at = datetime.fromisoformat(iso_timestamp)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - at > timedelta(hours=hours)


def zone_market_stats(city: str | None, district: str | None) -> dict | None:
    """The €/m² spread of the zone, from the listings already ingested.

    Percentiles rather than the mean: one 15.000 €/m² penthouse drags a mean
    anywhere, while p25–p75 says what flats in this zone actually ask. The
    district is used when it has enough listings to mean something; the town
    otherwise; fewer than five listings is no market at all.
    """
    from sqlalchemy import select
    from models.db import get_engine
    from models.schema import listings

    if not city or city == "desconocido":
        return None

    def _prices(use_district: bool) -> list[float]:
        conds = [listings.c.city == city, listings.c.price_per_m2.isnot(None),
                 listings.c.price_per_m2 > 200]
        if use_district:
            if not district:
                return []
            conds.append(listings.c.district == district)
        with get_engine().connect() as conn:
            rows = conn.execute(select(listings.c.price_per_m2).where(*conds)).fetchall()
        return sorted(r[0] for r in rows)

    for use_district, scope in ((True, district), (False, city)):
        vals = _prices(use_district)
        if len(vals) >= 5:
            def pct(p):
                return vals[min(len(vals) - 1, int(p * len(vals)))]
            return {"n": len(vals), "scope": scope,
                    "p25": pct(0.25), "median": pct(0.5), "p75": pct(0.75)}
    return None


def digest_due(now: datetime, last_sent_date: str, hour_utc: int = DIGEST_HOUR_UTC) -> bool:
    """Whether the daily digest still owes today's send.

    The bot no longer has a cron slot of its own for the digest: it runs
    inside a long-lived loop that ticks every few minutes, so it has to
    decide for itself. `last_sent_date` is the ISO date of the last digest
    ("" if never), stored alongside the rest of the bot's state.

    Late is better than never: once the hour has passed, any tick that day
    will send it. That matters because the loop restarts every few hours and
    may simply not be running at 07:00 sharp.
    """
    today = now.strftime("%Y-%m-%d")
    if last_sent_date == today:
        return False
    return now.hour >= hour_utc


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

                                # The scraping path predates search profiles and
                                # still gates on the composite score itself,
                                # since send_alert no longer does.
                                score = metrics.get("investment_score", 0) or 0
                                if (
                                    not cold_start
                                    and is_new
                                    and self.alerter.should_alert(score)
                                    and self.alerter.send_alert(listing_id, raw_dict, metrics, score)
                                ):
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
                 "enriched": 0, "dropped_after_enrich": 0, "rejected_after_enrich": 0,
                 "by_portal": {}, "by_profile": {}, "unreadable": [], "truncated": []}

        reader = self._get_mail_reader()
        if reader is None:
            return stats

        from ingest.gmail_api import MailboxUnavailable

        try:
            messages = reader.fetch_new() if hasattr(reader, "fetch_new") else reader.fetch_unseen()
        except MailboxUnavailable:
            # Deliberately not swallowed. A mailbox that will not open is the
            # one failure that stops everything downstream, and it used to
            # look identical to a quiet Saturday.
            raise
        except Exception as e:
            logger.error(f"Email fetch failed: {e}")
            stats["errors"] += 1
            return stats

        stats["emails"] = len(messages)

        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        fresh_hours = (self.config.get("alerts") or {}).get("fresh_hours", 24)
        now_utc = _dt.now(_tz.utc)

        for msg in messages:
            # An email read late — the mailbox was down, the token revoked —
            # still feeds prices, zones and the duplicate check, but it is not
            # news. Its listings are dated to when the portal sent them, so
            # the sweep sees them as old too, and none of them is alerted.
            received_ms = getattr(msg, "received_ms", 0) or 0
            received_at = (_dt.fromtimestamp(received_ms / 1000, _tz.utc)
                           if received_ms else None)
            stale_mail = bool(received_at and now_utc - received_at > _td(hours=fresh_hours))
            if stale_mail:
                stats["stale_emails"] = stats.get("stale_emails", 0) + 1
            try:
                listings = parse_email(
                    msg.sender, msg.subject, msg.html, msg.text,
                    problems=stats["unreadable"],
                    truncations=stats["truncated"],
                )
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
                    if stale_mail:
                        raw_dict["first_seen_at"] = received_at
                    self._ingest_listing(raw_dict, stats, portal_stats,
                                         alert=not stale_mail)

                except Exception as e:
                    logger.error(f"Error processing {raw.url}: {e}")
                    stats["errors"] += 1

        logger.info(f"Email ingest complete: {stats}")
        for portal, pstats in stats["by_portal"].items():
            logger.info(f"  📧 {portal}: {pstats['parsed']} anuncios, {pstats['alerts']} alertas")

        return stats

    def run_browser_scrape(self, force: bool = False) -> dict:
        """Read the configured portal searches with a headless browser.

        Runs at most every `interval_minutes`; the loop ticks every three,
        and a full pass over every search takes a few minutes and a real
        browser. `force` skips that wait (for manual runs).

        The first read of any search sees every flat on it as new — they
        are new to this database, under ids the emails never used — and
        would alert dozens at once. So that first pass is a seed: it stores
        what is already there, dated outside the freshness window so the
        sweep leaves it alone too, and alerts nothing. From the next pass
        on, a flat that was not on the page before is news. A search added
        to the config later is seeded the same way on its own first run.
        """
        import json
        from datetime import datetime, timedelta, timezone
        from models.db import get_telegram_state, set_telegram_state

        cfg = (self.config.get("browser_scraping") or {}).get("habitaclia") or {}
        stats = {"searches": 0, "seeded": 0, "parsed": 0, "new": 0, "updated": 0,
                 "filtered_out": 0, "rejected_after_enrich": 0, "enriched": 0,
                 "alerts_sent": 0, "errors": 0, "by_profile": {}, "by_portal": {}}
        if not cfg.get("enabled") or not cfg.get("searches"):
            return stats

        now = datetime.now(timezone.utc)
        interval = timedelta(minutes=cfg.get("interval_minutes", 30))
        last = get_telegram_state("browser_last_run_at", "")
        if not force and last:
            try:
                if now - datetime.fromisoformat(last) < interval:
                    return stats
            except ValueError:
                pass

        try:
            from scraper.habitaclia_browser import scrape
        except ImportError as e:
            logger.warning(f"Navegador no disponible: {e}")
            return stats

        try:
            seeded = set(json.loads(get_telegram_state("browser_seeded", "[]") or "[]"))
        except ValueError:
            seeded = set()

        fresh_hours = (self.config.get("alerts") or {}).get("fresh_hours", 24)
        seed_date = now - timedelta(hours=fresh_hours + 1)
        portal_stats = stats["by_portal"].setdefault("habitaclia", {"parsed": 0, "alerts": 0})

        empty_first_reads: list[str] = []
        for search in cfg["searches"]:
            url = search["url"]
            label = search.get("district") or search.get("city") or url[-40:]
            try:
                found = scrape(url, max_pages=cfg.get("max_pages", 3),
                               city=search.get("city"), district=search.get("district"))
            except Exception as e:
                logger.error(f"Navegador: {label} falló: {type(e).__name__}: {e}")
                stats["errors"] += 1
                continue
            stats["searches"] += 1

            first_time = url not in seeded
            for raw_dict in found:
                stats["parsed"] += 1
                portal_stats["parsed"] += 1
                if first_time:
                    raw_dict["first_seen_at"] = seed_date
                try:
                    self._ingest_listing(raw_dict, stats, portal_stats, alert=not first_time)
                except Exception as e:
                    logger.error(f"Navegador: error con {raw_dict.get('url', '?')[:80]}: {e}")
                    stats["errors"] += 1

            # Only a pass that actually saw flats counts as the seed. An
            # empty page — blocked, or the markup changed — seeding nothing
            # would let the first real read afterwards alert everything.
            if first_time and found:
                seeded.add(url)
                stats["seeded"] += 1
                logger.info(f"Navegador: {label} sembrada con {len(found)} pisos (sin avisar)")
            elif first_time:
                empty_first_reads.append(url)

        # An empty search is genuinely empty when the same pass found flats
        # elsewhere: the portal answered and the markup still parses, so the
        # zero is real. Sant Just Desvern had no flat of 70 m² under
        # 320.000 € on the day it was added; left unseeded, the first flat
        # ever to appear there would have been taken for the seed and
        # silenced — in exactly the zone where one listing matters most.
        # When nothing anywhere came back, the page is the suspect, and
        # nothing is seeded.
        if empty_first_reads and stats["parsed"] > 0:
            for url in empty_first_reads:
                seeded.add(url)
                stats["seeded"] += 1
            logger.info(f"Navegador: {len(empty_first_reads)} búsquedas vacías dadas por "
                        f"sembradas (el resto de la pasada sí encontró pisos)")

        set_telegram_state("browser_seeded", json.dumps(sorted(seeded)))
        set_telegram_state("browser_last_run_at", now.isoformat())

        # A pass that found nothing anywhere is a page that changed or a door
        # that closed, not a market with nothing for sale: twenty-one searches
        # across six zones do not all go empty at once. Silence here would
        # look exactly like a quiet week — the way a dead mailbox once went
        # unnoticed for three — so it is said out loud, once a day at most.
        if cfg["searches"] and stats["parsed"] == 0:
            last = get_telegram_state("browser_broken_notice_at", "")
            due = True
            if last:
                try:
                    due = now - datetime.fromisoformat(last) > timedelta(hours=24)
                except ValueError:
                    pass
            if due:
                reason = (f"{stats['errors']} búsquedas fallaron" if stats["errors"]
                          else "todas las páginas vinieron vacías")
                if self.alerter.send_pulse(
                        error=f"Habitaclia: el navegador no encuentra ningún piso en "
                              f"{len(cfg['searches'])} búsquedas ({reason}). Puede que "
                              f"hayan cambiado la web o bloqueado el acceso."):
                    set_telegram_state("browser_broken_notice_at", now.isoformat())
        logger.info(f"Navegador: {stats['searches']} búsquedas, {stats['parsed']} pisos, "
                    f"{stats['new']} nuevos, {stats['alerts_sent']} avisos, "
                    f"{stats['seeded']} sembradas, {stats['errors']} errores")
        return stats

    def _ingest_listing(self, raw_dict: dict, stats: dict, portal_stats: dict,
                        alert: bool = True) -> None:
        """Store one listing and, if it is new and may be alerted, send it.

        Shared by every source on purpose. Twice in this codebase a second
        path grew its own copy of these steps and skipped one — the sweep
        once sent flats whose advert said "NO SE PUEDE VISITAR NI FINANCIAR"
        because it never read the page. Email and browser listings go
        through the same matching, the same page check and the same
        duplicate guards, or they do not go out.

        `alert=False` stores the listing (prices, zones, duplicates all
        learn from it) without treating it as news.
        """
        from models.db import upsert_listing, upsert_metrics

        listing_id = f"{raw_dict['portal']}_{raw_dict['external_id']}"
        is_new = upsert_listing(raw_dict)
        stats["new" if is_new else "updated"] += 1

        metrics = self._compute_metrics(raw_dict)
        if metrics:
            upsert_metrics(listing_id, metrics)
        else:
            # No surface, so no yields — but a home search does not need
            # them, and dropping the listing here made it invisible to every
            # later pass as well.
            metrics = self._minimal_metrics(raw_dict)

        if not is_new or not alert:
            return

        # The listing's own words first, whatever the source. Emails were
        # always screened this way while being parsed; the browser's cards
        # were not, and a card can say "ocupado" as plainly as an email.
        from ingest.email_parsers import _REJECT_RE

        blocker = _REJECT_RE.search(f"{raw_dict.get('title', '')} {raw_dict.get('description', '')}")
        if blocker:
            stats["rejected_after_enrich"] += 1
            logger.info(f"{listing_id} descartado por su propio texto: {blocker.group(0)!r}")
            return

        hits = (self.profile_matcher.match(raw_dict, metrics)
                or self._locate_from_page(raw_dict, metrics)
                or self._near_miss(raw_dict, metrics))
        if not hits:
            stats["filtered_out"] += 1
            return

        # Read the advert before sending. Most sources leave out the
        # condition, and "para reformar" — or "ocupado" — changes everything.
        checked = self.verify(raw_dict, metrics)
        if checked is None:
            stats["rejected_after_enrich"] += 1
            return
        if checked[0] is not raw_dict:
            stats["enriched"] += 1
        raw_dict, metrics = checked
        hits = self.profile_matcher.match(raw_dict, metrics) or self._near_miss(raw_dict, metrics) or hits

        # A listing can satisfy more than one search; name them all so the
        # alert says why it is being shown.
        labels = " + ".join(p.label for p, _ in hits)
        reason = hits[0][1]
        for profile, _ in hits:
            stats["by_profile"][profile.name] = stats["by_profile"].get(profile.name, 0) + 1

        metrics = {
            **metrics,
            "matched_profiles": labels,
            "matched_purpose": hits[0][0].purpose,
            "match_reason": reason,
            "near_miss": reason.startswith(NEAR_MISS_PREFIX),
        }

        score = metrics.get("investment_score", 0) or 0
        dedup_key = self.alerter.property_signature(raw_dict)
        if self.alerter.send_alert(listing_id, raw_dict, metrics, score, dedup_key=dedup_key):
            stats["alerts_sent"] += 1
            portal_stats["alerts"] += 1
            logger.info(f"Alerta {listing_id} [{labels}]: {reason}")

    def _near_miss(self, listing: dict, metrics: dict, confirm_ready: bool = False) -> list:
        """Profiles the listing misses by a little while cheap for its zone."""
        zone = zone_market_stats(listing.get("city"), listing.get("district"))
        return self.profile_matcher.near_miss(listing, metrics, zone, confirm_ready)

    # Portals whose alert emails name the street but not the barri.
    _STREET_ONLY_PORTALS = ("fotocasa",)

    def _locate_from_page(self, listing: dict, metrics: dict) -> list:
        """Find the barri on the advert when the email left it out, and re-match.

        Fotocasa writes "Carrer d'Antonio Machado 10, Barcelona": no barri,
        so every one of its Barcelona flats failed the zone check. Only read
        when the barri is the one thing missing, and the listing is stored
        with it so later passes and the duplicate check see the same place.
        """
        if listing.get("portal") not in self._STREET_ONLY_PORTALS:
            return []
        if not self.profile_matcher.needs_district(listing, metrics):
            return []
        from ingest.enrich import district_from_page, fetch_page_text_fallback, floor_from_page
        from models.db import upsert_listing

        page = fetch_page_text_fallback(listing.get("url"))
        barri = district_from_page(page)
        if not barri:
            return []
        listing["district"] = barri
        # The same header states the floor, which the email also leaves out
        listing["floor"] = listing.get("floor") or floor_from_page(page)
        upsert_listing(listing)
        logger.info(f"{listing['portal']}_{listing['external_id']}: barrio leído en la ficha: {barri}")
        return self.profile_matcher.match(listing, metrics)

    def _enrich(self, listing: dict) -> dict | None:
        """Recover the description from the listing's own page.

        Returns an updated copy, or None when the page could not be read —
        which is the normal outcome for portals that refuse this IP, and must
        leave the listing exactly as it was rather than half-updated.
        """
        from ingest.enrich import fetch_description

        if not self.config.get("enrichment", {}).get("enabled", True):
            return None

        try:
            description = fetch_description(listing.get("url", ""))
        except Exception as e:
            logger.warning(f"enrich failed for {listing.get('url', '')[:60]}: {e}")
            return None

        if not description:
            return None

        # Appended, not replaced: the email's text carries the location and
        # spec line that the parser already keyed off.
        merged = dict(listing)
        merged["description"] = f"{listing.get('description', '')} {description}".strip()
        return merged

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

    def run_daily_digest(self) -> bool:
        """Send the daily digest, built from the same matching as the alerts.

        It used to run its own query against the old 0-100 score and skip the
        profiles entirely, so it recommended flats no search would have
        accepted and said nothing about why.
        """
        logger.info("Sending daily digest...")
        entries = self.digest_entries()

        # One flat per message, in the same full card as an alert, and never
        # one that has already gone out. A five-listing list left room for a
        # star rating and nothing else — which is how the digest came to
        # recommend flats without saying anything about them.
        pending = []
        for entry in entries:
            listing = entry["listing"]
            if not self.alerter.already_sent(
                listing["id"], self.alerter.property_signature(listing)
            ):
                pending.append(entry)

        logger.info(f"Digest: {len(entries)} matches, {len(pending)} not yet sent")

        # "3 encajan, ya te las mandé todas" is not news, it is a message
        # whose entire content is that there is no message. Sent once a day
        # it was merely pointless; sent every cycle, as a failed date guard
        # made it, it was the complaint.
        if not pending:
            logger.info("Nada nuevo que resumir — no se envía nada")
            return False

        ok = self.alerter.send_digest_header(len(entries), len(pending))

        for entry in pending:
            listing = entry["listing"]
            self.alerter.send_alert(
                listing["id"], listing, entry["metrics"],
                entry["metrics"].get("investment_score") or 0,
                dedup_key=self.alerter.property_signature(listing),
            )
        return ok

    def verify(self, listing: dict, metrics: dict) -> tuple[dict, dict] | None:
        """Read the whole advert before sending, and drop it if it disqualifies.

        Whether somebody lives in a flat is only ever said in the seller's
        own description. A listing in Rubí went out at 99.000 € for 76 m²
        whose advert opened with "POSIBLE OCUPACION DEL INMUEBLE, NO SE PUEDE
        VISITAR NI FINANCIAR", because it left by a path that never read it.

        Every match is read now, not only those whose condition was unknown:
        a card that says "reformado" says nothing about who is living there.
        Of the eighteen cheapest flats the browser found in its first days,
        ten were occupied, sold without possession, at auction, in "cesión de
        remate" or let — and not one of their cards said so; every one of
        their pages did. Habitaclia answers a plain request with a summary
        of about 150 characters ("Venta de piso en … Consulta los detalles en
        habitaclia"), which used to count as a page read and clean; when the
        direct read is that thin, the reader fetches the whole page.

        The reader's text is scanned for disqualifying wording only: it
        includes the portal's navigation, and taking that for the seller's
        description would invent a condition.

        Returns the listing and metrics to send with, or None to drop.
        """
        from ingest.email_parsers import _REJECT_RE
        from ingest.enrich import fetch_page_text_fallback, final_url
        from models.db import bump_daily, get_verify_verdict, set_verify_verdict

        lid = listing.get("id") or ""
        cached = get_verify_verdict(lid)
        if cached and cached[0] == "rechazada":
            return None
        # A page read clean is not re-read on every pass of the sweep — the
        # same waste the "rechazada" verdict once stopped — but a day later
        # it is, in case the advert changed.
        if cached and cached[0] == "limpia" and not _verdict_stale(cached[1], hours=24):
            return listing, metrics
        page_unreadable_known = bool(
            cached and cached[0] == "ilegible"
            and not _verdict_stale(cached[1], hours=6)
        )

        enriched = None if page_unreadable_known else self._enrich(listing)
        added = ""
        if enriched:
            added = (enriched.get("description") or "")[len(listing.get("description") or ""):]
        full_text = None
        if not page_unreadable_known and len(added.strip()) < 600:
            # Email links are redirects; the reader needs the page they land on.
            url = listing.get("url") or ""
            if "/alertas/" in url or "/r/?" in url or "utm_" in url:
                url = final_url(url) or url
            full_text = fetch_page_text_fallback(url)

        # Whatever was read is screened, thin or not — a summary can still
        # say "ocupado".
        scan = " ".join(filter(None, [
            str(listing.get("title") or ""),
            str((enriched or listing).get("description") or ""),
            full_text,
        ]))
        blocker = _REJECT_RE.search(scan)
        if blocker:
            logger.info(f"{lid} descartado al leer la ficha: {blocker.group(0)!r}")
            bump_daily("ocupado_ficha")
            set_verify_verdict(lid, "rechazada")
            return None

        # Only the whole advert counts as verified. A thin summary is not a
        # page read: the Esplugues flat that went out "verified" had only
        # "Venta de piso en … Consulta los detalles en habitaclia" behind it,
        # and its page said "inmueble alquilado. No es posible coordinar
        # visitas al interior". An unverified flat is held, not sent — the
        # user's rule is to avoid occupied flats for every source — and
        # retried in six hours, inside the 24-hour freshness window, so a
        # reader having a bad afternoon delays a good flat rather than
        # losing it.
        if len(added.strip()) < 600 and not full_text:
            portal = (listing.get("portal") or lid.split("_")[0]).lower()
            must_read = portal in (((self.config.get("alerts") or {}).get("verification") or {})
                                   .get("must_read_portals", ["habitaclia", "fotocasa"]))
            if not page_unreadable_known:
                set_verify_verdict(lid, "ilegible")
            if must_read:
                # A portal whose pages are normally read: failing to read one
                # is a hold, not a pass.
                if not page_unreadable_known:
                    logger.info(f"{lid} sin verificar: no pude leer la ficha entera")
                    bump_daily("no_verificable")
                    self._warn_if_reader_failing()
                return None
            # Idealista and the bank sites refuse both the runner and the
            # reader, every time. Holding their unread flats would hold all
            # of them, and Idealista is the user's main portal — so they go
            # out on what their email said, which was already screened, and
            # the notification says plainly that the page was not read.
            hits = self.profile_matcher.match(listing, metrics) or self._near_miss(listing, metrics)
            if hits and all(p.purpose == "investment" for p, _ in hits):
                return None
            for reason in metrics.get("suspicion") or []:
                pct = _re_mod.match(r"(\d+)% por debajo", reason)
                if pct and int(pct.group(1)) >= 55:
                    return None
            return listing, {**metrics, "condition_unknown": True, "unverified": True}

        # Readable and clean. What the direct read added can change the
        # match — "para reformar" moves the numbers — so it is matched again.
        if enriched:
            fresh = self._compute_metrics(enriched) or metrics
            # Read in full, a near miss must say it is ready to live in
            hits = (self.profile_matcher.match(enriched, fresh)
                    or self._near_miss(enriched, fresh, confirm_ready=True))
            if not hits:
                logger.info(f"{lid} ya no encaja al leer la ficha")
                set_verify_verdict(lid, "rechazada")
                return None
            set_verify_verdict(lid, "limpia")
            return enriched, {
                **fresh,
                "matched_profiles": " + ".join(p.label for p, _ in hits),
                "matched_purpose": hits[0][0].purpose,
                "match_reason": hits[0][1],
            }
        set_verify_verdict(lid, "limpia")
        return listing, metrics

    def _warn_if_reader_failing(self, threshold: int = 5) -> None:
        """Say so when adverts cannot be read, at most once a day.

        Unverified flats are now held rather than sent, so a reader that
        stops working would quietly hold every one of them. The mailbox once
        died for three weeks unheard; this must not be its sequel.
        """
        from datetime import datetime, timezone
        from models.db import daily_counters, get_telegram_state, set_telegram_state

        try:
            count = daily_counters().get("no_verificable", 0)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if count >= threshold and get_telegram_state("reader_failing_notice_day", "") != today:
                if self.alerter.send_pulse(
                        error=f"No he podido leer {count} fichas hoy: esos pisos quedan "
                              f"retenidos sin avisarte. Puede que el lector de páginas esté "
                              f"fallando y me esté perdiendo pisos."):
                    set_telegram_state("reader_failing_notice_day", today)
        except Exception as e:
            logger.warning(f"No pude comprobar el lector: {e}")

    def send_pending(self) -> int:
        """Send every current match that has never gone out.

        The safety net under quiet hours. A flat found at three in the
        morning is suppressed rather than recorded, so without this sweep it
        would sit in the database unmentioned for ever — the alert path only
        fires on freshly parsed email, and that email is never read twice.

        It also repairs anything lost to a Telegram outage or a crash
        between sending and saving state.
        """
        from datetime import datetime, timedelta, timezone

        # Only what is news. The sweep exists to catch what quiet hours or
        # an outage held back for a few hours; without a bound it also walks
        # the whole history, and after the rent table changed it found 601
        # never-sent matches from before 30 September and began delivering
        # them twenty a day — cards for flats advertised weeks earlier and
        # most likely gone. The user's rule, in his words: only what is
        # coming out at each moment.
        fresh_hours = (self.config.get("alerts") or {}).get("fresh_hours", 24)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=fresh_hours)

        sent = 0
        for entry in self.digest_entries():
            listing = entry["listing"]
            first_seen = listing.get("first_seen_at")
            if isinstance(first_seen, str):
                try:
                    first_seen = datetime.fromisoformat(first_seen)
                except ValueError:
                    first_seen = None
            if first_seen is not None:
                if first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=timezone.utc)
                if first_seen < cutoff:
                    continue
            signature = self.alerter.property_signature(listing)
            if self.alerter.already_sent(listing["id"], signature):
                continue

            checked = self.verify(listing, entry["metrics"])
            if checked is None:
                continue
            listing, metrics = checked

            if self.alerter.send_alert(
                listing["id"], listing, metrics,
                metrics.get("investment_score") or 0,
                dedup_key=signature,
            ):
                sent += 1
        if sent:
            logger.info(f"Pendientes enviados: {sent}")
        return sent

    def digest_entries(self, hours: int = 24) -> list[dict]:
        """Every current match, deduplicated, newest-first within each price."""
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(hours=hours)

        merged: dict = {}
        for listing, metrics, profile, reason in self._current_matches():
            # The same flat reaches us through several agencies and portals.
            # A listing that cannot be fingerprinted — Idealista truncates
            # the line before the surface on half its alerts — keeps its own
            # id as the key. It used to take the signature verbatim, so every
            # such listing collapsed onto the single key None and all but the
            # first vanished: five flats in the user's barrios, merged into
            # one unrelated advert and never seen again.
            key = self.alerter.property_signature(listing) or f"id:{listing['id']}"
            if key in merged:
                merged[key]["labels"].append(profile.label)
                continue

            first_seen = listing.get("first_seen_at")
            if first_seen is not None and first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=timezone.utc)

            merged[key] = {
                "listing": listing,
                "metrics": metrics,
                "profile": profile,
                "reason": reason,
                "labels": [profile.label],
                "is_new": bool(first_seen and first_seen >= since),
            }

        entries = sorted(
            merged.values(),
            # New first, then cheapest — the order someone actually reads in.
            key=lambda e: (not e["is_new"], e["listing"].get("price") or 0),
        )
        for entry in entries:
            entry["metrics"] = {
                **entry["metrics"],
                "matched_profiles": " + ".join(entry["labels"]),
                "matched_purpose": entry["profile"].purpose,
                "match_reason": entry["reason"],
            }
        return entries

    def run_feedback_poll(self) -> None:
        """Poll Telegram for button clicks, replies and typed instructions."""
        try:
            self.alerter.poll_feedback(command_handler=self.handle_command)
        except Exception as e:
            logger.error(f"Feedback poll failed: {e}")

    # ── Talking back ─────────────────────────────────────────────────────

    def handle_command(self, text: str) -> str | None:
        """Answer a message typed into the chat.

        Deliberately forgiving about wording: this is a chat, not a CLI, and
        being told "no entiendo ese comando" for writing "que tenemos?"
        instead of "/top" is the kind of thing that makes a tool go unused.
        """
        from analysis.profiles import normalise

        words = normalise(text)
        if not words:
            return None

        def asks(*terms):
            return any(t in words for t in terms)

        if asks("ayuda", "help", "comandos", "que puedes"):
            return self._help_text()

        if asks("tenemos", "top", "lista", "listado", "oportunidades",
                "resumen", "que hay", "pisos", "dime"):
            return self._summary_text()

        if asks("estado", "status", "funciona", "vivo", "salud"):
            return self._health_text()

        if asks("embudo", "hoy", "funnel", "cuantos", "cuántos"):
            return self._funnel_text()

        return (
            "No he entendido eso.\n\n"
            "Prueba con <b>qué tenemos</b> para ver las oportunidades, "
            "<b>estado</b> para saber si sigo vivo, o <b>ayuda</b>."
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "<b>Puedes escribirme</b>\n\n"
            "• <b>qué tenemos</b> — todo lo que encaja ahora mismo\n"
            "• <b>embudo</b> — cuántos entraron hoy y dónde murió cada uno\n"
            "• <b>estado</b> — qué buzón leo, cuándo lo miré y si sigo vivo\n"
            "• <b>ayuda</b> — esto\n\n"
            "<i>Respondo en la siguiente pasada, o sea en 3 minutos o menos.</i>"
        )

    def _current_matches(self) -> list[tuple[dict, dict, object, str]]:
        """Every stored listing that still satisfies a profile."""
        from models.db import get_engine
        from models.schema import listings as listings_tbl
        from sqlalchemy import select

        with get_engine().connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(listings_tbl))]

        found = []
        for listing in rows:
            metrics = self._compute_metrics(listing) or self._minimal_metrics(listing)
            for profile, reason in self.profile_matcher.match(listing, metrics):
                found.append((listing, metrics, profile, reason))
        return found

    def _summary_text(self) -> str:
        matches = self._current_matches()
        if not matches:
            return (
                "<b>Nada encaja ahora mismo.</b>\n\n"
                "Sigo mirando el correo. Te aviso en cuanto aparezca algo."
            )

        def money(v):
            return f"{v:,.0f}".replace(",", ".") if v else "?"

        by_purpose: dict[str, list] = {}
        seen: set = set()
        for listing, metrics, profile, reason in sorted(matches, key=lambda t: t[0]["price"] or 0):
            # The same flat is often listed by several agencies
            key = (listing.get("price"), listing.get("area_m2"))
            if key in seen:
                continue
            seen.add(key)
            by_purpose.setdefault(profile.purpose, []).append((listing, metrics, profile))

        lines = ["<b>📋 Lo que tenemos ahora</b>", ""]

        for purpose, heading in (("investment", "💰 <b>INVERSIÓN</b>"),
                                 ("home", "🏠 <b>PARA VIVIR</b>")):
            items = by_purpose.get(purpose)
            if not items:
                continue
            lines.append(heading)
            for listing, metrics, _ in items:
                where = listing.get("district") or listing.get("city") or "?"
                lines.append(
                    f'• <a href="{listing["url"]}">{money(listing["price"])}€ · {where}</a>'
                )
                if purpose == "investment":
                    lines.append(
                        f"  {money(listing.get('area_m2'))}m² · entrada "
                        f"{money(metrics.get('cash_needed'))}€ · neta "
                        f"{metrics.get('net_yield_pct') or 0:.1f}% · cashflow "
                        f"{metrics.get('monthly_cashflow') or 0:+,.0f}€/mes".replace(",", ".")
                    )
                else:
                    lines.append(
                        f"  {money(listing.get('area_m2'))}m² · entrada "
                        f"{money(metrics.get('cash_needed'))}€ · cuota "
                        f"{money(metrics.get('monthly_payment_total') or metrics.get('monthly_payment'))}€/mes"
                    )
                if metrics.get("rent_capped_zone") and purpose == "investment":
                    lines.append("  ⚠️ zona tensionada: el alquiler está topado")
                if metrics.get("reform_cost"):
                    lines.append(f"  🔨 incluye {money(metrics['reform_cost'])}€ de reforma")
            lines.append("")

        return "\n".join(lines).strip()

    def _funnel_text(self) -> str:
        """Where today's flats died, so the answer stops needing an engineer.

        "Plenty are published and nothing reaches me" and "the filters are
        doing their job" describe the same silence. Only the numbers tell
        them apart, and they were only in the logs.
        """
        from sqlalchemy import func, select
        from models.db import daily_counters, get_engine
        from models.schema import listings as listings_tbl
        from alerts import budget

        with get_engine().connect() as conn:
            new_today = conn.execute(
                select(func.count()).select_from(listings_tbl)
                .where(func.date(listings_tbl.c.first_seen_at) == func.date(func.datetime("now")))
            ).scalar() or 0

        sent = (budget.describe(self.config) or "").split(" de ")[0] or "0"
        drops = daily_counters()
        lines = [
            "<b>📈 Embudo de hoy</b>",
            "",
            f"Anuncios nuevos leídos: <b>{new_today}</b>",
            f"Fichas enviadas: <b>{sent.strip()}</b>",
        ]
        labels = {"ocupado_ficha": "la ficha dice ocupado/no financiable",
                  "no_verificable": "inversión sin poder verificar (no se envía)",
                  "duplicado": "mismo piso ya enviado con otras medidas"}
        if drops:
            lines.append("")
            lines.append("Descartados en la última milla:")
            for k, n in sorted(drops.items(), key=lambda t: -t[1]):
                lines.append(f"  • {n} — {labels.get(k, k)}")
        lines.append("")
        lines.append("<i>El resto no pasó tus filtros de zona, precio, tamaño "
                     "o planta. Los correos de Habitaclia siguen llegando "
                     "recortados: divide esas alertas para ver más.</i>")
        return "\n".join(lines)

    def _health_text(self) -> str:
        import datetime as dt

        from models.db import get_engine, get_telegram_state
        from models.schema import listings as listings_tbl
        from sqlalchemy import func, select

        with get_engine().connect() as conn:
            total = conn.execute(select(func.count()).select_from(listings_tbl)).scalar() or 0

        cursor = get_telegram_state("gmail_last_internal_date_ms", "")
        when = "nunca"
        if cursor:
            try:
                when = dt.datetime.fromtimestamp(
                    int(cursor) / 1000, tz=dt.timezone.utc
                ).strftime("%d/%m %H:%M UTC")
            except ValueError:
                pass

        # Nothing on disk records which account the bot reads — the OAuth
        # refresh token is the whole credential — so it has to be asked.
        mailbox = None
        try:
            reader = self._get_mail_reader()
            # The IMAP backend has no equivalent; it is configured with an
            # address rather than a token, so it never needed one.
            ask = getattr(reader, "account_address", None)
            if ask:
                mailbox = ask()
        except Exception as e:
            logger.error(f"No pude leer la dirección del buzón: {e}")

        # "Último correo leído: ayer" on its own is ambiguous: it means the
        # same thing whether no portal has written since yesterday or the bot
        # died yesterday. The heartbeat separates the two.
        from scheduler import heartbeat

        from alerts import budget

        return (
            "<b>✅ Funcionando</b>\n\n"
            f"Mensajes enviados: {budget.describe(self.config)}\n"
            f"Última pasada: {heartbeat.describe()}\n"
            f"Buzón que leo: <b>{mailbox or 'no he podido comprobarlo'}</b>\n"
            f"Último correo <i>nuevo</i>: {when}\n"
            f"Anuncios analizados: <b>{total}</b>\n"
            f"Perfiles activos: {len(self.profile_matcher.profiles)}\n\n"
            "<i>Reviso el correo cada 3 minutos, en marcha continua.</i>"
        )

    def _minimal_metrics(self, listing: dict) -> dict:
        """The little that can be said about a listing with no surface.

        Rent is estimated per m², so every yield, every €/m² and the whole
        suspicion machinery need an area, and _compute_metrics returns None
        without one. Both the ingest and the sweep then skipped the listing
        outright — invisible, uncounted, never mentioned.

        Half of Idealista's alert emails truncate the listing line before the
        surface ("Piso en Calle de la Marquesa..., El Guinardó, Barcel..."),
        so half of the portal the user cares most about was being dropped on
        a technicality: six flats in his own barrios in one week, among them
        Camp de l'Arpa at 325.000 € and Baix Guinardó at 310.000 €. The home
        search never wanted those numbers — it asks for a barrio, a price and
        a number of rooms, all of which arrived intact.

        The condition still gets read from the advert, so these listings go
        through the same page verification as everything else.
        """
        text = " ".join(str(listing.get(k) or "") for k in ("title", "description"))
        return {
            "condition_unknown": not bool(CONDITION_WORDS.search(text)),
            "metrics_unavailable": True,
        }

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

        # A flat advertised as needing work cannot be let until it is done,
        # and the bank does not lend for the works. Costing it at zero was
        # describing a flat that does not exist yet: the Sabadell listing read
        # 6,09% while its own advert said "oportunidad para reformar".
        import re as _re

        text = " ".join(str(listing.get(k) or "") for k in ("title", "description"))
        needs_work = bool(_re.search(r"\bpara\s+reformar\b|\ba\s+reformar\b|"
                                     r"\breformar\b|\ba\s+reformar\b|\breforma\s+integral\b",
                                     text, _re.I))
        reform_per_m2 = self.config.get("analysis", {}).get("reform_cost_per_m2", 0)
        reform_cost = (area * reform_per_m2) if (needs_work and reform_per_m2) else 0.0

        metrics = compute_all_metrics(
            price=price,
            area_m2=area,
            estimated_monthly_rent=monthly_rent,
            purchase_costs_pct=purchase_costs_pct,
            expense_ratio=expense_ratio,
            capital_growth_pct=growth_pct,
            financing=self.config.get("financing"),
            reform_cost=reform_cost,
        )
        metrics["needs_reform"] = needs_work

        # Some alert emails carry no description at all — the Sabadell listing
        # arrived as price, m² and rooms and nothing else, while its advert on
        # the portal said "oportunidad para reformar". Condition cannot be
        # ruled out from silence, so say so rather than imply the flat is fine.
        # Word count is a poor test — what is left after stripping the URL is
        # mostly the email's own furniture ("Modificar", "Dejar de recibir").
        # Ask the question directly instead: did the advert say anything at
        # all about the state of the flat?
        says_condition = bool(CONDITION_WORDS.search(text))
        metrics["condition_unknown"] = not says_condition

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

        # Why this one might be occupied or unmortgageable, when nobody has
        # said so outright. The portals that would say so refuse to be read,
        # so these are the tells left in the data — the same ones a person
        # notices at a glance and then has to open the advert to confirm.
        suspicion = []

        ppm2 = metrics.get("price_per_m2")
        if ppm2 and zone_avg_ppm2:
            discount = 100 * (1 - ppm2 / zone_avg_ppm2)
            # A genuine bargain runs 10-20% under the zone. Forty per cent
            # under is not a bargain, it is a reason.
            if discount >= 35:
                suspicion.append(
                    f"{discount:.0f}% por debajo de la zona "
                    f"({ppm2:,.0f} vs {zone_avg_ppm2:,.0f}€/m²)".replace(",", ".")
                )

        photos = listing.get("photo_urls") or []
        if isinstance(photos, list) and 0 < len(photos) <= 2:
            # A flat nobody can enter is photographed from the street.
            suspicion.append(f"solo {len(photos)} foto{'s' if len(photos) > 1 else ''}")
        elif not photos:
            suspicion.append("sin fotos")

        if metrics.get("condition_unknown"):
            suspicion.append("no he podido leer la ficha")

        metrics["suspicion"] = suspicion

        # The zone, in numbers the reader can argue with: what flats there
        # actually ask per m², and where this one sits against the median.
        market = zone_market_stats(city, district)
        if market:
            if ppm2 and market["median"]:
                market["delta_pct"] = round(100 * (ppm2 / market["median"] - 1))
            metrics["zone_market"] = market

        text_all = f"{listing.get('title', '')} {listing.get('description', '')}"
        hits = sorted({m.group(0).lower() for m in BRIGHT_RE.finditer(text_all)})
        if hits:
            metrics["bright"] = hits
        elif INTERIOR_RE.search(text_all):
            metrics["interior"] = True

        # The only structured danger signal there is data for: the lowest
        # household-income neighbourhoods (INE), the same list the investment
        # profile excludes outright. Shown as information wherever it applies,
        # because "cheap" and "in this list" together explain each other.
        watch = []
        for spec_profile in self.config.get("search_profiles") or []:
            watch += spec_profile.get("excluded_zones") or []
        if watch:
            from analysis.profiles import _place_matches
            where_txt = f"{listing.get('district') or ''} {listing.get('city') or ''}"
            if any(_place_matches(z, where_txt) for z in watch):
                metrics["watchlist_zone"] = True

        # Rent cap. The estimate above comes from market averages, but in a
        # declared zone the lawful rent is whatever the Generalitat's index
        # allows — often less. Flagging it keeps the yield honest: it is an
        # upper bound there, not a figure to underwrite a purchase with.
        from analysis.municipalities import is_tensioned

        tensioned = is_tensioned(city)
        metrics["rent_capped_zone"] = tensioned

        if tensioned:
            haircut = self.config.get("analysis", {}).get("tensioned_rent_haircut_pct", 0)
            if haircut:
                metrics["estimated_monthly_rent_uncapped"] = metrics["estimated_monthly_rent"]
                metrics["rent_haircut_pct"] = haircut

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
