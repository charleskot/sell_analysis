"""A hard daily ceiling on everything the bot sends.

Twice in one day a bug turned a scheduled message into a message every
three minutes, and both times the fix was to reason about the code path
that caused it. That is the wrong shape of defence: it protects against the
mistake already made, not the next one.

So the count lives at the single point every message passes through, and it
is absolute. Once a budget is spent the message is dropped, whatever asked
for it and however good its reasons. An unlabelled message is refused
outright rather than trusted, which means a future code path that sends
without declaring itself is silent instead of unbounded.

Counts are held in the same database as the rest of the bot's memory, so
they survive the loop being relayed every few hours, and are keyed by day
so they reset on their own with nothing to schedule.
"""
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

KEY = "sent_today"
UNLABELLED = "otro"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> tuple[str, dict]:
    """Today's tally, or a fresh one if the stored day has passed."""
    from models.db import get_telegram_state

    raw = get_telegram_state(KEY, "")
    if raw:
        try:
            stored = json.loads(raw)
            if stored.get("day") == _today():
                return stored["day"], dict(stored.get("counts") or {})
        except (ValueError, KeyError, TypeError):
            logger.warning("Contador de mensajes ilegible, empiezo de cero")
    return _today(), {}


def check(config: dict, kind: str) -> tuple[bool, str]:
    """Whether one more message of this kind may go out, and why not."""
    limits = (config.get("alerts", {}).get("telegram", {}) or {}).get("limits") or {}
    per_kind = limits.get("per_kind") or {}
    total_cap = limits.get("total_per_day")

    if kind not in per_kind:
        return False, f"tipo '{kind}' no declarado en limits.per_kind"

    _, counts = _load()
    spent = counts.get(kind, 0)
    cap = per_kind[kind]
    if spent >= cap:
        return False, f"tope de '{kind}' agotado ({spent}/{cap} hoy)"

    if total_cap is not None and sum(counts.values()) >= total_cap:
        return False, f"tope diario agotado ({sum(counts.values())}/{total_cap})"

    return True, ""


def record(kind: str) -> None:
    """Count a message that actually went out. Never raises."""
    from models.db import set_telegram_state

    try:
        day, counts = _load()
        counts[kind] = counts.get(kind, 0) + 1
        set_telegram_state(KEY, json.dumps({"day": day, "counts": counts}))
    except Exception as e:
        logger.warning(f"No pude contar el mensaje enviado: {e}")


def describe(config: dict) -> str:
    """Today's spend, for the status reply."""
    limits = (config.get("alerts", {}).get("telegram", {}) or {}).get("limits") or {}
    total_cap = limits.get("total_per_day")
    try:
        _, counts = _load()
    except Exception:
        return "no he podido leer el contador"

    spent = sum(counts.values())
    if not spent:
        return f"0 de {total_cap} hoy"
    detail = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    return f"{spent} de {total_cap} hoy ({detail})"
