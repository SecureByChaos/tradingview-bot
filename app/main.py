from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.logger import TradeCSVLogger, configure_logging
from app.models import ActiveTrade, WebhookPayload, WebhookResponse
from app.monitor import TradeMonitor
from app.option_finder import OptionFinder
from app.scheduler import create_scheduler
from app.smartapi_client import SmartAPIClient
from app.trade_manager import TradeManager

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

trade_logger = TradeCSVLogger(settings.trades_csv_path)
smartapi = SmartAPIClient(settings)
option_finder = OptionFinder(settings, smartapi)
trade_manager = TradeManager(settings, smartapi, option_finder, trade_logger)
monitor = TradeMonitor(trade_manager)
scheduler = create_scheduler(monitor)


@asynccontextmanager
async def lifespan(_: FastAPI):
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
def webhook(payload: WebhookPayload) -> WebhookResponse:
    try:
        return trade_manager.handle_signal(payload.signal)
    except Exception as exc:
        logger.exception("Webhook processing failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
