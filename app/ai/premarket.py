"""Pre-market briefing: support/resistance levels plus a genuine overnight
global-markets headline read, generated once before each trading day opens
and handed to Agentic AI's News & Sentiment Agent as extra context on every
one of that day's 5-minute cycles.

WHY THIS EXISTS
----------------
Prompted by the user asking whether the AI agents have any awareness of
overnight global markets before the Indian session opens, and sharing a real
"Global Market Cues" broker-style briefing (US/Europe/Asia index levels,
GIFT Nifty, Nifty/Sensex/Bank Nifty support-resistance tables) as the shape
wanted. Confirmed via live research before building anything: there is no
free, keyless, documented data source for GIFT Nifty or any Asian-market
index level. Fabricating those numbers would break this project's own
standing "never invent a number that isn't in the data" rule (see
CLAUDE.md's AI-subsystem conventions, e.g. app.ai.news's own severe-event
gate and headline formatter). So this builds only the two pieces that ARE
honestly buildable from data already in this app or already free and
keyless:

- Support/resistance levels for Nifty/Bank Nifty, from app.market_context.
  compute_levels() against this app's own already-stored 5-minute candles --
  a pure DB read, zero new SmartAPI cost, zero new computation (the exact
  function every other live module already calls for the same numbers).
- A genuine global-markets headline read, reusing app.ai.news's existing
  Google-News-RSS mechanism verbatim with a wider, global search instead of
  the India-only one -- real headlines shown as plain text, never a
  synthesized sentiment score.

No GIFT Nifty quote, no Dow/Nikkei/other global-index point value, and no
numeric "Nifty will open at X" prediction are produced anywhere in this
module, for that reason.

RESOURCE DESIGN -- explicit, per standing instruction (28 Nov 2026)
--------------------------------------------------------------------
The production box is RAM-constrained and has already had DB-pool and
memory incidents this project (see CLAUDE.md's Nov 2026 entries). Every
design choice here favours minimal footprint over completeness:

- Runs ONCE a day (a new low-cadence CronTrigger job registered directly in
  app.scheduler, same class as pre-market-health/ai-daily-summary), not on
  a 5-minute loop.
- Zero new LLM call. No synthesis/narrative step at all -- the plain-text
  levels and headlines are persisted as-is, and Agentic AI's own
  already-scheduled News Agent call (one shared call per 5-minute cycle,
  already happening regardless of this feature) reads this text as extra
  prompt context. A dedicated "write the narrative" call here would be a
  second LLM call this project doesn't need to add.
- Zero new table or migration. Reuses AIReport (app.db_models), the same
  "wholesale reuse" precedent AGENTIC_QA/AGENTIC_DAILY_LOG already
  established (25 Oct 2026), rather than a new model/table/index.
- Zero new network dependency. The global-headline fetch reuses app.ai.
  news.fetch_market_headlines() verbatim (it already accepts a custom feeds
  list and, as of this change, a custom lookback window) -- no new HTTP
  client, no new XML parser.
- The persisted text is read back by Agentic AI's per-cycle loop as a
  single cheap indexed query (within the session already open that cycle),
  never re-generated or re-fetched intracycle.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.news import fetch_market_headlines, format_headlines_for_prompt
from app.db_models import AIReport, ReportType
from app.market_context import compute_levels
from app.market_data import FIVE_MINUTE, load_bars
from app.platform import list_index_configs, log_event, today_ist
from app.time_utils import to_ist, utc_now

logger = logging.getLogger(__name__)

# One feed only -- Google News already aggregates many outlets per query, and
# this job runs once a day with no latency pressure, so a second HTTP round
# trip isn't worth the extra call against the resource-consciousness standard
# this module is held to. Same free, keyless mechanism app.ai.news already
# uses for India headlines, widened to global-market terms.
_GLOBAL_MARKET_FEEDS: list[tuple[str, str]] = [
    (
        "Google News (global markets)",
        "https://news.google.com/rss/search?q="
        + quote('"Wall Street" OR "Dow Jones" OR Nasdaq OR "S&P 500" OR "Asian markets" OR Nikkei OR "Federal Reserve"')
        + "&hl=en-IN&gl=IN&ceid=IN:en",
    ),
]

# Covers the prior US session's close through the Asian morning session --
# much wider than app.ai.news's own 90-minute intraday default, which answers
# a different question ("is this happening right now") than a pre-market
# briefing needs to. A reasoned starting point, not backtested, same status
# every other new threshold in this project carries before real history
# looks at it.
_GLOBAL_HEADLINE_LOOKBACK_MINUTES = 16 * 60

# How many 5-minute bars to load per index for compute_levels()'s own
# previous-day high/low/close read. Bounded explicitly (not "all history")
# so this can never load an unbounded candle table by accident -- comfortably
# covers several sessions even across a long weekend or holiday gap.
_LEVELS_LOOKBACK_BARS = 400


@dataclass(frozen=True)
class _IndexLevels:
    display_name: str
    previous_day_high: float
    previous_day_low: float
    previous_day_close: float
    pivot: float
    resistance_1: float
    support_1: float


def _levels_for_index(db: Session, index, session_date: date_type) -> Optional[_IndexLevels]:
    bars = load_bars(db, index.symbol, FIVE_MINUTE, limit=_LEVELS_LOOKBACK_BARS)
    if not bars:
        return None
    levels = compute_levels(bars, session_date)
    if levels.previous_day_high is None or levels.previous_day_low is None or levels.previous_day_close is None:
        return None
    # Classic (floor trader) pivot -- the same construction CPR itself is
    # built from in app.market_context.compute_cpr, just the single-pivot
    # form rather than that function's pivot/BC/TC triplet. Kept local and
    # this simple rather than reusing compute_cpr, since R1/S1 (not CPR's
    # narrow/wide classification) are what a briefing like this shows.
    pivot = round((levels.previous_day_high + levels.previous_day_low + levels.previous_day_close) / 3, 2)
    r1 = round(2 * pivot - levels.previous_day_low, 2)
    s1 = round(2 * pivot - levels.previous_day_high, 2)
    return _IndexLevels(
        display_name=index.display_name or index.symbol,
        previous_day_high=levels.previous_day_high,
        previous_day_low=levels.previous_day_low,
        previous_day_close=levels.previous_day_close,
        pivot=pivot,
        resistance_1=r1,
        support_1=s1,
    )


def _format_briefing_text(levels_by_index: list[_IndexLevels], headlines_text: str) -> str:
    lines = ["Pre-market briefing (support/resistance + overnight global headlines):"]
    if levels_by_index:
        lines.append("")
        lines.append("Support/resistance (from yesterday's close, classic pivot):")
        for lv in levels_by_index:
            lines.append(
                f"- {lv.display_name}: prev close {lv.previous_day_close}, pivot {lv.pivot}, "
                f"R1 {lv.resistance_1}, S1 {lv.support_1} "
                f"(prev day high/low {lv.previous_day_high}/{lv.previous_day_low})"
            )
    lines.append("")
    if headlines_text:
        lines.append("Overnight global market headlines:")
        lines.append(headlines_text)
    else:
        lines.append("Overnight global market headlines: none found this morning.")
    return "\n".join(lines)


def generate_premarket_briefing(db: Session, now_ist: Optional[datetime] = None) -> AIReport:
    """Builds and persists today's briefing. Meant to be called once, by
    run_premarket_briefing_job below, shortly before the session opens.

    No LLM call -- see the module docstring. The persisted summary_text is
    plain text, read back verbatim by get_todays_premarket_briefing_text()."""
    now_ist = now_ist or to_ist(utc_now())
    today = now_ist.date()

    levels_by_index: list[_IndexLevels] = []
    for index in list_index_configs(db):
        if not index.enabled:
            continue
        try:
            levels = _levels_for_index(db, index, today)
        except Exception:
            logger.exception("[PRE_MARKET] %s: levels computation failed", index.symbol)
            continue
        if levels is not None:
            levels_by_index.append(levels)
        else:
            logger.info("[PRE_MARKET] %s: insufficient candle history for levels this morning", index.symbol)

    headlines_text = ""
    try:
        headlines = fetch_market_headlines(
            feeds=_GLOBAL_MARKET_FEEDS, lookback_minutes=_GLOBAL_HEADLINE_LOOKBACK_MINUTES,
        )
        headlines_text = format_headlines_for_prompt(headlines)
    except Exception:
        logger.exception("[PRE_MARKET] global headline fetch failed")

    summary_text = _format_briefing_text(levels_by_index, headlines_text)
    stats = {
        "report_date": str(today),
        "levels": [lv.__dict__ for lv in levels_by_index],
        "headline_count": headlines_text.count("\n- ") + (1 if headlines_text else 0),
    }

    report = AIReport(
        report_type=ReportType.PRE_MARKET,
        period_start=today,
        period_end=today,
        title=f"Pre-Market Briefing -- {today}",
        summary_text=summary_text,
        stats_json=json.dumps(stats, default=str),
        provider="dummy",
        model="",
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    log_event(db, "REPORT", f"{report.title} generated", "INFO", {"report_id": report.id, "report_type": ReportType.PRE_MARKET})
    return report


def get_todays_premarket_briefing_text(db: Session, day: Optional[date_type] = None) -> str:
    """Single cheap indexed read -- the only thing Agentic AI's per-cycle
    loop does with this feature. "" (not a placeholder sentence) when
    nothing has been generated yet today, so the caller can omit the
    section entirely, matching app.ai.news.format_headlines_for_prompt's
    own empty-string convention."""
    day = day or today_ist()
    # Ordered by id, not generated_at -- the latter is a server-side
    # CURRENT_TIMESTAMP with only second resolution, which two rows can tie
    # on; id is strictly monotonic and unambiguous.
    report = db.scalar(
        select(AIReport)
        .where(AIReport.report_type == ReportType.PRE_MARKET, AIReport.period_start == day)
        .order_by(AIReport.id.desc())
        .limit(1)
    )
    return report.summary_text if report is not None else ""


def run_premarket_briefing_job() -> None:
    """Scheduler entry point (see app.scheduler's "pre-market-briefing" job).
    Mirrors app.ai.agentic's run_daily_log_job shape exactly: skips on a
    weekend/NSE holiday (CronTrigger's day_of_week doesn't know about
    holidays) and opens its own session, never raising out of a scheduler
    tick."""
    from app.database import SessionLocal
    from app.signal_validation import trading_day_reason

    if trading_day_reason(to_ist(utc_now())) is not None:
        return
    with SessionLocal() as db:
        try:
            generate_premarket_briefing(db)
        except Exception:
            logger.exception("[PRE_MARKET] briefing generation failed")
