"""Agentic AI -- a new, isolated strategy (origin="AGENTIC_AI"), built as a set
of specialized agents rather than one prompt doing everything, per explicit
request (25 Oct 2026). OpenAI-only, two model tiers with their own
independent AISettings-like row (AgenticAISettings, app/db_models.py) --
never the shared AISettings AI Origination/Autonomous AI already read, so
tuning this can never leak into them or vice versa (the exact failure the
gpt-6-luna reasoning_effort episode exposed on the shared row).

THIS FILE IS SCAFFOLDING, NOT A LIVE STRATEGY YET
--------------------------------------------------
No entry or exit logic exists here. Nothing in this module opens a trade,
and nothing schedules it. What exists:

  - Settings repository functions (get/create/update), mirroring
    app/ai/repository.py's exact shape for AISettings.
  - The backtest/QA agent: compute_origin_stats() + run_qa_report(). Built
    to work generically against ANY origin pattern, not just "AGENTIC_AI" --
    deliberately, since AGENTIC_AI has no trade history yet and won't for
    several more PRs (entry/exit agents are a separate, later piece). This
    makes the QA agent testable against real, already-live data (Autonomous
    AI, AI Origination, Quick Scalp, Validated Signal) from day one, rather
    than sitting unused until this strategy starts trading.

  - The logging agent: run_daily_log(). Replaces the manual "upload today's
    StrikeVault export, I'll read it" routine with a real scheduled report --
    reads closed trades directly from the live DB for a given IST calendar
    day across every tracked strategy (AI Origination, Autonomous AI, Quick
    Scalp, Validated Signal, plus this strategy's own still-empty AGENTIC_AI
    bucket), reuses compute_origin_stats() per strategy, and persists via
    the same AIReport table the QA agent already writes to. Runs alongside
    the existing manual chat-based routine at first, not as a replacement
    for it yet -- same validate-before-trusting discipline as everything
    else in this project.

  - The risk/sizing agent: compute_position_size() + check_risk_gates().
    Entirely deterministic, no LLM involved -- per the user's own framing
    for this strategy ("the system understands the financial losses which
    must be least"), position sizing and hard risk limits are exactly the
    kind of decision that should never depend on a model call succeeding or
    being well-calibrated. Like the QA agent, both functions are usable and
    testable today even though nothing yet calls them from a real entry
    path: compute_position_size() is a pure function callable from the
    dashboard's own sizing calculator right now, and check_risk_gates() is
    the exact gate a future entry agent MUST call before opening any
    AGENTIC_AI trade -- built and proven correct ahead of that agent existing,
    not alongside it under time pressure.

  - The entry/exit agent: check_agentic_entry() + check_agentic_exits(),
    wired via run_agentic_entry_exit_checks() (the "agentic-ai-check"
    scheduler job). This strategy's first and only live_model-backed
    component -- the fast, low-latency tier, per the user's explicit model
    tiering (live_model for entry/exit signalling, qa_model for the
    backtest/QA agent above, both already configured on AgenticAISettings).

    THE MODEL NEVER DECIDES RISK. It only ever outputs a direction
    (BUY_CE/BUY_PE/NONE at entry, EXIT/HOLD at exit) plus confidence and
    reasoning -- stop, target, and position size are computed entirely by
    the deterministic risk/sizing agent above (nominal_stop_percent/
    nominal_target_percent + compute_position_size()/check_risk_gates()).
    This is a deliberate departure from AI Origination's own model-proposed-
    stop pattern: the whole point of building the risk/sizing agent BEFORE
    this one was so a model-proposed risk number would never need to be
    trusted here, consistent with the user's own framing for this strategy
    ("the system understands the financial losses which must be least").

    Market input is deliberately minimal for this first pass -- current
    price, change vs previous close, today's range (app.platform.
    get_index_live_figures(), the same zero-SmartAPI-cost read app.ai.
    autonomous originally used), no indicators at all. This mirrors
    Autonomous AI's own first build rather than reusing its now-much-
    larger feature engine (EMA/ADX/VWAP/chop/recent-momentum, accrued over
    months of real incidents) -- richer market context for Agentic AI is a
    deliberate, separate future enhancement (most likely folded into the
    news-reading agent's own work, since both are about giving this
    strategy a fuller picture of "what is actually happening"), not
    something to build speculatively ahead of any real trade history here.

    Structurally paper-only, same construction and reasoning as every other
    experimental strategy in this project: TradingMode.PAPER is hardcoded at
    open, smartapi.place_market_order is never called anywhere in this
    module. One position at a time across BOTH indices combined by default
    (max_concurrent_positions=1, the same global cap check_risk_gates()
    already enforces), not a separate per-index slot the way AI Origination
    works -- same Single Active Position Rule precedent Validated Signal
    already established, and the simplest reading of check_risk_gates()'s
    own global (not per-index) concurrency count.

    Deliberately NOT built in this pass, named rather than silently
    skipped: Autonomous AI's accumulated deterministic exit matrix (peak-
    giveback trail, break-even violation, stall exit, structural
    invalidation) -- each of those was added after a specific real incident
    over months of Autonomous AI's own live history. Agentic AI has none
    yet; importing that matrix wholesale here would be applying thresholds
    tuned for a different strategy's real failures to one with no evidence
    of its own. What this pass DOES keep: the hard end-of-day square-off
    (TIME_EXIT, at the shared PlatformSettings.square_off_time) and the
    model's own per-cycle EXIT/HOLD re-ask (AI_DISCRETION_EXIT) -- plus the
    always-on mechanical stop/target backstop every FIXED-mode, non-SIGNAL/
    non-AI_ORIGIN_* trade already gets for free from monitor_open_trades'
    shared branch, zero new code there.

A future news-reading agent (not this one) is the next and final piece of
the originally agreed five-agent plan.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import AIClient
from app.ai.json_utils import extract_json_object
from app.db_models import (
    AgenticAISettings,
    AIReport,
    IndexConfig,
    ReportType,
    SLMode,
    StrategyTrade,
    TradeResult,
    TradeStatus,
    TradingMode,
)
from app.models import ExitReason, Signal
from app.platform import get_index_live_figures, get_or_create_settings, list_index_configs, log_event, today_ist
from app.premium_model import days_to_expiry, symmetric_premium_percent
from app.signal_validation import check_market_hours
from app.time_utils import parse_hhmm, to_ist, utc_now

logger = logging.getLogger(__name__)

# Same trust-minimum convention every scripts/*.py backtest in this project
# uses -- a bucket below this is reported, never silently trusted.
MIN_BUCKET_LIVE = 20
BOOTSTRAP_ROUNDS = 2000
# Seeded, per-module, not shared -- same convention scripts/*.py backtests
# already use (each one seeds its own RNG rather than sharing a global).
_BOOTSTRAP_SEED = 20261025


# ---------------------------------------------------------------------------
# Settings repository -- mirrors app/ai/repository.py's shape for AISettings
# ---------------------------------------------------------------------------


def get_agentic_settings(db: Session, settings_id: int = 1) -> Optional[AgenticAISettings]:
    return db.get(AgenticAISettings, settings_id)


def create_agentic_settings(db: Session, **values: Any) -> AgenticAISettings:
    settings = AgenticAISettings(**values)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def update_agentic_settings(db: Session, settings: AgenticAISettings, **values: Any) -> AgenticAISettings:
    for field, value in values.items():
        if not hasattr(AgenticAISettings, field):
            raise ValueError(f"Unknown Agentic AI setting: {field}")
        setattr(settings, field, value)
    db.commit()
    db.refresh(settings)
    return settings


# ---------------------------------------------------------------------------
# Stats computation -- generic over any origin pattern
# ---------------------------------------------------------------------------


def _bootstrap_mean_ci(values: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> Optional[tuple[float, float]]:
    """90% CI on the mean via resampling. None (not a fabricated interval)
    when there's nothing to resample."""
    if not values:
        return None
    rng = random.Random(_BOOTSTRAP_SEED)
    means = []
    for _ in range(rounds):
        sample = [rng.choice(values) for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(0.05 * rounds)]
    hi = means[int(0.95 * rounds) - 1]
    return lo, hi


def compute_origin_stats(
    db: Session,
    origin_pattern: str,
    on_date: Optional[date_type] = None,
) -> dict[str, Any]:
    """Win rate / P&L / exit-reason breakdown for every CLOSED trade matching
    origin_pattern, plus a bootstrap 90% CI on mean net P&L and an explicit
    below_trust_minimum flag -- never silently presented as reliable below
    MIN_BUCKET_LIVE observations. origin_pattern is a SQL LIKE pattern (a
    plain string with no % degrades to an exact match, same convention
    pull_option_candles.py's --origin already established), so this works
    unmodified against "AGENTIC_AI", "AUTONOMOUS_AI", "AI_ORIGIN_%", etc.

    on_date, when given, scopes to trades that closed on that single IST
    calendar day (used by run_daily_log()). Filtered in Python via to_ist()
    after an unfiltered SQL fetch, not a SQL-level date range -- exit_time
    is stored in UTC and SQLite does not round-trip tzinfo, so a naive
    column-level date comparison would be wrong by the UTC/IST offset (the
    same reason every other "today" filter in this codebase, e.g.
    get_ai_origination_today_highlights, does this in Python). None (the
    default) means all-time, unchanged from this function's original shape.
    """
    trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.origin.like(origin_pattern),
                StrategyTrade.status == TradeStatus.CLOSED,
            )
        )
    )
    if on_date is not None:
        trades = [t for t in trades if t.exit_time is not None and to_ist(t.exit_time).date() == on_date]
    n = len(trades)
    if n == 0:
        note = (
            f"No closed trades matching this origin pattern on {on_date}."
            if on_date is not None
            else "No closed trades matching this origin pattern."
        )
        return {
            "origin_pattern": origin_pattern,
            "on_date": str(on_date) if on_date is not None else None,
            "n": 0,
            "note": note,
        }

    wins = sum(1 for t in trades if t.result == "WIN")
    losses = sum(1 for t in trades if t.result == "LOSS")
    net_values = [t.net_pnl for t in trades]
    gross_values = [t.profit_loss for t in trades]
    exit_reasons: dict[str, int] = {}
    for t in trades:
        reason = t.exit_reason or "UNKNOWN"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    ci = _bootstrap_mean_ci(net_values)
    mean_net = sum(net_values) / n

    return {
        "origin_pattern": origin_pattern,
        "on_date": str(on_date) if on_date is not None else None,
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_rate_percent": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "net_pnl_total": round(sum(net_values), 2),
        "gross_pnl_total": round(sum(gross_values), 2),
        "mean_net_pnl_per_trade": round(mean_net, 2),
        "net_pnl_90pct_ci": [round(ci[0], 2), round(ci[1], 2)] if ci else None,
        "ci_excludes_zero": bool(ci and (ci[0] > 0 or ci[1] < 0)) if ci else None,
        "below_trust_minimum": n < MIN_BUCKET_LIVE,
        "trust_minimum": MIN_BUCKET_LIVE,
        "exit_reason_breakdown": exit_reasons,
    }


# ---------------------------------------------------------------------------
# QA agent -- calls the reasoning-heavy model to interpret stats already
# computed above. The model narrates; it never computes a number itself,
# same division of labour app/reports.py's _generate_narrative already uses.
# ---------------------------------------------------------------------------

_QA_SYSTEM_PROMPT = (
    "You are a disciplined quantitative trading analyst reviewing backtest statistics. "
    "You never invent a number that is not present in the data you are given. "
    "A result only counts as reliable when its sample size is at or above the stated trust "
    "minimum AND its 90% confidence interval excludes zero -- state explicitly whether both "
    "hold. If either does not, your verdict must be 'not yet enough evidence', not a soft-pedaled "
    "version of a positive or negative conclusion. Be concise and concrete."
)


@dataclass(frozen=True)
class _QACallResult:
    content: Optional[str]
    error: Optional[str]


def _call_openai_qa(settings: AgenticAISettings, user_prompt: str) -> _QACallResult:
    if not settings.api_key or not settings.qa_model:
        return _QACallResult(None, "Agentic AI QA model/API key not configured.")
    endpoint = (settings.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    response = AIClient().send(
        endpoint=endpoint,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        payload={
            "model": settings.qa_model,
            "reasoning_effort": settings.qa_reasoning_effort,
            "messages": [
                {"role": "system", "content": _QA_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=settings.timeout_seconds,
    )
    if response.error:
        return _QACallResult(None, response.error)
    try:
        content = response.response_body["choices"][0]["message"]["content"]
    except Exception as exc:
        return _QACallResult(None, f"Unexpected OpenAI response shape ({type(exc).__name__}: {exc})")
    return _QACallResult(content, None)


def _template_qa_narrative(stats: dict[str, Any]) -> str:
    """Fallback used when no QA model is configured -- states the same facts
    plainly rather than leaving the report blank."""
    if stats.get("n", 0) == 0:
        return f"No closed trades found for origin pattern '{stats['origin_pattern']}'."
    lines = [
        f"{stats['n']} closed trades for '{stats['origin_pattern']}' "
        f"({stats['wins']}W/{stats['losses']}L, {stats['win_rate_percent']}% win rate).",
        f"Net P&L: Rs {stats['net_pnl_total']:,.2f} (mean Rs {stats['mean_net_pnl_per_trade']:,.2f}/trade).",
    ]
    if stats["below_trust_minimum"]:
        lines.append(
            f"Sample size ({stats['n']}) is below the trust minimum ({stats['trust_minimum']}) -- "
            "not yet enough evidence to draw a reliable conclusion."
        )
    elif stats["ci_excludes_zero"]:
        lo, hi = stats["net_pnl_90pct_ci"]
        lines.append(f"90% CI on mean net P&L [{lo:,.2f}, {hi:,.2f}] excludes zero -- a reliable result.")
    else:
        lines.append("90% CI on mean net P&L crosses zero -- not reliable yet, despite a large enough sample.")
    return " ".join(lines)


def run_qa_report(db: Session, origin_pattern: str, label: str) -> AIReport:
    """Computes real stats for origin_pattern, asks the QA model (if
    configured) to interpret them with the discipline stated in
    _QA_SYSTEM_PROMPT, and persists the result via the existing AIReport
    table (ReportType.AGENTIC_QA) -- same reuse precedent as AI
    Origination's own summary report (15 Aug 2026), not a new table."""
    stats = compute_origin_stats(db, origin_pattern)
    settings = get_agentic_settings(db)

    provider = "dummy"
    model = ""
    narrative = _template_qa_narrative(stats)
    if settings is not None and settings.enabled and settings.api_key and settings.qa_model:
        user_prompt = (
            f"Backtest statistics for: {label}\n\n" + json.dumps(stats, default=str, indent=2) +
            "\n\nWrite a concise (3-6 sentence) analyst verdict strictly from the statistics above."
        )
        result = _call_openai_qa(settings, user_prompt)
        if result.content:
            narrative = result.content
            provider = "openai"
            model = settings.qa_model
        else:
            logger.info("[AGENTIC][QA] Model call failed, using template fallback: %s", result.error)

    today = today_ist()
    report = AIReport(
        report_type=ReportType.AGENTIC_QA,
        period_start=today,
        period_end=today,
        title=f"Agentic AI QA -- {label}",
        summary_text=narrative,
        stats_json=json.dumps(stats, default=str),
        provider=provider,
        model=model,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    log_event(db, "REPORT", f"{report.title} generated", "INFO", {"report_id": report.id, "report_type": ReportType.AGENTIC_QA})
    return report


# ---------------------------------------------------------------------------
# Logging agent -- a scheduled daily cross-strategy digest, replacing the
# manual "upload today's StrikeVault export, I'll read it" chat routine.
# ---------------------------------------------------------------------------

# Every strategy this project currently tracks daily by hand, plus this
# strategy's own (still-empty) bucket so it starts appearing automatically
# the moment a future entry/exit agent opens its first trade -- no code
# change needed here when that lands. AI_ORIGIN_% stays a LIKE pattern (both
# provider suffixes); every other value is a single fixed origin, matched
# exactly via the same LIKE-degrades-to-equality behaviour compute_origin_
# stats already relies on.
DAILY_LOG_STRATEGIES: dict[str, str] = {
    "AI Origination": "AI_ORIGIN_%",
    "Autonomous AI": "AUTONOMOUS_AI",
    "Quick Scalp": "QUICK_SCALP",
    "Validated Signal": "VALIDATED_SIGNAL",
    "Agentic AI": "AGENTIC_AI",
}


def _template_daily_log_narrative(report_date: date_type, by_strategy: dict[str, dict[str, Any]]) -> str:
    """Fallback used when no QA model is configured -- mirrors the shape
    this project's own manual daily chat recaps have used throughout."""
    lines = [f"Daily log for {report_date}:"]
    any_trades = False
    day_total = 0.0
    for label, stats in by_strategy.items():
        if stats.get("n", 0) == 0:
            continue
        any_trades = True
        day_total += stats["net_pnl_total"]
        lines.append(
            f"- {label}: {stats['n']} trades ({stats['wins']}W/{stats['losses']}L), "
            f"net Rs {stats['net_pnl_total']:,.2f}."
        )
    if not any_trades:
        return f"No closed trades across any tracked strategy on {report_date}."
    lines.append(f"Day total net P&L: Rs {day_total:,.2f}.")
    return "\n".join(lines)


def run_daily_log(db: Session, report_date: Optional[date_type] = None) -> AIReport:
    """Computes real per-strategy stats for report_date (default: today IST)
    across every strategy in DAILY_LOG_STRATEGIES, asks the QA model (if
    configured) to narrate the combined picture, and persists the result as
    an AIReport (ReportType.AGENTIC_DAILY_LOG) -- the same reuse precedent
    run_qa_report already established, not a new table.

    Runs alongside the existing manual chat-based daily routine, not as a
    replacement for it yet -- same validate-before-trusting discipline as
    every other change in this project. Nothing here stops the admin's own
    scheduled reminder to upload a StrikeVault export; this is a second,
    independent source of the same numbers to cross-check against until
    this one has earned trust."""
    if report_date is None:
        report_date = today_ist()

    by_strategy = {
        label: compute_origin_stats(db, pattern, on_date=report_date)
        for label, pattern in DAILY_LOG_STRATEGIES.items()
    }
    day_total_net_pnl = round(
        sum(s["net_pnl_total"] for s in by_strategy.values() if s.get("n", 0) > 0), 2
    )
    stats: dict[str, Any] = {
        "report_date": str(report_date),
        "by_strategy": by_strategy,
        "day_total_net_pnl": day_total_net_pnl,
    }

    settings = get_agentic_settings(db)
    provider = "dummy"
    model = ""
    narrative = _template_daily_log_narrative(report_date, by_strategy)
    if settings is not None and settings.enabled and settings.api_key and settings.qa_model:
        user_prompt = (
            f"Daily cross-strategy trading log for {report_date}\n\n" + json.dumps(stats, default=str, indent=2) +
            "\n\nWrite a concise (4-8 sentence) plain-text daily recap strictly from the statistics above -- "
            "per-strategy results and the day total. Call out the day's best and worst performing strategies "
            "if there's more than one with closed trades. If a strategy has zero closed trades, do not "
            "speculate about why."
        )
        result = _call_openai_qa(settings, user_prompt)
        if result.content:
            narrative = result.content
            provider = "openai"
            model = settings.qa_model
        else:
            logger.info("[AGENTIC][LOG] Model call failed, using template fallback: %s", result.error)

    report = AIReport(
        report_type=ReportType.AGENTIC_DAILY_LOG,
        period_start=report_date,
        period_end=report_date,
        title=f"Agentic AI Daily Log -- {report_date}",
        summary_text=narrative,
        stats_json=json.dumps(stats, default=str),
        provider=provider,
        model=model,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    log_event(db, "REPORT", f"{report.title} generated", "INFO", {"report_id": report.id, "report_type": ReportType.AGENTIC_DAILY_LOG})
    return report


def run_daily_log_job() -> None:
    """Scheduler entry point -- mirrors app/reports.py's run_daily_summary_job
    exactly: skips on a weekend/NSE holiday (CronTrigger's day_of_week
    doesn't know about holidays) and opens its own session, never raising
    out of a scheduler tick."""
    from app.database import SessionLocal
    from app.signal_validation import trading_day_reason

    if trading_day_reason(to_ist(utc_now())) is not None:
        return
    with SessionLocal() as db:
        try:
            run_daily_log(db)
        except Exception:
            logger.exception("[AGENTIC][LOG] Daily log generation failed")


# ---------------------------------------------------------------------------
# Risk/sizing agent -- entirely deterministic, no LLM. Per the user's own
# framing for this strategy ("the system understands the financial losses
# which must be least"), position sizing and hard risk limits are exactly
# the kind of decision that should never depend on a model call succeeding
# or being well-calibrated. A future entry agent MUST call check_risk_gates()
# before opening any AGENTIC_AI trade, and should size whatever it opens via
# compute_position_size() -- both already usable and testable today, ahead
# of that agent existing.
# ---------------------------------------------------------------------------

# This strategy's own fixed origin value, once it starts trading -- an exact
# match, never a LIKE pattern (unlike AI Origination's provider-suffixed
# AI_ORIGIN_%), same convention DAILY_LOG_STRATEGIES' own "Agentic AI" entry
# already uses.
AGENTIC_AI_ORIGIN = "AGENTIC_AI"

# Mirrors AgenticAISettings' own column defaults exactly -- used only when
# no settings row exists yet, so check_risk_gates()/evaluate_risk_now() are
# always callable (the dashboard's live risk-status card must render
# correctly before an admin has ever saved anything on this page).
_DEFAULT_RISK_SETTINGS = {
    "account_capital_rupees": 100000.0,
    "max_risk_percent_per_trade": 1.0,
    "max_daily_loss_percent": 3.0,
    "max_concurrent_positions": 1,
    "max_capital_deployed_percent": 20.0,
}


def resolve_risk_settings(settings: Optional[AgenticAISettings]) -> dict[str, float]:
    """The five risk/sizing fields, resolved to _DEFAULT_RISK_SETTINGS when
    settings is None (no row saved yet) -- the one place both
    check_risk_gates() and any caller building its own what-if calculation
    (e.g. the dashboard's position-size form) read these from, so neither
    can silently drift from the other's idea of what "unconfigured" means."""
    if settings is None:
        return dict(_DEFAULT_RISK_SETTINGS)
    return {
        "account_capital_rupees": settings.account_capital_rupees,
        "max_risk_percent_per_trade": settings.max_risk_percent_per_trade,
        "max_daily_loss_percent": settings.max_daily_loss_percent,
        "max_concurrent_positions": settings.max_concurrent_positions,
        "max_capital_deployed_percent": settings.max_capital_deployed_percent,
    }


def compute_position_size(
    account_capital: float,
    max_risk_percent_per_trade: float,
    entry_price: float,
    stop_loss_price: float,
    lot_size: int,
) -> int:
    """Risk-based position sizing: the rupee loss if this trade's own stop
    is hit must never exceed max_risk_percent_per_trade% of account_capital.
    Returns a whole number of lots, floored -- rounding up would silently
    risk more than the configured percent, the wrong direction to round in
    a function whose whole job is capping risk. Returns 0 (never a
    fabricated size) when any input is non-positive or the proposed stop
    carries no real per-unit risk (entry_price == stop_loss_price, which
    would otherwise divide by zero)."""
    if account_capital <= 0 or max_risk_percent_per_trade <= 0 or lot_size <= 0:
        return 0
    per_unit_risk = abs(entry_price - stop_loss_price)
    if per_unit_risk <= 0:
        return 0
    risk_amount = account_capital * (max_risk_percent_per_trade / 100.0)
    max_units = risk_amount / per_unit_risk
    return max(int(max_units // lot_size), 0)


@dataclass(frozen=True)
class RiskDecision:
    """allowed=False means a future entry agent must not open a new
    AGENTIC_AI trade right now -- reason is a stable, machine-checkable
    code; detail is the human-readable explanation, with the real numbers
    that produced the decision, for the dashboard and for logging."""

    allowed: bool
    reason: str
    detail: str


def check_risk_gates(db: Session, settings: Optional[AgenticAISettings], now_ist: datetime) -> RiskDecision:
    """The pre-entry gate a future entry agent MUST call before opening any
    AGENTIC_AI trade. Three independent checks, evaluated in a fixed order
    (most operationally urgent first) -- the first one that fails wins,
    rather than trying to rank severity across different kinds of limits:

      1. Daily loss circuit breaker -- today's already-REALIZED net P&L for
         AGENTIC_AI (closed trades only; an open trade's unrealized P&L is
         deliberately not counted here, same reasoning this project's other
         daily-loss gates use -- a floating loss can still recover, a
         realized one cannot) must not already be at or below the
         configured floor.
      2. Max concurrent open positions -- a hard cap on how many AGENTIC_AI
         trades can be open at once, independent of their size.
      3. Max capital deployed -- the combined investment_amount of every
         currently open AGENTIC_AI trade must leave room for at least one
         more at the configured ceiling.

    settings=None (not yet configured) falls back to resolve_risk_settings()'s
    own defaults, so this function never raises for lack of a settings row."""
    resolved = resolve_risk_settings(settings)
    capital = resolved["account_capital_rupees"]
    max_daily_loss_percent = resolved["max_daily_loss_percent"]
    max_concurrent = resolved["max_concurrent_positions"]
    max_deployed_percent = resolved["max_capital_deployed_percent"]

    today_stats = compute_origin_stats(db, AGENTIC_AI_ORIGIN, on_date=now_ist.date())
    realized_today = today_stats["net_pnl_total"] if today_stats.get("n", 0) > 0 else 0.0
    daily_loss_floor = -abs(capital * (max_daily_loss_percent / 100.0))
    if realized_today <= daily_loss_floor:
        return RiskDecision(
            False,
            "DAILY_LOSS_LIMIT",
            f"Today's realized net P&L (Rs {realized_today:,.2f}) has reached the daily loss limit "
            f"(Rs {daily_loss_floor:,.2f}, {max_daily_loss_percent}% of Rs {capital:,.2f} capital). "
            "No new entries for the rest of the day.",
        )

    open_trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.origin == AGENTIC_AI_ORIGIN,
                StrategyTrade.status == TradeStatus.OPEN,
            )
        )
    )
    if len(open_trades) >= max_concurrent:
        return RiskDecision(
            False,
            "MAX_CONCURRENT_POSITIONS",
            f"{len(open_trades)} AGENTIC_AI position(s) already open, at the configured limit "
            f"({max_concurrent}). No new entries until one closes.",
        )

    deployed = round(sum(t.investment_amount or 0.0 for t in open_trades), 2)
    deployed_ceiling = round(capital * (max_deployed_percent / 100.0), 2)
    if deployed >= deployed_ceiling:
        return RiskDecision(
            False,
            "MAX_CAPITAL_DEPLOYED",
            f"Rs {deployed:,.2f} already deployed across open AGENTIC_AI positions, at or above the "
            f"configured ceiling (Rs {deployed_ceiling:,.2f}, {max_deployed_percent}% of Rs {capital:,.2f} "
            "capital). No new entries until capital frees up.",
        )

    return RiskDecision(
        True,
        "OK",
        f"Clear to trade -- realized P&L today Rs {realized_today:,.2f} (floor Rs {daily_loss_floor:,.2f}), "
        f"{len(open_trades)}/{max_concurrent} positions open, Rs {deployed:,.2f}/{deployed_ceiling:,.2f} "
        "capital deployed.",
    )


def evaluate_risk_now(db: Session) -> dict[str, Any]:
    """Convenience wrapper for the dashboard's live risk-status card --
    check_risk_gates() evaluated at the real current moment, with its
    inputs/output flattened into a plain dict the template can render
    directly without reaching into a dataclass."""
    settings = get_agentic_settings(db)
    now_ist_dt = to_ist(utc_now())
    decision = check_risk_gates(db, settings, now_ist_dt)
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "detail": decision.detail,
        "evaluated_at": now_ist_dt.isoformat(),
    }


# ---------------------------------------------------------------------------
# Entry/exit agent -- the live_model tier (fast, low-latency). The model
# decides direction only (BUY_CE/BUY_PE/NONE at entry, EXIT/HOLD at exit);
# stop, target, and quantity are computed entirely by the deterministic
# risk/sizing agent above. See the module docstring's own section on this
# for the full reasoning.
# ---------------------------------------------------------------------------

# Only used when the shared PlatformSettings row itself is somehow missing
# (should not happen in practice -- get_or_create_settings always returns
# one) or its stored value is malformed. Mirrors that row's own column
# defaults, not a fresh, separately-chosen number.
_DEFAULT_TRADING_START = (9, 45)
_DEFAULT_TRADING_END = (15, 15)

# Same DTE floor and same "roll forward, never skip" reasoning as every
# other strategy in this project (see app.option_finder.find_atm_contract's
# own docstring) -- a given percentage stop survives noise meaningfully
# worse at very short DTE, so this strategy also simply never trades a
# contract closer than this, rather than accepting the extra risk.
_MIN_DTE_TO_TRADE = 5

SYSTEM_PROMPT_ENTRY = """You are a conservative trade-execution filter for intraday Indian index options (Nifty/Bank Nifty).
Your primary job is capital preservation, not finding trades. Your default decision MUST BE "NONE" unless you have
a genuinely clear, specific reason to act -- a plausible-sounding story is not enough.

You are shown only the current price, the change versus the previous trading day's close, and today's
high/low range so far. You have no indicators (no EMA, ADX, VWAP, RSI, or Supertrend), no candle history, no
option chain, and no news. This is a deliberate, known limitation of what you can see right now, not an
oversight -- weigh your own confidence accordingly, and prefer NONE whenever the picture in front of you
could plausibly be read either way.

You do not choose a stop-loss, target, or position size -- a separate, deterministic system owns those
entirely, regardless of what you say here. Your only job is to decide whether there is a clear enough
directional case to act on at all, and which direction.

Respond with a single valid JSON object only, with no markdown fences or extra text:
{"decision": "BUY_CE" | "BUY_PE" | "NONE", "confidence": 0.0-1.0, "reasoning": "A few direct sentences citing exactly what you saw"}"""

SYSTEM_PROMPT_EXIT = """You are a disciplined trade-management execution engine for intraday Indian index options (Nifty/Bank Nifty).
You are reviewing one already-open position. A separate, deterministic system enforces a hard stop-loss,
target, and end-of-day square-off independently of what you decide here -- those will catch this position
regardless of your answer, so you are not this trade's only protection. Your job is the judgment those fixed
rules cannot make: whether to exit now because the original case for this trade no longer looks right, even
though neither the stop nor the target has been hit yet.

You are shown the position's own entry price, current price, running P&L, and how long it has been open --
the same minimal, indicator-free view the entry decision was made from. Prefer HOLD when you have no
specific reason to exit -- cutting a position early on vague unease is not better risk management than
letting the fixed stop/target do their job; it is a different, unmeasured kind of risk.

Respond with a single valid JSON object only, with no markdown fences or extra text:
{"decision": "EXIT" | "HOLD", "confidence": 0.0-1.0, "reasoning": "A few direct sentences"}"""


@dataclass(frozen=True)
class _LiveCallResult:
    content: Optional[str]
    error: Optional[str]


def _call_openai_live(settings: AgenticAISettings, system_prompt: str, user_prompt: str) -> _LiveCallResult:
    if not settings.api_key or not settings.live_model:
        return _LiveCallResult(None, "Agentic AI live model/API key not configured.")
    endpoint = (settings.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    response = AIClient().send(
        endpoint=endpoint,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        payload={
            "model": settings.live_model,
            "reasoning_effort": settings.live_reasoning_effort,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=settings.timeout_seconds,
    )
    if response.error:
        return _LiveCallResult(None, response.error)
    try:
        content = response.response_body["choices"][0]["message"]["content"]
    except Exception as exc:
        return _LiveCallResult(None, f"Unexpected OpenAI response shape ({type(exc).__name__}: {exc})")
    return _LiveCallResult(content, None)


@dataclass(frozen=True)
class _EntryDecision:
    action: str  # BUY_CE / BUY_PE / NONE / ERROR
    confidence: Optional[float]
    reasoning: str


@dataclass(frozen=True)
class _ExitDecision:
    action: str  # EXIT / HOLD / ERROR
    confidence: Optional[float]
    reasoning: str


def _parse_confidence(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        confidence = float(value)
        if confidence > 1.0:
            confidence = confidence / 100.0
        return min(1.0, max(0.0, confidence))
    except (TypeError, ValueError):
        return None


def _parse_entry_response(text: Optional[str]) -> _EntryDecision:
    if not text:
        return _EntryDecision("ERROR", None, "No response text")
    try:
        data = json.loads(extract_json_object(text))
        if not isinstance(data, dict):
            return _EntryDecision("ERROR", None, "Invalid AI response (not a JSON object)")
        action = str(data.get("decision") or "").strip().upper()
        if action not in {"BUY_CE", "BUY_PE", "NONE"}:
            return _EntryDecision("ERROR", None, f"Unrecognised decision value {action!r}")
        return _EntryDecision(action, _parse_confidence(data.get("confidence")), str(data.get("reasoning") or ""))
    except Exception as exc:
        return _EntryDecision("ERROR", None, f"Could not parse AI response ({type(exc).__name__}: {exc})")


def _parse_exit_response(text: Optional[str]) -> _ExitDecision:
    if not text:
        return _ExitDecision("ERROR", None, "No response text")
    try:
        data = json.loads(extract_json_object(text))
        if not isinstance(data, dict):
            return _ExitDecision("ERROR", None, "Invalid AI response (not a JSON object)")
        action = str(data.get("decision") or "").strip().upper()
        if action not in {"EXIT", "HOLD"}:
            return _ExitDecision("ERROR", None, f"Unrecognised decision value {action!r}")
        return _ExitDecision(action, _parse_confidence(data.get("confidence")), str(data.get("reasoning") or ""))
    except Exception as exc:
        return _ExitDecision("ERROR", None, f"Could not parse AI response ({type(exc).__name__}: {exc})")


def _has_open_agentic_trade(db: Session, index_symbol: str) -> bool:
    return (
        db.scalar(
            select(StrategyTrade.id)
            .where(
                StrategyTrade.index_symbol == index_symbol,
                StrategyTrade.origin == AGENTIC_AI_ORIGIN,
                StrategyTrade.status == TradeStatus.OPEN,
            )
            .limit(1)
        )
        is not None
    )


def _build_entry_prompt(index_display_name: str, figures: dict[str, Any]) -> str:
    price = figures.get("price")
    change_abs = figures.get("change_abs")
    change_percent = figures.get("change_percent")
    day_low = figures.get("day_low")
    day_high = figures.get("day_high")
    return (
        f"Index: {index_display_name}\n"
        f"Current price: {price}\n"
        f"Change vs previous close: "
        + (f"{change_abs:+.2f} ({change_percent:+.2f}%)" if change_abs is not None and change_percent is not None else "unavailable")
        + "\n"
        f"Today's range so far: "
        + (f"{day_low} - {day_high}" if day_low is not None and day_high is not None else "unavailable")
        + "\n\nApply the evaluation rules above and respond with your decision."
    )


def _build_exit_prompt(trade: StrategyTrade, now_ist: datetime) -> str:
    entry_time_ist = to_ist(trade.entry_time) if trade.entry_time is not None else None
    elapsed_minutes = (now_ist - entry_time_ist).total_seconds() / 60 if entry_time_ist else None
    return (
        f"Position: {trade.signal} on {trade.index_symbol}, strike {trade.strike} {trade.option_type}\n"
        f"Entry price: {trade.entry_price}\n"
        f"Current price: {trade.current_premium}\n"
        f"Running P&L: {trade.pnl_percent}%\n"
        f"Time open: {f'{elapsed_minutes:.0f} minutes' if elapsed_minutes is not None else 'unknown'}\n\n"
        "Apply the evaluation rules above and respond with your decision."
    )


def open_agentic_trade(
    db: Session,
    index: IndexConfig,
    action: str,
    reasoning: str,
    settings: AgenticAISettings,
    smartapi: Any,
    option_finder: Any,
) -> Optional[StrategyTrade]:
    """Resolves a contract, computes stop/target/quantity entirely in
    Python (see module docstring), and opens the StrategyTrade row. The
    model's own action/reasoning are persisted for audit; nothing it says
    about risk is read, because it was never asked for a risk number."""
    option_type = "CE" if action == "BUY_CE" else "PE"
    signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE

    try:
        contract = option_finder.find_atm_contract(signal, index, 0, min_dte=_MIN_DTE_TO_TRADE)
    except Exception as exc:
        logger.info("[AGENTIC_AI] %s: Declined, could not resolve contract (%s)", index.symbol, exc)
        return None

    dte = days_to_expiry(contract.expiry, to_ist(utc_now()).date())
    try:
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[AGENTIC_AI] %s: Declined, could not resolve price (%s)", index.symbol, exc)
        return None
    if not entry_price:
        logger.info("[AGENTIC_AI] %s: Declined, LTP came back empty", index.symbol)
        return None

    stop_percent, stop_matched = symmetric_premium_percent(
        settings.nominal_stop_percent, index.symbol, contract.option_type, dte
    )
    target_percent, _ = symmetric_premium_percent(
        settings.nominal_target_percent, index.symbol, contract.option_type, dte
    )
    stoploss = round(entry_price * (1 - stop_percent / 100), 2)
    target = round(entry_price * (1 + target_percent / 100), 2)

    lots = compute_position_size(
        settings.account_capital_rupees, settings.max_risk_percent_per_trade,
        entry_price, stoploss, contract.lot_size,
    )
    if lots <= 0:
        logger.info(
            "[AGENTIC_AI] %s: Declined -- risk budget (%.2f%% of Rs %.2f) too small for one lot "
            "at this stop distance (entry %.2f, stop %.2f, lot size %s)",
            index.symbol, settings.max_risk_percent_per_trade, settings.account_capital_rupees,
            entry_price, stoploss, contract.lot_size,
        )
        log_event(
            db, "AGENTIC_AI",
            f"[{index.symbol}] Declined -- risk budget too small for one lot at this stop distance",
            level="INFO",
        )
        return None
    quantity = lots * contract.lot_size

    strategy_name = f"Agentic AI - {index.display_name or index.symbol}"
    trade = StrategyTrade(
        trade_id=uuid4().hex,
        strategy_name=strategy_name,
        signal=signal.value,
        index_symbol=index.symbol,
        exchange=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        symboltoken=contract.symboltoken,
        strike=contract.strike,
        expiry=contract.expiry,
        option_type=contract.option_type,
        quantity=quantity,
        investment_amount=round(entry_price * quantity, 2),
        entry_price=round(entry_price, 2),
        current_premium=round(entry_price, 2),
        stoploss=stoploss,
        target=target,
        entry_time=utc_now(),
        # Structurally paper-only -- see module docstring. No live order
        # path exists anywhere in this module.
        mode=TradingMode.PAPER,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        highest_price=round(entry_price, 2),
        lowest_price=round(entry_price, 2),
        trailing_active=False,
        # FIXED + this origin (not SIGNAL, not AI_ORIGIN_*) means
        # monitor_open_trades' shared branch gives this a plain stop/
        # target/time-exit backstop only, zero new code there.
        sl_mode=SLMode.FIXED,
        calibration_bucket_matched=stop_matched,
        origin=AGENTIC_AI_ORIGIN,
        ai_action=action,
        ai_reasoning=reasoning,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    log_event(
        db, "AGENTIC_AI",
        f"[{strategy_name}] opened {signal.value} @ strike {trade.strike}, {lots} lot(s)",
        payload={"trade_id": trade.trade_id, "reasoning": reasoning, "lots": lots},
    )
    logger.info("[AGENTIC_AI] opened %s for %s (%s lot(s))", signal.value, index.symbol, lots)
    return trade


def check_agentic_entry(
    db: Session,
    index: IndexConfig,
    now_ist: datetime,
    settings: AgenticAISettings,
    smartapi: Any,
    option_finder: Any,
    figures_by_symbol: dict[str, dict[str, Any]],
) -> Optional[StrategyTrade]:
    """One index's entry evaluation for this cycle. Gated, in order, by: an
    already-open AGENTIC_AI position on this specific index (cheap, no risk-
    gate query needed); the deterministic risk/sizing agent's check_risk_
    gates() (global across both indices -- see that function's own
    docstring); and a live price actually being available. Only once all
    three pass does this spend an LLM call."""
    if _has_open_agentic_trade(db, index.symbol):
        return None

    decision = check_risk_gates(db, settings, now_ist)
    if not decision.allowed:
        logger.info("[AGENTIC_AI] %s: Entry blocked by risk gate -- %s", index.symbol, decision.reason)
        return None

    figures = figures_by_symbol.get(index.symbol)
    if not figures or figures.get("price") is None:
        logger.info("[AGENTIC_AI] %s: Skipped, no live price available", index.symbol)
        return None

    user_prompt = _build_entry_prompt(index.display_name or index.symbol, figures)
    raw = _call_openai_live(settings, SYSTEM_PROMPT_ENTRY, user_prompt)
    if raw.error:
        logger.error("[AGENTIC_AI] %s entry call FAILED: %s", index.symbol, raw.error)
        log_event(db, "AGENTIC_AI", f"[{index.symbol}] entry call failed: {raw.error}", level="ERROR")
        return None
    parsed = _parse_entry_response(raw.content)
    if parsed.action == "ERROR":
        logger.error("[AGENTIC_AI] %s entry response unparseable: %s", index.symbol, parsed.reasoning)
        return None
    logger.info("[AGENTIC_AI] %s -> %s", index.symbol, parsed.action)
    if parsed.action == "NONE":
        log_event(
            db, "AGENTIC_AI", f"[{index.symbol}] declined ({parsed.reasoning})",
            payload={"confidence": parsed.confidence},
        )
        return None

    return open_agentic_trade(db, index, parsed.action, parsed.reasoning, settings, smartapi, option_finder)


def check_agentic_exits(
    db: Session,
    trade_manager: Any,
    settings: AgenticAISettings,
    end_hm: tuple[int, int],
) -> None:
    """Re-asks the live model, for every currently open AGENTIC_AI trade not
    already past the hard square-off time, whether to HOLD or EXIT -- and
    actually closes on EXIT via trade_manager.close_trade (the same helper
    monitor_open_trades' own backstop uses), so every exit path records a
    close identically. The backstop stop/target/time-exit this trade
    already carries (sl_mode=FIXED) keeps enforcing independently of
    whatever this function decides, every 30 seconds via the shared
    monitor -- this is a second, faster-reacting line of judgment on top of
    that, not a replacement for it."""
    trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == AGENTIC_AI_ORIGIN,
            )
        )
    )
    now_ist = to_ist(utc_now())
    past_cutoff = (now_ist.hour, now_ist.minute) >= end_hm

    for trade in trades:
        try:
            if trade.current_premium is None:
                continue

            if past_cutoff:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.TIME_EXIT)
                log_event(
                    db, "AGENTIC_AI",
                    f"[{trade.strategy_name}] squared off at {end_hm[0]:02d}:{end_hm[1]:02d} IST cutoff",
                    payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
                )
                logger.info(
                    "[AGENTIC_AI] %s squared off at %02d:%02d IST cutoff (%.2f%%)",
                    trade.trade_id, end_hm[0], end_hm[1], trade.pnl_percent or 0.0,
                )
                continue

            user_prompt = _build_exit_prompt(trade, now_ist)
            raw = _call_openai_live(settings, SYSTEM_PROMPT_EXIT, user_prompt)
            if raw.error:
                logger.error("[AGENTIC_AI] %s exit call FAILED: %s", trade.trade_id, raw.error)
                continue
            parsed = _parse_exit_response(raw.content)
            if parsed.action == "ERROR":
                logger.error("[AGENTIC_AI] %s exit response unparseable: %s", trade.trade_id, parsed.reasoning)
                continue
            log_event(
                db, "AGENTIC_AI",
                f"[{trade.strategy_name}] exit check: {parsed.action}",
                payload={
                    "trade_id": trade.trade_id, "reasoning": parsed.reasoning, "pnl_percent": trade.pnl_percent,
                },
            )
            if parsed.action == "EXIT":
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.AI_DISCRETION_EXIT)
                logger.info(
                    "[AGENTIC_AI] %s closed at model's own discretion (%.2f%%)",
                    trade.trade_id, trade.pnl_percent or 0.0,
                )
        except Exception:
            logger.exception("[AGENTIC_AI] exit check failed for trade %s", trade.trade_id)


def run_agentic_entry_exit_checks(
    smartapi: Any = None,
    option_finder: Any = None,
    trade_manager: Any = None,
    feed_store: Any = None,
    db: Optional[Session] = None,
) -> None:
    """Scheduler entry point (see app.scheduler's "agentic-ai-check" job).
    Owns its own DB session when called from the scheduler; accepts an
    existing session in tests -- same shape as app.ai.autonomous's
    run_autonomous_checks."""
    if smartapi is None or option_finder is None or trade_manager is None:
        logger.info("[AGENTIC_AI] Skipped: no smartapi/option_finder/trade_manager available in this context")
        return
    closed_reason = check_market_hours(utc_now())
    if closed_reason is not None:
        logger.info("[AGENTIC_AI] Cycle skipped -- %s", closed_reason.replace("Signal received ", "", 1))
        return

    from app.database import SessionLocal

    owns_session = db is None
    session = db or SessionLocal()
    try:
        settings = get_agentic_settings(session)
        if settings is None or not settings.enabled:
            logger.info("[AGENTIC_AI] Skipped: not enabled (Settings > Agentic AI)")
            return
        if not settings.api_key or not settings.live_model:
            logger.info("[AGENTIC_AI] Skipped: live model/API key not configured")
            return

        now_ist = to_ist(utc_now())
        indexes = [index for index in list_index_configs(session) if index.enabled]
        figures_by_symbol = {row["symbol"]: row for row in get_index_live_figures(session, smartapi, feed_store)}

        platform_settings = get_or_create_settings(session)
        end_hm = parse_hhmm(platform_settings.square_off_time, _DEFAULT_TRADING_END)

        # Exits first -- closing a stale position frees its slot for a
        # fresh entry the same cycle, same ordering app.ai.autonomous
        # already uses for the same reason.
        check_agentic_exits(session, trade_manager, settings, end_hm)

        start_hm = parse_hhmm(platform_settings.trading_start_time, _DEFAULT_TRADING_START)
        if (now_ist.hour, now_ist.minute) < start_hm or (now_ist.hour, now_ist.minute) >= end_hm:
            return

        for index in indexes:
            try:
                check_agentic_entry(session, index, now_ist, settings, smartapi, option_finder, figures_by_symbol)
            except Exception:
                logger.exception("[AGENTIC_AI] entry check failed for %s", index.symbol)
    finally:
        if owns_session:
            session.close()
