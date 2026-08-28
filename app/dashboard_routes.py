from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timezone
import json
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import reports
from app.auth import authenticate_admin, require_admin_api, require_admin_page
from app.ai.context_builder import SignalContextBuilder
from app.ai.factory import create_reviewer
from app.ai.repository import create_settings as create_ai_settings, get_settings as get_ai_settings, update_settings as update_ai_settings
from app.database import get_db
from app.db_models import BotStatus, IndexConfig, PlatformSettings, SLMode, StrategyConfig, StrategyTrade, StrategyTradeTick, TradeStatus, TradingMode
from app.platform import (
    compute_performance_kpis,
    get_ai_origination_today_highlights,
    get_dashboard_summary,
    get_index_live_figures,
    get_live_trading_status,
    get_market_conditions,
    get_open_trades_with_ticks,
    get_or_create_strategy_stats,
    get_or_create_settings,
    latest_logs,
    list_index_configs,
    log_event,
    origin_label,
    signal_strategy_names,
    strategy_metrics,
    strategy_trades_query_for_filter,
)
from sqlalchemy import func, select
from app.signal_validation import check_market_hours
from app.smartapi_client import SmartAPIError
from app.time_utils import IST, duration_label, format_ist, to_ist, utc_now
from app.trade_manager import TradeManager


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["ist"] = format_ist
templates.env.filters["duration"] = duration_label
templates.env.filters["origin_label"] = origin_label
router = APIRouter()


def get_trade_manager() -> TradeManager:
    return router.trade_manager  # type: ignore[attr-defined]


def get_smartapi() -> object:
    return router.smartapi  # type: ignore[attr-defined]


def get_live_feed_store() -> object:
    return router.live_feed_store  # type: ignore[attr-defined]


def get_health_manager() -> object:
    return router.health_manager  # type: ignore[attr-defined]


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_model=None)
def login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
    if authenticate_admin(username, password):
        request.session.clear()
        request.session["admin_authenticated"] = True
        request.session["admin_username"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"}, status_code=401)


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/smartapi-health", response_class=HTMLResponse)
def smartapi_health_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    health_manager: Annotated[object, Depends(get_health_manager)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "smartapi_health.html",
        {"request": request, "health": health_manager.latest(db)},
    )


# get_index_live_figures() used to make a real SmartAPI call per enabled
# index on every dashboard render (get_index_spot, throttled through the same
# process-wide 1 req/sec budget get_ltp/get_candles share -- see CLAUDE.md).
# A short TTL cache reduced that but didn't eliminate it: any request landing
# outside the TTL window still paid the SmartAPI cost, and under real
# afternoon dashboard traffic (7 Aug) that kept happening often enough to
# keep starving AI Origination's own candle-refresh calls. app/live_feed.py's
# persistent WebSocket feed replaces this entirely -- the price is already
# fresh in memory by the time any request arrives, so there's nothing left to
# cache. See CLAUDE.md, "Dashboard-driven SmartAPI rate exhaustion".
def _live_dashboard_data(db: Session, smartapi: object, live_feed_store: object) -> dict[str, object]:
    return {
        "indices": get_index_live_figures(db, smartapi, live_feed_store),
        "trades": get_open_trades_with_ticks(db),
        "today_highlights": get_ai_origination_today_highlights(db),
        # Pure DB read of what AI Origination already computed and persisted
        # on its own 5-min cycle (app/ai/origination_log.py) -- no new
        # SmartAPI calls, no new computation. See get_market_conditions.
        "conditions": get_market_conditions(db),
        # 14 Aug 2026: lets the frontend distinguish "market is genuinely
        # closed" from "the live feed is having a transient outage during
        # real trading hours" -- both render a per-index "stale" badge today,
        # but only the former should stop the top "Updated Xs ago" badge from
        # implying the page is watching something live. Cheap: one weekday/
        # holiday/hour check, no new SmartAPI call.
        "market_open": check_market_hours(utc_now()) is None,
    }


@router.get("/", response_class=HTMLResponse)
def live_dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    smartapi: Annotated[object, Depends(get_smartapi)],
    live_feed_store: Annotated[object, Depends(get_live_feed_store)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    data = _live_dashboard_data(db, smartapi, live_feed_store)
    return templates.TemplateResponse(
        "live_dashboard.html",
        {"request": request, **data},
    )


@router.get("/api/live-dashboard")
def live_dashboard_api(
    db: Annotated[Session, Depends(get_db)],
    smartapi: Annotated[object, Depends(get_smartapi)],
    live_feed_store: Annotated[object, Depends(get_live_feed_store)],
    _: Annotated[None, Depends(require_admin_api)] = None,
) -> dict[str, object]:
    return _live_dashboard_data(db, smartapi, live_feed_store)


@router.post("/health-check")
def run_health_check(
    health_manager: Annotated[object, Depends(get_health_manager)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    health_manager.run(notify=False)
    return RedirectResponse("/smartapi-health", status_code=303)


@router.get("/history", response_class=HTMLResponse)
def history(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    filter: str = "today",
    start: str | None = None,
    end: str | None = None,
    origin: str = "all",
    strategy: str = "",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    origin_filter = origin if origin in ("signal", "ai_origin") else None
    # The strategy sub-filter only makes sense paired with Signal Only --
    # AI trades don't have a real StrategyConfig-backed strategy name -- so a
    # stray ?strategy= on another origin is ignored rather than silently
    # filtering out every row.
    strategy_filter = strategy if origin == "signal" and strategy else None
    trades = list(db.scalars(
        strategy_trades_query_for_filter(filter, parse_date(start), parse_date(end), origin_filter, strategy_filter)
    ))
    # Net P&L in rupees across whatever filter/date-range/origin is currently
    # applied -- only closed trades have a real profit_loss (open trades default
    # to 0.0, which would understate nothing but also isn't a real realized
    # number yet, so they're excluded from this total on purpose).
    closed_trades = [trade for trade in trades if trade.status == TradeStatus.CLOSED]
    # KPI cards and equity/daily/win-loss charts -- formerly a standalone
    # /performance page (SIGNAL-only, unconditionally); folded in here and
    # generalized to reflect whichever origin/strategy filter is currently
    # selected, so the numbers always describe exactly the trades in the
    # table below them.
    performance = compute_performance_kpis(closed_trades)
    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "trades": trades,
            "filter": filter,
            "start": start or "",
            "end": end or "",
            "origin": origin,
            "strategy": strategy,
            "strategy_names": signal_strategy_names(db),
            "net_pnl_amount": performance["kpis"]["net_pnl_amount"],
            "closed_count": len(closed_trades),
            **performance,
        },
    )


@router.get("/history/export")
def history_export(
    db: Annotated[Session, Depends(get_db)],
    filter: str = "today",
    start: str | None = None,
    end: str | None = None,
    origin: str = "all",
    strategy: str = "",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> StreamingResponse:
    """CSV export of the Trade History table, honoring whatever filter/date-range/
    origin/strategy is currently applied on the page -- same query as the HTML
    view, just written out as a file instead of rendered."""
    origin_filter = origin if origin in ("signal", "ai_origin") else None
    strategy_filter = strategy if origin == "signal" and strategy else None
    trades = list(db.scalars(
        strategy_trades_query_for_filter(filter, parse_date(start), parse_date(end), origin_filter, strategy_filter)
    ))

    # MFE/MAE come from the 30-second premium samples in strategy_trade_ticks,
    # NOT from StrategyTrade.highest_price/lowest_price. Those two stored
    # columns feed the trailing-stop engine and are only maintained on the side
    # the trailing logic needs: for a long trade monitor_open_trades updates
    # highest_price and never touches lowest_price (it stays at its entry-time
    # seed value), and vice versa for shorts. So the stored low on a long trade
    # is not a real adverse excursion. The tick table has every sample for both
    # directions and backfills correctly for trades already closed.
    tick_extremes = {
        row.trade_id: (row.low, row.high)
        for row in db.execute(
            select(
                StrategyTradeTick.trade_id.label("trade_id"),
                func.min(StrategyTradeTick.premium).label("low"),
                func.max(StrategyTradeTick.premium).label("high"),
            ).group_by(StrategyTradeTick.trade_id)
        )
    }

    def _excursion(trade: StrategyTrade) -> tuple[str, str]:
        """(MFE %, MAE %) against entry, signed so favourable is always
        positive and adverse always negative regardless of long/short."""
        extremes = tick_extremes.get(trade.trade_id)
        if not extremes or not trade.entry_price:
            return "", ""
        low, high = extremes
        if low is None or high is None:
            return "", ""
        direction = -1 if trade.signal.startswith("SELL") else 1
        best = high if direction == 1 else low
        worst = low if direction == 1 else high
        mfe = ((best - trade.entry_price) / trade.entry_price) * 100 * direction
        mae = ((worst - trade.entry_price) / trade.entry_price) * 100 * direction
        return f"{mfe:.2f}", f"{mae:.2f}"

    def _trend_age(trade: StrategyTrade) -> tuple[str, str, str, str]:
        """Trend-age fields, read back out of the stored market context.

        Read from market_context_json rather than duplicated onto their own
        columns: the context is already persisted per trade in full, and a
        second copy would be one more thing that can silently disagree with
        the snapshot the decision was actually made on.

        Returns blanks for any trade whose context predates these fields --
        which is every trade before 5 Aug -- rather than zeros. A zero here
        would read as "brand new trend, no repeats", the opposite of unknown.
        """
        raw = trade.market_context_json
        if not raw:
            return "", "", "", ""
        try:
            context = json.loads(raw)
        except (TypeError, ValueError):
            return "", "", "", ""
        counts = context.get("same_direction_entries_today") or {}
        same_direction = ""
        if counts:
            # The count for THIS trade's own direction is the one that matters:
            # how many times this thesis had already been taken when it fired.
            same_direction = str(counts.get(trade.signal, ""))
        def _text(key: str) -> str:
            value = context.get(key)
            return "" if value is None else str(value)
        return (
            _text("trend_duration_bars"),
            _text("trend_duration_pct_of_session"),
            _text("move_extent_atr"),
            same_direction,
        )

    def _configured_percent(trade: StrategyTrade) -> tuple[str, str]:
        """(SL %, Target %) as actually configured at entry, recovered from the
        stored absolute stoploss/target levels. AI Origination sets these from
        the model's own proposal, so they vary per trade rather than being a
        single global setting."""
        if not trade.entry_price:
            return "", ""
        direction = -1 if trade.signal.startswith("SELL") else 1
        sl = ((trade.entry_price - trade.stoploss) / trade.entry_price) * 100 * direction
        target = ((trade.target - trade.entry_price) / trade.entry_price) * 100 * direction
        return f"{sl:.2f}", f"{target:.2f}"

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        # Existing columns -- unchanged order and labels so prior exports stay
        # directly comparable. All new fields are appended after these.
        "Strategy", "Origin", "Entry Time (IST)", "Exit Time (IST)", "Duration",
        "Signal", "Strike", "Entry", "Capital Invested (Rs)", "Exit", "P&L %", "P&L (Rs)", "Result", "Status", "Mode",
        # Costs -- P&L (Rs) above stays GROSS and unchanged; these are additive
        "Est. Cost (Rs)", "Net P&L (Rs)",
        # Tier 1 -- exit path and the risk band actually in force
        "Exit Reason", "SL Mode", "SL %", "Target %",
        # Tier 2 -- excursions
        "MFE %", "MAE %", "Premium High (stored)", "Premium Low (stored)",
        # Tier 3 -- model output at entry
        "AI Confidence", "AI Reasoning",
        # Tier 4 -- prompt-input quality (forward-only; blank for older trades)
        "Tick Samples", "Day OHLC Present", "Spot At Entry", "Expiry",
        # Risk in comparable units -- a percentage stop is not the same bet on a
        # CE as on a PE, since puts are 1.3-1.5x more index-sensitive
        "Stop (idx pts)", "Stop (ATR)", "Target (idx pts)", "Target (ATR)", "Risk Units Extrapolated",
        # Tier 6 -- data quality at entry (forward-only; blank for trades before
        # this column existed). YES means the candle refresh failed this cycle
        # and the entry decision was made on stale stored history rather than
        # a fresh pull -- see app/ai/originator.py's _load_market_context.
        "Data Stale",
        # Tier 7 -- correlated entries (forward-only). YES means the OTHER
        # provider opened the same strike and side within minutes, so the
        # account held two full-size positions on one thesis. Observation only;
        # nothing changes sizing. Also blank for trades before the column.
        "Correlated Entry", "Correlated With",
        # Trend age at entry, from the market context the decision was made on.
        # Recorded per trade rather than only in market_context_json so the
        # repeat-thesis pattern is filterable in a spreadsheet.
        "Trend Bars", "Trend % Session", "Move Since Trend Start (ATR)",
        "Same-Dir Entries Today",
    ])
    for trade in trades:
        mfe, mae = _excursion(trade)
        sl_percent, target_percent = _configured_percent(trade)
        writer.writerow([
            trade.strategy_name,
            origin_label(trade.origin),
            format_ist(trade.entry_time),
            format_ist(trade.exit_time),
            duration_label(trade.entry_time, trade.exit_time),
            trade.signal,
            trade.strike,
            trade.entry_price,
            f"{trade.investment_amount:.2f}" if trade.investment_amount is not None else "",
            trade.exit_price or "",
            f"{trade.pnl_percent:.2f}" if trade.pnl_percent is not None else "",
            f"{trade.profit_loss:.2f}" if trade.status == "CLOSED" else "",
            trade.result,
            trade.status,
            trade.mode,
            f"{trade.estimated_cost:.2f}" if trade.status == "CLOSED" else "",
            f"{trade.net_pnl:.2f}" if trade.status == "CLOSED" else "",
            trade.exit_reason or "",
            trade.sl_mode or "",
            sl_percent,
            target_percent,
            mfe,
            mae,
            trade.highest_price if trade.highest_price is not None else "",
            trade.lowest_price if trade.lowest_price is not None else "",
            f"{trade.ai_confidence:.4f}" if trade.ai_confidence is not None else "",
            (trade.ai_reasoning or "").replace("\n", " ").strip(),
            trade.tick_sample_count if trade.tick_sample_count is not None else "",
            "" if trade.day_ohlc_present is None else ("YES" if trade.day_ohlc_present else "NO"),
            trade.spot_at_entry if trade.spot_at_entry is not None else "",
            trade.expiry or "",
            trade.stop_index_points if trade.stop_index_points is not None else "",
            trade.stop_atr_multiple if trade.stop_atr_multiple is not None else "",
            trade.target_index_points if trade.target_index_points is not None else "",
            trade.target_atr_multiple if trade.target_atr_multiple is not None else "",
            "" if trade.risk_units_extrapolated is None else ("YES" if trade.risk_units_extrapolated else "NO"),
            "" if trade.data_stale is None else ("YES" if trade.data_stale else "NO"),
            "" if trade.concurrent_correlated_entry is None
            else ("YES" if trade.concurrent_correlated_entry else "NO"),
            trade.correlated_with_trade_id or "",
            *_trend_age(trade),
        ])
    buffer.seek(0)

    today_label = datetime.now(IST).strftime("%Y-%m-%d")
    filename = f"strikevault_trade_history_{filter}_{today_label}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/performance")
def performance_page() -> RedirectResponse:
    # Performance's KPIs/charts were folded into Trade History (15 Aug 2026)
    # rather than kept as a separate page -- same relocate-not-delete pattern
    # /strategies already uses for Settings > Strategies.
    return RedirectResponse("/history", status_code=307)


@router.get("/control", response_class=HTMLResponse)
def control_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    summary = get_dashboard_summary(db, router.trade_manager.get_active_trade())  # type: ignore[attr-defined]
    return templates.TemplateResponse("control.html", {"request": request, "summary": summary})


@router.get("/strategies", response_class=HTMLResponse)
def strategies_page() -> RedirectResponse:
    return RedirectResponse("/settings?tab=strategies", status_code=307)


@router.post("/strategies")
def create_strategy(
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    index_symbol: Annotated[str, Form()],
    expiry_itm_strikes: Annotated[int, Form()],
    tp_percent: Annotated[float, Form()],
    sl_percent: Annotated[float, Form()],
    sl_mode: Annotated[str, Form()],
    trailing_activation_percent: Annotated[float, Form()],
    trailing_offset_percent: Annotated[float, Form()],
    max_active_trades: Annotated[int, Form()],
    max_trades_per_day: Annotated[int, Form()],
    max_consecutive_losses: Annotated[int, Form()],
    daily_max_loss_percent: Annotated[float, Form()],
    lots_per_trade: Annotated[int, Form()],
    enabled: Annotated[str | None, Form()] = None,
    paper_trade: Annotated[str | None, Form()] = None,
    live_trade: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    if mode not in {TradingMode.PAPER, TradingMode.LIVE}:
        raise HTTPException(status_code=400, detail="Invalid trading mode")
    if sl_mode not in {SLMode.FIXED, SLMode.TRAILING}:
        raise HTTPException(status_code=400, detail="Invalid SL mode")
    if db.scalar(select(IndexConfig).where(IndexConfig.symbol == index_symbol)) is None:
        raise HTTPException(status_code=400, detail="Invalid index symbol")
    if lots_per_trade < 1:
        raise HTTPException(status_code=400, detail="Lots per trade must be at least 1")
    if expiry_itm_strikes not in (0, 1):
        raise HTTPException(status_code=400, detail="Expiry ITM strikes must be 0 or 1")
    strategy = StrategyConfig(
        name=name.strip(),
        enabled=enabled == "on",
        mode=mode,
        index_symbol=index_symbol,
        expiry_itm_strikes=expiry_itm_strikes,
        tp_percent=tp_percent,
        sl_percent=sl_percent,
        sl_mode=sl_mode,
        trailing_activation_percent=trailing_activation_percent,
        trailing_offset_percent=trailing_offset_percent,
        max_active_trades=max_active_trades,
        max_trades_per_day=max_trades_per_day,
        max_consecutive_losses=max_consecutive_losses,
        daily_max_loss_percent=daily_max_loss_percent,
        lots_per_trade=lots_per_trade,
        paper_trade=paper_trade == "on",
        live_trade=live_trade == "on",
    )
    db.add(strategy)
    db.commit()
    get_or_create_strategy_stats(db, strategy.name)
    log_event(db, "BOT", f"Strategy created: {strategy.name}")
    return RedirectResponse("/settings?tab=strategies", status_code=303)


@router.post("/strategies/{strategy_id}")
def update_strategy(
    strategy_id: int,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    index_symbol: Annotated[str, Form()],
    expiry_itm_strikes: Annotated[int, Form()],
    tp_percent: Annotated[float, Form()],
    sl_percent: Annotated[float, Form()],
    sl_mode: Annotated[str, Form()],
    trailing_activation_percent: Annotated[float, Form()],
    trailing_offset_percent: Annotated[float, Form()],
    max_active_trades: Annotated[int, Form()],
    max_trades_per_day: Annotated[int, Form()],
    max_consecutive_losses: Annotated[int, Form()],
    daily_max_loss_percent: Annotated[float, Form()],
    lots_per_trade: Annotated[int, Form()],
    enabled: Annotated[str | None, Form()] = None,
    paper_trade: Annotated[str | None, Form()] = None,
    live_trade: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    strategy = db.get(StrategyConfig, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if sl_mode not in {SLMode.FIXED, SLMode.TRAILING}:
        raise HTTPException(status_code=400, detail="Invalid SL mode")
    if db.scalar(select(IndexConfig).where(IndexConfig.symbol == index_symbol)) is None:
        raise HTTPException(status_code=400, detail="Invalid index symbol")
    if lots_per_trade < 1:
        raise HTTPException(status_code=400, detail="Lots per trade must be at least 1")
    if expiry_itm_strikes not in (0, 1):
        raise HTTPException(status_code=400, detail="Expiry ITM strikes must be 0 or 1")
    strategy.name = name.strip()
    strategy.enabled = enabled == "on"
    strategy.mode = mode
    strategy.index_symbol = index_symbol
    strategy.expiry_itm_strikes = expiry_itm_strikes
    strategy.tp_percent = tp_percent
    strategy.sl_percent = sl_percent
    strategy.sl_mode = sl_mode
    strategy.trailing_activation_percent = trailing_activation_percent
    strategy.trailing_offset_percent = trailing_offset_percent
    strategy.max_active_trades = max_active_trades
    strategy.max_trades_per_day = max_trades_per_day
    strategy.max_consecutive_losses = max_consecutive_losses
    strategy.daily_max_loss_percent = daily_max_loss_percent
    strategy.lots_per_trade = lots_per_trade
    strategy.paper_trade = paper_trade == "on"
    strategy.live_trade = live_trade == "on"
    db.commit()
    get_or_create_strategy_stats(db, strategy.name)
    log_event(db, "BOT", f"Strategy updated: {strategy.name}")
    return RedirectResponse("/settings?tab=strategies", status_code=303)


@router.post("/instruments/{index_id}")
def update_instrument(
    index_id: int,
    db: Annotated[Session, Depends(get_db)],
    display_name: Annotated[str, Form()],
    exchange_segment: Annotated[str, Form()],
    instrument_name: Annotated[str, Form()],
    spot_exchange: Annotated[str, Form()],
    spot_symbol: Annotated[str, Form()],
    spot_token: Annotated[str, Form()],
    lot_size: Annotated[int, Form()],
    strike_interval: Annotated[int, Form()],
    enabled: Annotated[str | None, Form()] = None,
    ai_origination_live_trade: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    index = db.get(IndexConfig, index_id)
    if index is None:
        raise HTTPException(status_code=404, detail="Index not found")
    if enabled == "on" and not spot_token.strip():
        raise HTTPException(status_code=400, detail="Cannot enable an index without a spot token")
    index.display_name = display_name.strip()
    index.exchange_segment = exchange_segment.strip().upper()
    index.instrument_name = instrument_name.strip().upper()
    index.spot_exchange = spot_exchange.strip().upper()
    index.spot_symbol = spot_symbol.strip()
    index.spot_token = spot_token.strip()
    index.lot_size = lot_size
    index.strike_interval = strike_interval
    index.enabled = enabled == "on"
    index.ai_origination_live_trade = ai_origination_live_trade == "on"
    db.commit()
    log_event(db, "BOT", f"Index config updated: {index.symbol}")
    return RedirectResponse("/settings?tab=instruments", status_code=303)


@router.post("/strategies/{strategy_id}/delete")
def delete_strategy(
    strategy_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    strategy = db.get(StrategyConfig, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    db.delete(strategy)
    db.commit()
    log_event(db, "BOT", f"Strategy deleted: {strategy.name}")
    return RedirectResponse("/settings?tab=strategies", status_code=303)


def _settings_context(db: Session, smartapi: object, tab: str) -> dict[str, object]:
    """Shared context for every /settings render (the plain GET, and the two
    AI-connection-test POSTs that re-render the page with a result banner) --
    factored out so the AI tab's settings/live-trading data doesn't need
    gathering three separate times now that AI Settings is a Settings tab
    rather than its own page."""
    strategies = list(db.scalars(select(StrategyConfig).order_by(StrategyConfig.name)))
    metrics = strategy_metrics(db)
    return {
        "settings": get_or_create_settings(db),
        "tab": tab,
        "strategies": strategies,
        "metrics_by_id": {item["strategy"].id: item for item in metrics},
        "indexes": list_index_configs(db),
        "ai_settings": get_ai_settings(db) or create_ai_settings(db, id=1),
        "live_trading": get_live_trading_status(db, smartapi),
        "ai_test_result": None,
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    smartapi: Annotated[object, Depends(get_smartapi)],
    tab: str = "general",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    if tab not in {"general", "notifications", "strategies", "instruments", "ai"}:
        tab = "general"
    return templates.TemplateResponse("settings.html", {"request": request, **_settings_context(db, smartapi, tab)})


def _parse_hhmm_strict(value: str) -> tuple[int, int] | None:
    """Like time_utils.parse_hhmm but returns None on anything malformed
    instead of silently falling back -- admin form input should be rejected,
    not quietly coerced."""
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


@router.post("/settings")
def update_settings_page(
    db: Annotated[Session, Depends(get_db)],
    square_off_time: Annotated[str, Form()],
    trading_start_time: Annotated[str, Form()] = "09:45",
    telegram_bot_token: Annotated[str, Form()] = "",
    telegram_chat_id: Annotated[str, Form()] = "",
    active_tab: Annotated[str, Form()] = "general",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    start_hm = _parse_hhmm_strict(trading_start_time)
    end_hm = _parse_hhmm_strict(square_off_time)
    # Bounds match this app's own established trading-day facts rather than
    # arbitrary ones: 09:15 is the NSE open (app/signal_validation.py's
    # NSE_HOLIDAYS/check_market_hours window), and 15:15 is the ceiling
    # because app/scheduler.py's daily-square-off safety-net cron is still
    # fixed at 15:15 -- a configured close later than that would let the cron
    # force-close trades before the admin's own intended closing time.
    if (
        start_hm is None
        or end_hm is None
        or start_hm < (9, 15)
        or end_hm > (15, 15)
        or start_hm >= end_hm
    ):
        raise HTTPException(status_code=400, detail="Invalid trading window: start/close must be HH:MM, 09:15 <= start < close <= 15:15")
    settings = get_or_create_settings(db)
    apply_settings(
        settings,
        square_off_time,
        trading_start_time,
        telegram_bot_token,
        telegram_chat_id,
    )
    db.commit()
    log_event(db, "BOT", "Settings updated from dashboard")
    if active_tab not in {"general", "notifications"}:
        active_tab = "general"
    return RedirectResponse(f"/settings?tab={active_tab}", status_code=303)


@router.get("/ai-settings")
def ai_settings_page() -> RedirectResponse:
    # AI Settings became a Settings tab (15 Aug 2026) rather than its own
    # page -- same relocate-not-delete pattern /strategies already uses.
    return RedirectResponse("/settings?tab=ai", status_code=307)


@router.post("/ai-settings")
def update_ai_settings_page(
    db: Annotated[Session, Depends(get_db)],
    mode: Annotated[str, Form()],
    provider: Annotated[str, Form()],
    model: Annotated[str, Form()],
    api_key: Annotated[str, Form()],
    base_url: Annotated[str, Form()],
    temperature: Annotated[float, Form()],
    timeout_seconds: Annotated[int, Form()],
    confidence_threshold: Annotated[int, Form()],
    system_prompt: Annotated[str, Form()],
    ai_origination_max_sl_percent: Annotated[float, Form()] = 50.0,
    ai_origination_max_same_direction_losses: Annotated[int, Form()] = 2,
    ai_origination_trail_activate_percent: Annotated[float, Form()] = 8.0,
    ai_origination_chop_gate_min_efficiency_ratio: Annotated[float, Form()] = 0.3,
    enabled: Annotated[str | None, Form()] = None,
    ai_origination_chop_gate_enabled: Annotated[str | None, Form()] = None,
    secondary_enabled: Annotated[str | None, Form()] = None,
    secondary_provider: Annotated[str, Form()] = "claude",
    secondary_model: Annotated[str, Form()] = "",
    secondary_api_key: Annotated[str, Form()] = "",
    secondary_base_url: Annotated[str, Form()] = "",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    valid_providers = {"dummy", "openai", "claude"}
    if (
        mode not in {"DISABLED", "SHADOW", "ADVISORY", "BLOCKING"}
        or provider not in valid_providers
        or secondary_provider not in valid_providers
        or not 0 <= temperature <= 2
        or timeout_seconds < 1
        or not 0 <= confidence_threshold <= 100
        # 5.0 mirrors app/ai/originator.py's _MIN_SL_TARGET_PERCENT floor --
        # duplicated rather than imported to avoid a dashboard_routes ->
        # originator import for one constant.
        or not 5.0 < ai_origination_max_sl_percent <= 100
        or ai_origination_max_same_direction_losses < 1
        or not 0.5 <= ai_origination_trail_activate_percent <= 50
        or not 0.0 <= ai_origination_chop_gate_min_efficiency_ratio <= 1.0
    ):
        raise HTTPException(status_code=400, detail="Invalid AI configuration")
    settings = get_ai_settings(db) or create_ai_settings(db, id=1)
    values = {
        "enabled": enabled == "on",
        "mode": mode,
        "provider": provider,
        "model": model.strip(),
        "base_url": base_url.strip(),
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        "confidence_threshold": confidence_threshold,
        "system_prompt": system_prompt,
        "ai_origination_max_sl_percent": ai_origination_max_sl_percent,
        "ai_origination_max_same_direction_losses": ai_origination_max_same_direction_losses,
        "ai_origination_trail_activate_percent": ai_origination_trail_activate_percent,
        "ai_origination_chop_gate_enabled": ai_origination_chop_gate_enabled == "on",
        "ai_origination_chop_gate_min_efficiency_ratio": ai_origination_chop_gate_min_efficiency_ratio,
        "secondary_enabled": secondary_enabled == "on",
        "secondary_provider": secondary_provider,
        "secondary_model": secondary_model.strip(),
        "secondary_base_url": secondary_base_url.strip(),
    }
    if api_key:
        values["api_key"] = api_key
    if secondary_api_key:
        values["secondary_api_key"] = secondary_api_key
    update_ai_settings(db, settings, **values)
    return RedirectResponse("/settings?tab=ai", status_code=303)


@router.post("/ai-settings/test", response_class=HTMLResponse)
def test_ai_settings(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    smartapi: Annotated[object, Depends(get_smartapi)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    settings = get_ai_settings(db) or create_ai_settings(db, id=1)
    result = create_reviewer(settings).analyze_signal(
        SignalContextBuilder().build("CONNECTION_TEST", "TEST", datetime.now(timezone.utc))
    )
    test_result = {
        "provider": settings.provider,
        "model": settings.model,
        "latency": result.latency_ms,
        "status": "ERROR" if result.decision == "ERROR" else "OK",
        "error": result.summary if result.decision == "ERROR" else "",
    }
    context = _settings_context(db, smartapi, "ai")
    context["ai_test_result"] = test_result
    return templates.TemplateResponse("settings.html", {"request": request, **context})


@router.post("/ai-settings/test-secondary", response_class=HTMLResponse)
def test_secondary_ai_settings(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    smartapi: Annotated[object, Depends(get_smartapi)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    from types import SimpleNamespace

    settings = get_ai_settings(db) or create_ai_settings(db, id=1)
    secondary_settings = SimpleNamespace(
        provider=settings.secondary_provider,
        model=settings.secondary_model,
        api_key=settings.secondary_api_key,
        base_url=settings.secondary_base_url,
        temperature=settings.temperature,
        timeout_seconds=settings.timeout_seconds,
        system_prompt=settings.system_prompt,
    )
    result = create_reviewer(secondary_settings).analyze_signal(
        SignalContextBuilder().build("CONNECTION_TEST", "TEST", datetime.now(timezone.utc))
    )
    test_result = {
        "provider": secondary_settings.provider,
        "model": secondary_settings.model,
        "latency": result.latency_ms,
        "status": "ERROR" if result.decision == "ERROR" else "OK",
        "error": result.summary if result.decision == "ERROR" else "",
        "secondary": True,
    }
    context = _settings_context(db, smartapi, "ai")
    context["ai_test_result"] = test_result
    return templates.TemplateResponse("settings.html", {"request": request, **context})


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    report_type: str = "",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    report_rows = [
        {"report": report, "stats": _context_json(report.stats_json)}
        for report in reports.list_reports(db, report_type)
    ]
    return templates.TemplateResponse(
        "reports.html",
        {
            "request": request,
            "reports": report_rows,
            "report_type": report_type,
        },
    )


@router.post("/reports/daily/generate")
def generate_daily_report_now(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    reports.generate_daily_summary(db)
    return RedirectResponse("/reports?report_type=DAILY", status_code=303)


@router.post("/reports/weekly/generate")
def generate_weekly_report_now(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    reports.generate_weekly_report(db)
    return RedirectResponse("/reports?report_type=WEEKLY", status_code=303)


@router.post("/reports/monthly/generate")
def generate_monthly_report_now(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    reports.generate_monthly_report(db)
    return RedirectResponse("/reports?report_type=MONTHLY", status_code=303)


@router.post("/reports/pattern/generate")
def generate_pattern_report_now(
    db: Annotated[Session, Depends(get_db)],
    lookback_days: Annotated[str, Form()] = "90",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    normalized = lookback_days.strip().lower()
    days = None if normalized in {"", "all", "0"} else int(normalized)
    reports.generate_pattern_discovery(db, days)
    return RedirectResponse("/reports?report_type=PATTERN", status_code=303)


@router.post("/reports/origination/generate")
def generate_origination_report_now(
    db: Annotated[Session, Depends(get_db)],
    lookback_days: Annotated[str, Form()] = "30",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    normalized = lookback_days.strip().lower()
    days = None if normalized in {"", "all", "0"} else int(normalized)
    reports.generate_origination_summary(db, days)
    return RedirectResponse("/reports?report_type=ORIGINATION", status_code=303)


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    return templates.TemplateResponse("logs.html", {"request": request, "logs": latest_logs(db, 200)})


def _context_json(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value) if value else {}
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def apply_settings(
    settings: PlatformSettings,
    square_off_time: str,
    trading_start_time: str,
    telegram_bot_token: str,
    telegram_chat_id: str,
) -> None:
    settings.square_off_time = square_off_time
    settings.trading_start_time = trading_start_time
    settings.telegram_bot_token = telegram_bot_token
    settings.telegram_chat_id = telegram_chat_id





