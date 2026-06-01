from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import api_routes, dashboard_routes
from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.logger import TradeCSVLogger, configure_logging
from app.models import ActiveTrade, WebhookPayload, WebhookResponse
from app.option_finder import OptionFinder
from app.platform import get_or_create_settings, log_event, trading_allowed
from app.platform_monitor import PlatformTradeMonitor
from app.risk import RiskProtectionService
from app.scheduler import create_scheduler
from app.smartapi_client import SmartAPIClient
from app.telegram_service import TelegramService
from app.trade_manager import TradeManager

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

trade_logger = TradeCSVLogger(settings.trades_csv_path)
smartapi = SmartAPIClient(settings)
option_finder = OptionFinder(settings, smartapi)
trade_manager = TradeManager(settings, smartapi, option_finder, trade_logger)
telegram = TelegramService()
risk_service = RiskProtectionService(trade_manager, telegram)
monitor = PlatformTradeMonitor(trade_manager, trade_logger, telegram, risk_service)
scheduler = create_scheduler(monitor)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        get_or_create_settings(db)
        log_event(db, "BOT", "Application startup")
    try:
        smartapi.authenticate()
    except Exception:
        logger.exception("SmartAPI authentication failed during startup")
        if settings.live_trading:
            raise
    scheduler.start()
    logger.info("Scheduler started")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


app = FastAPI(
    title="BankNifty Trading Bot",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    session_cookie="banknifty_admin_session",
    https_only=settings.secure_cookies,
    same_site="lax",
    max_age=60 * 60 * 8,
)

api_routes.router.trade_manager = trade_manager  # type: ignore[attr-defined]
api_routes.router.telegram = telegram  # type: ignore[attr-defined]
api_routes.router.scheduler = scheduler  # type: ignore[attr-defined]
api_routes.router.trade_logger = trade_logger  # type: ignore[attr-defined]
dashboard_routes.router.trade_manager = trade_manager  # type: ignore[attr-defined]
dashboard_routes.router.smartapi = smartapi  # type: ignore[attr-defined]

app.include_router(dashboard_routes.router)
app.include_router(api_routes.router)


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "live_trading": settings.live_trading}


@app.get("/active-trade", response_model=ActiveTrade | None)
def active_trade() -> ActiveTrade | None:
    return trade_manager.get_active_trade()


@app.get("/trades")
def trades() -> list[dict[str, str]]:
    return trade_logger.read_all()


@app.post("/webhook", response_model=WebhookResponse)
def webhook(payload: WebhookPayload, db: Session = Depends(get_db)) -> WebhookResponse:
    try:
        allowed, message = trading_allowed(db)
        log_event(db, "WEBHOOK", f"Webhook received: {payload.signal.value}")
        if not allowed:
            log_event(db, "WEBHOOK", f"Webhook ignored: {message}", "WARNING")
            return WebhookResponse(accepted=False, message=message)
        response = trade_manager.handle_signal(payload.signal)
        if response.accepted:
            log_event(db, "TRADE", f"Trade opened: {payload.signal.value}", payload=response.active_trade.model_dump() if response.active_trade else {})
            telegram.send(db, f"Trade Opened\nSignal: {payload.signal.value}")
        else:
            log_event(db, "WEBHOOK", f"Signal ignored: {response.message}", "WARNING")
        return response
    except Exception as exc:
        logger.exception("Webhook processing failed")
        log_event(db, "ERROR", "Webhook processing failed", "ERROR", {"error": str(exc)})
        telegram.send(db, f"System Error\nWebhook processing failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
