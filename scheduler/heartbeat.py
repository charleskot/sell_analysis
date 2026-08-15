"""What the bot did on its last cycle, so it can say so when asked.

Without this there is no way to tell "alive, but no portal has sent mail
since yesterday" from "dead since yesterday". Both look identical from the
chat: nothing arrives. The first is normal on a quiet Saturday; the second
needs fixing, and the user was left guessing which one he had.

Deliberately a plain file, not a row in the database. The database is
committed to git after every cycle, and a timestamp that changes every
three minutes would produce a commit every three minutes — burying the
runs that actually found something.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PATH = Path("state/.heartbeat")


PULSE_KEY = "last_pulse_at"


def pulse_due(minutes: int) -> bool:
    """Whether it is time to tell the chat the bot is still running.

    The clock lives in the database, not in a file. A file looked cheaper —
    no commit every half hour — but the runner did not keep it between
    cycles, so every cycle believed it had never pulsed and sent another
    line: one every three minutes instead of one every thirty. The database
    is the only store here that is deliberately restored at start and saved
    after each cycle, which is exactly the property this needs.
    """
    if not minutes:
        return False

    from models.db import get_telegram_state

    try:
        raw = get_telegram_state(PULSE_KEY, "")
    except Exception as e:                       # database not ready yet
        logger.warning(f"No pude leer el último pulso: {e}")
        return True
    if not raw:
        return True
    try:
        last_at = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_at).total_seconds() >= minutes * 60


def mark_pulse() -> None:
    """Never raises: failing to record a pulse must not end a cycle."""
    from models.db import set_telegram_state

    try:
        set_telegram_state(PULSE_KEY, datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.warning(f"No pude registrar el pulso: {e}")


def beat(**facts) -> None:
    """Record the outcome of a cycle. Never raises: this is bookkeeping."""
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"at": datetime.now(timezone.utc).isoformat(), **facts}
        PATH.write_text(json.dumps(payload))
    except OSError as e:
        logger.warning(f"No pude escribir el latido: {e}")


def last() -> dict | None:
    """The last recorded cycle, or None if this process has not seen one."""
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return None


def describe() -> str:
    """One line for the chat, in the terms the reader cares about."""
    beat_data = last()
    if not beat_data:
        return "aún no he completado un ciclo en esta ejecución"

    try:
        when = datetime.fromisoformat(beat_data["at"])
    except (KeyError, ValueError):
        return "no he podido leer mi propio latido"

    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 90:
        ago = "hace menos de un minuto"
    elif seconds < 3600:
        ago = f"hace {int(seconds // 60)} min"
    else:
        ago = f"hace {int(seconds // 3600)} h"

    parts = [f"ciclo {beat_data.get('cycle', '?')} {ago}"]
    if beat_data.get("error"):
        parts.append(f"⚠️ falló: {beat_data['error']}")
    else:
        emails = beat_data.get("emails")
        if emails:
            parts.append(f"{emails} correos, {beat_data.get('new', 0)} anuncios nuevos")
        else:
            parts.append("sin correo nuevo")
    return " · ".join(parts)
