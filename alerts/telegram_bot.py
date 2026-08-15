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

    def already_sent(self, listing_id: str, dedup_key: str | None = None) -> bool:
        """Whether this flat has already gone out, under any listing id."""
        from models.db import was_alert_sent_recently

        if was_alert_sent_recently(listing_id, self._cooldown_hours):
            return True
        return bool(dedup_key and was_alert_sent_recently(dedup_key, self._cooldown_hours))

    def send_digest_header(self, total: int, pending: int) -> bool:
        """The daily line that frames whatever cards follow it.

        The digest used to be one long list of five listings. Each entry had
        room for a star rating and nothing else, which is how it came to
        recommend flats without saying anything about them. The listings now
        go out one per message, in the same full card as an alert, and this
        is the note that introduces them — or reports that there is nothing,
        which is information too rather than silence.
        """
        if not self._enabled:
            return False
        return bool(self._send_sync(self._digest_header_text(total, pending)))

    def _digest_header_text(self, total: int, pending: int) -> str:
        from datetime import datetime

        today = datetime.now().strftime("%d/%m")
        if total == 0:
            body = ("Nada encaja todavía con tus búsquedas.\n"
                    "<i>Sigo leyendo el correo cada 3 minutos. En cuanto salga algo, "
                    "te llega aquí al momento.</i>")
        elif pending == 0:
            body = (f"<b>{total}</b> encajan ahora mismo, y ya te las mandé todas.\n"
                    "<i>Nada nuevo desde entonces.</i>")
        else:
            plural = "s" if pending > 1 else ""
            body = (f"<b>{total}</b> encajan · te mando <b>{pending}</b> "
                    f"nueva{plural} ahora ↓")

        return f"📊 <b>Resumen — {today}</b>\n\n{body}"

    @staticmethod
    def _money(value) -> str:
        """Spanish thousands separator. 186.000, not 186,000."""
        if value is None:
            return "?"
        return f"{value:,.0f}".replace(",", ".")

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
        """One property, with everything known about it.

        Two formatters used to diverge here, and the one for homes had grown
        thin: location, price, size, done. The buyer was then opening every
        listing to find out the floor, the condition, whether the price had
        already been cut and what the flat actually is — which is the work
        this bot exists to save.
        """
        purpose = metrics.get("matched_purpose")
        if purpose is None:
            purpose = "home" if metrics.get("match_profile") else "investment"

        city = (listing.get("city") or "").title()
        district = (listing.get("district") or "").title()
        where = f"{district}, {city}" if district else city or "?"
        price = listing.get("price") or 0
        url = listing.get("url", "")

        banner = "🏠 <b>PARA VIVIR</b>" if purpose == "home" else "💰 <b>PARA INVERTIR</b>"
        labels = metrics.get("matched_profiles") or ""
        out = [f"{banner}{'  ·  ' + self._esc(labels) if labels else ''}", ""]
        out.append(f"📍 <b>{self._esc(where)}</b>")

        spec = [f"<b>{self._money(price)}€</b>"]
        if listing.get("area_m2"):
            spec.append(f"{self._money(listing['area_m2'])}m²")
        if listing.get("rooms") is not None:
            spec.append(f"{listing['rooms']} hab")
        if listing.get("bathrooms"):
            spec.append(f"{listing['bathrooms']} baños")
        if metrics.get("price_per_m2"):
            spec.append(f"{self._money(metrics['price_per_m2'])}€/m²")
        out.append("💰 " + " · ".join(spec))

        # What the flat is, physically. Floor and condition decide a home as
        # firmly as the price does, and neither was being shown. The portal
        # is not part of that — it rides along with the link instead, so a
        # listing that arrived without floor or condition does not get a
        # line saying only "habitaclia".
        building = [self._esc(listing["floor"]) if listing.get("floor") else None,
                    self._condition_label(listing, metrics)]
        building = [b for b in building if b]
        if building:
            out.append("🏗 " + " · ".join(building))

        age = self._age_line(listing)
        if age:
            out.append(age)
        drop = self._price_move_line(listing)
        if drop:
            out.append(drop)

        out.append("")
        out.extend(self._money_block(metrics, purpose))

        if purpose == "investment":
            out.extend(self._zone_block(listing, metrics))

        out.extend(self._caveat_block(metrics, purpose))

        reasons = (metrics.get("score_breakdown") or {}).get("reasons_pass") or []
        if metrics.get("match_reason"):
            reasons = [metrics["match_reason"], *reasons]
        if reasons:
            out.append("")
            out.append("<b>Por qué encaja</b>")
            out.extend(f"  ✓ {self._esc(r)}" for r in reasons)

        blurb = self._blurb(listing)
        if blurb:
            out.append("")
            out.append(f"📝 <i>{self._esc(blurb)}</i>")

        out.append("")
        portal = listing.get("portal")
        out.append(f'<a href="{self._esc(url)}">Ver anuncio'
                   f'{" en " + self._esc(portal) if portal else ""}</a>')
        return "\n".join(out)

    def _money_block(self, metrics: dict, purpose: str) -> list[str]:
        """Entry cash and monthly payment — the two numbers that decide it."""
        out = []
        cash = metrics.get("cash_needed")
        gap = metrics.get("cash_gap") or 0
        mortgage = metrics.get("monthly_payment")
        gap_payment = metrics.get("gap_loan_payment") or 0
        total = metrics.get("monthly_payment_total") or mortgage

        if cash is not None:
            out.append(f"🏦 Entrada + gastos: <b>{self._money(cash)}€</b>")
            # Naming the shortfall matters: it separates a deal fundable with
            # the money in hand from one needing a second loan to enter.
            if gap > 0:
                out.append(f"   ↳ de tu bolsillo {self._money(cash - gap)}€ "
                           f"+ crédito {self._money(gap)}€")
            if metrics.get("reserve_used"):
                out.append("   ↳ usando la reserva por encima de los 30.000€")
        if mortgage:
            out.append(f"📉 Cuota hipoteca: {self._money(mortgage)}€/mes")
        if gap_payment > 0:
            out.append(f"📉 Cuota crédito: {self._money(gap_payment)}€/mes")
            out.append(f"   ↳ total <b>{self._money(total)}€/mes</b>")

        if purpose == "investment":
            rent = metrics.get("estimated_monthly_rent")
            cashflow = metrics.get("monthly_cashflow")
            net = metrics.get("net_yield_pct") or 0
            gross = metrics.get("gross_yield_pct") or 0
            payback = metrics.get("payback_years")
            if rent:
                out.append(f"💵 Alquiler estimado: {self._money(rent)}€/mes")
            if cashflow is not None:
                sign = "🟢" if cashflow > 0 else "🔴"
                out.append(f"{sign} Cashflow: <b>{cashflow:+,.0f}€/mes</b>".replace(",", "."))
            out.append(f"📈 Rentabilidad neta: <b>{net:.1f}%</b>  (bruta {gross:.1f}%)")
            if payback:
                out.append(f"⏳ Se paga sola en {payback:.0f} años")
        return out

    def _zone_block(self, listing: dict, metrics: dict) -> list[str]:
        """The zone, in the terms there is actual data for."""
        from analysis.municipalities import population_of

        pop = population_of(listing.get("city"))
        if not pop:
            return []
        # Town size stands in for everyday services and, more to the point,
        # for how long the flat takes to let and to sell again.
        return ["", f"🏙 {(listing.get('city') or '').title()} · {self._money(pop)} habitantes"]

    def _caveat_block(self, metrics: dict, purpose: str) -> list[str]:
        """Everything that makes the numbers above less certain.

        The rent caveats are only caveats if the flat is being let. On a home
        they describe a purchase nobody is making.
        """
        out = []
        if metrics.get("reform_cost"):
            out.append("")
            out.append(f"🔨 Incluye <b>{self._money(metrics['reform_cost'])}€</b> estimados de "
                       f"reforma (500€/m²), ya sumados a la entrada y descontados de la "
                       f"rentabilidad.")
        elif metrics.get("condition_unknown"):
            out.append("")
            out.append("❓ <b>Estado sin confirmar</b> — no pude leer la ficha, así que no sé "
                       "si necesita reforma. Si la necesita, los números empeoran.")

        if purpose != "investment":
            return out

        if metrics.get("rent_capped_zone"):
            out.append("")
            out.append("⚠️ <b>Zona tensionada</b> — el alquiler está topado por el índice de la "
                       "Generalitat, que suele quedar por debajo de la media. La rentabilidad "
                       "de arriba es un techo, no una previsión.")
        if metrics.get("estimated_monthly_rent"):
            out.append("")
            out.append("<i>Alquiler estimado por media de zona, no por comparables de este "
                       "piso. Contrástalo antes de decidir.</i>")
        return out

    def _age_line(self, listing: dict) -> str | None:
        """How long it has been on the market, as far as we have seen it.

        A flat that has sat for weeks is a different conversation from one
        that appeared this morning.
        """
        from datetime import datetime, timezone

        first = listing.get("first_seen_at")
        if not first:
            return None
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - first).days
        if days <= 0:
            return "🆕 Visto hoy por primera vez"
        if days == 1:
            return "📅 Lo vi por primera vez ayer"
        return f"📅 Lo vi por primera vez hace {days} días"

    def _price_move_line(self, listing: dict) -> str | None:
        """Whether the asking price has already moved, and by how much.

        A cut is the clearest signal a seller is negotiable, and it is data
        the bot has been quietly accumulating without ever showing it.
        """
        history = listing.get("price_history") or []
        current = listing.get("price")
        if not history or not current:
            return None
        try:
            first = float(history[0]["price"])
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        if not first or abs(first - current) < 1:
            return None
        delta = current - first
        pct = delta / first * 100
        if delta < 0:
            return (f"📉 Ha bajado desde {self._money(first)}€ "
                    f"(<b>{self._money(delta)}€</b>, {pct:.1f}%)")
        return f"📈 Ha subido desde {self._money(first)}€ (+{self._money(delta)}€, +{pct:.1f}%)"

    @staticmethod
    def _blurb(listing: dict, limit: int = 260) -> str | None:
        """The opening of the seller's own description.

        Everything else in the card is a number the bot derived; this is the
        only place the flat gets to describe itself. But what the email
        parsers salvage is often not prose at all — leftover table pipes,
        tracking URLs and half-escaped tags from the alert email. Showing
        that is worse than showing nothing, so anything carrying markup is
        dropped rather than cleaned: a half-repaired fragment still reads as
        a broken bot.
        """
        text = " ".join((listing.get("description") or "").split())
        if len(text) < 40:
            return None
        if any(marker in text for marker in ("http", "<", ">", "&gt;", "&lt;", "&amp;", "|")):
            return None
        if len(text) <= limit:
            return text
        cut = text[:limit].rsplit(" ", 1)[0]
        return f"{cut}…"
