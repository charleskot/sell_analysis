from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.pipeline import run_daily

log = logging.getLogger(__name__)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        run_daily,
        trigger=CronTrigger(
            hour=settings.daily_run_hour,
            minute=settings.daily_run_minute,
            timezone=settings.timezone,
        ),
        id="daily_run",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    log.info(
        "Scheduler armado: diario a las %02d:%02d (%s)",
        settings.daily_run_hour, settings.daily_run_minute, settings.timezone,
    )
    return scheduler
