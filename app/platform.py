from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from app.db_models import AIExitCall, AITradeReview, BotState, BotStatus, DailyStats, IndexConfig, IndexPriceTick, IndexSymbol, LogEvent, PlatformSettings, StrategyConfig, StrategyDailyStats, StrategyStats, StrategyTrade, StrategyTradeTick, TradeRecord, TradeResult, TradeStatus, TradingMode
from app.time_utils import duration_label, format_ist, iso_utc, to_ist, utc_now

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
    # origin == "SIGNAL" only: this feeds strategy_trading_allowed()'s daily
    # trade-count and max-loss gates, which must reflect real signal trades
    # only -- AI_ALT_* evaluation trades must never trip these limits.
    records = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.strategy_name == strategy_name,
                func.date(StrategyTrade.exit_time) == day.isoformat(),
                StrategyTrade.status == TradeStatus.CLOSED,
                StrategyTrade.origin == "SIGNAL",
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
    # origin == "SIGNAL" only, so platform-wide daily stats reflect real trading
    # only and aren't skewed by AI_ALT_* evaluation trades.
    records = list(
        db.scalars(
            select(StrategyTrade).where(
                func.date(StrategyTrade.exit_time) == day.isoformat(),
                StrategyTrade.status == TradeStatus.CLOSED,
                StrategyTrade.origin == "SIGNAL",
            ).order_by(StrategyTrade.exit_time)
        )
    )
    stats = get_or_create_daily_stats(db, day)
    stats.trade_count = len(records)
    stats.pnl_percent = round(sum(record.pnl_percent for record in records), 2)
    stats.pnl_amount = round(sum(record.profit_loss for record in records), 2)
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
    # origin == "SIGNAL" only -- these feed the homepage's Open Trades count and
    # Current State tile, which must reflect real trading only. An open
    # AI_ALT_* evaluation trade must not inflate the count or make the state
    # tile show a position that isn't actually there.
    open_count = int(
        db.scalar(
            select(func.count()).select_from(StrategyTrade).where(
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == "SIGNAL",
            )
        )
        or 0
    )
    open_option_types = list(
        db.scalars(
            select(StrategyTrade.option_type).where(
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == "SIGNAL",
            )
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
        "pnl_amount": stats.pnl_amount,
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


def strategy_trades_query_for_filter(
    filter_name: str, start: date | None, end: date | None, origin: str | None = None
) -> Select[tuple[StrategyTrade]]:
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
    if origin == "signal":
        query = query.where(StrategyTrade.origin == "SIGNAL")
    elif origin == "ai_alt":
        # Must match AI_ALT_* specifically, not just "anything but SIGNAL" --
        # that broader check also pulled in AI_ORIGIN_* trades (fully
        # independent, self-originated AI trades) onto the AI Alternatives
        # page, making it look like alternatives were being generated for
        # AI Origin trades when none actually were.
        query = query.where(StrategyTrade.origin.like("AI_ALT_%"))
    return query.order_by(StrategyTrade.entry_time.desc())


def origin_comparison_metrics(db: Session, filter_name: str, start: date | None, end: date | None) -> list[dict[str, Any]]:
    """Side-by-side evaluation view: for each origin (SIGNAL, AI_ALT_OPENAI,
    AI_ALT_CLAUDE, ...) seen in the selected date range, closed-trade win
    rate and net P&L, so the AI alternative-call evaluation can actually be
    judged against the real signal instead of guessed at."""
    trades = list(db.scalars(strategy_trades_query_for_filter(filter_name, start, end)))
    closed_by_origin: dict[str, list[StrategyTrade]] = {}
    for trade in trades:
        if trade.status != TradeStatus.CLOSED:
            continue
        closed_by_origin.setdefault(trade.origin, []).append(trade)

    def sort_key(name: str) -> tuple[int, str]:
        return (0, "") if name == "SIGNAL" else (1, name)

    metrics: list[dict[str, Any]] = []
    for origin_name in sorted(closed_by_origin, key=sort_key):
        rows = closed_by_origin[origin_name]
        wins = sum(1 for row in rows if row.result == TradeResult.WIN)
        losses = sum(1 for row in rows if row.result == TradeResult.LOSS)
        total = len(rows)
        metrics.append(
            {
                "origin": origin_name,
                "trade_count": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / total) * 100, 2) if total else 0.0,
                "net_pnl": round(sum(row.profit_loss for row in rows), 2),
                "avg_pnl_percent": round(sum(row.pnl_percent for row in rows) / total, 2) if total else 0.0,
            }
        )
    return metrics


_INDEX_TICK_THROTTLE_SECONDS = 25
_INDEX_DISPLAY_NAMES = {"BANKNIFTY": "Bank Nifty", "NIFTY": "Nifty", "SENSEX": "Sensex"}


def _index_display_name(symbol: str | None) -> str:
    return _INDEX_DISPLAY_NAMES.get((symbol or "").upper(), (symbol or "").title())


def get_performance_summary(
    db: Session,
    filter_name: str,
    start: date | None,
    end: date | None,
    strategy_name: str | None = None,
) -> dict[str, Any]:
    """Owner-variant of the client reporting portal
    (docs/client-reporting-portal-design.md): same KPI / equity-curve /
    daily-P&L / win-loss shape, with strategy attribution added -- a
    strategy filter and a Strategy column on recent trades, neither of
    which the eventual client-facing version will have. origin == SIGNAL
    trades only, same rule as everywhere else."""
    trades = list(db.scalars(strategy_trades_query_for_filter(filter_name, start, end, "signal")))
    closed = [
        trade for trade in trades
        if trade.status == TradeStatus.CLOSED and (strategy_name is None or trade.strategy_name == strategy_name)
    ]
    closed.sort(key=lambda trade: trade.exit_time or trade.entry_time)

    daily_totals: dict[str, float] = {}
    daily_amounts: dict[str, float] = {}
    for trade in closed:
        exit_ist = to_ist(trade.exit_time)
        if exit_ist is None:
            continue
        day = exit_ist.date().isoformat()
        daily_totals[day] = daily_totals.get(day, 0.0) + trade.pnl_percent
        daily_amounts[day] = daily_amounts.get(day, 0.0) + trade.profit_loss
    daily_pnl = [
        {"date": day, "pnl_percent": round(daily_totals[day], 2), "pnl_amount": round(daily_amounts[day], 2)}
        for day in sorted(daily_totals)
    ]

    equity_curve: list[dict[str, Any]] = []
    running = 0.0
    running_amount = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for point in daily_pnl:
        running += point["pnl_percent"]
        running_amount += point["pnl_amount"]
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
        equity_curve.append({
            "date": point["date"],
            "cumulative_percent": round(running, 2),
            "cumulative_amount": round(running_amount, 2),
        })

    wins = sum(1 for trade in closed if trade.result == TradeResult.WIN)
    losses = sum(1 for trade in closed if trade.result == TradeResult.LOSS)
    total = len(closed)
    net_pnl_amount = round(sum(trade.profit_loss for trade in closed), 2)

    recent_trades = [
        {
            "date": to_ist(trade.exit_time).strftime("%d %b") if to_ist(trade.exit_time) else "",
            "strategy_name": trade.strategy_name,
            "index_display_name": _index_display_name(trade.index_symbol),
            "position_label": "Long call" if trade.option_type == "CE" else "Long put",
            "result": trade.result,
            "pnl_percent": trade.pnl_percent,
            "profit_loss": trade.profit_loss,
        }
        for trade in reversed(closed[-20:])
    ]

    strategies = sorted(set(db.scalars(select(StrategyTrade.strategy_name).where(StrategyTrade.origin == "SIGNAL").distinct())))

    return {
        "kpis": {
            "net_return_percent": round(running, 2),
            "net_pnl_amount": net_pnl_amount,
            "win_rate": round((wins / total) * 100, 2) if total else 0.0,
            "total_trades": total,
            "max_drawdown_percent": round(-max_drawdown, 2),
        },
        "daily_pnl": daily_pnl,
        "equity_curve": equity_curve,
        "win_loss": {"wins": wins, "losses": losses},
        "recent_trades": recent_trades,
        "strategies": strategies,
        "selected_strategy": strategy_name or "",
    }


def record_index_tick_if_stale(db: Session, index_symbol: str, price: float) -> None:
    """Throttled IndexPriceTick recorder, shared by the live-dashboard figure
    fetch and app/ai/originator.py's momentum check -- both need real spot-price
    history, and originator.py runs on a schedule independent of whether anyone
    has the dashboard open, so tick recording can't stay dashboard-only."""
    latest_tick = db.scalar(
        select(IndexPriceTick)
        .where(IndexPriceTick.index_symbol == index_symbol)
        .order_by(IndexPriceTick.recorded_at.desc())
        .limit(1)
    )
    latest_ist = to_ist(latest_tick.recorded_at) if latest_tick is not None else None
    now_ist = to_ist(utc_now())
    if latest_ist is None or (now_ist - latest_ist).total_seconds() >= _INDEX_TICK_THROTTLE_SECONDS:
        db.add(IndexPriceTick(index_symbol=index_symbol, price=price))
        db.commit()


def get_index_live_figures(db: Session, smartapi: Any) -> list[dict[str, Any]]:
    """Live index figures (Nifty/Sensex/Bank Nifty) for the live dashboards --
    figures only, no per-index chart by design. Change and day range are
    computed from our own recorded ticks, using today's first tick as the
    reference point, rather than a broker "previous close" field -- the
    current SmartAPI client wrapper only exposes LTP, not a reliable
    previous-close, so inventing one would be dishonest."""
    figures: list[dict[str, Any]] = []
    indexes = list(db.scalars(select(IndexConfig).where(IndexConfig.enabled.is_(True)).order_by(IndexConfig.symbol)))
    today = today_ist().isoformat()
    for index in indexes:
        entry: dict[str, Any] = {
            "symbol": index.symbol,
            "display_name": index.display_name or index.symbol,
            "price": None,
            "change_abs": None,
            "change_percent": None,
            "day_low": None,
            "day_high": None,
        }
        try:
            price = round(smartapi.get_index_spot(index), 2)
        except Exception as exc:
            entry["error"] = str(exc)
            figures.append(entry)
            continue

        record_index_tick_if_stale(db, index.symbol, price)

        todays_ticks = list(
            db.scalars(
                select(IndexPriceTick)
                .where(
                    IndexPriceTick.index_symbol == index.symbol,
                    func.date(IndexPriceTick.recorded_at) == today,
                )
                .order_by(IndexPriceTick.recorded_at)
            )
        )
        entry["price"] = price
        if todays_ticks:
            reference = todays_ticks[0].price
            all_prices = [tick.price for tick in todays_ticks] + [price]
            entry["change_abs"] = round(price - reference, 2)
            entry["change_percent"] = round(((price - reference) / reference) * 100, 2) if reference else 0.0
            entry["day_low"] = round(min(all_prices), 2)
            entry["day_high"] = round(max(all_prices), 2)
        figures.append(entry)
    return figures


def get_open_trades_with_ticks(db: Session, tick_limit: int = 20) -> list[dict[str, Any]]:
    """Real (origin == SIGNAL) open trades for the live dashboards, each with
    its recent premium history for a sparkline and the strategy that took
    it. Never includes AI_ALT_* evaluation trades."""
    trades = list(
        db.scalars(
            select(StrategyTrade)
            .where(StrategyTrade.status == TradeStatus.OPEN, StrategyTrade.origin == "SIGNAL")
            .order_by(StrategyTrade.entry_time.desc())
        )
    )
    result: list[dict[str, Any]] = []
    for trade in trades:
        ticks = list(
            db.scalars(
                select(StrategyTradeTick)
                .where(StrategyTradeTick.trade_id == trade.trade_id)
                .order_by(StrategyTradeTick.recorded_at.desc())
                .limit(tick_limit)
            )
        )
        ticks.reverse()
        history = [tick.premium for tick in ticks] or [trade.entry_price]
        if history[-1] != trade.current_premium and trade.current_premium is not None:
            history.append(trade.current_premium)
        result.append(
            {
                "trade_id": trade.trade_id,
                "strategy_name": trade.strategy_name,
                "index_symbol": trade.index_symbol,
                "option_type": trade.option_type,
                "index_display_name": _index_display_name(trade.index_symbol),
                "position_label": "Long call" if trade.option_type == "CE" else "Long put",
                "entry_price": trade.entry_price,
                "investment_amount": trade.investment_amount,
                "current_premium": trade.current_premium,
                "pnl_percent": trade.pnl_percent,
                "entry_time_ist": format_ist(trade.entry_time),
                "history": history,
            }
        )
    return result


def get_exit_shadow_summary(db: Session) -> dict[str, Any]:
    """Data for the AI Exit Calls page -- a read-only view over app/ai/exit_shadow.py's
    output. Never influences the real trade; this only ever reads AIExitCall +
    StrategyTrade rows for display."""
    open_trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == "SIGNAL",
            ).order_by(StrategyTrade.entry_time.desc())
        )
    )
    live: list[dict[str, Any]] = []
    for trade in open_trades:
        calls = list(
            db.scalars(
                select(AIExitCall)
                .where(AIExitCall.trade_id == trade.trade_id)
                .order_by(AIExitCall.checked_at.desc())
            )
        )
        latest_by_provider: dict[str, AIExitCall] = {}
        for call in calls:
            latest_by_provider.setdefault(call.provider, call)
        holding_minutes = None
        if trade.entry_time is not None:
            entry_ist = to_ist(trade.entry_time)
            now_ist = to_ist(utc_now())
            if entry_ist is not None and now_ist is not None:
                holding_minutes = max(int((now_ist - entry_ist).total_seconds() // 60), 0)
        live.append({
            "trade_id": trade.trade_id,
            "strategy_name": trade.strategy_name,
            "index_display_name": _index_display_name(trade.index_symbol),
            "position_label": "Long call" if trade.option_type == "CE" else "Long put",
            "entry_price": trade.entry_price,
            "current_premium": trade.current_premium,
            "pnl_percent": trade.pnl_percent,
            "holding_minutes": holding_minutes,
            "calls": [
                {
                    "provider": call.provider,
                    "decision": call.decision,
                    "confidence": call.confidence,
                    "reasoning": call.reasoning,
                    "checked_at": format_ist(call.checked_at),
                }
                for call in latest_by_provider.values()
            ],
            "checks_run": len(calls),
        })

    exit_trade_ids = list(
        db.scalars(select(AIExitCall.trade_id).where(AIExitCall.decision == "EXIT").distinct())
    )
    comparisons_with_time: list[tuple[datetime, dict[str, Any]]] = []
    if exit_trade_ids:
        trades_by_id = {
            trade.trade_id: trade
            for trade in db.scalars(select(StrategyTrade).where(StrategyTrade.trade_id.in_(exit_trade_ids)))
        }
        for trade_id in exit_trade_ids:
            trade = trades_by_id.get(trade_id)
            if trade is None:
                continue
            exit_call = db.scalar(
                select(AIExitCall)
                .where(AIExitCall.trade_id == trade_id, AIExitCall.decision == "EXIT")
                .order_by(AIExitCall.checked_at.asc())
                .limit(1)
            )
            if exit_call is None:
                continue
            shadow_pnl = exit_call.pnl_percent_at_check
            comparisons_with_time.append((exit_call.checked_at, {
                "trade_id": trade_id,
                "strategy_name": trade.strategy_name,
                "index_display_name": _index_display_name(trade.index_symbol),
                "position_label": "Long call" if trade.option_type == "CE" else "Long put",
                "ai_called_at": format_ist(exit_call.checked_at),
                "ai_provider": exit_call.provider,
                "ai_reasoning": exit_call.reasoning,
                "shadow_pnl_percent": shadow_pnl,
                "real_status": trade.status,
                "real_pnl_percent": trade.pnl_percent if trade.status == TradeStatus.CLOSED else None,
                "real_exit_reason": trade.exit_reason if trade.status == TradeStatus.CLOSED else None,
            }))
        comparisons_with_time.sort(key=lambda item: item[0], reverse=True)
    comparisons = [row for _, row in comparisons_with_time]

    # Closed SIGNAL trades that got shadow-checked (AIExitCall rows exist)
    # but where the AI never actually called EXIT -- every check said HOLD
    # (or errored). Before this, that history had nowhere to surface: the
    # "live" table above only covers trades still OPEN, and "comparisons"
    # only ever included a trade once the AI called EXIT on it specifically.
    # The checks still ran and wrote rows the whole time -- they just sat in
    # the database with no page ever reading them back for a HOLD-only trade.
    checked_trade_ids = list(db.scalars(select(AIExitCall.trade_id).distinct()))
    held_only: list[dict[str, Any]] = []
    if checked_trade_ids:
        held_conditions = [
            StrategyTrade.trade_id.in_(checked_trade_ids),
            StrategyTrade.status == TradeStatus.CLOSED,
            StrategyTrade.origin == "SIGNAL",
        ]
        if exit_trade_ids:
            held_conditions.append(StrategyTrade.trade_id.notin_(exit_trade_ids))
        held_trades = list(
            db.scalars(
                select(StrategyTrade).where(*held_conditions).order_by(StrategyTrade.exit_time.desc()).limit(30)
            )
        )
        for trade in held_trades:
            calls = list(
                db.scalars(
                    select(AIExitCall)
                    .where(AIExitCall.trade_id == trade.trade_id)
                    .order_by(AIExitCall.checked_at.desc())
                )
            )
            if not calls:
                continue
            latest_by_provider: dict[str, AIExitCall] = {}
            for call in calls:
                latest_by_provider.setdefault(call.provider, call)
            held_only.append({
                "trade_id": trade.trade_id,
                "strategy_name": trade.strategy_name,
                "index_display_name": _index_display_name(trade.index_symbol),
                "position_label": "Long call" if trade.option_type == "CE" else "Long put",
                "calls": [
                    {
                        "provider": call.provider,
                        "decision": call.decision,
                        "confidence": call.confidence,
                        "reasoning": call.reasoning,
                        "checked_at": format_ist(call.checked_at),
                    }
                    for call in latest_by_provider.values()
                ],
                "checks_run": len(calls),
                "real_pnl_percent": trade.pnl_percent,
                "real_result": trade.result,
                "real_exit_reason": trade.exit_reason,
                "exit_time": format_ist(trade.exit_time),
            })

    return {"live": live, "comparisons": comparisons, "held_only": held_only}


def get_origination_summary(db: Session, limit: int = 30) -> dict[str, Any]:
    """Data for the AI Origination page -- read-only view over app/ai/originator.py's
    output. These are trades the AI opened entirely on its own (no TradingView
    signal involved), tagged origin AI_ORIGIN_*, isolated from real trading logic
    exactly like AI_ALT_* trades."""
    open_trades = list(
        db.scalars(
            select(StrategyTrade)
            .where(StrategyTrade.status == TradeStatus.OPEN, StrategyTrade.origin.like("AI_ORIGIN_%"))
            .order_by(StrategyTrade.entry_time.desc())
        )
    )
    closed_trades = list(
        db.scalars(
            select(StrategyTrade)
            .where(StrategyTrade.status == TradeStatus.CLOSED, StrategyTrade.origin.like("AI_ORIGIN_%"))
            .order_by(StrategyTrade.exit_time.desc())
            .limit(limit)
        )
    )

    def _row(trade: StrategyTrade, closed: bool) -> dict[str, Any]:
        return {
            "trade_id": trade.trade_id,
            "provider": trade.origin[len("AI_ORIGIN_"):].title() if trade.origin else "",
            "index_display_name": _index_display_name(trade.index_symbol),
            "position_label": "Long call" if trade.option_type == "CE" else "Long put",
            "mode": trade.mode,
            "entry_price": trade.entry_price,
            "investment_amount": trade.investment_amount,
            "current_premium": trade.current_premium,
            "exit_price": trade.exit_price if closed else None,
            "pnl_percent": trade.pnl_percent,
            "profit_loss": trade.profit_loss if closed else None,
            "result": trade.result if closed else None,
            "confidence": trade.ai_confidence,
            "reasoning": trade.ai_reasoning,
            "entry_time": format_ist(trade.entry_time),
            "exit_time": format_ist(trade.exit_time) if closed else None,
        }

    live = [_row(trade, closed=False) for trade in open_trades]
    history = [_row(trade, closed=True) for trade in closed_trades]

    wins = sum(1 for trade in closed_trades if trade.result == TradeResult.WIN)
    losses = sum(1 for trade in closed_trades if trade.result == TradeResult.LOSS)
    total_closed = len(closed_trades)
    kpis = {
        "total_originated": len(open_trades) + len(closed_trades),
        "open_count": len(open_trades),
        "closed_count": total_closed,
        "win_rate": round((wins / total_closed) * 100, 2) if total_closed else 0.0,
        "net_pnl_percent": round(sum(trade.pnl_percent or 0 for trade in closed_trades), 2),
        "net_pnl_amount": round(sum(trade.profit_loss or 0 for trade in closed_trades), 2),
    }

    return {"live": live, "history": history, "kpis": kpis}


def get_today_activity(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    """Plain-language, strategy-attributed activity feed for the live
    dashboards, derived directly from real (origin == SIGNAL) trade
    entry/exit times rather than parsing internal log-event strings."""
    today = today_ist().isoformat()
    trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.origin == "SIGNAL",
                func.date(StrategyTrade.entry_time) == today,
            )
        )
    )
    closed_today = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.origin == "SIGNAL",
                StrategyTrade.exit_time.is_not(None),
                func.date(StrategyTrade.exit_time) == today,
            )
        )
    )
    events: list[dict[str, Any]] = []
    for trade in trades:
        position = "long call" if trade.option_type == "CE" else "long put"
        events.append(
            {
                "timestamp": trade.entry_time,
                "time_label": to_ist(trade.entry_time).strftime("%I:%M %p") if to_ist(trade.entry_time) else "",
                "message": f"[{trade.strategy_name}] Entered {_index_display_name(trade.index_symbol)} {position}",
            }
        )
    for trade in closed_today:
        position = "long call" if trade.option_type == "CE" else "long put"
        sign = "+" if (trade.pnl_percent or 0) >= 0 else ""
        events.append(
            {
                "timestamp": trade.exit_time,
                "time_label": to_ist(trade.exit_time).strftime("%I:%M %p") if to_ist(trade.exit_time) else "",
                "message": f"[{trade.strategy_name}] Closed {_index_display_name(trade.index_symbol)} {position}, {sign}{trade.pnl_percent:.1f}%",
            }
        )
    events.sort(key=lambda event: event["timestamp"], reverse=True)
    # Drop the raw datetime before returning -- only used for sorting above.
    # Jinja's `tojson` filter (used to seed the initial page render) can't
    # serialize a datetime object, and only time_label/message are ever
    # displayed.
    for event in events:
        del event["timestamp"]
    return events[:limit]


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
        # origin == "SIGNAL" only: dashboard performance metrics should reflect
        # real trading only, not AI_ALT_* evaluation trades.
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.strategy_name == strategy.name,
                    StrategyTrade.origin == "SIGNAL",
                )
            )
        )
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
        "origin": trade.origin,
        "source_trade_id": trade.source_trade_id,
        "ai_action": trade.ai_action,
        "ai_confidence": trade.ai_confidence,
        "ai_reasoning": trade.ai_reasoning,
    }


def latest_logs(db: Session, limit: int = 100) -> list[LogEvent]:
    return list(db.scalars(select(LogEvent).order_by(LogEvent.created_at.desc()).limit(limit)))


def api_status(db: Session, active_trade: Any | None) -> dict[str, Any]:
    return get_dashboard_summary(db, active_trade)
