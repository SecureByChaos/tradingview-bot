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

Future PRs (not this one): a live_model-backed entry agent, an exit agent,
a risk/sizing agent (mostly deterministic, LLM only where real judgement is
needed -- see CLAUDE.md's 25 Oct 2026 discussion), and a news-reading agent.
Each should land as its own PR, same incremental discipline every other
strategy in this project has followed.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import AIClient
from app.db_models import AgenticAISettings, AIReport, ReportType, StrategyTrade, TradeStatus
from app.platform import log_event, today_ist

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


def compute_origin_stats(db: Session, origin_pattern: str) -> dict[str, Any]:
    """Win rate / P&L / exit-reason breakdown for every CLOSED trade matching
    origin_pattern, plus a bootstrap 90% CI on mean net P&L and an explicit
    below_trust_minimum flag -- never silently presented as reliable below
    MIN_BUCKET_LIVE observations. origin_pattern is a SQL LIKE pattern (a
    plain string with no % degrades to an exact match, same convention
    pull_option_candles.py's --origin already established), so this works
    unmodified against "AGENTIC_AI", "AUTONOMOUS_AI", "AI_ORIGIN_%", etc.
    """
    trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.origin.like(origin_pattern),
                StrategyTrade.status == TradeStatus.CLOSED,
            )
        )
    )
    n = len(trades)
    if n == 0:
        return {
            "origin_pattern": origin_pattern,
            "n": 0,
            "note": "No closed trades matching this origin pattern.",
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
