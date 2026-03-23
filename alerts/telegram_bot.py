"""Telegram alerts for high-score listings."""
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

    def should_alert(self, score: float) -> bool:
        return self._enabled and score >= self._min_score

    def send_alert(self, listing_id: str, listing: dict, metrics: dict, score: float) -> bool:
        from models.db import was_alert_sent_recently, record_alert_sent

        if not self.should_alert(score):
            return False

        if was_alert_sent_recently(listing_id, self._cooldown_hours):
            logger.debug(f"Alert cooldown active for {listing_id}")
            return False

        message = self._format_message(listing, metrics, score)

        try:
            success = asyncio.run(self._send_async(message))
        except RuntimeError:
            # Already in event loop
            loop = asyncio.get_event_loop()
            success = loop.run_until_complete(self._send_async(message))

        if success:
            record_alert_sent(listing_id, "telegram", message[:200])
            logger.info(f"Telegram alert sent for {listing_id} (score {score})")

        return success

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
        price = listing.get("price", 0)
        area = listing.get("area_m2", 0)
        rooms = listing.get("rooms", "?")
        city = listing.get("city", "").title()
        district = listing.get("district", "").title() if listing.get("district") else ""
        location = f"{district}, {city}" if district else city
        url = listing.get("url", "")

        gross_yield = metrics.get("gross_yield_pct", 0) or 0
        net_yield = metrics.get("net_yield_pct", 0) or 0
        payback = metrics.get("payback_years", 0) or 0
        monthly_rent = metrics.get("estimated_monthly_rent", 0) or 0

        stars = "⭐" * min(5, int(score / 20))

        msg = (
            f"*{stars} OPORTUNIDAD DE INVERSIÓN - Score: {score:.0f}/100*\n\n"
            f"📍 {location}\n"
            f"💰 Precio: {price:,.0f}€ | {area:.0f}m² | {rooms} hab\n"
            f"📈 Rentabilidad bruta: *{gross_yield:.1f}%*\n"
            f"📊 Rentabilidad neta: {net_yield:.1f}%\n"
            f"🏠 Alquiler estimado: {monthly_rent:.0f}€/mes\n"
            f"⏳ Payback: {payback:.0f} años\n\n"
            f"[Ver anuncio]({url})"
        )
        return msg
