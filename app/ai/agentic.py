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

    27 Oct 2026, real production evidence, explicit instruction: on day
    one of the live multi-agent entry/exit agent (below), the Orchestrator
    repeatedly wanted to trade and compute_position_size() repeatedly came
    back 0 lots -- the configured risk-per-trade budget genuinely couldn't
    afford one lot at real Nifty/Bank Nifty ATM premiums against the
    configured nominal stop. Tightening the math further (a bigger risk %)
    only narrows the gap, it doesn't close it for every premium -- so per
    explicit instruction ("I want to trade it whatever it takes as it is
    paper trades only"), open_agentic_trade() now floors a 0-lot result up
    to _MIN_LOTS_PAPER_FLOOR (1) rather than declining the trade outright,
    logged and persisted with an explicit forced_minimum_lot flag every
    time it fires -- see that constant's own comment for why this is safe
    specifically because this module is structurally paper-only.
    compute_position_size() ITSELF is untouched and still floors down to 0
    normally -- the floor lives only in this one caller, not in the shared
    sizing function the dashboard's own what-if calculator also uses, so
    that calculator keeps answering the real question ("could this size
    at all") rather than a paper-trading-only one.

  - The entry/exit agent, rebuilt into TRUE multi-agent fusion (25 Oct
    2026, same day as the news agent above): a dedicated Technical & Flow
    Agent, a dedicated News & Sentiment Agent, and a final Master
    Orchestrator Agent that reconciles both verdicts into the actual
    decision -- three independent live_model calls per index per cycle
    (two shared across indices) instead of one flat prompt, per explicit
    instruction ("Idea was to build agents for each process and make a
    decesion combinin all the intelligence" / "i want everything in
    combination which helps in taking better trades"), choosing this over
    the cheaper single-combined-call alternative with the stated tradeoff
    (3-4x the live_model calls and latency per cycle, each extra call a
    new source of noise) explicitly accepted. Wired via run_agentic_entry_
    exit_checks() (the "agentic-ai-check" scheduler job), all three calls
    on the same live_model/live_reasoning_effort tier -- no new settings
    field, since this is a change to how many calls are made and what each
    one sees, not a change to which model answers them.

    THE MODEL NEVER DECIDES RISK, unchanged by this rebuild. Across all
    three calls, the only things ever produced are a directional lean/
    decision, a confidence, and reasoning text -- stop, target, and
    position size are still computed entirely by the deterministic risk/
    sizing agent above (nominal_stop_percent/nominal_target_percent +
    compute_position_size()/check_risk_gates()), exactly as before this
    rebuild. The Orchestrator is a fourth opinion on DIRECTION, never a
    second opinion on SIZE.

    Technical & Flow Agent (_compute_technical_features + get_technical_
    verdict): per enabled index, per cycle, builds its OWN candle-fetch-
    and-refresh orchestration (own WebSocket-freshness check via
    latest_bar_age_seconds, own REST fallback via smartapi.get_candles,
    own store_bars/load_bars/resample calls) around the shared, already-
    battle-tested app.market_context.build_market_context() -- mirroring,
    not reusing, app.ai.autonomous's private _compute_features for exactly
    the same reason every AI subsystem in this project keeps its own
    orchestration layer around that one shared pure-computation function:
    a change or bug in one module's candle-fetch plumbing must never
    silently affect another's (see CLAUDE.md's "deliberately independent
    subsystems" principle). Deliberately narrower than Autonomous AI's own
    feature set: EMA9/21 regime, ADX, Kaufman chop efficiency ratio,
    recent ~15-min price change, and PDH/PDL distance -- no futures-VWAP
    substitution (that needs its own futures-contract resolution and
    candle fetch, a real added cost this first pass skips; chop+ADX+EMA-
    regime+recent-momentum already give a genuine, non-trivial technical
    read without it). Returns None (fails closed, same convention
    build_market_context itself uses) when there isn't enough INDEX
    candle history yet -- an index with no technical read this cycle
    skips its entry decision outright, the same "missing context means
    skip, never guess" rule this codebase applies everywhere.

    The LLM call itself is handed the already-computed indicator VALUES
    (never raw price alone) and asked for a structured, three-way lean
    (BULLISH/BEARISH/NEUTRAL) plus confidence and reasoning -- the same
    "show computed context, ask for a read" shape AI Origination's own
    prompt already uses, scoped down to interpretation rather than a
    trade decision, since the Orchestrator (not this agent) makes the
    final call.

    News & Sentiment Agent, upgraded from a flat text dump into its own
    verdict (get_news_verdict): ONE shared call per cycle (headlines are
    market-wide, not index-specific, so this is not re-run per index),
    given the same real headlines app.ai.news.fetch_market_headlines()
    already fetches, asked for the same three-way lean/confidence/
    reasoning shape as the Technical Agent so the Orchestrator can weigh
    both on equal footing. Explicitly told most headlines are routine
    noise and NEUTRAL is the correct default -- same discipline the old
    flat-text prompt already stated, now enforced on a dedicated agent
    whose only job is this read, rather than folded as an aside into the
    entry prompt. Zero headlines skips the call entirely and returns a
    real NEUTRAL verdict directly (nothing to assess, not worth spending
    a call to say so); a call failure ALSO degrades to a NEUTRAL fallback
    verdict rather than blocking the pipeline -- news is supplementary
    context, not a required gate, so a provider hiccup on this call must
    never by itself prevent the Technical Agent and Orchestrator from
    still running. This is a deliberate asymmetry from the Technical
    Agent, whose own failure DOES skip the cycle for that index -- the
    technical read is this pipeline's primary, required signal; the news
    read is corroborating context.

    Master Orchestrator Agent (_build_orchestrator_prompt + the entry
    call itself, reusing _EntryDecision/_parse_entry_response's existing
    BUY_CE/BUY_PE/NONE schema unchanged): the final decision-maker, shown
    the Technical Agent's verdict, the News Agent's verdict, the raw
    headline list, and the same price/range context the old single-call
    design already showed. News is deliberately an INDICATOR, not a
    second required gate (26 Oct 2026 -- the first prompt wording made any
    disagreement a near-veto over even a clear Technical case, which is
    not what was asked for): the Technical Agent's read is the primary
    driver; News is corroborating weight, read against its own stated
    confidence, not against its mere presence or absence. Agreement
    between the two still makes the strongest case to act; a NEUTRAL news
    read is explicitly not disagreement; genuine disagreement only lowers
    confidence or forces NONE in proportion to how confident each agent
    actually was, never automatically. The Orchestrator still defaults to
    NONE absent a genuinely clear case, same capital-preservation framing
    the single-call prompt always had. The deterministic severe-event
    kill switch (app.ai.news.classify_severe_event) is a different
    category entirely and is NOT softened by this -- it is a safety
    circuit breaker for a genuine black-swan headline, not an alpha
    signal, and stays an absolute block checked before any of the three
    agents is ever asked.

    The exit side gained the SAME two verdicts as supplementary context
    (SYSTEM_PROMPT_EXIT / _build_exit_prompt), reusing whatever was
    already computed for that index this cycle rather than spending two
    more calls per open position -- an exit re-ask is still exactly one
    call, same as before this rebuild; only what it's shown grew. A
    missing technical verdict (e.g. insufficient candle history right
    now) is shown as "unavailable" rather than skipping the exit call
    outright -- the backstop stop/target/time-exit protect this trade
    regardless, so a thin technical read is not a reason to stop asking
    the model for its own exit judgment.

    28 Oct 2026, real production evidence, explicit instruction: a real
    position ran up to +19% peak P&L and was still open at +5% by the
    time the model was re-asked -- and its own exit prompt never showed
    it the peak at all, only the current snapshot each cycle, so the
    model had no way to know a giveback had even happened. _peak_pnl_
    percent() now reads trade.highest_price (already tracked for every
    open trade by the shared 30s monitor_open_trades backstop regardless
    of origin, just never previously read here) and _build_exit_prompt
    shows it alongside current P&L. SYSTEM_PROMPT_EXIT was extended to
    weigh the two together with the Technical & Flow Agent's own read --
    a real giveback against a thesis that's lost momentum is a genuine
    reason to exit and lock in profit; the same giveback against a thesis
    the Technical Agent still supports is ordinary noise and HOLD stays
    correct. Deliberately still no hard deterministic peak-giveback rule
    (per explicit instruction: "determine what's the peak, what's market
    is behaving, and it should keep it or exit" -- the model's own
    judgment, not a fixed formula) -- this is additional INFORMATION for
    the existing exit agent, not a new gate, so stop/target/quantity are
    completely untouched and "the model never decides risk" still holds.

    Market input beyond the above is still deliberately minimal -- see
    the Technical Agent's own narrower-than-Autonomous-AI feature set
    above. Structurally paper-only, same construction and reasoning as
    every other experimental strategy in this project: TradingMode.PAPER
    is hardcoded at open, smartapi.place_market_order is never called
    anywhere in this module. One position at a time across BOTH indices
    combined by default (max_concurrent_positions=1, the same global cap
    check_risk_gates() already enforces), not a separate per-index slot
    the way AI Origination works -- same Single Active Position Rule
    precedent Validated Signal already established.

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

    28 Oct 2026, same day, second real-evidence pass (not a contradiction of
    the paragraph above -- "no evidence of its own" stopped being true the
    same day it was written): the peak-P&L prompt fix directly above still
    wasn't enough on its own the first time real data looked at it. A real
    Nifty BUY_PE ran to +43.42% peak MFE and the model's own exit judgment --
    re-asked every 5 minutes, ~20+ times across the trade's 1h41m lifetime,
    already shown its own peak on every one of those asks -- held through
    nearly all of it, closing at +2.49%; a Bank Nifty BUY_CE the same day
    gave back +7.64% MFE down to -1.42%. Fixed two ways, not one:

      1. SYSTEM_PROMPT_EXIT's giveback paragraph now closes the specific
         loophole this data exposed: restating that the Technical read
         "still supports the thesis" was never, by itself, a resolution --
         that same read was already true at the peak too, so repeating it
         supplies no new information. Once the giveback is large, HOLD now
         requires naming something concrete and CURRENT that still points
         to further upside, not merely that nothing has technically broken
         yet. A shallow pullback on a position that never ran up much still
         needs none of this and stays an easy HOLD -- this only tightens
         the case that was previously too easy to wave through on autopilot.
         Still no fixed numeric giveback threshold in the prompt itself --
         the model decides how "large" is large, per the same explicit
         instruction behind the first peak-P&L pass.

      2. Unlike the rest of Autonomous AI's matrix (still not imported, see
         above), the peak-giveback GIVEBACK_STOP floor specifically now IS
         added for this origin (app.multi_strategy._GIVEBACK_STOP_ORIGINS) --
         not a new, unvalidated threshold invented for this strategy, but
         the exact same already-backtested mechanism (floor=12% of entry,
         locks in 70% of the peak gain once armed) already running live for
         QUICK_SCALP/AUTONOMOUS_AI today, reused rather than retuned. This is
         the floor UNDER the model's own judgment, not a replacement for it
         -- the per-cycle EXIT/HOLD re-ask above still runs exactly as
         before and can still exit earlier and smarter than this mechanical
         floor would; this only bounds how much a slow or indecisive cycle
         can give back while the model is still deciding. See that
         constant's own module-level comment in app/multi_strategy.py for
         the full reasoning on why this origin fits the same shape
         QUICK_SCALP/AUTONOMOUS_AI already proved it on.

  - The news agent (app.ai.news, 25 Oct 2026) -- the headline-fetching and
    deterministic severe-event half of the news agent described above.
    Fetches recent India-market headlines from free, keyless RSS feeds. A
    separate, deterministic "severe event" keyword match (circuit
    breaker, war, bank run, ...) blocks all new entries and force-closes
    every open AGENTIC_AI position via ExitReason.NEWS_KILL_SWITCH, before
    the model -- any of the three agents above -- is ever asked, checked
    first and cheapest every cycle -- the same "the model never decides
    risk" principle the risk/sizing agent established for P&L, applied
    here to a market-wide shock. See app/ai/news.py's own docstring for
    why this doesn't need the same backtest-before-shipping discipline a
    directional signal would, and for the real limitation that this
    sandbox cannot verify any of the three feed URLs are actually
    reachable.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import AIClient
from app.ai.json_utils import extract_json_object
from app.ai.news import classify_severe_event, fetch_market_headlines, format_headlines_for_prompt
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
from app.market_context import build_market_context
from app.market_data import (
    BAR_FRESHNESS_SECONDS,
    FIFTEEN_MINUTE,
    FIVE_MINUTE,
    ONE_MINUTE,
    latest_bar_age_seconds,
    load_bars,
    parse_smartapi_row,
    resample,
    store_bars,
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
# Entry/exit agent, rebuilt into true multi-agent fusion -- see the module
# docstring's own section on this for the full reasoning. Three live_model
# calls per index per cycle (two shared across indices): Technical & Flow
# Agent, News & Sentiment Agent, Master Orchestrator Agent. The model never
# decides risk, unchanged -- stop, target, and quantity are still computed
# entirely by the deterministic risk/sizing agent above.
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

# 27 Oct 2026, explicit instruction: a signal that already cleared every
# other gate (severe-event kill switch, risk-gate, live price, Technical
# Agent, Orchestrator) should never be thrown away purely because the
# risk-based lot count rounded down to zero -- "whatever it takes... it is
# paper trades only." compute_position_size() is left completely untouched
# (it still floors down normally for the dashboard's own what-if
# calculator, which has no business inflating what a real account could
# actually size), and the resolved lots are still computed and logged
# first for audit -- only open_agentic_trade's own floor is raised, and
# only because this module is structurally paper-only (TradingMode.PAPER
# hardcoded, no live order path anywhere in this file): there is no real
# capital this floor could ever put at risk. The realized risk at this
# minimum can genuinely exceed max_risk_percent_per_trade's own configured
# ceiling when it fires -- that is the whole point, and it is flagged
# explicitly in both the log line and the persisted trade's own event
# payload, never silently.
_MIN_LOTS_PAPER_FLOOR = 1

# Same warmup window / load cap app.ai.autonomous's own _compute_features
# uses for the identical reason -- enough 1-min history to resample 5m/15m
# bars and warm up ADX/EMA, without loading an unbounded amount of history
# every cycle. A deliberately separate pair of constants, not imported --
# see the module docstring's "deliberately independent subsystems" note.
_CANDLE_WARMUP_DAYS = 7
_CANDLE_LOAD_LIMIT = 3000


@dataclass(frozen=True)
class _TechnicalFeatures:
    """The Technical & Flow Agent's raw, already-computed indicator values
    for one index, one cycle -- never raw price alone. Deliberately
    narrower than app.ai.autonomous's own _Features (no futures-VWAP
    substitution in this first pass; see the module docstring)."""

    ema9: Optional[float]
    ema21: Optional[float]
    trend_regime: str  # BULLISH / BEARISH / NEUTRAL / UNKNOWN
    adx: Optional[float]
    chop_efficiency_ratio: Optional[float]
    chop_label: str  # CHOPPY / MIXED / CLEAN / UNKNOWN
    recent_price_change_percent: Optional[float]
    pdh: Optional[float]
    pdl: Optional[float]
    dist_to_pdh: Optional[float]
    dist_to_pdl: Optional[float]


def _trend_regime_label(fast_ema: Optional[float], slow_ema: Optional[float]) -> str:
    """Same EMA9-vs-EMA21 comparison app.ai.autonomous's own _trend_regime
    already uses, duplicated rather than imported per this module's
    established "deliberately independent" convention (see the module
    docstring)."""
    if fast_ema is None or slow_ema is None:
        return "UNKNOWN"
    if fast_ema > slow_ema:
        return "BULLISH"
    if fast_ema < slow_ema:
        return "BEARISH"
    return "NEUTRAL"


def _chop_label(ratio: Optional[float]) -> str:
    """Same 0.3/0.5 Kaufman Efficiency Ratio band boundaries app.ai.
    autonomous's own _chop_label (and, before it, app.ai.originator's
    _efficiency_ratio_text) already use, duplicated rather than imported."""
    if ratio is None:
        return "UNKNOWN"
    if ratio < 0.3:
        return "CHOPPY"
    if ratio < 0.5:
        return "MIXED"
    return "CLEAN"


def _compute_technical_features(
    db: Session,
    index: IndexConfig,
    spot: float,
    now_ist: datetime,
    smartapi: Any,
) -> Optional[_TechnicalFeatures]:
    """This agent's OWN candle-fetch-and-refresh orchestration around the
    shared, already-battle-tested app.market_context.build_market_context()
    -- see the module docstring for why this mirrors rather than reuses
    app.ai.autonomous's private _compute_features. Returns None (fails
    closed) when there isn't enough INDEX candle history yet to compute
    ADX/EMA honestly, same convention build_market_context itself uses."""
    if not index.spot_token:
        return None

    age = latest_bar_age_seconds(db, index.symbol, ONE_MINUTE, now_ist)
    if age is not None and age <= BAR_FRESHNESS_SECONDS:
        logger.debug(
            "[AGENTIC_AI][TECH] %s: 1-min history is %.0fs old (WebSocket-fed), skipping REST refresh",
            index.symbol, age,
        )
    else:
        try:
            rows = smartapi.get_candles(
                exchange=index.spot_exchange,
                symboltoken=index.spot_token,
                interval=ONE_MINUTE,
                from_dt=(now_ist - timedelta(days=_CANDLE_WARMUP_DAYS)).strftime("%Y-%m-%d %H:%M"),
                to_dt=now_ist.strftime("%Y-%m-%d %H:%M"),
            )
            if rows:
                store_bars(db, index.symbol, ONE_MINUTE, [parse_smartapi_row(row) for row in rows])
        except Exception as exc:
            logger.info("[AGENTIC_AI][TECH] %s: candle refresh failed (%s), using stored history", index.symbol, exc)

    bars_1m = load_bars(db, index.symbol, ONE_MINUTE, limit=_CANDLE_LOAD_LIMIT)
    if not bars_1m:
        return None
    bars_5m = resample(bars_1m, FIVE_MINUTE)
    bars_15m = resample(bars_1m, FIFTEEN_MINUTE)
    context = build_market_context(
        index_symbol=index.symbol,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_15m=bars_15m,
        spot=spot,
        as_of=now_ist.replace(tzinfo=None),
    )
    if context is None:
        return None

    pdh = context.levels.previous_day_high
    pdl = context.levels.previous_day_low
    return _TechnicalFeatures(
        ema9=context.ema9,
        ema21=context.ema21,
        trend_regime=_trend_regime_label(context.ema9, context.ema21),
        adx=context.adx,
        chop_efficiency_ratio=context.chop_efficiency_ratio,
        chop_label=_chop_label(context.chop_efficiency_ratio),
        recent_price_change_percent=context.recent_price_change_percent,
        pdh=pdh,
        pdl=pdl,
        dist_to_pdh=round(pdh - spot, 2) if pdh is not None else None,
        dist_to_pdl=round(spot - pdl, 2) if pdl is not None else None,
    )


@dataclass(frozen=True)
class _AgentVerdict:
    """The shared output shape for both sub-agents (Technical & Flow, News
    & Sentiment) -- a directional LEAN, never a trade decision; only the
    Orchestrator ever produces BUY_CE/BUY_PE/NONE. lean="ERROR" means the
    call itself failed (unparseable response, provider error, not
    configured) -- callers decide whether that's fail-closed (Technical,
    a required signal) or fail-soft to NEUTRAL (News, supplementary)."""

    lean: str  # BULLISH / BEARISH / NEUTRAL / ERROR
    confidence: Optional[float]
    reasoning: str


def _parse_agent_verdict(text: Optional[str]) -> _AgentVerdict:
    if not text:
        return _AgentVerdict("ERROR", None, "No response text")
    try:
        data = json.loads(extract_json_object(text))
        if not isinstance(data, dict):
            return _AgentVerdict("ERROR", None, "Invalid AI response (not a JSON object)")
        lean = str(data.get("lean") or "").strip().upper()
        if lean not in {"BULLISH", "BEARISH", "NEUTRAL"}:
            return _AgentVerdict("ERROR", None, f"Unrecognised lean value {lean!r}")
        return _AgentVerdict(lean, _parse_confidence(data.get("confidence")), str(data.get("reasoning") or ""))
    except Exception as exc:
        return _AgentVerdict("ERROR", None, f"Could not parse AI response ({type(exc).__name__}: {exc})")


SYSTEM_PROMPT_TECHNICAL = """You are the Technical & Flow Agent, one of three independent agents feeding a final trading decision for
intraday Indian index options (Nifty/Bank Nifty). You do NOT decide whether to trade -- a separate Orchestrator
agent does that, combining your read with a News & Sentiment agent's own independent read. Your only job is to
interpret the already-computed technical indicator values you are shown into a single directional lean.

You are shown: the EMA9-vs-EMA21 regime (BULLISH/BEARISH/NEUTRAL), ADX (trend strength -- below ~20 generally
means no real trend is established, above ~25 generally means one is), the Kaufman Efficiency Ratio label for
the last ~1 hour (CHOPPY/MIXED/CLEAN -- how directly price has actually travelled, independent of ADX, which is
a slower, more lagging signal), the recent ~15-minute price change, and distance to the previous day's high/low.
Any of these can read UNKNOWN/unavailable -- treat that as missing information, never as a tiebreaker in either
direction.

A CHOPPY efficiency reading or a NEUTRAL/UNKNOWN regime are both reasons to lean NEUTRAL yourself, even if one
other reading looks superficially directional -- a single strong-looking number inside an otherwise mixed
picture is not a clear technical case. Reserve BULLISH/BEARISH for when multiple readings genuinely agree.

Respond with a single valid JSON object only, with no markdown fences or extra text:
{"lean": "BULLISH" | "BEARISH" | "NEUTRAL", "confidence": 0.0-1.0, "reasoning": "A few direct sentences citing exactly which readings drove this"}"""


def _build_technical_prompt(index_display_name: str, features: _TechnicalFeatures) -> str:
    return (
        f"Index: {index_display_name}\n"
        f"EMA9/EMA21 regime: {features.trend_regime}"
        + (f" (EMA9={features.ema9}, EMA21={features.ema21})" if features.ema9 is not None else "")
        + "\n"
        f"ADX: {features.adx if features.adx is not None else 'unavailable'}\n"
        f"Market efficiency (last ~1hr): {features.chop_label}"
        + (f" ({features.chop_efficiency_ratio})" if features.chop_efficiency_ratio is not None else "")
        + "\n"
        f"Recent price change (last ~15 min): "
        + (f"{features.recent_price_change_percent:+.2f}%" if features.recent_price_change_percent is not None else "unavailable")
        + "\n"
        f"Distance to previous day's high: {features.dist_to_pdh if features.dist_to_pdh is not None else 'unavailable'}\n"
        f"Distance to previous day's low: {features.dist_to_pdl if features.dist_to_pdl is not None else 'unavailable'}\n"
        "\nApply the evaluation rules above and respond with your lean."
    )


def get_technical_verdict(settings: AgenticAISettings, index_display_name: str, features: _TechnicalFeatures) -> _AgentVerdict:
    """The Technical & Flow Agent's own LLM call -- always spent once
    features are available, since this is this pipeline's primary,
    required signal (unlike the News agent below, a call failure here is
    NOT papered over with a NEUTRAL fallback; callers fail closed)."""
    raw = _call_openai_live(settings, SYSTEM_PROMPT_TECHNICAL, _build_technical_prompt(index_display_name, features))
    if raw.error:
        return _AgentVerdict("ERROR", None, raw.error)
    return _parse_agent_verdict(raw.content)


SYSTEM_PROMPT_NEWS = """You are the News & Sentiment Agent, one of three independent agents feeding a final trading decision for
intraday Indian index options (Nifty/Bank Nifty). You do NOT decide whether to trade -- a separate Orchestrator
agent does that, combining your read with a Technical & Flow agent's own independent read. Your only job is to
read the real headlines you are shown and produce a single directional lean for how they bear on Indian equity
markets right now.

Most ordinary business/market headlines are routine noise and change nothing about today's price action --
NEUTRAL is the correct default. Give a headline real directional weight only when it describes a genuinely
specific, concrete event plausibly moving the broad market (a central bank rate decision, a major macro data
release, an index-level policy announcement) rather than generic commentary, a single company's results, or a
headline merely describing price action that has already happened (a headline saying "Nifty rallies" is a
description, not new information). The mere presence of news, on its own, is not directional evidence.

Respond with a single valid JSON object only, with no markdown fences or extra text:
{"lean": "BULLISH" | "BEARISH" | "NEUTRAL", "confidence": 0.0-1.0, "reasoning": "A few direct sentences citing which headline(s), if any, drove this"}"""


def _build_news_prompt(headlines_text: str) -> str:
    return (
        f"Recent market headlines (most recent first):\n{headlines_text}\n"
        "\nApply the evaluation rules above and respond with your lean."
    )


def get_news_verdict(settings: AgenticAISettings, headlines_text: str) -> _AgentVerdict:
    """The News & Sentiment Agent's own LLM call -- ONE shared call per
    cycle, not per index, since headlines are market-wide. Zero headlines
    skips the call entirely (nothing to assess, not worth spending a call
    to say so); a call failure ALSO degrades to a NEUTRAL fallback rather
    than an ERROR -- news is supplementary context here, so a provider
    hiccup on this call must never by itself block the Technical Agent or
    the Orchestrator from still running (see the module docstring's note
    on this deliberate asymmetry with the Technical Agent)."""
    if not headlines_text:
        return _AgentVerdict("NEUTRAL", None, "No recent headlines available this cycle.")
    raw = _call_openai_live(settings, SYSTEM_PROMPT_NEWS, _build_news_prompt(headlines_text))
    if raw.error:
        logger.info("[AGENTIC_AI][NEWS] verdict call failed, falling back to NEUTRAL: %s", raw.error)
        return _AgentVerdict("NEUTRAL", None, f"News agent call failed ({raw.error}); treated as no signal.")
    verdict = _parse_agent_verdict(raw.content)
    if verdict.lean == "ERROR":
        logger.info("[AGENTIC_AI][NEWS] verdict unparseable, falling back to NEUTRAL: %s", verdict.reasoning)
        return _AgentVerdict("NEUTRAL", None, f"News agent response unparseable ({verdict.reasoning}); treated as no signal.")
    return verdict


SYSTEM_PROMPT_ORCHESTRATOR = """You are the Master Orchestrator for intraday Indian index options (Nifty/Bank Nifty) trade entries.
Two independent agents have already given you their own read: a Technical & Flow Agent's directional lean from
price/indicator data, and a News & Sentiment Agent's directional lean from real recent headlines. Your job is to
combine both into the actual trade decision -- you did not compute either read yourself, and you must not
second-guess the specific readings each agent cites, only weigh their conclusions against each other.

Your primary job is capital preservation, not finding trades. Your default decision MUST BE "NONE" unless you
have a genuinely clear case -- a plausible-sounding story from either agent alone is not enough.

Treat the Technical & Flow Agent's read as the primary driver of your decision -- it is built from real
price/indicator data about this specific index right now. Treat the News & Sentiment Agent's read as one
additional, corroborating signal, not a second gate the trade must also clear -- most headlines are routine
noise, and the News Agent's own instructions already tell it to default to NEUTRAL when that's the case.
When the two agents AGREE, that is the strongest case to act, and confidence should be higher than the
Technical read alone would justify. A NEUTRAL news read is not disagreement -- decide on the Technical read
alone when News has nothing to add. When News actively DISAGREES with a Technical lean that is itself only
moderately confident, that is a real reason to lower your confidence or prefer NONE. But when the Technical
read is clear and well-supported and News's own opposing confidence is low, do not let a single weak
contrarian headline override a clear technical case on disagreement alone -- name the disagreement in your
reasoning and weigh it by how confident each agent actually was, not by its mere presence.

You do not choose a stop-loss, target, or position size -- a separate, deterministic system owns those
entirely, regardless of what you say here. Your only job is to decide whether there is a clear enough
combined directional case to act on at all, and which direction.

Respond with a single valid JSON object only, with no markdown fences or extra text:
{"decision": "BUY_CE" | "BUY_PE" | "NONE", "confidence": 0.0-1.0, "reasoning": "A few direct sentences on how the two agents' reads combined"}"""

SYSTEM_PROMPT_EXIT = """You are a disciplined trade-management execution engine for intraday Indian index options (Nifty/Bank Nifty).
You are reviewing one already-open position. A separate, deterministic system enforces a hard stop-loss,
target, and end-of-day square-off independently of what you decide here -- those will catch this position
regardless of your answer, so you are not this trade's only protection. Your job is the judgment those fixed
rules cannot make: whether to exit now because the original case for this trade no longer looks right, even
though neither the stop nor the target has been hit yet.

You are shown the position's own entry price, current price, running P&L, its own PEAK P&L (the best
unrealized gain it has reached so far, which may be well above its current running P&L), how long it has
been open, and the same two independent sub-agent reads a fresh entry decision would be shown right now --
a Technical & Flow Agent's own directional lean, and a News & Sentiment Agent's own directional lean (either
may read NEUTRAL/unavailable; treat that as missing information, not as either reassurance or an alarm).

A position that has given back a meaningful share of a real peak gain is a genuine reason to weigh exiting
and locking in profit, not just a number to note -- especially when the Technical & Flow Agent's own read
suggests the move that drove that peak has lost momentum or reversed. Weigh how much of the peak has been
given back together with what both agents currently say, not the peak number alone: a position that never
moved much giving back a little is not the same as one that ran up sharply and has already fallen most of
the way back down.

Once the giveback is large, simply noting that the Technical read "still supports the original thesis" is
not by itself a resolution -- that same read was already true when the position was at its peak too, so
repeating it is not new information and does not explain why holding from here is still the better bet.
HOLD in that situation requires naming something concrete and current that still points to a further
favorable move -- a fresh confirming reading, a level just held, a specific reason this pullback looks like
noise rather than the move reversing -- not merely that nothing has technically broken yet. If your own
reasoning only restates why the trade was entered, or notes the absence of a reversal signal, treat that as
insufficient and lean toward EXIT instead. A position that never ran up much to begin with needs none of
this scrutiny -- a shallow pullback below a small peak is ordinary noise and HOLD remains correct by default.
Prefer HOLD when you have no specific reason to exit and the giveback itself is not large -- cutting a
position early on vague unease about a trade that never really moved is not better risk management than
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


def _peak_pnl_percent(trade: StrategyTrade) -> float:
    """The best unrealized gain this position has reached so far, from
    trade.highest_price -- already tracked for every open trade regardless
    of origin by the shared 30s monitor_open_trades backstop, just never
    previously read by this module's own exit agent (see the module
    docstring's 28 Oct 2026 entry). Same formula app.ai.autonomous's own
    _peak_pnl_percent already uses, duplicated per this module's
    established "deliberately independent" convention: highest_price is
    the raw premium high, not a direction-adjusted figure, and every
    Agentic AI trade (CE or PE) is a BUY -- a higher premium always means
    more profit regardless of option_type, so no CE/PE sign-flip is
    needed here, unlike pnl_percent's own sign conventions elsewhere in
    this codebase for short-side strategies. Falls back to the trade's
    own current pnl_percent (never a fabricated number) when highest_price
    hasn't been populated yet."""
    if not trade.entry_price or trade.highest_price is None:
        return trade.pnl_percent or 0.0
    return round((trade.highest_price - trade.entry_price) / trade.entry_price * 100, 2)


def _format_verdict(label: str, verdict: Optional[_AgentVerdict]) -> str:
    """Shared rendering for both sub-agent verdicts into either the
    Orchestrator's or the exit prompt's text -- "unavailable" (never a
    fabricated lean) when verdict is None or itself came back ERROR."""
    if verdict is None or verdict.lean == "ERROR":
        return f"{label}: unavailable"
    confidence_text = f", confidence {verdict.confidence:.2f}" if verdict.confidence is not None else ""
    reasoning_text = f" -- {verdict.reasoning}" if verdict.reasoning else ""
    return f"{label}: {verdict.lean}{confidence_text}{reasoning_text}"


def _build_orchestrator_prompt(
    index_display_name: str,
    figures: dict[str, Any],
    technical_verdict: Optional[_AgentVerdict],
    news_verdict: Optional[_AgentVerdict],
    headlines_text: str = "",
) -> str:
    price = figures.get("price")
    change_abs = figures.get("change_abs")
    change_percent = figures.get("change_percent")
    day_low = figures.get("day_low")
    day_high = figures.get("day_high")
    base = (
        f"Index: {index_display_name}\n"
        f"Current price: {price}\n"
        f"Change vs previous close: "
        + (f"{change_abs:+.2f} ({change_percent:+.2f}%)" if change_abs is not None and change_percent is not None else "unavailable")
        + "\n"
        f"Today's range so far: "
        + (f"{day_low} - {day_high}" if day_low is not None and day_high is not None else "unavailable")
        + "\n\n"
        + _format_verdict("Technical & Flow Agent", technical_verdict)
        + "\n"
        + _format_verdict("News & Sentiment Agent", news_verdict)
        + "\n"
    )
    if headlines_text:
        base += f"\nRaw recent headlines, for your own context (most recent first):\n{headlines_text}\n"
    return base + "\nApply the evaluation rules above and respond with your decision."


def _build_exit_prompt(
    trade: StrategyTrade,
    now_ist: datetime,
    headlines_text: str = "",
    technical_verdict: Optional[_AgentVerdict] = None,
    news_verdict: Optional[_AgentVerdict] = None,
) -> str:
    entry_time_ist = to_ist(trade.entry_time) if trade.entry_time is not None else None
    elapsed_minutes = (now_ist - entry_time_ist).total_seconds() / 60 if entry_time_ist else None
    base = (
        f"Position: {trade.signal} on {trade.index_symbol}, strike {trade.strike} {trade.option_type}\n"
        f"Entry price: {trade.entry_price}\n"
        f"Current price: {trade.current_premium}\n"
        f"Running P&L: {trade.pnl_percent}%\n"
        f"Peak P&L (highest point this position has reached): {_peak_pnl_percent(trade)}%\n"
        f"Time open: {f'{elapsed_minutes:.0f} minutes' if elapsed_minutes is not None else 'unknown'}\n\n"
        + _format_verdict("Technical & Flow Agent", technical_verdict)
        + "\n"
        + _format_verdict("News & Sentiment Agent", news_verdict)
        + "\n"
    )
    if headlines_text:
        base += f"\nRaw recent headlines, for your own context (most recent first):\n{headlines_text}\n"
    return base + "\nApply the evaluation rules above and respond with your decision."


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
    forced_minimum_lot = False
    if lots <= 0:
        # See _MIN_LOTS_PAPER_FLOOR's own comment -- paper-only, so a
        # signal that cleared every other gate is never declined purely
        # on sizing arithmetic rounding to zero. The configured risk
        # budget is still the real number reported everywhere else; only
        # the quantity is floored, and that override is always flagged,
        # never silent.
        lots = _MIN_LOTS_PAPER_FLOOR
        forced_minimum_lot = True
        logger.info(
            "[AGENTIC_AI] %s: risk-based sizing rounded to 0 lots (risk budget %.2f%% of Rs %.2f, entry "
            "%.2f, stop %.2f, lot size %s) -- forcing the minimum %s lot(s) since this strategy is "
            "structurally paper-only",
            index.symbol, settings.max_risk_percent_per_trade, settings.account_capital_rupees,
            entry_price, stoploss, contract.lot_size, _MIN_LOTS_PAPER_FLOOR,
        )
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
        f"[{strategy_name}] opened {signal.value} @ strike {trade.strike}, {lots} lot(s)"
        + (" (forced to the paper-only minimum lot -- see reasoning)" if forced_minimum_lot else ""),
        payload={
            "trade_id": trade.trade_id, "reasoning": reasoning, "lots": lots,
            "forced_minimum_lot": forced_minimum_lot,
        },
    )
    logger.info(
        "[AGENTIC_AI] opened %s for %s (%s lot(s)%s)",
        signal.value, index.symbol, lots, ", forced minimum" if forced_minimum_lot else "",
    )
    return trade


def check_agentic_entry(
    db: Session,
    index: IndexConfig,
    now_ist: datetime,
    settings: AgenticAISettings,
    smartapi: Any,
    option_finder: Any,
    figures_by_symbol: dict[str, dict[str, Any]],
    is_severe: bool = False,
    headlines_text: str = "",
    technical_verdict: Optional[_AgentVerdict] = None,
    news_verdict: Optional[_AgentVerdict] = None,
) -> Optional[StrategyTrade]:
    """One index's entry evaluation for this cycle. Gated, in order, by: the
    news agent's severe-event kill switch (app.ai.news.classify_severe_
    event -- cheapest and most decisive, so checked first); an already-open
    AGENTIC_AI position on this specific index (cheap, no risk-gate query
    needed); the deterministic risk/sizing agent's check_risk_gates()
    (global across both indices -- see that function's own docstring); a
    live price actually being available; and a real Technical Agent
    verdict for this index (computed once per cycle in run_agentic_entry_
    exit_checks, shared with the exit side -- a missing/ERROR verdict here
    means insufficient candle history or a failed call, and this is the
    pipeline's primary required signal, so entry fails closed rather than
    asking the Orchestrator to decide on half a picture). Only once all
    five pass does this spend the Orchestrator's own LLM call.
    news_verdict is always a real verdict by the time this is called
    (get_news_verdict never returns None -- see its own docstring), so it
    is passed straight through to the Orchestrator's prompt regardless."""
    if is_severe:
        logger.info("[AGENTIC_AI] %s: Entry blocked -- severe-event news kill switch is active", index.symbol)
        return None

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

    if technical_verdict is None or technical_verdict.lean == "ERROR":
        logger.info(
            "[AGENTIC_AI] %s: Skipped, no usable Technical Agent verdict this cycle%s",
            index.symbol, f" ({technical_verdict.reasoning})" if technical_verdict is not None else "",
        )
        return None

    user_prompt = _build_orchestrator_prompt(
        index.display_name or index.symbol, figures, technical_verdict, news_verdict, headlines_text,
    )
    raw = _call_openai_live(settings, SYSTEM_PROMPT_ORCHESTRATOR, user_prompt)
    if raw.error:
        logger.error("[AGENTIC_AI] %s orchestrator call FAILED: %s", index.symbol, raw.error)
        log_event(db, "AGENTIC_AI", f"[{index.symbol}] orchestrator call failed: {raw.error}", level="ERROR")
        return None
    parsed = _parse_entry_response(raw.content)
    if parsed.action == "ERROR":
        logger.error("[AGENTIC_AI] %s orchestrator response unparseable: %s", index.symbol, parsed.reasoning)
        return None
    logger.info(
        "[AGENTIC_AI] %s -> %s (technical=%s, news=%s)",
        index.symbol, parsed.action, technical_verdict.lean, news_verdict.lean if news_verdict else "NEUTRAL",
    )
    if parsed.action == "NONE":
        log_event(
            db, "AGENTIC_AI", f"[{index.symbol}] declined ({parsed.reasoning})",
            payload={
                "confidence": parsed.confidence,
                "technical_lean": technical_verdict.lean,
                "news_lean": news_verdict.lean if news_verdict else "NEUTRAL",
            },
        )
        return None

    return open_agentic_trade(db, index, parsed.action, parsed.reasoning, settings, smartapi, option_finder)


def check_agentic_exits(
    db: Session,
    trade_manager: Any,
    settings: AgenticAISettings,
    end_hm: tuple[int, int],
    is_severe: bool = False,
    severe_matches: Optional[list[str]] = None,
    headlines_text: str = "",
    technical_verdicts_by_index: Optional[dict[str, _AgentVerdict]] = None,
    news_verdict: Optional[_AgentVerdict] = None,
) -> None:
    """Re-asks the live model, for every currently open AGENTIC_AI trade not
    already past the hard square-off time or caught by the news kill
    switch, whether to HOLD or EXIT -- and actually closes on EXIT via
    trade_manager.close_trade (the same helper monitor_open_trades' own
    backstop uses), so every exit path records a close identically. The
    backstop stop/target/time-exit this trade already carries
    (sl_mode=FIXED) keeps enforcing independently of whatever this function
    decides, every 30 seconds via the shared monitor -- this is a second,
    faster-reacting line of judgment on top of that, not a replacement for
    it.

    is_severe (app.ai.news.classify_severe_event) force-closes every open
    position via ExitReason.NEWS_KILL_SWITCH, checked before the hard
    square-off cutoff -- a genuine black-swan match is reported with its
    own distinct reason even when the cutoff would also apply, which is
    the more informative of the two in that rare overlap.

    technical_verdicts_by_index/news_verdict are the SAME verdicts already
    computed once this cycle for the entry side (see run_agentic_entry_
    exit_checks) -- reused here as supplementary context, never a second
    Technical/News call per open position. Unlike the entry side, a
    missing technical verdict here does NOT skip the exit call -- the
    backstop stop/target/time-exit already protect this trade regardless,
    so a thin technical read is shown as "unavailable" rather than
    withholding the model's own exit judgment entirely."""
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
    technical_verdicts_by_index = technical_verdicts_by_index or {}

    for trade in trades:
        try:
            if trade.current_premium is None:
                continue

            if is_severe:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.NEWS_KILL_SWITCH)
                log_event(
                    db, "AGENTIC_AI",
                    f"[{trade.strategy_name}] closed by the news kill switch: {severe_matches}",
                    level="WARNING",
                    payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent, "matched_headlines": severe_matches},
                )
                logger.warning(
                    "[AGENTIC_AI] %s closed by news kill switch (%.2f%%): %s",
                    trade.trade_id, trade.pnl_percent or 0.0, severe_matches,
                )
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

            user_prompt = _build_exit_prompt(
                trade, now_ist, headlines_text,
                technical_verdicts_by_index.get(trade.index_symbol), news_verdict,
            )
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

        # News fetched once per cycle, shared by every index's entry check
        # and by the exit check -- not once per index. fetch_market_
        # headlines() is fail-soft (a total feed outage just means an empty
        # list), so a news-fetch problem degrades this cycle's context,
        # never blocks trading.
        headlines = fetch_market_headlines()
        is_severe, severe_matches = classify_severe_event(headlines)
        headlines_text = format_headlines_for_prompt(headlines)
        if is_severe:
            logger.warning("[AGENTIC_AI] Severe-event news kill switch triggered: %s", severe_matches)
            log_event(
                session, "AGENTIC_AI", f"Severe-event news kill switch triggered: {severe_matches}",
                level="WARNING", payload={"matched_headlines": severe_matches},
            )

        # True multi-agent fusion (25 Oct 2026): the News Agent's own
        # verdict (ONE shared call, market-wide, not per index) and every
        # enabled index's Technical Agent verdict (one call per index,
        # unconditionally -- see the module docstring) are both computed
        # ONCE here, before either exits or entries run, so the exit side
        # and the entry side read the exact same picture this cycle rather
        # than two independent, possibly-divergent reads of the same
        # moment. Skipped entirely when severe -- every open position is
        # being force-closed and no new entry can happen this cycle
        # regardless, so neither verdict would ever be read.
        news_verdict: Optional[_AgentVerdict] = None
        technical_verdicts_by_index: dict[str, _AgentVerdict] = {}
        if not is_severe:
            news_verdict = get_news_verdict(settings, headlines_text)
            for index in indexes:
                figures = figures_by_symbol.get(index.symbol)
                if not figures or figures.get("price") is None:
                    continue
                features = _compute_technical_features(session, index, figures["price"], now_ist, smartapi)
                if features is None:
                    logger.info("[AGENTIC_AI][TECH] %s: insufficient candle history this cycle", index.symbol)
                    continue
                technical_verdicts_by_index[index.symbol] = get_technical_verdict(
                    settings, index.display_name or index.symbol, features,
                )

        platform_settings = get_or_create_settings(session)
        end_hm = parse_hhmm(platform_settings.square_off_time, _DEFAULT_TRADING_END)

        # Exits first -- closing a stale position frees its slot for a
        # fresh entry the same cycle, same ordering app.ai.autonomous
        # already uses for the same reason.
        check_agentic_exits(
            session, trade_manager, settings, end_hm, is_severe, severe_matches, headlines_text,
            technical_verdicts_by_index, news_verdict,
        )

        start_hm = parse_hhmm(platform_settings.trading_start_time, _DEFAULT_TRADING_START)
        if (now_ist.hour, now_ist.minute) < start_hm or (now_ist.hour, now_ist.minute) >= end_hm:
            return

        for index in indexes:
            try:
                check_agentic_entry(
                    session, index, now_ist, settings, smartapi, option_finder, figures_by_symbol,
                    is_severe, headlines_text,
                    technical_verdicts_by_index.get(index.symbol), news_verdict,
                )
            except Exception:
                logger.exception("[AGENTIC_AI] entry check failed for %s", index.symbol)
    finally:
        if owns_session:
            session.close()
