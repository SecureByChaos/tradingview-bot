from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import authenticate_admin, require_admin_page
from app.database import get_db
from app.db_models import BotStatus, PlatformSettings, TradingMode
from app.platform import (
    get_dashboard_summary,
    get_or_create_settings,
    latest_logs,
    log_event,
    trades_query_for_filter,
)
from app.smartapi_client import SmartAPIError
from app.trade_manager import TradeManager

templates = Jinja2Templates(directory="app/templates")
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
    return templates.TemplateResponse("dashboard.html", {"request": request, "summary": summary, "logs": latest_logs(db, 20)})


@router.get("/active-trade-page", response_class=HTMLResponse)
def active_trade_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    trade_manager: Annotated[TradeManager, Depends(get_trade_manager)],
    smartapi: Annotated[object, Depends(get_smartapi)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    trade = trade_manager.get_active_trade()
    current_premium = None
    live_pnl = None
    if trade is not None:
        try:
            current_premium = smartapi.get_ltp(trade.contract.exchange, trade.contract.tradingsymbol, trade.contract.symboltoken)
            live_pnl = round(((current_premium - trade.entry_price) / trade.entry_price) * 100, 2)
        except Exception:
            current_premium = None
    return templates.TemplateResponse(
        "active_trade.html",
        {"request": request, "trade": trade, "current_premium": current_premium, "live_pnl": live_pnl},
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
    trades = list(db.scalars(trades_query_for_filter(filter, parse_date(start), parse_date(end))))
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
