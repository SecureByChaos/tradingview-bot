from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import authenticate_admin, require_admin_page
from app.ai.context_builder import SignalContextBuilder
from app.ai.factory import create_reviewer
from app.ai.repository import create_settings as create_ai_settings, get_settings as get_ai_settings, update_settings as update_ai_settings
from app.database import get_db
from app.db_models import BotStatus, PlatformSettings, StrategyConfig, StrategyTrade, TradeStatus, TradingMode
from app.platform import (
    get_dashboard_summary,
    get_or_create_strategy_stats,
    get_or_create_settings,
    latest_logs,
    log_event,
    strategy_metrics,
    strategy_trades_query_for_filter,
)
from sqlalchemy import select
from app.smartapi_client import SmartAPIError
from app.time_utils import duration_label, format_ist
from app.trade_manager import TradeManager

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["ist"] = format_ist
templates.env.filters["duration"] = duration_label
router = APIRouter()


def get_trade_manager() -> TradeManager:
    return router.trade_manager  # type: ignore[attr-defined]


def get_smartapi() -> object:
    return router.smartapi  # type: ignore[attr-defined]


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


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    trade_manager: Annotated[TradeManager, Depends(get_trade_manager)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    summary = get_dashboard_summary(db, trade_manager.get_active_trade())
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "summary": summary, "logs": latest_logs(db, 20), "strategy_metrics": strategy_metrics(db)},
    )


@router.get("/active-trade-page", response_class=HTMLResponse)
def active_trade_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    trade_manager: Annotated[TradeManager, Depends(get_trade_manager)],
    smartapi: Annotated[object, Depends(get_smartapi)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    open_trades = list(db.scalars(select(StrategyTrade).where(StrategyTrade.status == TradeStatus.OPEN).order_by(StrategyTrade.entry_time.desc())))
    for trade in open_trades:
        try:
            current_premium = smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            trade.current_premium = round(current_premium, 2)
            trade.pnl_percent = round(((current_premium - trade.entry_price) / trade.entry_price) * 100, 2)
        except Exception:
            pass
    return templates.TemplateResponse(
        "active_trade.html",
        {"request": request, "trades": open_trades},
    )


@router.get("/history", response_class=HTMLResponse)
def history(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    filter: str = "today",
    start: str | None = None,
    end: str | None = None,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    trades = list(db.scalars(strategy_trades_query_for_filter(filter, parse_date(start), parse_date(end))))
    return templates.TemplateResponse(
        "history.html",
        {"request": request, "trades": trades, "filter": filter, "start": start or "", "end": end or ""},
    )


@router.get("/control", response_class=HTMLResponse)
def control_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    summary = get_dashboard_summary(db, router.trade_manager.get_active_trade())  # type: ignore[attr-defined]
    return templates.TemplateResponse("control.html", {"request": request, "summary": summary})


@router.get("/strategies", response_class=HTMLResponse)
def strategies_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    strategies = list(db.scalars(select(StrategyConfig).order_by(StrategyConfig.name)))
    metrics = strategy_metrics(db)
    return templates.TemplateResponse(
        "strategies.html",
        {"request": request, "strategies": strategies, "metrics": metrics, "metrics_by_id": {item["strategy"].id: item for item in metrics}},
    )


@router.post("/strategies")
def create_strategy(
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    tp_percent: Annotated[float, Form()],
    sl_percent: Annotated[float, Form()],
    max_active_trades: Annotated[int, Form()],
    capital_per_trade: Annotated[float, Form()],
    enabled: Annotated[str | None, Form()] = None,
    paper_trade: Annotated[str | None, Form()] = None,
    live_trade: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    if mode not in {TradingMode.PAPER, TradingMode.LIVE}:
        raise HTTPException(status_code=400, detail="Invalid trading mode")
    strategy = StrategyConfig(
        name=name.strip(),
        enabled=enabled == "on",
        mode=mode,
        tp_percent=tp_percent,
        sl_percent=sl_percent,
        max_active_trades=max_active_trades,
        capital_per_trade=capital_per_trade,
        paper_trade=paper_trade == "on",
        live_trade=live_trade == "on",
    )
    db.add(strategy)
    db.commit()
    get_or_create_strategy_stats(db, strategy.name)
    log_event(db, "BOT", f"Strategy created: {strategy.name}")
    return RedirectResponse("/strategies", status_code=303)


@router.post("/strategies/{strategy_id}")
def update_strategy(
    strategy_id: int,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    tp_percent: Annotated[float, Form()],
    sl_percent: Annotated[float, Form()],
    max_active_trades: Annotated[int, Form()],
    capital_per_trade: Annotated[float, Form()],
    enabled: Annotated[str | None, Form()] = None,
    paper_trade: Annotated[str | None, Form()] = None,
    live_trade: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    strategy = db.get(StrategyConfig, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    strategy.name = name.strip()
    strategy.enabled = enabled == "on"
    strategy.mode = mode
    strategy.tp_percent = tp_percent
    strategy.sl_percent = sl_percent
    strategy.max_active_trades = max_active_trades
    strategy.capital_per_trade = capital_per_trade
    strategy.paper_trade = paper_trade == "on"
    strategy.live_trade = live_trade == "on"
    db.commit()
    get_or_create_strategy_stats(db, strategy.name)
    log_event(db, "BOT", f"Strategy updated: {strategy.name}")
    return RedirectResponse("/strategies", status_code=303)


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
    return RedirectResponse("/strategies", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    return templates.TemplateResponse("settings.html", {"request": request, "settings": get_or_create_settings(db)})


@router.post("/settings")
def update_settings_page(
    db: Annotated[Session, Depends(get_db)],
    trading_mode: Annotated[str, Form()],
    stop_loss_percent: Annotated[float, Form()],
    target_percent: Annotated[float, Form()],
    max_trades_per_day: Annotated[int, Form()],
    max_consecutive_losses: Annotated[int, Form()],
    daily_max_loss_percent: Annotated[float, Form()],
    square_off_time: Annotated[str, Form()],
    telegram_bot_token: Annotated[str, Form()] = "",
    telegram_chat_id: Annotated[str, Form()] = "",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    settings = get_or_create_settings(db)
    apply_settings(
        settings,
        trading_mode,
        stop_loss_percent,
        target_percent,
        max_trades_per_day,
        max_consecutive_losses,
        daily_max_loss_percent,
        square_off_time,
        telegram_bot_token,
        telegram_chat_id,
    )
    db.commit()
    log_event(db, "BOT", "Settings updated from dashboard")
    return RedirectResponse("/settings", status_code=303)


@router.get("/ai-settings", response_class=HTMLResponse)
def ai_settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    settings = get_ai_settings(db) or create_ai_settings(db, id=1)
    return templates.TemplateResponse("ai_settings.html", {"request": request, "settings": settings, "test_result": None})


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
    enabled: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    if (
        mode not in {"DISABLED", "SHADOW", "ADVISORY", "BLOCKING"}
        or provider not in {"dummy", "openai"}
        or not 0 <= temperature <= 2
        or timeout_seconds < 1
        or not 0 <= confidence_threshold <= 100
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
    }
    if api_key:
        values["api_key"] = api_key
    update_ai_settings(db, settings, **values)
    return RedirectResponse("/ai-settings", status_code=303)


@router.post("/ai-settings/test", response_class=HTMLResponse)
def test_ai_settings(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
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
    return templates.TemplateResponse("ai_settings.html", {"request": request, "settings": settings, "test_result": test_result})


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    return templates.TemplateResponse("logs.html", {"request": request, "logs": latest_logs(db, 200)})


def apply_settings(
    settings: PlatformSettings,
    trading_mode: str,
    stop_loss_percent: float,
    target_percent: float,
    max_trades_per_day: int,
    max_consecutive_losses: int,
    daily_max_loss_percent: float,
    square_off_time: str,
    telegram_bot_token: str,
    telegram_chat_id: str,
) -> None:
    if trading_mode not in {TradingMode.PAPER, TradingMode.LIVE}:
        raise HTTPException(status_code=400, detail="Invalid trading mode")
    settings.trading_mode = trading_mode
    settings.stop_loss_percent = stop_loss_percent
    settings.target_percent = target_percent
    settings.max_trades_per_day = max_trades_per_day
    settings.max_consecutive_losses = max_consecutive_losses
    settings.daily_max_loss_percent = daily_max_loss_percent
    settings.square_off_time = square_off_time
    settings.telegram_bot_token = telegram_bot_token
    settings.telegram_chat_id = telegram_chat_id
