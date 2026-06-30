from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import api_routes, dashboard_routes, v7_router
from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.logger import TradeCSVLogger, configure_logging
from app.db_models import StrategyConfig, StrategyTrade, TradeResult, TradeStatus
from app.models import WebhookPayload, WebhookResponse
from app.multi_strategy import MultiStrategyTradeManager
from app.multi_strategy_monitor import MultiStrategyMonitor
from app.option_finder import OptionFinder
from app.platform import get_or_create_settings, get_or_create_state, get_or_create_strategy_stats, log_event, rebuild_daily_stats, reset_daily_risk_if_needed, serialize_strategy_trade, strategy_trades_query_for_filter, today_ist, trading_allowed
from sqlalchemy import select
from app.risk import RiskProtectionService
from app.scheduler import create_scheduler
from app.smartapi_client import SmartAPIClient
from app.telegram_service import TelegramService
from app.time_utils import utc_now
from app.trade_manager import TradeManager
from app.v7_manager import V7Manager

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
v7_manager = V7Manager(settings, smartapi, option_finder, telegram)
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
v7_router.router.v7_manager = v7_manager  # type: ignore[attr-defined]

app.include_router(dashboard_routes.router)
app.include_router(api_routes.router)
app.include_router(v7_router.router)


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
        reset_daily_risk_if_needed(db)
        strategy_name = (payload.strategy or settings.default_strategy_name).strip()
        log_event(db, "WEBHOOK", f"[{strategy_name}] Webhook received: {payload.signal.value}")
        if strategy_name.upper() == "V7":
            return v7_manager.handle_signal(db, payload.signal)

        strategy = db.scalar(select(StrategyConfig).where(StrategyConfig.name == strategy_name))
        if strategy is None:
            message = f"Rejected: strategy '{strategy_name}' does not exist"
            log_event(db, "WEBHOOK", message, "WARNING")
            return WebhookResponse(accepted=False, message=message)

        strategy_stats = get_or_create_strategy_stats(db, strategy.name)
        if strategy_stats.risk_locked:
            message = f"Strategy {strategy.name} locked due to consecutive losses"
            log_event(db, "WEBHOOK", message, "WARNING")
            return WebhookResponse(accepted=False, message=message)

        platform_settings = get_or_create_settings(db)
        state = get_or_create_state(db)
        today_stats = rebuild_daily_stats(db, today_ist())
        if state.risk_locked or today_stats.risk_locked or today_stats.pnl_percent <= platform_settings.daily_max_loss_percent:
            message = "Global risk lock active"
            log_event(db, "WEBHOOK", message, "WARNING")
            return WebhookResponse(accepted=False, message=message)

        allowed, message = trading_allowed(db)
        if not allowed:
            log_event(db, "WEBHOOK", f"Webhook ignored: {message}", "WARNING")
            return WebhookResponse(accepted=False, message=message)

        recent_loss = db.scalar(
            select(StrategyTrade.id)
            .where(
                StrategyTrade.strategy_name == strategy_name,
                StrategyTrade.result == TradeResult.LOSS,
                StrategyTrade.exit_time.is_not(None),
                StrategyTrade.exit_time >= utc_now() - timedelta(minutes=30),
            )
            .limit(1)
        )
        if recent_loss is not None:
            message = "Signal rejected due to cooldown after recent loss."
            log_event(db, "WEBHOOK", message, "WARNING")
            return WebhookResponse(accepted=False, message=message)

        response = multi_strategy_manager.handle_signal(db, strategy_name, payload.signal)
        if response.accepted and not payload.signal.value.startswith("SELL"):
            telegram.send(db, f"Trade Opened\n[{strategy_name}] {payload.signal.value}")
        else:
            log_event(db, "WEBHOOK", f"Signal ignored: {response.message}", "WARNING")
        return response
    except Exception as exc:
        logger.exception("Webhook processing failed")
        log_event(db, "ERROR", "Webhook processing failed", "ERROR", {"error": str(exc)})
        telegram.send(db, f"System Error\nWebhook processing failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
