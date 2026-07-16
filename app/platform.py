from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from app.db_models import AITradeReview, BotState, BotStatus, DailyStats, IndexConfig, IndexSymbol, LogEvent, PlatformSettings, StrategyConfig, StrategyDailyStats, StrategyStats, StrategyTrade, TradeRecord, TradeResult, TradeStatus, TradingMode
from app.time_utils import duration_label, format_ist, iso_utc, to_ist

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


def get_or_create_strategy_stats(db: Session, strategy_name: str) -> StrategyStats:
    stats = db.scalar(select(StrategyStats).where(StrategyStats.strategy_name == strategy_name))
    if stats is None:
        stats = StrategyStats(strategy_name=strategy_name)
        db.add(stats)
        db.commit()
        db.refresh(stats)
    return stats


def reset_daily_risk_if_needed(db: Session) -> None:
    message = "Daily risk reset completed."
    last_reset = db.scalar(select(LogEvent).where(LogEvent.message == message).order_by(LogEvent.created_at.desc()).limit(1))
    if last_reset is not None and to_ist(last_reset.created_at).date() == today_ist():
        return

    for stats in db.scalars(select(StrategyStats)):
        stats.consecutive_losses = 0
        stats.risk_locked = False
    state = get_or_create_state(db)
    state.risk_locked = False
    daily_stats = get_or_create_daily_stats(db, today_ist())
    daily_stats.consecutive_losses = 0
    daily_stats.risk_locked = False
    db.commit()
    log_event(db, "RISK", message)


def update_strategy_stats_after_close(db: Session, strategy_name: str, result: str) -> StrategyStats:
    strategy = db.scalar(select(StrategyConfig).where(StrategyConfig.name == strategy_name))
    max_consecutive_losses = strategy.max_consecutive_losses if strategy is not None else 2
    stats = get_or_create_strategy_stats(db, strategy_name)
    if result == TradeResult.LOSS:
        stats.consecutive_losses += 1
        stats.risk_locked = stats.consecutive_losses >= max_consecutive_losses
    elif result == TradeResult.WIN:
        stats.consecutive_losses = 0
        stats.risk_locked = False
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
    """Admin-level gate only (bot on/off, manual kill-switch). Per-strategy trade-count and
    daily-loss limits are enforced separately by strategy_trading_allowed()."""
    state = get_or_create_state(db)
    if state.risk_locked:
        return False, "Trading disabled: daily risk lock is active"
    if state.status != BotStatus.RUNNING:
        return False, f"Trading disabled: bot status is {state.status}"
    if not state.trading_allowed:
        return False, "Trading disabled by admin"
    return True, "Trading allowed"


def get_or_create_strategy_daily_stats(db: Session, strategy_name: str, trade_date: date | None = None) -> StrategyDailyStats:
    day = trade_date or today_ist()
    stats = db.scalar(
        select(StrategyDailyStats).where(
            StrategyDailyStats.strategy_name == strategy_name,
            StrategyDailyStats.trade_date == day,
        )
    )
    if stats is None:
        stats = StrategyDailyStats(strategy_name=strategy_name, trade_date=day)
        db.add(stats)
        db.commit()
        db.refresh(stats)
    return stats


def rebuild_strategy_daily_stats(db: Session, strategy_name: str, trade_date: date | None = None) -> StrategyDailyStats:
    day = trade_date or today_ist()
    records = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.strategy_name == strategy_name,
                func.date(StrategyTrade.exit_time) == day.isoformat(),
                StrategyTrade.status == TradeStatus.CLOSED,
            )
        )
    )
    stats = get_or_create_strategy_daily_stats(db, strategy_name, day)
    stats.trade_count = len(records)
    stats.pnl_percent = round(sum(record.pnl_percent for record in records), 2)
    stats.wins = sum(1 for record in records if record.result == TradeResult.WIN)
    stats.losses = sum(1 for record in records if record.result == TradeResult.LOSS)
    db.commit()
    db.refresh(stats)
    return stats


def strategy_trading_allowed(db: Session, strategy: StrategyConfig) -> tuple[bool, str]:
    stats = rebuild_strategy_daily_stats(db, strategy.name)
    if stats.trade_count >= strategy.max_trades_per_day:
        return False, f"Trading disabled for {strategy.name}: maximum trades per day reached"
    if stats.pnl_percent <= strategy.daily_max_loss_percent:
        return False, f"Trading disabled for {strategy.name}: daily max loss reached"
    return True, "Trading allowed"


def get_index_config(db: Session, symbol: str) -> IndexConfig | None:
    return db.scalar(select(IndexConfig).where(IndexConfig.symbol == (symbol or IndexSymbol.BANKNIFTY).upper()))


def list_index_configs(db: Session) -> list[IndexConfig]:
    return list(db.scalars(select(IndexConfig).order_by(IndexConfig.symbol)))


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
    records = list(
        db.scalars(
            select(StrategyTrade).where(
                func.date(StrategyTrade.exit_time) == day.isoformat(),
                StrategyTrade.status == TradeStatus.CLOSED,
            ).order_by(StrategyTrade.exit_time)
        )
    )
    stats = get_or_create_daily_stats(db, day)
    stats.trade_count = len(records)
    stats.pnl_percent = round(sum(record.pnl_percent for record in records), 2)
    stats.wins = sum(1 for record in records if record.result == TradeResult.WIN)
    stats.losses = sum(1 for record in records if record.result == TradeResult.LOSS)
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
    stats = rebuild_daily_stats(db)
    open_count = int(db.scalar(select(func.count()).select_from(StrategyTrade).where(StrategyTrade.status == TradeStatus.OPEN)) or 0)
    open_option_types = list(
        db.scalars(
            select(StrategyTrade.option_type).where(StrategyTrade.status == TradeStatus.OPEN)
        )
    )
    current_state = "FLAT"
    if "CE" in open_option_types:
        current_state = "LONG_CE"
    elif "PE" in open_option_types:
        current_state = "LONG_PE"
    failed_entry = db.scalar(
        select(LogEvent)
        .where(LogEvent.event_type == "STATE", LogEvent.message.like("%FAILED_ENTRY%"))
        .order_by(LogEvent.created_at.desc())
        .limit(1)
    )
    if current_state == "FLAT" and failed_entry is not None:
        current_state = "FAILED_ENTRY"
    return {
        "bot_status": state.status,
        "active_trade": active_trade,
        "open_trades": open_count,
        "trade_count": stats.trade_count,
        "wins": stats.wins,
        "losses": stats.losses,
        "pnl_percent": stats.pnl_percent,
        "consecutive_losses": stats.consecutive_losses,
        "trading_allowed": state.trading_allowed and state.status == BotStatus.RUNNING and not state.risk_locked,
        "daily_risk_status": "LOCKED" if state.risk_locked else "OK",
        "risk_locked": state.risk_locked,
        "current_state": current_state,
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


def strategy_trades_query_for_filter(filter_name: str, start: date | None, end: date | None) -> Select[tuple[StrategyTrade]]:
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

    query = select(StrategyTrade)
    if start is not None:
        query = query.where(func.date(StrategyTrade.entry_time) >= start.isoformat())
    if end is not None:
        query = query.where(func.date(StrategyTrade.entry_time) <= end.isoformat())
    return query.order_by(StrategyTrade.entry_time.desc())


def daily_stats_query_for_filter(filter_name: str, start: date | None, end: date | None) -> Select[tuple[DailyStats]]:
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

    query = select(DailyStats)
    if start is not None:
        query = query.where(DailyStats.trade_date >= start)
    if end is not None:
        query = query.where(DailyStats.trade_date <= end)
    return query.order_by(DailyStats.trade_date.desc())


def ai_reviews_query_for_filter(
    review_date: str = "",
    strategy: str = "",
    provider: str = "",
    decision: str = "",
    trade_result: str = "",
) -> Select[tuple[AITradeReview]]:
    query = select(AITradeReview)
    if review_date:
        day = date.fromisoformat(review_date)
        start = datetime.combine(day, time.min, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
        end = datetime.combine(day, time.max, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
        query = query.where(AITradeReview.created_at.between(start, end))
    if strategy:
        query = query.where(AITradeReview.strategy == strategy)
    if provider:
        query = query.where(AITradeReview.provider == provider)
    if decision:
        query = query.where(AITradeReview.decision == decision)
    if trade_result:
        query = query.where(AITradeReview.actual_result == trade_result)
    return query.order_by(AITradeReview.created_at.desc())


def strategy_metrics(db: Session) -> list[dict[str, Any]]:
    strategies = list(db.scalars(select(StrategyConfig).order_by(StrategyConfig.name)))
    metrics: list[dict[str, Any]] = []
    for strategy in strategies:
        trades = list(db.scalars(select(StrategyTrade).where(StrategyTrade.strategy_name == strategy.name)))
        closed = [trade for trade in trades if trade.status == TradeStatus.CLOSED]
        wins = sum(1 for trade in closed if trade.result == TradeResult.WIN)
        losses = sum(1 for trade in closed if trade.result == TradeResult.LOSS)
        open_trades = sum(1 for trade in trades if trade.status == TradeStatus.OPEN)
        total = len(closed)
        metrics.append(
            {
                "strategy": strategy,
                "stats": get_or_create_strategy_stats(db, strategy.name),
                "open_trades": open_trades,
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / total) * 100, 2) if total else 0.0,
                "net_pnl": round(sum(trade.profit_loss for trade in closed), 2),
            }
        )
    return metrics


def serialize_strategy_trade(trade: StrategyTrade) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "strategy_name": trade.strategy_name,
        "signal": trade.signal,
        "index_symbol": trade.index_symbol,
        "strike": trade.strike,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "entry_time": trade.entry_time,
        "entry_time_utc": iso_utc(trade.entry_time),
        "entry_time_ist": format_ist(trade.entry_time),
        "exit_time": trade.exit_time,
        "exit_time_utc": iso_utc(trade.exit_time),
        "exit_time_ist": format_ist(trade.exit_time),
        "duration": duration_label(trade.entry_time, trade.exit_time),
        "profit_loss": trade.profit_loss,
        "pnl_percent": trade.pnl_percent,
        "result": trade.result,
        "status": trade.status,
        "mode": trade.mode,
        "exit_reason": trade.exit_reason,
        "current_premium": trade.current_premium,
    }


def latest_logs(db: Session, limit: int = 100) -> list[LogEvent]:
    return list(db.scalars(select(LogEvent).order_by(LogEvent.created_at.desc()).limit(limit)))


def api_status(db: Session, active_trade: Any | None) -> dict[str, Any]:
    return get_dashboard_summary(db, active_trade)
