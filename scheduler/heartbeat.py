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
