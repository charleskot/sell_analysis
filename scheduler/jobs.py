"""Pipeline orchestration and APScheduler job definitions."""
import logging
from datetime import datetime, timezone

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
                 "by_portal": {}, "by_profile": {}, "unreadable": []}

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

        for msg in messages:
            try:
                listings = parse_email(
                    msg.sender, msg.subject, msg.html, msg.text,
                    problems=stats["unreadable"],
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

                    # Only now, for the handful that matched, is it worth
                    # reading the listing's own page. The email left out the
                    # condition for most of them, and "para reformar" changes
                    # the numbers by tens of thousands — enough to disqualify
                    # a match that looked fine a moment ago.
                    if metrics.get("condition_unknown"):
                        enriched = self._enrich(raw_dict)
                        if enriched:
                            stats["enriched"] += 1
                            raw_dict = enriched

                            # The reject rules ran against the email, which
                            # said nothing. The page does: three of the four
                            # best-yielding flats turned out to be occupied or
                            # explicitly unmortgageable, with the yield being
                            # the premium for exactly that.
                            from ingest.email_parsers import _REJECT_RE

                            blocker = _REJECT_RE.search(
                                f"{raw_dict.get('title', '')} {raw_dict.get('description', '')}"
                            )
                            if blocker:
                                logger.info(
                                    f"{listing_id} rejected after reading its page: "
                                    f"{blocker.group(0)!r}"
                                )
                                stats["rejected_after_enrich"] += 1
                                continue

                            metrics = self._compute_metrics(raw_dict) or metrics
                            hits = self.profile_matcher.match(raw_dict, metrics)
                            if not hits:
                                logger.info(
                                    f"{listing_id} dropped after reading its page: "
                                    f"{metrics.get('needs_reform') and 'needs work' or 'no longer matches'}"
                                )
                                stats["dropped_after_enrich"] += 1
                                continue

                    # A listing can satisfy more than one search; name them all
                    # so the alert says why it is being shown.
                    labels = " + ".join(p.label for p, _ in hits)
                    reason = hits[0][1]
                    for profile, _ in hits:
                        stats["by_profile"][profile.name] = (
                            stats["by_profile"].get(profile.name, 0) + 1
                        )

                    # The alert is laid out by purpose, so carry it through
                    metrics = {
                        **metrics,
                        "matched_profiles": labels,
                        "matched_purpose": hits[0][0].purpose,
                        "match_reason": reason,
                    }

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

    def send_pending(self) -> int:
        """Send every current match that has never gone out.

        The safety net under quiet hours. A flat found at three in the
        morning is suppressed rather than recorded, so without this sweep it
        would sit in the database unmentioned for ever — the alert path only
        fires on freshly parsed email, and that email is never read twice.

        It also repairs anything lost to a Telegram outage or a crash
        between sending and saving state.
        """
        sent = 0
        for entry in self.digest_entries():
            listing = entry["listing"]
            signature = self.alerter.property_signature(listing)
            if self.alerter.already_sent(listing["id"], signature):
                continue
            if self.alerter.send_alert(
                listing["id"], listing, entry["metrics"],
                entry["metrics"].get("investment_score") or 0,
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
            # The same flat reaches us through several agencies and portals
            key = self.alerter.property_signature(listing)
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
            metrics = self._compute_metrics(listing)
            if not metrics:
                continue
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
