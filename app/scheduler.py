from __future__ import annotations

from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

from app import reports
from app.monitor import TradeMonitor
from app.signal_validation import trading_day_reason
from app.time_utils import to_ist, utc_now

IST = ZoneInfo("Asia/Kolkata")


def _run_pre_market_health_if_trading_day(health_manager: object) -> None:
    """CronTrigger's day_of_week="mon-fri" doesn't know about NSE holidays,
    so the scheduled job fired (and made a real broker health-check call) on
    every holiday that lands on a weekday. Wrapped rather than gating
    HealthManager.run() itself -- the "Run Health Check" button on the
    SmartAPI Health page also calls run() directly, and a holiday is exactly
    a day an admin might legitimately want to run it manually (e.g. checking
    credentials ahead of the next trading day). Module-level (not a closure
    inside create_scheduler) so it's directly testable without spinning up a
    real scheduler."""
    if trading_day_reason(to_ist(utc_now())) is not None:
        return
    health_manager.run()


def create_scheduler(
    monitor: TradeMonitor,
    health_manager: object | None = None,
    originator_job: Callable[[], None] | None = None,
    option_chain_job: Callable[[], None] | None = None,
    option_chain_interval_minutes: int = 5,
    closing_auction_job: Callable[[], None] | None = None,
    autonomous_job: Callable[[], None] | None = None,
    quick_scalp_job: Callable[[], None] | None = None,
    validated_signal_entry_job: Callable[[], None] | None = None,
    validated_signal_exit_job: Callable[[], None] | None = None,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(
        monitor.tick,
        trigger=IntervalTrigger(seconds=30),
        id="trade-monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if originator_job is not None:
        # 18 Aug 2026: was IntervalTrigger(minutes=5), firing 24/7 by
        # construction (IntervalTrigger has no day/time-of-day option) --
        # every firing outside 09:15-15:30 IST was a guaranteed no-op via the
        # in-job check_market_hours() gate, but still real scheduler overhead
        # and a log line every 5 minutes all night and every weekend. Narrowed
        # to weekdays + roughly session hours here, same pattern
        # option-chain-collect already uses below: the cron is a coarse first
        # gate, the job's own check_market_hours() call remains the real one
        # (covers the 09:00-09:15/15:30-15:55 slop this range leaves and NSE
        # holidays, which CronTrigger has no calendar for) -- a holiday now
        # costs a handful of cheap no-op firings within this window rather
        # than 288 of them across a full day.
        scheduler.add_job(
            originator_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5", timezone=IST),
            id="ai-origination-check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if autonomous_job is not None:
        # Same coarse-cron-plus-in-job-market-hours-gate shape as
        # ai-origination-check directly above -- app.ai.autonomous.
        # run_autonomous_checks calls check_market_hours() itself as its
        # first real gate, this cron is only here to stop the job waking up
        # 24/7 the way ai-origination-check used to before 18 Aug 2026.
        scheduler.add_job(
            autonomous_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5", timezone=IST),
            id="autonomous-ai-check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if quick_scalp_job is not None:
        # 1-minute resolution, not 5 -- see app.quick_scalp's own module
        # docstring for why: there is no LLM cost to amortize against a
        # slower cadence here, so "quick" scalping gets a genuinely quick
        # decision loop. Same coarse-cron-plus-in-job-check_market_hours-gate
        # shape as every other AI-adjacent job in this file.
        scheduler.add_job(
            quick_scalp_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*", timezone=IST),
            id="quick-scalp-check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if validated_signal_entry_job is not None:
        # 5-minute cron, matching the spec's own 5-minute spot-candle
        # cadence for setup/trigger evaluation -- see app.validated_signal's
        # module docstring for why this is its own job rather than hooked
        # into ai-origination-check's cycle the way the superseded build
        # was: the Single Active Position Rule needs to see BOTH indices at
        # once per cycle to resolve a concurrent-signal tie-break. Same
        # coarse-cron-plus-in-job-check_market_hours-gate shape as every
        # other AI-adjacent job in this file.
        scheduler.add_job(
            validated_signal_entry_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5", timezone=IST),
            id="validated-signal-entry-check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if validated_signal_exit_job is not None:
        # 5 seconds -- the spec's own explicit exit-poll cadence (Section 5),
        # the fastest job in this codebase. No day_of_week here (IntervalTrigger
        # has no such option, same limitation trade-monitor above has) -- the
        # job's own trading_day_reason() gate handles weekday/holiday, and
        # deliberately has NO hour-of-day component either, same reasoning as
        # trade-monitor: this must keep running through the whole trading day
        # to catch a position right up to and past either hard-exit time.
        # Returns immediately with zero SmartAPI calls when nothing is open,
        # so the 5-second cadence costs nothing in the (overwhelmingly common)
        # idle case.
        scheduler.add_job(
            validated_signal_exit_job,
            trigger=IntervalTrigger(seconds=5),
            id="validated-signal-exit-check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if option_chain_job is not None:
        # Archival only -- nothing live reads what this writes. Restricted to
        # weekdays and roughly session hours by cron as a first gate; the job
        # itself re-checks market hours, so a holiday costs one no-op call
        # rather than a wasted sweep.
        #
        # coalesce + max_instances=1 matter more here than elsewhere: if the
        # process is paused or the broker is slow, a backlog of chain sweeps
        # firing at once is exactly the burst that would contend with live
        # trading's rate-limit budget.
        scheduler.add_job(
            option_chain_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-15",
                minute=f"*/{max(option_chain_interval_minutes, 1)}",
                timezone=IST,
            ),
            id="option-chain-collect",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
    scheduler.add_job(
        monitor.square_off,
        # day_of_week added 17 Aug 2026 -- every other cron job in this file
        # already has it; this one didn't, so it technically fired at 15:15 on
        # Saturday/Sunday too. Harmless in practice (square_off_all's own
        # empty-open-trades early return makes a weekend firing a no-op), but
        # a real SmartAPI-touching call with no reason to happen outside a
        # trading day. Flagged but deliberately not fixed on 14 Aug since that
        # task was scoped elsewhere; fixed now while touching this file for
        # the same class of gap (see app/live_feed.py's market-hours gate,
        # same date).
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=15, timezone=IST),
        id="daily-square-off",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if closing_auction_job is not None:
        # 15:45, after the auction concludes (~15:35) and after derivatives
        # stop at 15:40. Deliberately NOT at 15:35 -- the closing bar has to be
        # published and served by the historical API before it can be fetched,
        # and this job runs once a day, so ten minutes of slack costs nothing
        # while being early costs the whole point of the job.
        scheduler.add_job(
            closing_auction_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=45, timezone=IST),
            id="closing-auction-capture",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if health_manager is not None:
        scheduler.add_job(
            lambda: _run_pre_market_health_if_trading_day(health_manager),
            trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=IST),
            id="pre-market-health",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        reports.run_daily_summary_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=IST),
        id="ai-daily-summary",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reports.run_weekly_report_job,
        trigger=CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=IST),
        id="ai-weekly-report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reports.run_monthly_report_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone=IST),
        id="ai-monthly-report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
