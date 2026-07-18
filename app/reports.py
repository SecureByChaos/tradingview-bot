from __future__ import annotations

import calendar
import json
import logging
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.ai.client import AIClient
from app.ai.repository import get_settings as get_ai_settings
from app.database import SessionLocal
from app.db_models import AIReport, AISettings, AITradeReview, ReportType, StrategyTrade, TradeResult, TradeStatus
from app.platform import log_event, today_ist
from app.time_utils import to_ist

logger = logging.getLogger(__name__)

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MIN_TIME_PATTERN_SAMPLE = 3


# ---------------------------------------------------------------------------
# Trading-day helpers
# ---------------------------------------------------------------------------

def is_last_trading_day_of_month(day: date) -> bool:
    """Approximates 'last market working day' as the last weekday (Mon-Fri) of the month.

    There is no exchange holiday calendar wired into this bot, so weekends are the only
    non-trading days accounted for here.
    """
    last_day_num = calendar.monthrange(day.year, day.month)[1]
    cursor = date(day.year, day.month, last_day_num)
    while cursor.weekday() >= 5:  # Saturday=5, Sunday=6
        cursor -= timedelta(days=1)
    return cursor == day


def _week_bounds(reference: date) -> tuple[date, date]:
    start = reference - timedelta(days=reference.weekday())
    end = start + timedelta(days=4)
    return start, end


def _month_bounds(reference: date) -> tuple[date, date]:
    start = reference.replace(day=1)
    last_day_num = calendar.monthrange(reference.year, reference.month)[1]
    end = reference.replace(day=last_day_num)
    return start, end


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def _closed_trades_between(db: Session, start: date, end: date) -> list[StrategyTrade]:
    # origin == "SIGNAL" only: periodic AI performance reports should summarize
    # real trading only, not AI_ALT_* evaluation trades.
    return list(
        db.scalars(
            select(StrategyTrade)
            .where(
                StrategyTrade.status == TradeStatus.CLOSED,
                func.date(StrategyTrade.exit_time) >= start.isoformat(),
                func.date(StrategyTrade.exit_time) <= end.isoformat(),
                StrategyTrade.origin == "SIGNAL",
            )
            .order_by(StrategyTrade.exit_time)
        )
    )


def _reviews_with_outcome_between(db: Session, start: date, end: date) -> list[AITradeReview]:
    return list(
        db.scalars(
            select(AITradeReview)
            .where(
                AITradeReview.actual_result.is_not(None),
                func.date(AITradeReview.created_at) >= start.isoformat(),
                func.date(AITradeReview.created_at) <= end.isoformat(),
            )
            .order_by(AITradeReview.created_at)
        )
    )


def reports_query_for_filter(report_type: str = "") -> Select[tuple[AIReport]]:
    query = select(AIReport).order_by(AIReport.generated_at.desc())
    if report_type:
        query = query.where(AIReport.report_type == report_type)
    return query


def list_reports(db: Session, report_type: str = "", limit: int = 50) -> list[AIReport]:
    query = reports_query_for_filter(report_type)
    if limit:
        query = query.limit(limit)
    return list(db.scalars(query))


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def _trade_stats(trades: list[StrategyTrade]) -> dict[str, Any]:
    total = len(trades)
    wins = sum(1 for t in trades if t.result == TradeResult.WIN)
    losses = sum(1 for t in trades if t.result == TradeResult.LOSS)
    breakeven = sum(1 for t in trades if t.result == TradeResult.BREAKEVEN)
    net_pnl = round(sum(t.profit_loss for t in trades), 2)
    avg_pnl_percent = round(sum(t.pnl_percent for t in trades) / total, 2) if total else 0.0

    by_strategy: dict[str, dict[str, Any]] = {}
    by_option_type: dict[str, dict[str, Any]] = {}
    by_exit_reason: dict[str, int] = {}
    holding_minutes: list[int] = []

    for trade in trades:
        strategy_bucket = by_strategy.setdefault(
            trade.strategy_name, {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        )
        strategy_bucket["trades"] += 1
        if trade.result == TradeResult.WIN:
            strategy_bucket["wins"] += 1
        elif trade.result == TradeResult.LOSS:
            strategy_bucket["losses"] += 1
        strategy_bucket["net_pnl"] = round(strategy_bucket["net_pnl"] + trade.profit_loss, 2)

        option_bucket = by_option_type.setdefault(
            trade.option_type or "UNKNOWN", {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        )
        option_bucket["trades"] += 1
        if trade.result == TradeResult.WIN:
            option_bucket["wins"] += 1
        elif trade.result == TradeResult.LOSS:
            option_bucket["losses"] += 1
        option_bucket["net_pnl"] = round(option_bucket["net_pnl"] + trade.profit_loss, 2)

        reason = trade.exit_reason or "UNKNOWN"
        by_exit_reason[reason] = by_exit_reason.get(reason, 0) + 1

        entry_ist = to_ist(trade.entry_time)
        exit_ist = to_ist(trade.exit_time)
        if entry_ist is not None and exit_ist is not None:
            holding_minutes.append(max(int((exit_ist - entry_ist).total_seconds() // 60), 0))

    for bucket_group in (by_strategy, by_option_type):
        for bucket in bucket_group.values():
            bucket["win_rate"] = round((bucket["wins"] / bucket["trades"]) * 100, 2) if bucket["trades"] else 0.0

    best_strategy = max(by_strategy.items(), key=lambda kv: kv[1]["net_pnl"])[0] if by_strategy else None
    worst_strategy = min(by_strategy.items(), key=lambda kv: kv[1]["net_pnl"])[0] if by_strategy else None

    max_consecutive_losses = 0
    streak = 0
    for trade in trades:  # trades are ordered chronologically by exit_time
        if trade.result == TradeResult.LOSS:
            streak += 1
            max_consecutive_losses = max(max_consecutive_losses, streak)
        else:
            streak = 0

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": round((wins / total) * 100, 2) if total else 0.0,
        "net_pnl": net_pnl,
        "avg_pnl_percent": avg_pnl_percent,
        "avg_holding_minutes": round(sum(holding_minutes) / len(holding_minutes), 1) if holding_minutes else 0.0,
        "by_strategy": by_strategy,
        "by_option_type": by_option_type,
        "by_exit_reason": by_exit_reason,
        "best_strategy": best_strategy,
        "worst_strategy": worst_strategy,
        "max_consecutive_losses": max_consecutive_losses,
    }


def _confidence_percent(value: float) -> float:
    """AITradeReview.confidence has historically been stored either as a 0-1 fraction
    or a 0-100 percentage depending on provider path; normalize to a 0-100 scale."""
    return value * 100 if value <= 1 else value


def _ai_correlation_stats(reviews: list[AITradeReview]) -> dict[str, Any]:
    total = len(reviews)
    graded = [r for r in reviews if r.ai_correct is not None]
    agreement_rate = (
        round((sum(1 for r in graded if r.ai_correct) / len(graded)) * 100, 2) if graded else None
    )

    by_decision: dict[str, dict[str, Any]] = {}
    for review in reviews:
        bucket = by_decision.setdefault(review.decision, {"count": 0, "wins": 0})
        bucket["count"] += 1
        if review.actual_result == TradeResult.WIN:
            bucket["wins"] += 1
    for bucket in by_decision.values():
        bucket["win_rate"] = round((bucket["wins"] / bucket["count"]) * 100, 2) if bucket["count"] else 0.0

    confidence_buckets = [(0, 50), (50, 70), (70, 90), (90, 101)]
    by_confidence: dict[str, dict[str, Any]] = {}
    for low, high in confidence_buckets:
        label = f"{low}-{min(high, 100)}%"
        bucket_reviews = [r for r in reviews if low <= _confidence_percent(r.confidence) < high]
        count = len(bucket_reviews)
        wins = sum(1 for r in bucket_reviews if r.actual_result == TradeResult.WIN)
        graded_bucket = [r for r in bucket_reviews if r.ai_correct is not None]
        by_confidence[label] = {
            "count": count,
            "win_rate": round((wins / count) * 100, 2) if count else 0.0,
            "ai_accuracy": (
                round((sum(1 for r in graded_bucket if r.ai_correct) / len(graded_bucket)) * 100, 2)
                if graded_bucket
                else None
            ),
        }

    by_strategy: dict[str, dict[str, Any]] = {}
    for review in reviews:
        bucket = by_strategy.setdefault(review.strategy, {"count": 0, "correct": 0, "graded": 0})
        bucket["count"] += 1
        if review.ai_correct is not None:
            bucket["graded"] += 1
            if review.ai_correct:
                bucket["correct"] += 1
    for bucket in by_strategy.values():
        bucket["accuracy"] = round((bucket["correct"] / bucket["graded"]) * 100, 2) if bucket["graded"] else None

    return {
        "total_reviews_with_outcome": total,
        "ai_agreement_rate": agreement_rate,
        "by_decision": by_decision,
        "by_confidence_bucket": by_confidence,
        "by_strategy": by_strategy,
    }


def _time_pattern_stats(trades: list[StrategyTrade]) -> dict[str, Any]:
    by_hour: dict[int, dict[str, Any]] = {}
    by_weekday: dict[str, dict[str, Any]] = {}
    for trade in trades:
        entry_ist = to_ist(trade.entry_time)
        if entry_ist is None:
            continue
        hour_bucket = by_hour.setdefault(entry_ist.hour, {"trades": 0, "wins": 0})
        hour_bucket["trades"] += 1
        if trade.result == TradeResult.WIN:
            hour_bucket["wins"] += 1

        weekday_name = _WEEKDAY_NAMES[entry_ist.weekday()]
        weekday_bucket = by_weekday.setdefault(weekday_name, {"trades": 0, "wins": 0})
        weekday_bucket["trades"] += 1
        if trade.result == TradeResult.WIN:
            weekday_bucket["wins"] += 1

    for bucket_group in (by_hour, by_weekday):
        for bucket in bucket_group.values():
            bucket["win_rate"] = round((bucket["wins"] / bucket["trades"]) * 100, 2) if bucket["trades"] else 0.0

    by_hour_labeled = {f"{hour:02d}:00": data for hour, data in sorted(by_hour.items())}

    eligible_hours = {k: v for k, v in by_hour_labeled.items() if v["trades"] >= _MIN_TIME_PATTERN_SAMPLE}
    eligible_weekdays = {k: v for k, v in by_weekday.items() if v["trades"] >= _MIN_TIME_PATTERN_SAMPLE}
    best_hour = max(eligible_hours.items(), key=lambda kv: kv[1]["win_rate"])[0] if eligible_hours else None
    worst_hour = min(eligible_hours.items(), key=lambda kv: kv[1]["win_rate"])[0] if eligible_hours else None
    best_weekday = max(eligible_weekdays.items(), key=lambda kv: kv[1]["win_rate"])[0] if eligible_weekdays else None
    worst_weekday = min(eligible_weekdays.items(), key=lambda kv: kv[1]["win_rate"])[0] if eligible_weekdays else None

    return {
        "by_hour": by_hour_labeled,
        "by_weekday": by_weekday,
        "best_hour": best_hour,
        "worst_hour": worst_hour,
        "best_weekday": best_weekday,
        "worst_weekday": worst_weekday,
        "min_sample_size": _MIN_TIME_PATTERN_SAMPLE,
    }


# ---------------------------------------------------------------------------
# Narrative generation
# ---------------------------------------------------------------------------

def _call_openai_narrative(settings: AISettings, user_prompt: str) -> tuple[Optional[str], Optional[str]]:
    if not settings.api_key or not settings.model:
        return None, "OpenAI API key or model is not configured."
    endpoint = (settings.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    system_prompt = settings.system_prompt.strip() or (
        "You are a trading performance analyst. Write concise, factual summaries strictly from the "
        "statistics provided. Never invent numbers that are not present in the data."
    )
    client = AIClient()
    response = client.send(
        endpoint=endpoint,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        payload={
            "model": settings.model,
            "temperature": settings.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=settings.timeout_seconds,
    )
    if response.error:
        return None, response.error
    try:
        content = response.response_body["choices"][0]["message"]["content"]
        return str(content).strip(), None
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"Unexpected OpenAI response shape: {exc}"


def _template_narrative(report_label: str, period_label: str, stats: dict[str, Any]) -> str:
    if "trade_stats" in stats:
        return _template_pattern_narrative(period_label, stats)
    lines = [f"{report_label.title()} for {period_label}."]
    total = stats.get("total_trades", 0)
    if not total:
        lines.append("No closed trades were recorded in this period.")
        return "\n".join(lines)
    lines.append(
        f"Total trades: {total}. Wins: {stats.get('wins', 0)}. Losses: {stats.get('losses', 0)}. "
        f"Win rate: {stats.get('win_rate', 0)}%. Net P&L: {stats.get('net_pnl', 0)}."
    )
    by_strategy = stats.get("by_strategy", {})
    if stats.get("best_strategy"):
        best = by_strategy.get(stats["best_strategy"], {})
        lines.append(
            f"Best performing strategy: {stats['best_strategy']} "
            f"(net P&L {best.get('net_pnl', 0)}, win rate {best.get('win_rate', 0)}%)."
        )
    if stats.get("worst_strategy") and stats.get("worst_strategy") != stats.get("best_strategy"):
        worst = by_strategy.get(stats["worst_strategy"], {})
        lines.append(
            f"Worst performing strategy: {stats['worst_strategy']} "
            f"(net P&L {worst.get('net_pnl', 0)}, win rate {worst.get('win_rate', 0)}%)."
        )
    if stats.get("max_consecutive_losses", 0) >= 2:
        lines.append(f"Longest losing streak in the period: {stats['max_consecutive_losses']} trades.")
    lines.append("(Generated from raw statistics; configure an AI provider in AI Settings for a narrative summary.)")
    return "\n".join(lines)


def _template_pattern_narrative(period_label: str, stats: dict[str, Any]) -> str:
    trade_stats = stats.get("trade_stats", {})
    correlation = stats.get("ai_correlation", {})
    time_patterns = stats.get("time_patterns", {})
    lines = [f"Pattern Discovery for {period_label}."]
    if not trade_stats.get("total_trades"):
        lines.append("No closed trades were recorded in this period.")
        return "\n".join(lines)
    lines.append(
        f"Analyzed {trade_stats.get('total_trades', 0)} closed trades "
        f"(win rate {trade_stats.get('win_rate', 0)}%, net P&L {trade_stats.get('net_pnl', 0)})."
    )
    if correlation.get("ai_agreement_rate") is not None:
        lines.append(
            f"AI review agreement with actual outcomes: {correlation['ai_agreement_rate']}% "
            f"across {correlation.get('total_reviews_with_outcome', 0)} graded reviews."
        )
    if time_patterns.get("best_hour"):
        lines.append(f"Best-performing entry hour: {time_patterns['best_hour']} IST.")
    if time_patterns.get("worst_hour"):
        lines.append(f"Weakest entry hour: {time_patterns['worst_hour']} IST.")
    if time_patterns.get("best_weekday"):
        lines.append(f"Best-performing weekday: {time_patterns['best_weekday']}.")
    if time_patterns.get("worst_weekday"):
        lines.append(f"Weakest weekday: {time_patterns['worst_weekday']}.")
    lines.append("(Generated from raw statistics; configure an AI provider in AI Settings for a narrative summary.)")
    return "\n".join(lines)


def _generate_narrative(db: Session, report_label: str, period_label: str, stats: dict[str, Any]) -> tuple[str, str, str]:
    settings = get_ai_settings(db)
    if settings is not None and settings.enabled and settings.provider == "openai" and settings.api_key and settings.model:
        user_prompt = (
            f"Report type: {report_label}\nPeriod: {period_label}\n\n"
            "Statistics (JSON):\n" + json.dumps(stats, default=str, indent=2) +
            "\n\nWrite a concise plain-text summary (4-8 sentences) of trading performance for this period, "
            "based strictly on the statistics above. Do not invent any numbers not present in the data. "
            "Call out the best and worst performing strategies, notable win-rate patterns, and any risk "
            "concerns such as loss streaks or a low win rate. If a data point is missing or zero, do not "
            "speculate about the reason."
        )
        content, error = _call_openai_narrative(settings, user_prompt)
        if content:
            return content, "openai", settings.model
        logger.info("[Reports] OpenAI narrative generation failed, using template fallback: %s", error)
    return _template_narrative(report_label, period_label, stats), "dummy", ""


# ---------------------------------------------------------------------------
# Report generators
# ---------------------------------------------------------------------------

def _save_report(
    db: Session,
    report_type: str,
    period_start: date,
    period_end: date,
    title: str,
    summary_text: str,
    stats: dict[str, Any],
    provider: str,
    model: str,
) -> AIReport:
    report = AIReport(
        report_type=report_type,
        period_start=period_start,
        period_end=period_end,
        title=title,
        summary_text=summary_text,
        stats_json=json.dumps(stats, default=str),
        provider=provider,
        model=model or "",
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    log_event(db, "REPORT", f"{title} generated", "INFO", {"report_id": report.id, "report_type": report_type})
    return report


def generate_daily_summary(db: Session, report_date: date | None = None) -> AIReport:
    day = report_date or today_ist()
    trades = _closed_trades_between(db, day, day)
    stats = _trade_stats(trades)
    period_label = day.strftime("%d %b %Y")
    summary_text, provider, model = _generate_narrative(db, "daily summary", period_label, stats)
    return _save_report(db, ReportType.DAILY, day, day, f"Daily Summary - {period_label}", summary_text, stats, provider, model)


def generate_weekly_report(db: Session, reference: date | None = None) -> AIReport:
    ref = reference or today_ist()
    start, end = _week_bounds(ref)
    trades = _closed_trades_between(db, start, end)
    stats = _trade_stats(trades)
    period_label = f"{start.strftime('%d %b %Y')} - {end.strftime('%d %b %Y')}"
    summary_text, provider, model = _generate_narrative(db, "weekly report", period_label, stats)
    return _save_report(db, ReportType.WEEKLY, start, end, f"Weekly Report - {period_label}", summary_text, stats, provider, model)


def generate_monthly_report(db: Session, reference: date | None = None) -> AIReport:
    ref = reference or today_ist()
    start, end = _month_bounds(ref)
    trades = _closed_trades_between(db, start, end)
    stats = _trade_stats(trades)
    period_label = ref.strftime("%B %Y")
    summary_text, provider, model = _generate_narrative(db, "monthly report", period_label, stats)
    return _save_report(db, ReportType.MONTHLY, start, end, f"Monthly Report - {period_label}", summary_text, stats, provider, model)


def generate_pattern_discovery(db: Session, lookback_days: int | None = 90) -> AIReport:
    today = today_ist()
    start = today - timedelta(days=lookback_days - 1) if lookback_days else date(2000, 1, 1)
    trades = _closed_trades_between(db, start, today)
    reviews = _reviews_with_outcome_between(db, start, today)
    stats = {
        "lookback_days": lookback_days,
        "trade_stats": _trade_stats(trades),
        "ai_correlation": _ai_correlation_stats(reviews),
        "time_patterns": _time_pattern_stats(trades),
    }
    period_label = (
        f"{start.strftime('%d %b %Y')} - {today.strftime('%d %b %Y')}"
        if lookback_days
        else f"All-time through {today.strftime('%d %b %Y')}"
    )
    summary_text, provider, model = _generate_narrative(db, "pattern discovery", period_label, stats)
    return _save_report(db, ReportType.PATTERN, start, today, f"Pattern Discovery - {period_label}", summary_text, stats, provider, model)


# ---------------------------------------------------------------------------
# Scheduler job wrappers (each opens its own DB session)
# ---------------------------------------------------------------------------

def run_daily_summary_job() -> None:
    with SessionLocal() as db:
        try:
            generate_daily_summary(db)
        except Exception:
            logger.exception("[Reports] Daily summary generation failed")


def run_weekly_report_job() -> None:
    with SessionLocal() as db:
        try:
            generate_weekly_report(db)
        except Exception:
            logger.exception("[Reports] Weekly report generation failed")


def run_monthly_report_job() -> None:
    today = today_ist()
    if not is_last_trading_day_of_month(today):
        return
    with SessionLocal() as db:
        try:
            generate_monthly_report(db, today)
        except Exception:
            logger.exception("[Reports] Monthly report generation failed")
