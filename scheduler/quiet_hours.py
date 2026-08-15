"""When the bot is not allowed to make the phone buzz.

A flat that appears at three in the morning is not worth waking anyone for;
it will still be there at breakfast. But it must not be lost either, which
is the trap here — suppressing a message is easy, suppressing it *without
dropping the listing* is the part that needs care. Nothing is recorded as
sent while quiet, so the next waking cycle picks it up and sends it then.

Replies to messages typed into the chat are never quiet. If the user is
awake and asking, he gets an answer.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ = "Europe/Madrid"


def is_quiet(config: dict, now: datetime | None = None) -> bool:
    """Whether pushes are currently suppressed.

    Hours are local to the user, not UTC: "no me escribas de noche" means
    his night, and Spain is one or two hours ahead depending on the season.
    """
    spec = (config.get("alerts", {}).get("telegram", {}) or {}).get("quiet_hours") or {}
    start, end = spec.get("from"), spec.get("to")
    if start is None or end is None:
        return False

    now = now or datetime.now(timezone.utc)
    try:
        local_hour = now.astimezone(ZoneInfo(spec.get("tz", TZ))).hour
    except Exception as e:                       # missing tz database
        logger.warning(f"No pude resolver la zona horaria, sin silencio: {e}")
        return False

    if start == end:
        return False
    if start < end:                              # e.g. 01:00 → 06:00
        return start <= local_hour < end
    return local_hour >= start or local_hour < end   # e.g. 23:00 → 08:00
