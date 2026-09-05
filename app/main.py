from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

from app import api_routes, dashboard_routes
from app.config import get_settings
from app.database import SessionLocal, engine, get_db, init_db
from app.logger import TradeCSVLogger, configure_logging
from app.db_models import IndexConfig, StrategyConfig, StrategyTrade, TradeResult, TradeStatus
from app.models import WebhookPayload, WebhookResponse
from app.multi_strategy import MultiStrategyTradeManager
from app.multi_strategy_monitor import MultiStrategyMonitor
from app.option_finder import OptionFinder
from app.platform import get_or_create_settings, get_or_create_strategy_stats, log_event, reset_daily_risk_if_needed, serialize_strategy_trade, strategy_trades_query_for_filter, strategy_trading_allowed, trading_allowed
from sqlalchemy import select
from app.risk import RiskProtectionService
from app.signal_validation import check_duplicate_signal, check_market_hours, check_webhook_staleness
from app.ai.autonomous import run_autonomous_checks
from app.ai.originator import run_origination_checks
from app.quick_scalp import run_quick_scalp_checks
from app.validated_signal import run_validated_signal_entry_checks, run_validated_signal_exit_checks
from app.live_feed import IndexFeed, LiveFeedStore
from app.market_data import capture_closing_auction
from app.option_chain import build_collector_client, run_chain_collection
from app.scheduler import create_scheduler
from app.smartapi_client import SmartAPIClient
from app.telegram_service import TelegramService
from app.time_utils import utc_now
from app.trade_manager import TradeManager
from app.v7_manager import V7Manager
from app.health.health_manager import HealthManager

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
monitor = MultiStrategyMonitor(multi_strategy_manager, risk_service, v7_manager)
health_manager = HealthManager(smartapi, engine, telegram)

# Persistent index-spot WebSocket feed. Replaces per-request/cached SmartAPI
# calls from the dashboard entirely -- see CLAUDE.md, "Dashboard-driven
# SmartAPI rate exhaustion", and app/live_feed.py's module docstring for why
# and for what's NOT been verified against the real feed from this sandbox.
# Never fatal to start (see lifespan below): a feed problem should degrade
# the dashboard to "unavailable", never block trading, which doesn't depend
# on this feed at all.
live_feed_store = LiveFeedStore()

# Option-chain archival. Collection only: nothing in the trading path reads it,
# and it is months away from being evaluable. It is wired in now because the
# data cannot be backfilled -- Angel serves no chain history, so the archive can
# only ever start from the day it starts.
chain_client, chain_dedicated = (
    build_collector_client(settings, smartapi)
    if settings.option_chain_collection_enabled
    else (None, False)
)
if chain_client is not None and not chain_dedicated:
    logger.info(
        "[CHAIN] Collector is SHARING the live SmartAPI rate-limit budget. It yields to "
        "live trading after a rate limit, but this is not isolation -- set "
        "SMARTAPI_ANALYTICS_* to a second API key for a genuinely separate budget."
    )

scheduler = create_scheduler(
    monitor,
    health_manager,
    originator_job=lambda: run_origination_checks(smartapi, option_finder),
    option_chain_job=(
        (lambda: run_chain_collection(chain_client, dedicated=chain_dedicated))
        if chain_client is not None else None
    ),
    option_chain_interval_minutes=settings.option_chain_interval_minutes,
    closing_auction_job=lambda: capture_closing_auction(smartapi, SessionLocal),
    autonomous_job=lambda: run_autonomous_checks(smartapi, option_finder, multi_strategy_manager, live_feed_store),
    quick_scalp_job=lambda: run_quick_scalp_checks(smartapi, option_finder, multi_strategy_manager),
    validated_signal_entry_job=lambda: run_validated_signal_entry_checks(smartapi, option_finder),
    validated_signal_exit_job=lambda: run_validated_signal_exit_checks(smartapi, multi_strategy_manager),
)
health_manager.scheduler = scheduler


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
    if chain_dedicated and chain_client is not None:
        # Never fatal, whatever live_trading says. This client only reads market
        # data for an archive; failing startup over it would let a data-
        # collection problem stop trading, which is backwards.
        try:
            chain_client.authenticate()
        except Exception:
            logger.exception(
                "[CHAIN] Analytics SmartAPI authentication failed; option-chain "
                "collection will retry on its next cycle"
            )
    index_feed = None
    try:
        with SessionLocal() as db:
            enabled_indexes = list(db.scalars(select(IndexConfig).where(IndexConfig.enabled.is_(True))))
        index_feed = IndexFeed(smartapi, live_feed_store, enabled_indexes)
        index_feed.start()
    except Exception:
        # Same reasoning as the option-chain collector above: a feed problem
        # degrades the dashboard, it must never stop trading from starting.
        logger.exception("[LIVEFEED] Failed to start index live feed; dashboard prices will read unavailable")
    scheduler.start()
    logger.info("Scheduler started")
    try:
        yield
    finally:
        if index_feed is not None:
            index_feed.stop()
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


app = FastAPI(
    title="StrikeVault",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    session_cookie="strikevault_admin_session",
    https_only=settings.secure_cookies,
    same_site="lax",
    max_age=60 * 60 * 8,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
health_manager.app = app

api_routes.router.trade_manager = trade_manager  # type: ignore[attr-defined]
api_routes.router.multi_strategy_manager = multi_strategy_manager  # type: ignore[attr-defined]
api_routes.router.v7_manager = v7_manager  # type: ignore[attr-defined]
api_routes.router.telegram = telegram  # type: ignore[attr-defined]
api_routes.router.scheduler = scheduler  # type: ignore[attr-defined]
api_routes.router.trade_logger = trade_logger  # type: ignore[attr-defined]
dashboard_routes.router.trade_manager = trade_manager  # type: ignore[attr-defined]
dashboard_routes.router.multi_strategy_manager = multi_strategy_manager  # type: ignore[attr-defined]
dashboard_routes.router.smartapi = smartapi  # type: ignore[attr-defined]
dashboard_routes.router.live_feed_store = live_feed_store  # type: ignore[attr-defined]
dashboard_routes.router.health_manager = health_manager  # type: ignore[attr-defined]

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


_TV_INDICATOR_FIELDS = (
    "ema9", "ema20", "ema21", "ema_gap", "vwap", "rsi", "atr", "adx", "di_plus", "di_minus",
    "supertrend", "volume_ratio", "orb_high", "orb_low", "filters", "rr_ratio",
)


@app.post("/webhook", response_model=WebhookResponse)
def webhook(payload: WebhookPayload, db: Session = Depends(get_db)) -> WebhookResponse:
    try:
        reset_daily_risk_if_needed(db)
        strategy_name = (payload.strategy or settings.default_strategy_name).strip()
        _log_tradingview_indicators(payload.indicators.model_dump(exclude_none=True) if payload.indicators else None)
        log_event(db, "WEBHOOK", f"[{strategy_name}] Webhook received: {payload.signal.value}")
        payload_market_data = payload.market_data.model_dump(exclude_none=True) if payload.market_data else None
        _run_integrity_checks(db, strategy_name, payload.signal.value, payload_market_data)
        if strategy_name.upper() == "V7":
            return v7_manager.handle_signal(db, payload.signal, payload_market_data)

        strategy = db.scalar(select(StrategyConfig).where(StrategyConfig.name == strategy_name))
        if strategy is None:
            message = f"Rejected: strategy '{strategy_name}' does not exist"
            log_event(db, "WEBHOOK", message, "WARNING")
            return WebhookResponse(accepted=False, message=message)

        strategy_stats = get_or_create_strategy_stats(db, strategy.name)
        if strategy_stats.risk_locked:
            message = f"Strategy {strategy.name} locked due to consecutive losses or daily loss limit"
            log_event(db, "WEBHOOK", message, "WARNING")
            return WebhookResponse(accepted=False, message=message)

        allowed, message = trading_allowed(db)
        if not allowed:
            log_event(db, "WEBHOOK", f"Webhook ignored: {message}", "WARNING")
            return WebhookResponse(accepted=False, message=message)

        allowed, message = strategy_trading_allowed(db, strategy)
        if not allowed:
            log_event(db, "WEBHOOK", f"Webhook ignored: {message}", "WARNING")
            return WebhookResponse(accepted=False, message=message)

        # origin == "SIGNAL" only -- a losing AI_ALT_* evaluation trade must
        # never trigger the real strategy's cooldown-after-loss.
        recent_loss = db.scalar(
            select(StrategyTrade.id)
            .where(
                StrategyTrade.strategy_name == strategy_name,
                StrategyTrade.result == TradeResult.LOSS,
                StrategyTrade.exit_time.is_not(None),
                StrategyTrade.exit_time >= utc_now() - timedelta(minutes=30),
                StrategyTrade.origin == "SIGNAL",
            )
            .limit(1)
        )
        if recent_loss is not None:
            message = "Signal rejected due to cooldown after recent loss."
            log_event(db, "WEBHOOK", message, "WARNING")
            return WebhookResponse(accepted=False, message=message)

        response = multi_strategy_manager.handle_signal(db, strategy_name, payload.signal, payload_market_data)
        if response.accepted:
            # SELL_CE/SELL_PE are now observation-only (see
            # MultiStrategyTradeManager.record_exit_suggestion) and are already
            # logged via their own STATE/TRADE events inside handle_signal --
            # don't also mislabel an accepted signal as "ignored" here.
            if not payload.signal.value.startswith("SELL"):
                telegram.send(db, f"Trade Opened\n[{strategy_name}] {payload.signal.value}")
        else:
            log_event(db, "WEBHOOK", f"Signal ignored: {response.message}", "WARNING")
        return response
    except Exception as exc:
        logger.exception("Webhook processing failed")
        log_event(db, "ERROR", "Webhook processing failed", "ERROR", {"error": str(exc)})
        telegram.send(db, f"System Error\nWebhook processing failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _run_integrity_checks(
    db: Session,
    strategy_name: str,
    signal: str,
    market_data: dict[str, object] | None,
) -> None:
    """Log-only signal-integrity checks -- freshness, market hours, and replay
    detection. None of these block the webhook; they exist purely to surface
    a data-trust flag on the logs/dashboard when something looks off. See
    app/signal_validation.py."""
    received_at = utc_now()
    for warning in (
        check_market_hours(received_at),
        check_webhook_staleness(market_data, received_at),
        check_duplicate_signal(strategy_name, signal, market_data, received_at),
    ):
        if warning:
            log_event(db, "VALIDATION", warning, "WARNING", {"strategy": strategy_name, "signal": signal})


def _log_tradingview_indicators(indicators: dict[str, object] | None) -> None:
    if not indicators:
        logger.info("[AI] TradingView indicators received: 0")
        return
    received = [name for name in _TV_INDICATOR_FIELDS if _is_present(indicators.get(name))]
    missing = [name for name in _TV_INDICATOR_FIELDS if name not in received]
    logger.info("[AI] TradingView indicators received: %s", len(received))
    logger.info("[AI] TradingView indicators provided: %s", ", ".join(received) if received else "None")
    logger.info("[AI] TradingView indicators missing: %s", ", ".join(missing) if missing else "None")


def _is_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return True


@app.exception_handler(RequestValidationError)
async def webhook_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    body_text = ""
    try:
        body = await request.body()
        body_text = body.decode("utf-8", errors="replace")
    except Exception:
        body_text = "<unable to read request body>"
    logger.error("========== WEBHOOK VALIDATION ERROR ==========")
    logger.error("Validation Errors: %s", exc.errors())
    logger.error("Complete JSON Request: %s", body_text)
    with SessionLocal() as db:
        try:
            payload_obj = json.loads(body_text) if body_text and body_text.startswith("{") else {"raw_body": body_text}
        except Exception:
            payload_obj = {"raw_body": body_text}
        log_event(db, "WEBHOOK", "Webhook validation failed", "ERROR", {"errors": exc.errors(), "request": payload_obj})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})
