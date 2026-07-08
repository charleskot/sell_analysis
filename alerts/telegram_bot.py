"""Telegram alerts and daily digest for real estate investment opportunities."""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class TelegramAlerter:
    def __init__(self, config: dict):
        alert_cfg = config.get("alerts", {}).get("telegram", {})
        self._token = self._resolve(alert_cfg.get("token", ""))
        self._chat_id = self._resolve(alert_cfg.get("chat_id", ""))
        self._min_score = alert_cfg.get("min_score_threshold", 70)
        self._cooldown_hours = alert_cfg.get("cooldown_hours", 168)
        self._enabled = bool(self._token and self._chat_id and not self._token.startswith("${"))

        if not self._enabled:
            logger.info("Telegram alerts disabled (no token/chat_id configured)")

    def _resolve(self, value: str) -> str:
        """Resolve ${ENV_VAR} references."""
        if value.startswith("${") and value.endswith("}"):
            env_key = value[2:-1]
            return os.environ.get(env_key, "")
        return value

    @property
    def enabled(self) -> bool:
        return self._enabled

    def should_alert(self, score: float) -> bool:
        return self._enabled and score >= self._min_score

    def send_alert(self, listing_id: str, listing: dict, metrics: dict, score: float) -> bool:
        """Send individual property alert if score meets threshold and cooldown allows."""
        from models.db import was_alert_sent_recently, record_alert_sent

        if not self.should_alert(score):
            return False

        if was_alert_sent_recently(listing_id, self._cooldown_hours):
            logger.debug(f"Alert cooldown active for {listing_id}")
            return False

        message = self._format_message(listing, metrics, score)
        success = self._send_sync(message)

        if success:
            record_alert_sent(listing_id, "telegram", message[:200])
            logger.info(f"Telegram alert sent for {listing_id} (score {score:.0f})")

        return success

    def send_daily_digest(self) -> bool:
        """Send a daily summary of the top investment opportunities from the DB."""
        if not self._enabled:
            return False

        top_listings = self._get_top_listings(limit=5, min_score=self._min_score)
        message = self._format_digest(top_listings)
        success = self._send_sync(message)
        if success:
            logger.info(f"Daily digest sent: {len(top_listings)} top opportunities")
        return success

    def _get_top_listings(self, limit: int = 5, min_score: float = 0) -> list[dict]:
        """Query DB for top scoring active listings."""
        try:
            from models.db import get_engine
            from models.schema import listings, investment_metrics
            from sqlalchemy import select, and_

            with get_engine().connect() as conn:
                rows = conn.execute(
                    select(
                        listings.c.portal,
                        listings.c.url,
                        listings.c.price,
                        listings.c.area_m2,
                        listings.c.rooms,
                        listings.c.city,
                        listings.c.district,
                        listings.c.condition,
                        investment_metrics.c.investment_score,
                        investment_metrics.c.gross_yield_pct,
                        investment_metrics.c.net_yield_pct,
                        investment_metrics.c.estimated_monthly_rent,
                        investment_metrics.c.payback_years,
                    )
                    .join(investment_metrics, investment_metrics.c.listing_id == listings.c.id)
                    .where(and_(
                        listings.c.is_active == True,
                        investment_metrics.c.investment_score >= min_score,
                    ))
                    .order_by(investment_metrics.c.investment_score.desc())
                    .limit(limit)
                ).fetchall()

            return [dict(r._mapping) for r in rows]
        except Exception as e:
            logger.error(f"Error fetching top listings for digest: {e}")
            return []

    def _format_digest(self, listings_data: list[dict]) -> str:
        from datetime import datetime
        now = datetime.now().strftime("%d/%m/%Y")

        if not listings_data:
            return (
                f"📊 *Resumen diario — {now}*\n\n"
                f"No hay viviendas con match ≥ {self._min_score:.0f} en este momento.\n"
                f"El scraper sigue buscando en tus zonas — te avisaré en cuanto aparezca algo."
            )

        lines = [f"🏠 *Top {len(listings_data)} viviendas — {now}*\n"]
        for i, r in enumerate(listings_data, 1):
            score = r.get("investment_score") or 0
            price = r.get("price") or 0
            area = r.get("area_m2") or 0
            rooms = r.get("rooms") or "?"
            city = (r.get("city") or "").title()
            district = (r.get("district") or "").title()
            location = f"{district}, {city}" if district else city
            url = r.get("url", "")
            stars = "⭐" * min(5, max(1, int(score / 20)))
            lines.append(
                f"{i}. {stars} *{score:.0f}/100* — {location}\n"
                f"   💰 {price:,.0f}€ | {area:.0f}m² | {rooms} hab\n"
                f"   [Ver anuncio]({url})"
            )

        return "\n".join(lines)

    def _send_sync(self, message: str) -> bool:
        """Send a Telegram message, handling both sync and async contexts."""
        try:
            return asyncio.run(self._send_async(message))
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
                return loop.run_until_complete(self._send_async(message))
            except Exception as e:
                logger.error(f"Telegram send failed (event loop): {e}")
                return False

    async def _send_async(self, message: str) -> bool:
        try:
            from telegram import Bot
            bot = Bot(token=self._token)
            await bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
            return True
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    def _format_message(self, listing: dict, metrics: dict, score: float) -> str:
        # Detect residence-mode by presence of match_profile
        if metrics.get("match_profile"):
            return self._format_residence_message(listing, metrics, score)
        return self._format_investment_message(listing, metrics, score)

    def _format_residence_message(self, listing: dict, metrics: dict, score: float) -> str:
        price = listing.get("price", 0)
        area = listing.get("area_m2", 0)
        rooms = listing.get("rooms", "?")
        city = (listing.get("city") or "").title()
        district = (listing.get("district") or "").title() if listing.get("district") else ""
        location = f"{district}, {city}" if district else city
        url = listing.get("url", "")
        floor = listing.get("floor") or ""

        bd = metrics.get("score_breakdown", {})
        profile = metrics.get("match_profile", "?")
        profile_label = "Obra nueva" if profile == "new" else "Reciente (≤20a)"
        stars = "⭐" * min(5, max(1, int(score / 20)))

        reasons = bd.get("reasons_pass", [])
        reasons_txt = "\n".join(f"  ✓ {r}" for r in reasons) if reasons else ""

        msg = (
            f"*{stars} NUEVA VIVIENDA — Match {score:.0f}/100 — {profile_label}*\n\n"
            f"📍 {location}\n"
            f"💰 {price:,.0f}€ | {area:.0f}m² | {rooms} hab"
        )
        if floor:
            msg += f" | {floor}"
        msg += f"\n{reasons_txt}\n\n[Ver anuncio]({url})"
        return msg

    def _format_investment_message(self, listing: dict, metrics: dict, score: float) -> str:
        price = listing.get("price", 0)
        area = listing.get("area_m2", 0)
        rooms = listing.get("rooms", "?")
        city = (listing.get("city") or "").title()
        district = (listing.get("district") or "").title() if listing.get("district") else ""
        location = f"{district}, {city}" if district else city
        url = listing.get("url", "")

        gross_yield = metrics.get("gross_yield_pct", 0) or 0
        net_yield = metrics.get("net_yield_pct", 0) or 0
        payback = metrics.get("payback_years", 0) or 0
        monthly_rent = metrics.get("estimated_monthly_rent", 0) or 0

        stars = "⭐" * min(5, int(score / 20))

        msg = (
            f"*{stars} NUEVA OPORTUNIDAD — Score: {score:.0f}/100*\n\n"
            f"📍 {location}\n"
            f"💰 Precio: {price:,.0f}€ | {area:.0f}m² | {rooms} hab\n"
            f"📈 Rentabilidad bruta: *{gross_yield:.1f}%*\n"
            f"📊 Rentabilidad neta: {net_yield:.1f}%\n"
            f"🏠 Alquiler estimado: {monthly_rent:.0f}€/mes\n"
            f"⏳ Payback: {payback:.0f} años\n\n"
            f"[Ver anuncio]({url})"
        )
        return msg
