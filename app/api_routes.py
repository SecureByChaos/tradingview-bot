from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_admin_api
from app.ai.context_repository import get_latest_context_log
from app.database import get_db
from app.db_models import BotStatus, LogEvent, StrategyConfig, StrategyTrade, TradeStatus
from app.platform import (
    ai_reviews_query_for_filter,
    api_status,
    daily_stats_query_for_filter,
    get_or_create_strategy_stats,
    get_or_create_settings,
    get_or_create_state,
    list_index_configs,
    log_event,
    rebuild_daily_stats,
    set_bot_state,
    serialize_strategy_trade,
    sync_trade_row,
    strategy_metrics,
    strategy_trades_query_for_filter,
)
from app.reports import reports_query_for_filter
from sqlalchemy import select
from app.telegram_service import TelegramService
from app.time_utils import format_ist
from app.trade_manager import TradeManager

router = APIRouter(prefix="/api")


class SettingsPayload(BaseModel):
    square_off_time: str
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def manager() -> TradeManager:
    return router.trade_manager  # type: ignore[attr-defined]


def multi_manager():
    return router.multi_strategy_manager  # type: ignore[attr-defined]


def v7_manager():
    return router.v7_manager  # type: ignore[attr-defined]


def telegram() -> TelegramService:
    return router.telegram  # type: ignore[attr-defined]


@router.get("/status")
def status(
    db: Annotated[Session, Depends(get_db)],
    trade_manager: Annotated[TradeManager, Depends(manager)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, Any]:
    payload = api_status(db, trade_manager.get_active_trade())
    payload["strategies"] = [
        {
            "name": item["strategy"].name,
            "mode": item["strategy"].mode,
            "open_trades": item["open_trades"],
            "total_trades": item["total_trades"],
            "wins": item["wins"],
            "losses": item["losses"],
            "win_rate": item["win_rate"],
            "net_pnl": item["net_pnl"],
            "consecutive_losses": item["stats"].consecutive_losses,
            "risk_locked": item["stats"].risk_locked,
            "enabled": item["strategy"].enabled,
        }
        for item in strategy_metrics(db)
    ]
    return payload


@router.get("/active-trade")
def active_trade(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> Any:
    trades = db.scalars(select(StrategyTrade).where(StrategyTrade.status == TradeStatus.OPEN).order_by(StrategyTrade.entry_time.desc()))
    return [serialize_strategy_trade(trade) for trade in trades]


@router.get("/trades")
def trades(
    db: Annotated[Session, Depends(get_db)],
    filter: str = "30d",
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> list[dict[str, Any]]:
    records = db.scalars(strategy_trades_query_for_filter(filter, None, None))
    return [serialize_strategy_trade(record) for record in records]


@router.get("/trades/export")
def trades_export(
    db: Annotated[Session, Depends(get_db)],
    filter: str = "30d",
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> StreamingResponse:
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "Trade ID",
            "Strategy",
            "Index",
            "Signal",
            "Date (IST)",
            "Entry Time (IST)",
            "Exit Time (IST)",
            "Duration",
            "Entry",
            "Exit",
            "P&L",
            "P&L %",
            "Result",
            "Status",
            "Mode",
        ],
    )
    writer.writeheader()
    for trade in db.scalars(strategy_trades_query_for_filter(filter, None, None)):
        row = serialize_strategy_trade(trade)
        writer.writerow(
            {
                "Trade ID": row["trade_id"],
                "Strategy": row["strategy_name"],
                "Index": row["index_symbol"],
                "Signal": row["signal"],
                "Date (IST)": row["entry_time_ist"].split(" ", 1)[0] if row["entry_time_ist"] else "",
                "Entry Time (IST)": row["entry_time_ist"],
                "Exit Time (IST)": row["exit_time_ist"],
                "Duration": row["duration"],
                "Entry": row["entry_price"],
                "Exit": row["exit_price"],
                "P&L": row["profit_loss"],
                "P&L %": row["pnl_percent"],
                "Result": row["result"],
                "Status": row["status"],
                "Mode": row["mode"],
            }
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=strategy_trades_ist.csv"},
    )


@router.get("/logs/export")
def logs_export(
    db: Annotated[Session, Depends(get_db)],
    limit: int = 1000,
    event_type: str = "",
    level: str = "",
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> StreamingResponse:
    query = select(LogEvent).order_by(LogEvent.created_at.desc())
    if event_type:
        query = query.where(LogEvent.event_type == event_type)
    if level:
        query = query.where(LogEvent.level == level)
    if limit:
        query = query.limit(limit)
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["Time (IST)", "Type", "Level", "Message", "Payload"])
    writer.writeheader()
    for log in db.scalars(query):
        writer.writerow(
            {
                "Time (IST)": format_ist(log.created_at),
                "Type": log.event_type,
                "Level": log.level,
                "Message": log.message,
                "Payload": log.payload,
            }
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=logs_ist.csv"},
    )


@router.get("/ai-reviews/export")
def ai_reviews_export(
    db: Annotated[Session, Depends(get_db)],
    review_date: str = "",
    strategy: str = "",
    provider: str = "",
    decision: str = "",
    trade_result: str = "",
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> StreamingResponse:
    query = ai_reviews_query_for_filter(review_date, strategy, provider, decision, trade_result)
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "Time (IST)",
            "Strategy",
            "Signal",
            "Review Type",
            "Provider",
            "Model",
            "Decision",
            "Confidence",
            "Trade Result",
            "Actual P&L",
            "AI Correct",
            "Summary",
        ],
    )
    writer.writeheader()
    for review in db.scalars(query):
        writer.writerow(
            {
                "Time (IST)": format_ist(review.created_at),
                "Strategy": review.strategy,
                "Signal": review.signal,
                "Review Type": "ENTRY" if review.signal.startswith("BUY") else "EXIT",
                "Provider": review.provider,
                "Model": review.model,
                "Decision": review.decision,
                "Confidence": review.confidence,
                "Trade Result": review.actual_result or "",
                "Actual P&L": review.actual_pnl if review.actual_pnl is not None else "",
                "AI Correct": "" if review.ai_correct is None else ("YES" if review.ai_correct else "NO"),
                "Summary": review.summary,
            }
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ai_reviews_ist.csv"},
    )


@router.get("/daily-stats/export")
def daily_stats_export(
    db: Annotated[Session, Depends(get_db)],
    filter: str = "30d",
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> StreamingResponse:
    query = daily_stats_query_for_filter(filter, None, None)
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["Date", "Trade Count", "Wins", "Losses", "P&L %", "Consecutive Losses", "Risk Locked"],
    )
    writer.writeheader()
    for stats in db.scalars(query):
        writer.writerow(
            {
                "Date": stats.trade_date.isoformat(),
                "Trade Count": stats.trade_count,
                "Wins": stats.wins,
                "Losses": stats.losses,
                "P&L %": stats.pnl_percent,
                "Consecutive Losses": stats.consecutive_losses,
                "Risk Locked": "YES" if stats.risk_locked else "NO",
            }
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=daily_stats.csv"},
    )


@router.get("/reports/export")
def reports_export(
    db: Annotated[Session, Depends(get_db)],
    report_type: str = "",
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> StreamingResponse:
    query = reports_query_for_filter(report_type)
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["Type", "Title", "Period Start", "Period End", "Generated At (IST)", "Provider", "Model", "Summary"],
    )
    writer.writeheader()
    for report in db.scalars(query):
        writer.writerow(
            {
                "Type": report.report_type,
                "Title": report.title,
                "Period Start": report.period_start.isoformat(),
                "Period End": report.period_end.isoformat(),
                "Generated At (IST)": format_ist(report.generated_at),
                "Provider": report.provider,
                "Model": report.model,
                "Summary": report.summary_text,
            }
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ai_reports.csv"},
    )


@router.get("/strategies")
def strategies(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> list[dict[str, Any]]:
    payload = []
    for strategy in db.scalars(select(StrategyConfig).order_by(StrategyConfig.name)):
        stats = get_or_create_strategy_stats(db, strategy.name)
        payload.append({
            "id": strategy.id,
            "name": strategy.name,
            "enabled": strategy.enabled,
            "mode": strategy.mode,
            "index_symbol": strategy.index_symbol,
            "tp_percent": strategy.tp_percent,
            "sl_percent": strategy.sl_percent,
            "sl_mode": strategy.sl_mode,
            "trailing_activation_percent": strategy.trailing_activation_percent,
            "trailing_offset_percent": strategy.trailing_offset_percent,
            "max_active_trades": strategy.max_active_trades,
            "max_trades_per_day": strategy.max_trades_per_day,
            "max_consecutive_losses": strategy.max_consecutive_losses,
            "daily_max_loss_percent": strategy.daily_max_loss_percent,
            "capital_per_trade": strategy.capital_per_trade,
            "paper_trade": strategy.paper_trade,
            "live_trade": strategy.live_trade,
            "consecutive_losses": stats.consecutive_losses,
            "risk_locked": stats.risk_locked,
        })
    return payload


@router.get("/instruments")
def instruments(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> list[dict[str, Any]]:
    return [
        {
            "id": index.id,
            "symbol": index.symbol,
            "display_name": index.display_name,
            "exchange_segment": index.exchange_segment,
            "instrument_name": index.instrument_name,
            "spot_exchange": index.spot_exchange,
            "spot_symbol": index.spot_symbol,
            "spot_token": index.spot_token,
            "lot_size": index.lot_size,
            "strike_interval": index.strike_interval,
            "enabled": index.enabled,
        }
        for index in list_index_configs(db)
    ]


@router.get("/strategy-stats")
def strategy_stats(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> list[dict[str, Any]]:
    return [
        {
            "strategy_name": item["strategy"].name,
            "consecutive_losses": item["stats"].consecutive_losses,
            "risk_locked": item["stats"].risk_locked,
        }
        for item in strategy_metrics(db)
    ]


@router.get("/ai/latest-context")
def latest_ai_context(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, Any]:
    row = get_latest_context_log(db)
    if row is None:
        return {}
    return {
        "id": row.id,
        "timestamp": row.timestamp,
        "strategy": row.strategy,
        "signal": row.signal,
        "event_type": row.event_type,
        "paper_live": row.paper_live,
        "trade_id": row.trade_id,
        "trade_number": row.trade_number,
        "session": row.session,
        "context_json": _safe_json(row.context_json),
        "request_json": _safe_json(row.request_json),
        "payload_size": row.payload_size,
        "context_version": row.context_version,
        "prompt_version": row.prompt_version,
        "model": row.model,
        "completeness_percent": row.completeness_percent,
        "missing_fields": _safe_json(row.missing_fields),
        "latency_ms": row.latency_ms,
        "decision": row.decision,
        "confidence": row.confidence,
        "reason_to_buy": _safe_json(row.reason_to_buy),
        "reason_not_to_buy": _safe_json(row.reason_not_to_buy),
        "summary": row.summary,
        "created_at": row.created_at,
    }


@router.get("/settings")
def get_settings_api(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, Any]:
    settings = get_or_create_settings(db)
    return {
        column: getattr(settings, column)
        for column in [
            "square_off_time",
            "telegram_bot_token",
            "telegram_chat_id",
        ]
    }


@router.post("/settings")
def update_settings_api(
    payload: SettingsPayload,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, str]:
    settings = get_or_create_settings(db)
    for key, value in payload.model_dump().items():
        setattr(settings, key, value)
    db.commit()
    log_event(db, "BOT", "Settings updated from API")
    return {"status": "ok"}


@router.post("/start")
def start_bot(
    db: Annotated[Session, Depends(get_db)],
    notifier: Annotated[TelegramService, Depends(telegram)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, str]:
    set_bot_state(db, BotStatus.RUNNING, trading_allowed=True, risk_locked=False)
    log_event(db, "BOT", "Bot started")
    notifier.send(db, "Bot Started")
    return {"status": BotStatus.RUNNING}


@router.post("/stop")
def stop_bot(
    db: Annotated[Session, Depends(get_db)],
    notifier: Annotated[TelegramService, Depends(telegram)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, str]:
    set_bot_state(db, BotStatus.STOPPED, trading_allowed=False)
    log_event(db, "BOT", "Bot stopped")
    notifier.send(db, "Bot Stopped")
    return {"status": BotStatus.STOPPED}


@router.post("/restart")
def restart_bot(
    db: Annotated[Session, Depends(get_db)],
    notifier: Annotated[TelegramService, Depends(telegram)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, str]:
    scheduler = router.scheduler  # type: ignore[attr-defined]
    if scheduler.running:
        try:
            scheduler.pause_job("trade-monitor")
            scheduler.resume_job("trade-monitor")
        except Exception:
            pass
    set_bot_state(db, BotStatus.RUNNING, trading_allowed=True, risk_locked=False)
    log_event(db, "BOT", "Bot restarted")
    notifier.send(db, "Bot Restarted")
    return {"status": BotStatus.RUNNING}


@router.post("/kill-switch")
def kill_switch(
    db: Annotated[Session, Depends(get_db)],
    trade_manager: Annotated[TradeManager, Depends(manager)],
    v7: Annotated[object, Depends(v7_manager)],
    notifier: Annotated[TelegramService, Depends(telegram)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, str]:
    active = trade_manager.get_active_trade()
    multi = router.multi_strategy_manager  # type: ignore[attr-defined]
    multi.square_off_all(db)
    for option_type in ("CE", "PE"):
        try:
            v7.close_trade(db, option_type)
        except Exception:
            pass
    if active is not None:
        trade_manager.square_off_open_trade()
    set_bot_state(db, BotStatus.STOPPED, trading_allowed=False)
    log_event(db, "RISK", "Kill switch activated", "WARNING")
    notifier.send(db, "Kill Switch Activated")
    return {"status": BotStatus.STOPPED}


@router.post("/reset-daily-lock")
def reset_daily_lock(
    db: Annotated[Session, Depends(get_db)],
    notifier: Annotated[TelegramService, Depends(telegram)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, str]:
    stats = rebuild_daily_stats(db)
    stats.risk_locked = False
    db.commit()
    state = get_or_create_state(db)
    state.risk_locked = False
    state.status = BotStatus.STOPPED
    state.trading_allowed = False
    db.commit()
    log_event(db, "RISK", "Daily risk lock reset by admin")
    notifier.send(db, "Daily Risk Lock Reset")
    return {"status": "RESET"}


def _safe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value
