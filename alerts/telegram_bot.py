"""Telegram alerts and daily digest for real estate investment opportunities."""
import logging
import os

from alerts import budget

logger = logging.getLogger(__name__)


class TelegramAlerter:
    def __init__(self, config: dict):
        alert_cfg = config.get("alerts", {}).get("telegram", {})
        self._token = self._resolve(alert_cfg.get("token", ""))
        self._chat_id = self._resolve(alert_cfg.get("chat_id", ""))
        self._min_score = alert_cfg.get("min_score_threshold", 70)
        self._cooldown_hours = alert_cfg.get("cooldown_hours", 168)
        self._config = config
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
        from models.db import record_alert_sent
        from scheduler.quiet_hours import is_quiet

        if not self._enabled:
            return False

        # Nothing is recorded as sent while quiet, so this listing is picked
        # up and sent by the first waking cycle rather than lost. Suppressing
        # the message is the easy half; not dropping the flat is the point.
        if is_quiet(self._config):
            logger.info(f"Horario nocturno: {listing_id} esperará a mañana")
            return False

        # Deliberately no score threshold here. Whether a listing is worth
        # sending is the caller's decision — the search profiles make it now.
        # The composite score measures rental return, which says nothing about
        # a home to live in: the flats matching that profile score 30-40 and
        # were being dropped in silence.

        # Ever, not within a cooldown window. A window made sense when the
        # only way to reach here was a freshly parsed email, which is read
        # once; it stops making sense the moment the same listing can be
        # re-read — repairing a bad parse would re-send everything older
        # than the window. Nothing here ever wants the same flat twice.
        if self.already_sent(listing_id, dedup_key):
            logger.debug(f"{listing_id} ya enviado, no se repite")
            return False

        # The same flat, listed again by another agency that measured it
        # differently. Identical asking price in the same town with a
        # similar area is that flat, not a new one.
        from models.db import was_similar_listing_sent

        if was_similar_listing_sent(listing.get("city"), listing.get("price"),
                                    listing.get("area_m2"),
                                    listing.get("district")):
            logger.info(f"{listing_id}: mismo piso ya enviado con otras medidas")
            from models.db import bump_daily
            bump_daily("duplicado")
            return False

        message = self._format_message(listing, metrics, score)
        keyboard = self._feedback_keyboard(listing_id)
        message_id = self._send_sync(message, reply_markup=keyboard, kind="piso")

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
                if cb_data == "fb:done":
                    # The stamp left in place of the buttons. Tapping it is
                    # not a new verdict; acknowledge so it does not spin.
                    self._answer_callback(cb["id"], "Ya clasificado")
                    continue
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
                        self._send_sync(reply, kind="respuesta")
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

    VERDICT_STAMP = {
        "yes": "✅ ME INTERESA",
        "no": "❌ DESCARTADO",
        "maybe": "🤔 VER LUEGO",
    }

    def _append_feedback_line(self, chat_id, message_id, current_text: str, verdict: str) -> None:
        """Stamp the verdict onto the card, replacing the buttons.

        Only the keyboard is touched. Rewriting the text meant re-sending it
        as plain text — Telegram hands the body back with the markup already
        stripped — which flattened "Ver anuncio" into a word you cannot tap.
        It also wrote the confirmation in Markdown while sending as HTML, so
        the underscores arrived literally.

        Replacing the keyboard leaves the card intact and settles it
        visually: three taps to judge become one stamp, and a card already
        judged no longer looks like one still waiting, which is what made
        scrolling back confusing.
        """
        if not chat_id or not message_id:
            return
        import requests

        stamp = self.VERDICT_STAMP.get(verdict, "GUARDADO")
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._token}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": [[
                        {"text": stamp, "callback_data": "fb:done"},
                    ]]},
                },
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"editMessageReplyMarkup failed: {e}")

    def send_pulse(self, stats: dict | None = None, error: str | None = None) -> bool:
        """Proof of life, whether or not there is anything to report.

        Silence is ambiguous: a quiet mailbox and a dead bot read exactly
        the same from the chat, and the bot did sit dead for twenty hours
        without either side noticing. A line every half hour costs almost
        nothing and removes the ambiguity entirely.
        """
        if not self._enabled:
            return False
        return bool(self._send_sync(self._pulse_text(stats, error), kind="fallo"))

    def _pulse_text(self, stats: dict | None, error: str | None) -> str:
        from datetime import datetime

        now = datetime.now().strftime("%H:%M")
        if error:
            return (f"🔴 <b>{now}</b> · sigo en marcha, pero <b>falla la lectura del "
                    f"correo</b>\n<i>{self._esc(error)}</i>")

        stats = stats or {}
        alerts = stats.get("alerts_sent") or 0
        if alerts:
            # An alert already proved the bot is alive, so this only counts up.
            return f"🟢 <b>{now}</b> · {alerts} enviadas en esta pasada ↑"

        parsed = stats.get("parsed") or 0
        if parsed:
            return (f"🟢 <b>{now}</b> · funcionando · {parsed} anuncios revisados, "
                    f"<b>ninguno encaja</b>")
        return f"🟢 <b>{now}</b> · funcionando · <b>sin novedades</b>"

    def send_unreadable_warning(self, problems: list) -> bool:
        """Tell the chat that a portal wrote and we could not read it.

        This failure is otherwise invisible: a portal whose link format we do
        not match looks exactly like a portal with nothing to send. Habitaclia
        sat in that state for days. Four bank portals were added without ever
        having seen one of their emails, so it is worth saying out loud.
        """
        if not self._enabled or not problems:
            return False
        return bool(self._send_sync(self._unreadable_text(problems), kind="fallo"))

    def _unreadable_text(self, problems: list) -> str:
        portals = sorted({p for p, _ in problems})
        subject = problems[0][1][:80]
        return (
            f"⚠️ <b>No sé leer el correo de {self._esc(', '.join(portals))}</b>\n\n"
            f"Trae precios, así que es una alerta de verdad, pero no reconozco el "
            f"formato de sus enlaces y me quedo sin los anuncios.\n\n"
            f"<i>{self._esc(subject)}</i>\n\n"
            f"Reenvíame ese email y lo arreglo."
        )

    def send_truncation_warning(self, truncations: list) -> bool:
        """Tell the chat which alert is too wide to fit in an email.

        Habitaclia announced 855 new listings across eight emails and the
        emails carried 141 — a "comarca Barcelonès" alert delivered 26 of
        405. Nothing is broken: the portal truncates its own mail. But from
        the chat it reads as the bot missing things, and the fix is on the
        portal, so it has to reach the person who can make it.
        """
        if not self._enabled or not truncations:
            return False
        return bool(self._send_sync(self._truncation_text(truncations), kind="fallo"))

    def _truncation_text(self, truncations: list) -> str:
        worst = sorted(truncations, key=lambda t: t[2] / max(t[1], 1))[:3]
        lines = ["⚠️ <b>Una alerta tuya es demasiado amplia</b>", ""]
        for portal, claimed, got, subject in worst:
            lines.append(f"<b>{self._esc(portal)}</b> dice traer {claimed} anuncios "
                         f"y el correo solo trae <b>{got}</b> "
                         f"({100 * got // max(claimed, 1)}%)")
            lines.append(f"  <i>{self._esc(subject[:70])}</i>")
        lines.append("")
        lines.append("El portal corta el correo. Divide esa búsqueda en otras más "
                     "estrechas — por barrio, o con tope de precio — y dejarás de "
                     "perder anuncios.")
        return "\n".join(lines)

    def already_sent(self, listing_id: str, dedup_key: str | None = None) -> bool:
        """Whether this flat has ever gone out, under any listing id.

        Ever, not recently: this gates the sweeps that re-examine stored
        listings, and every listing leaves a 24-hour window eventually. On
        the cooldown question a nightly sweep would re-send the catalogue
        once a day, for ever.
        """
        from models.db import was_alert_ever_sent

        if was_alert_ever_sent(listing_id):
            return True
        return bool(dedup_key and was_alert_ever_sent(dedup_key))

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
        return bool(self._send_sync(self._digest_header_text(total, pending), kind="resumen"))

    def _digest_header_text(self, total: int, pending: int) -> str:
        from datetime import datetime

        # Only ever called with something to announce. The "nothing new"
        # wordings that used to live here were the noise: a message whose
        # whole content was that there was no message.
        today = datetime.now().strftime("%d/%m")
        plural = "s" if pending > 1 else ""
        return (f"📊 <b>Resumen — {today}</b>\n\n"
                f"<b>{total}</b> encajan · te mando <b>{pending}</b> "
                f"nueva{plural} ahora ↓")

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

    def _send_sync(self, message: str, reply_markup: dict | None = None,
                   kind: str = budget.UNLABELLED) -> int | bool:
        """Send a Telegram message, if the day's budget still allows it.

        The single point everything leaves through, and therefore the only
        place worth counting. Twice in one day a bug turned a scheduled
        message into one every three minutes; both times the fix reasoned
        about the code path that caused it, which defends against the
        mistake already made rather than the next one. The ceiling here is
        absolute: budget spent means the message is dropped, whatever asked
        for it.

        `kind` must be declared in config. An unlabelled send is refused,
        so a future code path that forgets to say what it is goes silent
        instead of unbounded.
        """
        import requests
        allowed, why = budget.check(self._config, kind)
        if not allowed:
            logger.warning(f"Mensaje '{kind}' NO enviado: {why}")
            return False

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
                budget.record(kind)
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
        if purpose == "investment" and metrics.get("suspicion"):
            banner = "⚠️ <b>SOSPECHOSO</b> · " + banner
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

        # What the seller says about light — never inferred from silence
        if metrics.get("bright"):
            out.append("☀️ " + " · ".join(metrics["bright"][:3]))
        elif metrics.get("interior"):
            out.append("🌑 interior (menos luz)")

        age = self._age_line(listing)
        if age:
            out.append(age)
        drop = self._price_move_line(listing)
        if drop:
            out.append(drop)

        out.append("")
        out.extend(self._money_block(metrics, purpose))

        out.extend(self._market_block(metrics))

        if purpose == "investment":
            out.extend(self._zone_block(listing, metrics))

        # The only structured danger signal there is data for. Named as what
        # it is — income, not crime — so the reader knows what was measured.
        if metrics.get("watchlist_zone"):
            out.append("")
            out.append("🚨 <b>Barrio entre los de menor renta del área</b> (Atlas INE): "
                       "más difícil de alquilar y revender. Visita la zona antes "
                       "de decidir.")

        out.extend(self._caveat_block(metrics, purpose))

        # Led with, not buried: if this is a flat somebody is living in, it
        # is the only thing on the card that matters.
        if purpose == "investment" and metrics.get("suspicion"):
            out.append("")
            out.append("⚠️ <b>Míralo con lupa</b>")
            out.extend(f"  • {self._esc(r)}" for r in metrics["suspicion"])
            out.append("<i>Comprueba en la ficha si está ocupado o si el banco "
                       "no da hipoteca. Las fotos del exterior son mala señal.</i>")

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

    def _market_block(self, metrics: dict) -> list[str]:
        """What the zone actually asks per m², and where this flat sits.

        Built from the listings the bot itself has ingested — the same pool
        the reader is choosing from — not from a published index. The spread
        (p25–p75) says more than any single average, and the delta against
        the median is the number that turns "48% under the zone" from a
        suspicion into a measurement.
        """
        zm = metrics.get("zone_market")
        if not zm:
            return []
        out = ["", f"📊 Mercado en {self._esc(str(zm['scope']).title())} "
                   f"({zm['n']} anuncios): {self._money(zm['p25'])}–"
                   f"{self._money(zm['p75'])}€/m² · mediana {self._money(zm['median'])}€/m²"]
        delta = zm.get("delta_pct")
        if delta is not None:
            if delta <= -8:
                out.append(f"   ↳ este piso está un <b>{-delta}% por debajo</b> de la mediana")
            elif delta >= 8:
                out.append(f"   ↳ este piso está un <b>{delta}% por encima</b> de la mediana")
            else:
                out.append("   ↳ este piso está en la media de su zona")
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
        if metrics.get("metrics_unavailable"):
            # Said plainly, because the card is visibly missing the €/m², the
            # entry cash and the market comparison that every other card has,
            # and silence there reads as "this flat has no numbers" rather
            # than "the email arrived without its surface".
            out.append("")
            out.append("📐 <b>El anuncio llegó sin los m²</b> — el correo de Idealista corta "
                       "la línea. Sin superficie no puedo calcular €/m², entrada ni "
                       "comparación con la zona: ábrelo para verlos.")

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
        if isinstance(first, str):
            # Depending on how the row was loaded this is a datetime or the
            # text SQLite stored. A card that raises here is an alert lost.
            try:
                first = datetime.fromisoformat(first)
            except ValueError:
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
