from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from app.db_models import BotState, BotStatus, DailyStats, LogEvent, PlatformSettings, TradeRecord, TradingMode

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> date:
    return datetime.now(IST).date()


def get_or_create_state(db: Session) -> BotState:
    state = db.get(BotState, 1)
    if state is None:
        state = BotState(id=1, status=BotStatus.STOPPED, trading_allowed=False, risk_locked=False)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def get_or_create_settings(db: Session) -> PlatformSettings:
    settings = db.get(PlatformSettings, 1)
    if settings is None:
        settings = PlatformSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def get_or_create_daily_stats(db: Session, trade_date: date | None = None) -> DailyStats:
    day = trade_date or today_ist()
    stats = db.scalar(select(DailyStats).where(DailyStats.trade_date == day))
    if stats is None:
        stats = DailyStats(trade_date=day)
        db.add(stats)
        db.commit()
        db.refresh(stats)
    return stats


def log_event(
    db: Session,
    event_type: str,
    message: str,
    level: str = "INFO",
    payload: dict[str, Any] | None = None,
) -> None:
    db.add(
        LogEvent(
            event_type=event_type,
            level=level,
            message=message,
            payload=json.dumps(payload or {}, default=str),
        )
    )
    db.commit()


def set_bot_state(db: Session, status: str, trading_allowed: bool, risk_locked: bool | None = None) -> BotState:
    state = get_or_create_state(db)
    state.status = status
    state.trading_allowed = trading_allowed
    if risk_locked is not None:
        state.risk_locked = risk_locked
    db.commit()
    db.refresh(state)
    return state


def trading_allowed(db: Session) -> tuple[bool, str]:
    state = get_or_create_state(db)
    settings = get_or_create_settings(db)
    stats = rebuild_daily_stats(db)
    if state.risk_locked:
        return False, "Trading disabled: daily risk lock is active"
    if state.status != BotStatus.RUNNING:
        return False, f"Trading disabled: bot status is {state.status}"
    if not state.trading_allowed:
        return False, "Trading disabled by admin"
    if stats.trade_count >= settings.max_trades_per_day:
        return False, "Trading disabled: maximum trades per day reached"
    if stats.consecutive_losses >= settings.max_consecutive_losses:
        return False, "Trading disabled: maximum consecutive losses reached"
    if stats.pnl_percent <= settings.daily_max_loss_percent:
        return False, "Trading disabled: daily max loss reached"
    return True, "Trading allowed"


def sync_trade_row(db: Session, row: dict[str, str], trading_mode: str) -> TradeRecord | None:
    if not row.get("exit_time"):
        return None
    exit_time = datetime.fromisoformat(row["exit_time"])
    existing = db.scalar(
        select(TradeRecord).where(
            TradeRecord.exit_time == exit_time,
            TradeRecord.signal == row.get("signal", ""),
            TradeRecord.strike == int(float(row.get("strike") or 0)),
        )
    )
    if existing is not None:
        return existing

    record = TradeRecord(
        date=date.fromisoformat(row["date"]),
        signal=row["signal"],
        strike=int(float(row["strike"])),
        entry_price=float(row["entry_price"]),
        exit_price=float(row["exit_price"]),
        stoploss=float(row["stoploss"]),
        target=float(row["target"]),
        entry_time=datetime.fromisoformat(row["entry_time"]),
        exit_time=exit_time,
        exit_reason=row["exit_reason"],
        pnl_percent=float(row["pnl_percent"]),
        trading_mode=trading_mode,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    rebuild_daily_stats(db, record.date)
    return record


def rebuild_daily_stats(db: Session, trade_date: date | None = None) -> DailyStats:
    day = trade_date or today_ist()
    records = list(db.scalars(select(TradeRecord).where(TradeRecord.date == day).order_by(TradeRecord.exit_time)))
    stats = get_or_create_daily_stats(db, day)
    stats.trade_count = len(records)
    stats.pnl_percent = round(sum(record.pnl_percent for record in records), 2)
    stats.wins = sum(1 for record in records if record.pnl_percent > 0)
    stats.losses = sum(1 for record in records if record.pnl_percent < 0)
    consecutive = 0
    for record in reversed(records):
        if record.pnl_percent < 0:
            consecutive += 1
        else:
            break
    stats.consecutive_losses = consecutive
    db.commit()
    db.refresh(stats)
    return stats


def get_dashboard_summary(db: Session, active_trade: Any | None) -> dict[str, Any]:
    state = get_or_create_state(db)
    settings = get_or_create_settings(db)
    stats = rebuild_daily_stats(db)
    return {
        "bot_status": state.status,
        "trading_mode": settings.trading_mode,
        "active_trade": active_trade,
        "trade_count": stats.trade_count,
        "wins": stats.wins,
        "losses": stats.losses,
        "pnl_percent": stats.pnl_percent,
        "consecutive_losses": stats.consecutive_losses,
        "trading_allowed": state.trading_allowed and state.status == BotStatus.RUNNING and not state.risk_locked,
        "daily_risk_status": "LOCKED" if state.risk_locked else "OK",
        "risk_locked": state.risk_locked,
    }


def trades_query_for_filter(filter_name: str, start: date | None, end: date | None) -> Select[tuple[TradeRecord]]:
    today = today_ist()
    if filter_name == "7d":
        start = today - timedelta(days=6)
        end = today
    elif filter_name == "30d":
        start = today - timedelta(days=29)
        end = today
    elif filter_name == "today":
        start = today
        end = today

    query = select(TradeRecord)
    if start is not None:
        query = query.where(TradeRecord.date >= start)
    if end is not None:
        query = query.where(TradeRecord.date <= end)
    return query.order_by(TradeRecord.exit_time.desc())


def latest_logs(db: Session, limit: int = 100) -> list[LogEvent]:
    return list(db.scalars(select(LogEvent).order_by(LogEvent.created_at.desc()).limit(limit)))


def api_status(db: Session, active_trade: Any | None) -> dict[str, Any]:
    return get_dashboard_summary(db, active_trade)
