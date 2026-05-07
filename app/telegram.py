from __future__ import annotations

import html
import logging
from typing import List

import httpx

from app.config import settings

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000  # Telegram limit is 4096; leave headroom


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def _format_price(amount) -> str:
    if amount is None:
        return ""
    return f"{int(amount):,}".replace(",", ".") + " €"


def _summary_message(opportunities: List[dict], stats: dict, run_date: str) -> str:
    n = len(opportunities)
    plural = "es" if n != 1 else ""
    return (
        f"<b>🏠 {n} oportunidad{plural} de subasta</b>\n"
        f"📅 {run_date} · score mínimo {settings.min_opportunity_score}\n"
        f"🔍 {stats.get('scraped', '?')} subastas revisadas · "
        f"{stats.get('new_count', '?')} nuevas · "
        f"{stats.get('analyzed', '?')} analizadas"
    )


def _opportunity_message(o: dict) -> str:
    auction = o["auction"]
    analysis = o["analysis"]

    score = analysis.opportunity_score
    if score >= 75:
        emoji = "🟢"
    elif score >= 60:
        emoji = "🟡"
    else:
        emoji = "⚪"

    rec = (analysis.recommendation or "").upper()
    title = _esc(auction.title) or "Subasta sin título"
    location_bits = []
    if auction.address:
        location_bits.append(_esc(auction.address))
    if auction.city:
        location_bits.append(_esc(auction.city))
    if auction.province:
        location_bits.append(f"({_esc(auction.province)})")
    location = ", ".join(location_bits) if location_bits else "—"

    price_line_bits = []
    if auction.starting_price:
        price_line_bits.append(f"💶 <b>{_format_price(auction.starting_price)}</b>")
    if analysis.estimated_market_value_eur:
        price_line_bits.append(f"mercado: {_format_price(analysis.estimated_market_value_eur)}")
    if analysis.estimated_discount_pct is not None:
        price_line_bits.append(f"<b>-{analysis.estimated_discount_pct:.0f}%</b>")
    price_line = " · ".join(price_line_bits)

    metrics = []
    if auction.surface_m2:
        metrics.append(f"{int(auction.surface_m2)} m²")
    if analysis.estimated_rental_yield_pct is not None:
        metrics.append(f"yield {analysis.estimated_rental_yield_pct:.1f}%")
    if analysis.estimated_flip_roi_pct is not None:
        metrics.append(f"ROI flip {analysis.estimated_flip_roi_pct:.1f}%")
    metrics_line = " · ".join(metrics)

    summary = _esc(analysis.summary or "")[:600]

    pros = "\n".join(f"  ✓ {_esc(p)}" for p in (analysis.pros or [])[:3])
    cons = "\n".join(f"  ✗ {_esc(c)}" for c in (analysis.cons or [])[:3])
    risks = "\n".join(f"  ⚠ {_esc(r)}" for r in (analysis.legal_risks or [])[:3])

    parts = [
        f"{emoji} <b>{score}/100 · {rec}</b>",
        f"<b>{title}</b>",
        f"📍 {auction.source.upper()} · {location}",
    ]
    if price_line:
        parts.append(price_line)
    if metrics_line:
        parts.append(f"📊 {metrics_line}")
    parts.append("")
    parts.append(summary)
    if pros:
        parts.append("\n<b>Pros</b>\n" + pros)
    if cons:
        parts.append("\n<b>Contras</b>\n" + cons)
    if risks:
        parts.append("\n<b>Riesgos</b>\n" + risks)
    parts.append(f'\n<a href="{_esc(auction.url)}">Ver subasta original →</a>')

    msg = "\n".join(parts)
    if len(msg) > MAX_LEN:
        msg = msg[:MAX_LEN - 50] + "\n…(truncado)"
    return msg


def _post(text: str) -> None:
    url = API_URL.format(token=settings.telegram_bot_token)
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = httpx.post(url, json=payload, timeout=30)
    if r.status_code >= 400:
        log.error("Telegram API %s: %s", r.status_code, r.text)
        r.raise_for_status()


def notify(opportunities: List[dict], stats: dict, run_date: str) -> None:
    """Send the daily report to Telegram. One summary + one message per opportunity."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        log.warning("Telegram no configurado (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID). No se envía.")
        return

    log.info("Enviando %d mensajes a Telegram chat %s", len(opportunities) + 1, settings.telegram_chat_id)
    _post(_summary_message(opportunities, stats, run_date))
    for o in opportunities:
        try:
            _post(_opportunity_message(o))
        except Exception as e:
            log.warning("Telegram mensaje individual falló: %s", e)
    log.info("Telegram enviado.")
