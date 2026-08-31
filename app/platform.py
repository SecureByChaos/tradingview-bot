from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from app.db_models import AIOriginationLog, BotState, BotStatus, Candle, DailyStats, IndexConfig, IndexPriceTick, IndexSymbol, LogEvent, PlatformSettings, StrategyConfig, StrategyDailyStats, StrategyStats, StrategyTrade, StrategyTradeTick, TradeRecord, TradeResult, TradeStatus, TradingMode
from app.market_context import ADX_NO_TREND, ADX_TRENDING
from app.market_data import ONE_MINUTE
from app.signal_validation import check_market_hours
from app.time_utils import duration_label, format_ist, iso_utc, to_ist, utc_now

logger = logging.getLogger(__name__)

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


def get_live_trading_status(db: Session, smartapi: Any) -> dict[str, Any]:
    """Read-only summary of AI Origination's two-key live-trading gate
    (CLAUDE.md, "Live-trading safety"), for the AI Settings page. Deliberately
    NOT a control -- the per-index ai_origination_live_trade checkbox already
    lives in Settings > Instruments (app/dashboard_routes.py's
    update_instrument route) and stays the one place that writes it, so this
    can't drift into a second, differently-behaving toggle. server_flag_on
    reads SMARTAPI_LIVE_TRADING (smartapi.settings.live_trading) -- an env
    var, deliberately not settable from the UI at all, since a UI bug must
    never be able to flip the half of the gate that's supposed to require a
    server-side deploy."""
    indices = [
        {
            "symbol": index.symbol,
            "display_name": index.display_name or index.symbol,
            "live": bool(index.ai_origination_live_trade),
        }
        for index in list_index_configs(db)
        if index.enabled
    ]
    return {
        "server_flag_on": bool(getattr(getattr(smartapi, "settings", None), "live_trading", False)),
        "indices": indices,
    }


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
    filter_name: str, start: date | None, end: date | None, origin: str | None = None, strategy_name: str | None = None
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
    elif origin == "ai_origin":
        # Fully self-originated AI trades (no TradingView signal involved),
        # kept distinct from AI_ALT_* (evaluation-only trades, no longer
        # filterable from Trade History -- see CLAUDE.md, "AI Alternatives
        # removed from Trade History").
        query = query.where(StrategyTrade.origin.like("AI_ORIGIN_%"))
    if strategy_name:
        query = query.where(StrategyTrade.strategy_name == strategy_name)
    return query.order_by(StrategyTrade.entry_time.desc())


def signal_strategy_names(db: Session) -> list[str]:
    """Distinct strategy names among real SIGNAL trades, for Trade History's
    strategy sub-filter -- shown only when Origin = Signal Only, since AI
    trades don't carry a meaningful separate strategy name."""
    return sorted(set(db.scalars(select(StrategyTrade.strategy_name).where(StrategyTrade.origin == "SIGNAL").distinct())))


_INDEX_TICK_THROTTLE_SECONDS = 25
_INDEX_DISPLAY_NAMES = {"BANKNIFTY": "Bank Nifty", "NIFTY": "Nifty", "SENSEX": "Sensex"}


def _index_display_name(symbol: str | None) -> str:
    return _INDEX_DISPLAY_NAMES.get((symbol or "").upper(), (symbol or "").title())


def compute_performance_kpis(closed_trades: list[StrategyTrade]) -> dict[str, Any]:
    """KPI / equity-curve / daily-P&L / win-loss numbers for an already
    date/origin/strategy-filtered set of closed trades. Takes the trade list
    rather than querying itself so the Trade History page (the sole caller,
    since the standalone Performance page was folded into it) can compute
    both the trades table and these stats from one query instead of two, and
    so the numbers always describe exactly the population shown in the table
    below them -- previously this was hardcoded to origin == SIGNAL only;
    now it reflects whatever origin/strategy filter is currently selected.

    28 Aug 2026: every percent figure here is capital-weighted (net P&L
    divided by capital invested), not a naive sum of each trade's own
    pnl_percent. Summing raw per-trade percentages is not a valid aggregate
    return -- a small trade with a big % gain and a large trade with a
    modest % loss can sum to a POSITIVE percent while the actual rupee
    total is NEGATIVE, which is exactly what a real selection showed: a
    +2.93% "Net return" next to a -829.75 "Net P&L" on the same 3 trades.
    Every rupee figure also switched from trade.profit_loss (gross -- see
    this file's own "Costs" convention) to trade.net_pnl (net of
    estimated_cost), so a KPI literally labelled "Net P&L" is actually net,
    not gross wearing that label."""
    closed = sorted(closed_trades, key=lambda trade: trade.exit_time or trade.entry_time)

    daily_amounts: dict[str, float] = {}
    daily_invested: dict[str, float] = {}
    for trade in closed:
        exit_ist = to_ist(trade.exit_time)
        if exit_ist is None:
            continue
        day = exit_ist.date().isoformat()
        daily_amounts[day] = daily_amounts.get(day, 0.0) + trade.net_pnl
        daily_invested[day] = daily_invested.get(day, 0.0) + trade.investment_amount
    daily_pnl = [
        {
            "date": day,
            "pnl_percent": round(daily_amounts[day] / daily_invested[day] * 100, 2) if daily_invested[day] else 0.0,
            "pnl_amount": round(daily_amounts[day], 2),
        }
        for day in sorted(daily_amounts)
    ]

    equity_curve: list[dict[str, Any]] = []
    running_amount = 0.0
    running_invested = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for day in sorted(daily_amounts):
        running_amount += daily_amounts[day]
        running_invested += daily_invested[day]
        cumulative_percent = round(running_amount / running_invested * 100, 2) if running_invested else 0.0
        peak = max(peak, cumulative_percent)
        max_drawdown = max(max_drawdown, peak - cumulative_percent)
        equity_curve.append({
            "date": day,
            "cumulative_percent": cumulative_percent,
            "cumulative_amount": round(running_amount, 2),
        })

    wins = sum(1 for trade in closed if trade.result == TradeResult.WIN)
    losses = sum(1 for trade in closed if trade.result == TradeResult.LOSS)
    total = len(closed)
    net_pnl_amount = round(sum(trade.net_pnl for trade in closed), 2)
    total_invested = sum(trade.investment_amount for trade in closed)
    net_return_percent = round(net_pnl_amount / total_invested * 100, 2) if total_invested else 0.0

    return {
        "kpis": {
            "net_return_percent": net_return_percent,
            "net_pnl_amount": net_pnl_amount,
            "win_rate": round((wins / total) * 100, 2) if total else 0.0,
            "total_trades": total,
            "max_drawdown_percent": round(-max_drawdown, 2),
        },
        "daily_pnl": daily_pnl,
        "equity_curve": equity_curve,
        "win_loss": {"wins": wins, "losses": losses},
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


def get_index_live_figures(db: Session, smartapi: Any, feed_store: Any = None) -> list[dict[str, Any]]:
    """Live index figures (Nifty/Sensex/Bank Nifty) for the live dashboards --
    figures only, no per-index chart by design. Day range is computed from
    today's recorded ticks. Change is computed against the previous trading
    day's LAST recorded tick, not a broker "previous close" field -- the
    current SmartAPI client wrapper only exposes LTP, not a reliable
    previous-close, so inventing one would be dishonest.

    21 Aug 2026: change used to be computed against TODAY's first tick
    (effectively today's opening print), which reads as "change since open"
    while every other source (TradingView included) shows "change since
    previous close" -- these routinely disagree by however much the index
    gapped overnight, which is exactly the mismatch reported that day
    (Bank Nifty off by ~115 points, Nifty by ~36). Fixed then with a
    previous-day-last-IndexPriceTick approximation.

    25 Aug 2026: that approximation itself turned out to still mismatch the
    broker by ~36-107 points on an expiry day. Confirmed with real data via
    a temporary [PREVCLOSE] diagnostic log: the reference tick was recorded
    at 15:25 IST, 4-10 minutes before the Closing Auction Session actually
    settles (~15:29-15:35, see "Closing Auction Session" in CLAUDE.md) --
    tick recording is gated to check_market_hours()'s 09:15-15:30 window, so
    the last tick before that gate closes is not guaranteed to be the real
    settlement print. Now prefers the same CAS-corrected 1-minute candle
    close capture_closing_auction() (app/market_data.py) already writes for
    AI Origination's own previous-close reads -- reusing that fix rather
    than maintaining two previous-close mechanisms. Falls back to the old
    previous-day-tick approximation, then today's first tick, only when no
    candle history exists yet for an index (e.g. brand new, or candle
    backfill hasn't run) -- same fail-soft order as before, just with the
    accurate source tried first.

    Price source: prefers feed_store (app/live_feed.py's persistent WebSocket
    feed) when given and it has a value -- zero SmartAPI calls, whether this
    is called once or a thousand times. Falls back to a direct smartapi call
    ONLY when feed_store is None (feed feature not wired in, e.g. tests) or
    hasn't produced a first tick yet for a newly-enabled index. Deliberately
    does NOT fall back to smartapi on an ordinary feed disconnect/staleness --
    that would reintroduce the exact per-request SmartAPI cost the feed
    exists to eliminate; a stale feed entry is still used, just flagged via
    is_live=False.

    14 Aug 2026: tick recording below is skipped entirely outside market
    hours. This function is driven by dashboard polling (every 10s per open
    browser tab, see live_dashboard.html), which has no market-hours
    awareness of its own -- unlike originator.py's own call to
    record_index_tick_if_stale, which is now upstream of the market-hours
    gate added in run_origination_checks (see CLAUDE.md, "SmartAPI calls
    stopped outside market hours"), this call site had none. A frozen
    weekend/holiday price was being re-recorded as a new IndexPriceTick
    roughly every _INDEX_TICK_THROTTLE_SECONDS for as long as anyone had the
    dashboard open -- real DB writes for a value that never changed, adding
    nothing today's-first-tick/day-range math below can use. The figures
    themselves (and the feed's own is_live/stale badge) are unaffected --
    this only stops the redundant write, not the display."""
    figures: list[dict[str, Any]] = []
    indexes = list(db.scalars(select(IndexConfig).where(IndexConfig.enabled.is_(True)).order_by(IndexConfig.symbol)))
    today = today_ist().isoformat()
    is_trading_now = check_market_hours(utc_now()) is None
    for index in indexes:
        entry: dict[str, Any] = {
            "symbol": index.symbol,
            "display_name": index.display_name or index.symbol,
            "price": None,
            "change_abs": None,
            "change_percent": None,
            "day_low": None,
            "day_high": None,
            "is_live": None,
        }
        feed_entry = feed_store.get(index.symbol) if feed_store is not None else None
        price_is_fresh = True
        if feed_entry is not None:
            price = round(feed_entry["price"], 2)
            entry["is_live"] = feed_entry["is_live"]
        elif feed_store is None:
            try:
                price = round(smartapi.get_index_spot(index), 2)
            except Exception as exc:
                # Full detail server-side only. This dict is returned
                # verbatim as JSON by /api/live-dashboard -- str(exc) on a
                # SmartAPIError can embed Angel's raw response body, which
                # isn't meant to leave the process even to an authenticated
                # dashboard viewer (CodeQL: information exposure through an
                # exception, PR #9).
                logger.warning("get_index_live_figures: spot fetch failed for %s: %s", index.symbol, exc)
                entry["error"] = "Live price temporarily unavailable"
                figures.append(entry)
                continue
        else:
            # feed_store is wired but has never produced a tick for this
            # index THIS PROCESS -- routine right after a restart while the
            # market's closed, since app/live_feed.py's 17 Aug market-hours
            # gate means the WS feed no longer even attempts to connect in
            # that state (previously it kept retrying every 10s and would
            # often pick up a value quickly). Rather than showing
            # "Unavailable" for however long the market stays closed, fall
            # back to the last IndexPriceTick ever recorded for this index --
            # a plain DB read, zero SmartAPI cost, same fail-closed spirit as
            # LiveFeedStore.get() itself. is_live=False so the dashboard's
            # existing "stale" badge (already reads "showing last known
            # price") renders correctly with no template change needed for
            # this case specifically.
            last_tick = db.scalar(
                select(IndexPriceTick)
                .where(IndexPriceTick.index_symbol == index.symbol)
                .order_by(IndexPriceTick.recorded_at.desc())
                .limit(1)
            )
            if last_tick is None:
                entry["error"] = "Live feed has not produced a price for this index yet"
                figures.append(entry)
                continue
            price = round(last_tick.price, 2)
            entry["is_live"] = False
            price_is_fresh = False

        # Never record a fallback (not-fresh) price as a new tick -- that
        # would insert a possibly-days-old value into today's tick history
        # and corrupt the change/day-range math below for the rest of the
        # day. Only ever relevant in the rare case this fallback fires while
        # is_trading_now is still True (feed hasn't produced its first tick
        # of the session yet).
        if is_trading_now and price_is_fresh:
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
            # 25 Aug 2026: the CAS-corrected candle close is tried first --
            # see the docstring above for why the old tick-based reference
            # could land a few minutes before the real settlement print.
            previous_day_candle = db.scalar(
                select(Candle)
                .where(
                    Candle.index_symbol == index.symbol,
                    Candle.interval == ONE_MINUTE,
                    func.date(Candle.ts_ist) < today,
                )
                .order_by(Candle.ts_ist.desc())
                .limit(1)
            )
            if previous_day_candle is not None:
                reference = previous_day_candle.close
                reference_source = "candle"
                reference_recorded_at = previous_day_candle.ts_ist
            else:
                previous_day_tick = db.scalar(
                    select(IndexPriceTick)
                    .where(
                        IndexPriceTick.index_symbol == index.symbol,
                        func.date(IndexPriceTick.recorded_at) < today,
                    )
                    .order_by(IndexPriceTick.recorded_at.desc())
                    .limit(1)
                )
                reference = previous_day_tick.price if previous_day_tick is not None else todays_ticks[0].price
                reference_source = (
                    "previous-day tick" if previous_day_tick is not None
                    else "today's first tick (no prior-day candle or tick found)"
                )
                reference_recorded_at = (
                    previous_day_tick.recorded_at if previous_day_tick is not None else todays_ticks[0].recorded_at
                )
            # Diagnostic log kept from the 25 Aug investigation -- confirms
            # which source and timestamp this function picked as "previous
            # close" per index, directly diffable against the broker's own
            # figure on a live trading day.
            logger.info(
                "[PREVCLOSE] %s: reference=%.2f (%s, recorded_at=%s) current=%.2f",
                index.symbol, reference, reference_source, reference_recorded_at, price,
            )
            all_prices = [tick.price for tick in todays_ticks] + [price]
            entry["change_abs"] = round(price - reference, 2)
            entry["change_percent"] = round(((price - reference) / reference) * 100, 2) if reference else 0.0
            entry["day_low"] = round(min(all_prices), 2)
            entry["day_high"] = round(max(all_prices), 2)
        figures.append(entry)
    return figures


def _classify_chop(ratio: float | None) -> str:
    """Same three-band read as app/ai/originator.py's _efficiency_ratio_text
    (<0.3 choppy, 0.3-0.5 mixed, >=0.5 clean) -- duplicated rather than
    imported: app/ai/originator.py already imports FROM this module
    (get_or_create_settings/list_index_configs/log_event/etc.), so importing
    back from it here would be circular. Same duplication _classify_
    tradability already established for the ADX bands, same reason."""
    if ratio is None:
        return "UNKNOWN"
    if ratio < 0.3:
        return "CHOPPY"
    if ratio < 0.5:
        return "MIXED"
    return "CLEAN"


def _classify_tradability(adx: float | None) -> str:
    """Three-band read of the same ADX thresholds app/market_context.py's
    regime classification and the AI Origination system prompt already use
    (ADX_NO_TREND=20, ADX_TRENDING=25) -- not a new, independently-invented
    line. Matches the model's own prompt wording: below 20 "no established
    trend", 20-25 "a trend is developing", above 25 "continuation is better
    supported"."""
    if adx is None:
        return "UNKNOWN"
    if adx >= ADX_TRENDING:
        return "TRENDING"
    if adx >= ADX_NO_TREND:
        return "MARGINAL"
    return "NOT_TRADABLE"


def get_market_conditions(db: Session) -> list[dict[str, Any]]:
    """Latest AI Origination market-condition snapshot per enabled index --
    a read-only view over app/ai/origination_log.py's persisted rows.

    Zero new computation, zero new SmartAPI calls: this reads exactly what
    _load_market_context() already computed and record_decision() already
    wrote on origination's own 5-min cycle -- the same regime/ADX/CPR/setups
    values the [AI][ORIGIN][CTX] log line prints and the model's own prompt
    is built from. Both providers share one market_context per index per
    cycle (see originator.py), so picking the single latest row regardless
    of which provider wrote it is correct, not an arbitrary choice.

    `tradability` is informational only -- see _classify_tradability. Nothing
    in the trading path reads this; it exists so "is this index trending
    right now" is answerable from the dashboard instead of grepping logs.

    27 Aug 2026: also surfaces chop_efficiency_ratio (see app/market_context.
    py's compute_efficiency_ratio) and the model's own confidence/setup_
    quality/entry_quality/risk_quality/market_alignment for the same latest
    row -- same zero-new-computation read, just more of the columns that
    row already has. All five are null on a SLOT_OCCUPIED marker row (see
    the "Market Conditions panel froze" fix -- that marker carries real
    context/chop data but no real decision, so confidence/sub-scores
    genuinely don't exist for it, and null says so honestly rather than
    inventing a number)."""
    conditions: list[dict[str, Any]] = []
    for index in db.scalars(select(IndexConfig).where(IndexConfig.enabled.is_(True)).order_by(IndexConfig.symbol)):
        entry: dict[str, Any] = {
            "symbol": index.symbol,
            "display_name": index.display_name or index.symbol,
            "regime": None,
            "adx": None,
            "cpr": None,
            "setups": [],
            "data_stale": None,
            "last_updated": None,
            "tradability": "UNKNOWN",
            "chop_efficiency_ratio": None,
            "chop_label": "UNKNOWN",
            "confidence": None,
            "setup_quality": None,
            "entry_quality": None,
            "risk_quality": None,
            "market_alignment": None,
        }
        latest = db.scalar(
            select(AIOriginationLog)
            .where(AIOriginationLog.index_name == index.symbol)
            .order_by(AIOriginationLog.timestamp.desc())
            .limit(1)
        )
        if latest is not None:
            entry["regime"] = latest.regime
            entry["adx"] = latest.adx
            entry["cpr"] = latest.cpr
            try:
                entry["setups"] = json.loads(latest.setups) if latest.setups else []
            except (TypeError, ValueError):
                entry["setups"] = []
            entry["data_stale"] = latest.data_stale
            entry["last_updated"] = iso_utc(latest.timestamp)
            entry["tradability"] = _classify_tradability(latest.adx)
            entry["chop_efficiency_ratio"] = latest.chop_efficiency_ratio
            entry["chop_label"] = _classify_chop(latest.chop_efficiency_ratio)
            entry["confidence"] = latest.confidence
            entry["setup_quality"] = latest.setup_quality
            entry["entry_quality"] = latest.entry_quality
            entry["risk_quality"] = latest.risk_quality
            entry["market_alignment"] = latest.market_alignment
        conditions.append(entry)
    return conditions


def origin_label(origin: str | None) -> str:
    if not origin or origin == "SIGNAL":
        return "Signal"
    if origin.startswith("AI_ALT_"):
        provider = origin[len("AI_ALT_"):].title()
        return f"AI Alt · {provider}"
    if origin.startswith("AI_ORIGIN_"):
        provider = origin[len("AI_ORIGIN_"):].title()
        return f"AI Origin · {provider}"
    return origin


def get_open_trades_with_ticks(db: Session, tick_limit: int = 20) -> list[dict[str, Any]]:
    """Open trades for the live dashboard's Active Trades panel, each with its
    recent premium history for a sparkline.

    Real (origin == SIGNAL) trades plus AI_ORIGIN_* trades -- both paper and
    live, distinguished by the mode field below, matching how the AI
    Origination page itself (now removed, 15 Aug 2026) always showed both.
    Matched with LIKE 'AI_ORIGIN_%', never != 'SIGNAL' -- see CLAUDE.md,
    "The origin field is the isolation mechanism": that exact bug once put
    AI_ALT_* trades where they didn't belong. AI_ALT_* evaluation trades are
    still deliberately excluded -- they're a shadow/comparison feature, not a
    position anyone is holding."""
    trades = list(
        db.scalars(
            select(StrategyTrade)
            .where(
                StrategyTrade.status == TradeStatus.OPEN,
                or_(StrategyTrade.origin == "SIGNAL", StrategyTrade.origin.like("AI_ORIGIN_%")),
            )
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
                "origin": trade.origin,
                "source_label": origin_label(trade.origin),
                "mode": trade.mode,
                "index_symbol": trade.index_symbol,
                "option_type": trade.option_type,
                "strike": trade.strike,
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


def get_validated_signal_trades(db: Session) -> list[StrategyTrade]:
    """All Validated Signal trades (app.validated_signal), open and closed,
    newest first -- the sole population behind the /validated-signal page.
    origin == "VALIDATED_SIGNAL" exactly (not a LIKE match) since it is one
    single fixed value, not a family of provider-suffixed values the way
    AI_ORIGIN_*/AI_ALT_* are."""
    return list(
        db.scalars(
            select(StrategyTrade)
            .where(StrategyTrade.origin == "VALIDATED_SIGNAL")
            .order_by(StrategyTrade.entry_time.desc())
        )
    )


def get_ai_origination_today_highlights(db: Session) -> dict[str, Any]:
    """Replaces the old SIGNAL-strategy activity feed (get_today_activity,
    removed 28 Aug 2026) now that AI Origination is the only thing actually
    running -- that feed only ever showed rule-based-strategy entry/exit
    lines ("[BNV7] Entered...") and had nothing to say once strategies
    stopped being used. Everything here is scoped to origin LIKE
    'AI_ORIGIN_%' and today (IST), and reads data already written on every
    origination cycle -- zero new computation, zero new SmartAPI calls.

    Four pieces:
    - index_comparison: today's closed-trade record per enabled index
      (trades/wins/losses/net P&L), so Bank Nifty and Nifty can be read
      head-to-head. Uses net_pnl (net of cost), not gross profit_loss --
      see compute_performance_kpis's own 28 Aug fix for why that matters.
    - funnel: how many decision cycles ran today, how many declined (NONE),
      how many wanted to trade but never got a trade_id (blocked by the
      confidence floor or a gate), how many actually opened. SLOT_OCCUPIED
      marker rows are excluded -- they're not real decisions, see the
      "Market Conditions panel froze" fix.
    - sharpest_call: today's best closed trade and its own ai_reasoning, or
      -- if nothing has closed yet today -- the single highest-confidence
      NONE decline, so there's always something to show once cycles have
      run.
    - near_misses: up to 5 most recent BUY_CE/BUY_PE decisions today that
      never opened a trade, newest first, with confidence and reasoning.
    """
    today = today_ist()

    logs_today = [
        row
        for row in db.scalars(
            select(AIOriginationLog).where(AIOriginationLog.timestamp >= utc_now() - timedelta(hours=30))
        )
        if to_ist(row.timestamp) is not None and to_ist(row.timestamp).date() == today
    ]
    real_decisions = [row for row in logs_today if row.decision != "SLOT_OCCUPIED"]
    wanted_to_trade = [row for row in real_decisions if row.decision in ("BUY_CE", "BUY_PE")]
    blocked_decisions = [row for row in wanted_to_trade if not row.trade_id]

    funnel = {
        "total_cycles": len(real_decisions),
        "declined": sum(1 for row in real_decisions if row.decision == "NONE"),
        "opened": sum(1 for row in wanted_to_trade if row.trade_id),
        "blocked": len(blocked_decisions),
        "errors": sum(1 for row in real_decisions if row.decision == "ERROR"),
    }

    closed_ai_today = [
        trade
        for trade in db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.origin.like("AI_ORIGIN_%"),
                StrategyTrade.status == TradeStatus.CLOSED,
            )
        )
        if to_ist(trade.exit_time) is not None and to_ist(trade.exit_time).date() == today
    ]

    index_comparison: list[dict[str, Any]] = []
    for index in db.scalars(select(IndexConfig).where(IndexConfig.enabled.is_(True)).order_by(IndexConfig.symbol)):
        trades = [trade for trade in closed_ai_today if trade.index_symbol == index.symbol]
        wins = sum(1 for trade in trades if trade.result == TradeResult.WIN)
        losses = sum(1 for trade in trades if trade.result == TradeResult.LOSS)
        total = len(trades)
        index_comparison.append(
            {
                "symbol": index.symbol,
                "display_name": index.display_name or index.symbol,
                "trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / total) * 100, 2) if total else 0.0,
                "net_pnl": round(sum(trade.net_pnl for trade in trades), 2),
            }
        )

    sharpest_call: dict[str, Any] | None = None
    if closed_ai_today:
        best = max(closed_ai_today, key=lambda trade: trade.pnl_percent)
        sharpest_call = {
            "kind": "trade",
            "index_display_name": _index_display_name(best.index_symbol),
            "strike": best.strike,
            "position_label": "Long Call" if best.option_type == "CE" else "Long Put",
            "pnl_percent": best.pnl_percent,
            "reasoning": best.ai_reasoning or "",
        }
    else:
        none_decisions = [row for row in real_decisions if row.decision == "NONE" and row.confidence is not None]
        if none_decisions:
            best_none = max(none_decisions, key=lambda row: row.confidence)
            sharpest_call = {
                "kind": "decline",
                "index_display_name": _index_display_name(best_none.index_name),
                "confidence": best_none.confidence,
                "reasoning": best_none.reasoning or "",
            }

    near_misses = sorted(blocked_decisions, key=lambda row: row.timestamp, reverse=True)[:5]
    near_miss_entries = [
        {
            "index_display_name": _index_display_name(row.index_name),
            "action": row.decision,
            "confidence": row.confidence,
            "reasoning": row.reasoning or "",
            "time_label": to_ist(row.timestamp).strftime("%I:%M %p") if to_ist(row.timestamp) else "",
        }
        for row in near_misses
    ]

    return {
        "funnel": funnel,
        "index_comparison": index_comparison,
        "sharpest_call": sharpest_call,
        "near_misses": near_miss_entries,
    }


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
