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

    def poll_feedback(self, command_handler=None) -> int:
        """Poll Telegram for button clicks, text replies and commands.

        command_handler is called with any standalone message — one that is
        not a reply to an alert — and whatever string it returns is sent back.
        It lives outside this class because answering "what have we got"
        needs the profile matcher, which the alerter has no business knowing
        about.
        """
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

            msg = upd.get("message")

            # 2) A standalone message is an instruction, not a note
            if msg and not msg.get("reply_to_message") and command_handler:
                text = (msg.get("text") or "").strip()
                if text:
                    try:
                        reply = command_handler(text)
                    except Exception as e:
                        logger.error(f"command handler failed: {e}")
                        reply = "Algo ha fallado al preparar la respuesta."
                    if reply:
                        self._send_sync(reply)
                        n += 1
                        continue

            # 3) Text reply to an alert (free-form note)
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
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"editMessageText failed: {e}")

    def send_digest(self, entries: list[dict]) -> bool:
        """Send the daily digest of everything that currently matches.

        `entries` come from the same profile matching as the alerts, already
        deduplicated. This used to be its own SQL query ordered by the old
        0-100 score, which is how a 58 m² one-bedroom in a town no home
        search covers ended up at the top of a "Top 5 viviendas" list, with
        no analysis attached to explain itself.
        """
        if not self._enabled:
            return False
        return bool(self._send_sync(self._format_digest(entries)))

    @staticmethod
    def _money(value) -> str:
        """Spanish thousands separator. 186.000, not 186,000."""
        if value is None:
            return "?"
        return f"{value:,.0f}".replace(",", ".")

    def _format_digest(self, entries: list[dict]) -> str:
        from datetime import datetime

        today = datetime.now().strftime("%d/%m/%Y")
        if not entries:
            return (
                f"📊 <b>Resumen — {today}</b>\n\n"
                "Nada encaja ahora mismo con ninguna de tus búsquedas.\n"
                "<i>Sigo leyendo el correo cada 3 minutos.</i>"
            )

        fresh = sum(1 for e in entries if e.get("is_new"))
        lines = [
            f"📊 <b>Resumen — {today}</b>",
            f"<i>{len(entries)} encajan · {fresh} nuevas en 24h</i>",
            "",
        ]

        for purpose, heading in (("investment", "💰 <b>PARA INVERTIR</b>"),
                                 ("home", "🏠 <b>PARA VIVIR</b>")):
            group = [e for e in entries if e["profile"].purpose == purpose]
            if not group:
                continue
            lines.append(heading)
            lines.append("")
            for i, entry in enumerate(group[:5], 1):
                lines.append(self._digest_entry(i, entry, purpose))
                lines.append("")
            if len(group) > 5:
                lines.append(f"<i>…y {len(group) - 5} más. Escribe "
                             f"<b>qué tenemos</b> para verlas todas.</i>")
                lines.append("")

        return "\n".join(lines).strip()

    def _digest_entry(self, i: int, entry: dict, purpose: str) -> str:
        """One listing, with the analysis that decides it.

        A digest line without the numbers is worse than no digest: it looks
        like a recommendation while hiding everything the recommendation
        rests on.
        """
        from analysis.municipalities import population_of

        listing, metrics = entry["listing"], entry["metrics"]
        city = (listing.get("city") or "").title()
        district = (listing.get("district") or "").title()
        where = f"{district}, {city}" if district else city or "?"
        url = listing.get("url", "")
        area = listing.get("area_m2")
        ppm2 = metrics.get("price_per_m2")

        flag = "🆕 " if entry.get("is_new") else ""
        out = [f"{i}. {flag}<b>{self._esc(where)} — {self._money(listing.get('price'))}€</b>"]

        spec = [f"{self._money(area)}m²" if area else None,
                f"{listing.get('rooms')} hab" if listing.get("rooms") else None,
                f"{self._money(ppm2)}€/m²" if ppm2 else None,
                self._condition_label(listing, metrics)]
        out.append("   " + " · ".join(s for s in spec if s))

        cash = metrics.get("cash_needed")
        gap = metrics.get("cash_gap") or 0
        total_payment = metrics.get("monthly_payment_total") or metrics.get("monthly_payment")
        if cash is not None:
            cash_line = f"   🏦 Entrada {self._money(cash)}€"
            if gap > 0:
                cash_line += f" (tuyo {self._money(cash - gap)}€ + crédito {self._money(gap)}€)"
            if total_payment:
                cash_line += f" · 📉 cuota {self._money(total_payment)}€/mes"
            out.append(cash_line)

        if purpose == "investment":
            rent = metrics.get("estimated_monthly_rent")
            cashflow = metrics.get("monthly_cashflow")
            net = metrics.get("net_yield_pct") or 0
            gross = metrics.get("gross_yield_pct") or 0
            if rent:
                out.append(f"   💵 Alquiler est. {self._money(rent)}€/mes")
            if cashflow is not None:
                sign = "🟢" if cashflow > 0 else "🔴"
                out.append(f"   {sign} Cashflow {cashflow:+,.0f}€/mes".replace(",", ".")
                           + f" · 📈 neta <b>{net:.1f}%</b> (bruta {gross:.1f}%)")
            else:
                out.append(f"   📈 Rentabilidad neta <b>{net:.1f}%</b> (bruta {gross:.1f}%)")

        # The zone, in the two terms there is actual data for: how big the
        # town is, and whether its rents are legally capped. Both are about
        # letting the flat out, so neither belongs on one he will live in —
        # a rent cap does not affect the buyer of his own home.
        if purpose == "investment":
            zone = []
            pop = population_of(listing.get("city"))
            if pop:
                zone.append(f"{self._money(pop)} hab")
            if metrics.get("rent_capped_zone"):
                zone.append("⚠️ zona tensionada (alquiler topado)")
            if zone:
                out.append(f"   🏙 {' · '.join(zone)}")

        if metrics.get("reform_cost"):
            out.append(f"   🔨 Reforma estimada {self._money(metrics['reform_cost'])}€ "
                       f"(ya sumada arriba)")
        elif metrics.get("condition_unknown"):
            out.append("   ❓ Estado sin confirmar — no pude leer la ficha")

        # Which of his searches this answers. The matcher's own reason string
        # restates the money already shown a line above; the search name is
        # the thing he cannot work out for himself from the numbers.
        if metrics.get("matched_profiles"):
            out.append(f"   ✓ {self._esc(metrics['matched_profiles'])}")
        out.append(f'   <a href="{self._esc(url)}">Ver anuncio</a>')
        return "\n".join(out)

    @staticmethod
    def _condition_label(listing: dict, metrics: dict) -> str | None:
        if metrics.get("condition_unknown"):
            return None
        return {"nuevo": "obra nueva", "buen_estado": "buen estado",
                "a_reformar": "a reformar"}.get(listing.get("condition"))

    def _send_sync(self, message: str, reply_markup: dict | None = None) -> int | bool:
        """Send a Telegram message. Returns the message_id on success (truthy),
        False on failure. reply_markup adds inline buttons.
        """
        import requests
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
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

    @staticmethod
    def _esc(text) -> str:
        """Escape the three characters Telegram's HTML mode treats as markup."""
        return (str(text).replace("&", "&amp;")
                         .replace("<", "&lt;")
                         .replace(">", "&gt;"))

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
            f"<b>{stars} NUEVA VIVIENDA — Match {score:.0f}/100 — {self._esc(profile_label)}</b>\n\n"
            f"📍 {self._esc(location)}\n"
            f"💰 {price:,.0f}€ | {area:.0f}m² | {rooms} hab"
        )
        if floor:
            msg += f" | {self._esc(floor)}"
        msg += f'\n{self._esc(reasons_txt)}\n\n<a href="{self._esc(url)}">Ver anuncio</a>'
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
            f"<b>{self._esc(header)}</b>  {stars}\n\n"
            f"📍 {self._esc(location)}\n"
            f"💰 {price:,.0f}€ · {area:.0f}m² · {rooms} hab · {ppm2:,.0f}€/m²\n"
        )

        # A home and an investment are judged on different numbers. Rent,
        # yield and cashflow describe a flat that is let out; on one the buyer
        # will live in they are noise, and showing "cashflow -924€/mes" on the
        # flat someone wants to move into misreads the whole purchase.
        if metrics.get("matched_purpose") == "home":
            cash_needed = metrics.get("cash_needed")
            gap = metrics.get("cash_gap") or 0
            payment = metrics.get("monthly_payment_total") or metrics.get("monthly_payment")
            if cash_needed is not None:
                msg += f"\n🏦 Entrada + gastos: <b>{cash_needed:,.0f}€</b>\n"
                if gap > 0:
                    msg += f"   ↳ de tu bolsillo {cash_needed - gap:,.0f}€ + crédito {gap:,.0f}€\n"
            if payment:
                msg += f"📉 Cuota total: <b>{payment:,.0f}€/mes</b>\n"
            msg += f"\n<a href=\"{self._esc(url)}\">Ver anuncio</a>"
            return msg

        # Leverage figures are the ones that decide an investment, so they lead
        # when financing is configured.
        cash_needed = metrics.get("cash_needed")
        cashflow = metrics.get("monthly_cashflow")
        payment = metrics.get("monthly_payment")

        if cash_needed is not None and cashflow is not None:
            gap = metrics.get("cash_gap") or 0
            gap_payment = metrics.get("gap_loan_payment") or 0
            total_payment = metrics.get("monthly_payment_total") or payment

            msg += f"\n🏦 Entrada + gastos: <b>{cash_needed:,.0f}€</b>\n"
            # Naming the shortfall matters: it is the difference between a
            # deal the buyer can fund and one that needs a second loan.
            if gap > 0:
                msg += f"   ↳ de tu bolsillo {cash_needed - gap:,.0f}€ + crédito {gap:,.0f}€\n"
            msg += f"📉 Cuota hipoteca: {payment:,.0f}€/mes\n"
            if gap_payment > 0:
                msg += f"📉 Cuota crédito: {gap_payment:,.0f}€/mes\n"
                msg += f"   ↳ total {total_payment:,.0f}€/mes\n"
            msg += (
                f"💵 Alquiler estimado: {monthly_rent:,.0f}€/mes\n"
                f"{'🟢' if cashflow > 0 else '🔴'} Cashflow: <b>{cashflow:+,.0f}€/mes</b>\n"
                f"📈 Rentabilidad neta: <b>{net_yield:.1f}%</b>  (bruta {gross_yield:.1f}%)\n"
            )
        else:
            msg += (
                f"\n💵 Alquiler estimado: {monthly_rent:,.0f}€/mes\n"
                f"📈 Rentabilidad neta: <b>{net_yield:.1f}%</b>  (bruta {gross_yield:.1f}%)\n"
            )

        # The rent figure comes from zone averages, not comparables for this
        # flat — worth saying, because every number above depends on it.
        if metrics.get("reform_cost"):
            msg += (
                f"\n🔨 Incluye <b>{metrics['reform_cost']:,.0f}€</b> estimados de reforma "
                f"(500€/m²), ya sumados a la entrada y descontados de la rentabilidad.\n"
            )
        elif metrics.get("condition_unknown"):
            msg += (
                "\n❓ <b>Estado sin confirmar</b> — este anuncio llegó sin descripción, "
                "así que no sé si necesita reforma. Si la necesita, la rentabilidad baja.\n"
            )

        if metrics.get("rent_capped_zone"):
            msg += (
                "\n⚠️ <b>Zona tensionada</b> — el alquiler está topado por el "
                "índice de la Generalitat, que suele quedar por debajo de la media. "
                "La rentabilidad de arriba es un techo, no una previsión: "
                "consulta el índice antes de comprometerte.\n"
            )
        msg += f"\n<i>Alquiler estimado por zona, contrastar antes de decidir.</i>\n"
        msg += f'\n<a href="{self._esc(url)}">Ver anuncio</a>'
        return msg
