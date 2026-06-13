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
from app.db_models import StrategyTrade, TradeStatus
from app.models import WebhookPayload, WebhookResponse
from app.multi_strategy import MultiStrategyTradeManager
from app.multi_strategy_monitor import MultiStrategyMonitor
from app.option_finder import OptionFinder
from app.platform import get_or_create_settings, log_event, serialize_strategy_trade, strategy_trades_query_for_filter, trading_allowed
from sqlalchemy import select
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
multi_strategy_manager = MultiStrategyTradeManager(settings, smartapi, option_finder, telegram)
risk_service = RiskProtectionService(multi_strategy_manager, telegram)
monitor = MultiStrategyMonitor(multi_strategy_manager, risk_service)
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
api_routes.router.multi_strategy_manager = multi_strategy_manager  # type: ignore[attr-defined]
api_routes.router.telegram = telegram  # type: ignore[attr-defined]
api_routes.router.scheduler = scheduler  # type: ignore[attr-defined]
api_routes.router.trade_logger = trade_logger  # type: ignore[attr-defined]
dashboard_routes.router.trade_manager = trade_manager  # type: ignore[attr-defined]
dashboard_routes.router.multi_strategy_manager = multi_strategy_manager  # type: ignore[attr-defined]
dashboard_routes.router.smartapi = smartapi  # type: ignore[attr-defined]

app.include_router(dashboard_routes.router)
app.include_router(api_routes.router)


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "live_trading": settings.live_trading}


@app.get("/active-trade")
def active_trade(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    trades = db.scalars(select(StrategyTrade).where(StrategyTrade.status == TradeStatus.OPEN).order_by(StrategyTrade.entry_time.desc()))
    return [serialize_strategy_trade(trade) for trade in trades]


@app.get("/trades")
def trades(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [serialize_strategy_trade(trade) for trade in db.scalars(strategy_trades_query_for_filter("30d", None, None))]


@app.post("/webhook", response_model=WebhookResponse)
def webhook(payload: WebhookPayload, db: Session = Depends(get_db)) -> WebhookResponse:
    try:
        allowed, message = trading_allowed(db)
        strategy_name = payload.strategy or settings.default_strategy_name
        log_event(db, "WEBHOOK", f"[{strategy_name}] Webhook received: {payload.signal.value}")
        if not allowed:
            log_event(db, "WEBHOOK", f"Webhook ignored: {message}", "WARNING")
            return WebhookResponse(accepted=False, message=message)
        response = multi_strategy_manager.handle_signal(db, payload.strategy, payload.signal)
        if response.accepted:
            telegram.send(db, f"Trade Opened\n[{strategy_name}] {payload.signal.value}")
        else:
            log_event(db, "WEBHOOK", f"Signal ignored: {response.message}", "WARNING")
        return response
    except Exception as exc:
        logger.exception("Webhook processing failed")
        log_event(db, "ERROR", "Webhook processing failed", "ERROR", {"error": str(exc)})
        telegram.send(db, f"System Error\nWebhook processing failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
