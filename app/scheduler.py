from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

from app.monitor import TradeMonitor

IST = ZoneInfo("Asia/Kolkata")


def create_scheduler(monitor: TradeMonitor) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(
        monitor.tick,
        trigger=IntervalTrigger(seconds=30),
        id="trade-monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        monitor.square_off,
        trigger=CronTrigger(hour=15, minute=15, timezone=IST),
        id="daily-square-off",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
