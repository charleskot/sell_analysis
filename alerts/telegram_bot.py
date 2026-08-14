"""Telegram alerts and daily digest for real estate investment opportunities."""
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

        # Loud startup log so it's easy to see on Railway
        if self._enabled:
            token_preview = f"{self._token[:8]}...{self._token[-4:]}"
            logger.info(
                f"Telegram alerts ENABLED (token={token_preview}, "
                f"chat_id={self._chat_id}, min_score={self._min_score})"
            )
        else:
            has_token = bool(self._token) and not self._token.startswith("${")
            has_chat = bool(self._chat_id) and not self._chat_id.startswith("${")
            logger.warning(
                f"Telegram alerts DISABLED — has_token={has_token}, has_chat_id={has_chat}. "
                f"Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID env vars."
            )

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

    def send_alert(self, listing_id: str, listing: dict, metrics: dict, score: float,
                   dedup_key: str | None = None) -> bool:
        """Send individual property alert if score meets threshold and cooldown allows.

        dedup_key suppresses repeat alerts for the *same property* listed under
        different ids. In Spain the same flat is routinely marketed by several
        agencies, each with its own listing id, so the per-id cooldown alone
        would send one alert per agency for one flat.
        """
        from models.db import was_alert_sent_recently, record_alert_sent

        if not self._enabled:
            return False

        # Deliberately no score threshold here. Whether a listing is worth
        # sending is the caller's decision — the search profiles make it now.
        # The composite score measures rental return, which says nothing about
        # a home to live in: the flats matching that profile score 30-40 and
        # were being dropped in silence.
        if was_alert_sent_recently(listing_id, self._cooldown_hours):
            logger.debug(f"Alert cooldown active for {listing_id}")
            return False

        if dedup_key and was_alert_sent_recently(dedup_key, self._cooldown_hours):
            logger.info(f"Skipping {listing_id}: same property already alerted ({dedup_key})")
            return False

        message = self._format_message(listing, metrics, score)
        keyboard = self._feedback_keyboard(listing_id)
        message_id = self._send_sync(message, reply_markup=keyboard)

        if message_id:
            record_alert_sent(listing_id, "telegram", message[:200], chat_message_id=message_id)
            if dedup_key:
                # Recorded against the property signature so a re-listing by
                # another agency is recognised as the same flat.
                record_alert_sent(dedup_key, "telegram", "", chat_message_id=message_id)
            logger.info(f"Telegram alert sent for {listing_id} (score {score:.0f})")
            return True
        return False

    @staticmethod
    def property_signature(listing: dict) -> str | None:
        """Stable fingerprint for 'the same flat', independent of listing id.

        Price, built area, rooms and city together identify a property closely
        enough in practice. Returns None when any component is missing, so an
        incomplete listing is never deduped against an unrelated one.
        """
        price = listing.get("price")
        area = listing.get("area_m2")
        rooms = listing.get("rooms")
        city = (listing.get("city") or "").strip().lower()

        if not price or not area or rooms is None or not city or city == "desconocido":
            return None
        return f"sig:{city}:{price:.0f}:{area:.0f}:{rooms}"

    @staticmethod
    def _feedback_keyboard(listing_id: str) -> dict:
        """Inline keyboard with 3 feedback buttons per alert."""
        return {
            "inline_keyboard": [[
                {"text": "👍 Me interesa", "callback_data": f"fb:yes:{listing_id}"},
                {"text": "🤔 Ver luego", "callback_data": f"fb:maybe:{listing_id}"},
                {"text": "👎 No", "callback_data": f"fb:no:{listing_id}"},
            ]]
        }

    def poll_feedback(self) -> int:
        """Poll Telegram for callback_query (button clicks) and text replies.
        Records feedback in DB. Returns number of events processed."""
        if not self._enabled:
            return 0

        import requests
        from models.db import (
            get_telegram_state, set_telegram_state, record_feedback,
            get_engine,
        )
        from models.schema import alerts_sent
        from sqlalchemy import select

        offset = get_telegram_state("last_update_id", "0")
        try:
            offset_int = int(offset) + 1
        except ValueError:
            offset_int = 0

        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self._token}/getUpdates",
                params={"offset": offset_int, "timeout": 0, "allowed_updates": '["callback_query","message"]'},
                timeout=15,
            )
            data = r.json()
        except Exception as e:
            logger.error(f"poll_feedback getUpdates failed: {e}")
            return 0

        if not data.get("ok"):
            logger.error(f"getUpdates non-ok: {data}")
            return 0

        n = 0
        max_id = int(offset)
        for upd in data.get("result", []):
            max_id = max(max_id, upd["update_id"])

            # 1) Button click
            cb = upd.get("callback_query")
            if cb:
                cb_data = cb.get("data", "")
                if cb_data.startswith("fb:"):
                    parts = cb_data.split(":", 2)
                    if len(parts) == 3:
                        _, verdict, lid = parts
                        try:
                            record_feedback(lid, verdict)
                            n += 1
                            # Acknowledge the button so it doesn't hang
                            self._answer_callback(cb["id"], self._feedback_ack(verdict))
                            # Edit message to append feedback line
                            msg = cb.get("message", {})
                            self._append_feedback_line(
                                msg.get("chat", {}).get("id"),
                                msg.get("message_id"),
                                msg.get("text", ""),
                                verdict,
                            )
                        except Exception as e:
                            logger.error(f"record_feedback failed: {e}")
                continue

            # 2) Text reply to an alert (free-form note)
            msg = upd.get("message")
            if msg and msg.get("reply_to_message"):
                replied_to_id = msg["reply_to_message"].get("message_id")
                note = msg.get("text", "").strip()
                if replied_to_id and note:
                    # Look up which listing the reply refers to
                    with get_engine().connect() as conn:
                        row = conn.execute(
                            select(alerts_sent.c.listing_id)
                            .where(alerts_sent.c.chat_message_id == replied_to_id)
                            .order_by(alerts_sent.c.id.desc())
                            .limit(1)
                        ).fetchone()
                    if row:
                        try:
                            record_feedback(row.listing_id, "note", note)
                            n += 1
                            logger.info(f"Text feedback saved for {row.listing_id}: {note[:50]}")
                        except Exception as e:
                            logger.error(f"record_feedback (note) failed: {e}")

        set_telegram_state("last_update_id", str(max_id))
        total = len(data.get("result", []))
        logger.info(f"poll_feedback: {n} feedback events, {total} raw updates, offset={offset_int}")
        return n

    @staticmethod
    def _feedback_ack(verdict: str) -> str:
        return {
            "yes": "👍 Guardado como 'me interesa'",
            "no": "👎 Guardado como 'no me gusta'",
            "maybe": "🤔 Guardado como 'ver luego'",
        }.get(verdict, "Guardado")

    def _answer_callback(self, callback_query_id: str, text: str) -> None:
        import requests
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._token}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"answerCallbackQuery failed: {e}")

    def _append_feedback_line(self, chat_id, message_id, current_text: str, verdict: str) -> None:
        """Edit the alert message to reflect the recorded feedback."""
        if not chat_id or not message_id:
            return
        import requests
        label = self._feedback_ack(verdict)
        new_text = f"{current_text}\n\n_{label}_"
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._token}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": new_text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"editMessageText failed: {e}")

    def send_daily_digest(self) -> bool:
        """Send a daily summary of NEW opportunities (first seen in last 24h).
        Sends nothing if no new listings — no noise.
        """
        if not self._enabled:
            return False

        top_listings = self._get_top_listings(limit=5, min_score=self._min_score, hours=24)
        if not top_listings:
            logger.info("Daily digest: no new listings in last 24h → skipping send")
            return False
        message = self._format_digest(top_listings)
        result = self._send_sync(message)
        if result:
            logger.info(f"Daily digest sent: {len(top_listings)} NEW top opportunities (24h)")
            return True
        return False

    def _get_top_listings(self, limit: int = 5, min_score: float = 0, hours: int = 24) -> list[dict]:
        """Query DB for top-scoring listings FIRST SEEN in the last `hours`.

        The user only wants new opportunities, not the same top-5 every day.
        """
        try:
            from datetime import datetime, timedelta, timezone
            from models.db import get_engine
            from models.schema import listings, investment_metrics
            from sqlalchemy import select, and_

            since = datetime.now(timezone.utc) - timedelta(hours=hours)

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
                        listings.c.first_seen_at >= since,
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

    def _send_sync(self, message: str, reply_markup: dict | None = None) -> int | bool:
        """Send a Telegram message. Returns the message_id on success (truthy),
        False on failure. reply_markup adds inline buttons.
        """
        import requests
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(url, json=payload, timeout=15)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                return int(data["result"].get("message_id") or 0) or True
            logger.error(f"Telegram send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"Telegram send exception: {e}")
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
        monthly_rent = metrics.get("estimated_monthly_rent", 0) or 0
        ppm2 = metrics.get("price_per_m2", 0) or 0

        # Which of the user's searches this listing answers
        header = metrics.get("matched_profiles") or "Nueva oportunidad"
        stars = "⭐" * min(5, int(score / 20))

        msg = (
            f"*{header}*  {stars}\n\n"
            f"📍 {location}\n"
            f"💰 {price:,.0f}€ · {area:.0f}m² · {rooms} hab · {ppm2:,.0f}€/m²\n"
        )

        # Leverage figures are the ones that decide an investment, so they lead
        # when financing is configured.
        cash_needed = metrics.get("cash_needed")
        cashflow = metrics.get("monthly_cashflow")
        payment = metrics.get("monthly_payment")

        if cash_needed is not None and cashflow is not None:
            msg += (
                f"\n🏦 Entrada + gastos: *{cash_needed:,.0f}€*\n"
                f"📉 Cuota: {payment:,.0f}€/mes\n"
                f"💵 Alquiler estimado: {monthly_rent:,.0f}€/mes\n"
                f"{'🟢' if cashflow > 0 else '🔴'} Cashflow: *{cashflow:+,.0f}€/mes*\n"
                f"📈 Rentabilidad neta: *{net_yield:.1f}%*  (bruta {gross_yield:.1f}%)\n"
            )
        else:
            msg += (
                f"\n💵 Alquiler estimado: {monthly_rent:,.0f}€/mes\n"
                f"📈 Rentabilidad neta: *{net_yield:.1f}%*  (bruta {gross_yield:.1f}%)\n"
            )

        # The rent figure comes from zone averages, not comparables for this
        # flat — worth saying, because every number above depends on it.
        msg += f"\n_Alquiler estimado por zona, contrastar antes de decidir._\n"
        msg += f"\n[Ver anuncio]({url})"
        return msg
