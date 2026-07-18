from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import reports
from app.auth import authenticate_admin, require_admin_page
from app.ai.context_builder import SignalContextBuilder
from app.ai.context_repository import get_context_log_for_review
from app.ai.factory import create_reviewer
from app.ai.repository import create_settings as create_ai_settings, get_settings as get_ai_settings, update_settings as update_ai_settings
from app.database import get_db
from app.db_models import AIContextLog, AITradeReview, BotStatus, IndexConfig, PlatformSettings, SLMode, StrategyConfig, StrategyTrade, TradeStatus, TradingMode
from app.platform import (
    ai_reviews_query_for_filter,
    get_dashboard_summary,
    get_or_create_strategy_stats,
    get_or_create_settings,
    latest_logs,
    list_index_configs,
    log_event,
    strategy_metrics,
    strategy_trades_query_for_filter,
)
from sqlalchemy import case, func, select
from app.smartapi_client import SmartAPIError
from app.time_utils import IST, duration_label, format_ist, to_ist
from app.trade_manager import TradeManager

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["ist"] = format_ist
templates.env.filters["duration"] = duration_label
router = APIRouter()


def get_trade_manager() -> TradeManager:
    return router.trade_manager  # type: ignore[attr-defined]


def get_smartapi() -> object:
    return router.smartapi  # type: ignore[attr-defined]


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


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    trade_manager: Annotated[TradeManager, Depends(get_trade_manager)],
    health_manager: Annotated[object, Depends(get_health_manager)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    summary = get_dashboard_summary(db, trade_manager.get_active_trade())
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "summary": summary, "health": health_manager.latest(db), "logs": latest_logs(db, 20), "strategy_metrics": strategy_metrics(db)},
    )


@router.post("/health-check")
def run_health_check(
    health_manager: Annotated[object, Depends(get_health_manager)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    health_manager.run(notify=False)
    return RedirectResponse("/", status_code=303)


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
    if expiry_itm_strikes < 0:
        raise HTTPException(status_code=400, detail="Expiry ITM strikes cannot be negative")
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
    if expiry_itm_strikes < 0:
        raise HTTPException(status_code=400, detail="Expiry ITM strikes cannot be negative")
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


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    tab: str = "general",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    if tab not in {"general", "notifications", "strategies", "instruments"}:
        tab = "general"
    strategies = list(db.scalars(select(StrategyConfig).order_by(StrategyConfig.name)))
    metrics = strategy_metrics(db)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": get_or_create_settings(db),
            "tab": tab,
            "strategies": strategies,
            "metrics_by_id": {item["strategy"].id: item for item in metrics},
            "indexes": list_index_configs(db),
        },
    )


@router.post("/settings")
def update_settings_page(
    db: Annotated[Session, Depends(get_db)],
    square_off_time: Annotated[str, Form()],
    telegram_bot_token: Annotated[str, Form()] = "",
    telegram_chat_id: Annotated[str, Form()] = "",
    active_tab: Annotated[str, Form()] = "general",
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> RedirectResponse:
    settings = get_or_create_settings(db)
    apply_settings(
        settings,
        square_off_time,
        telegram_bot_token,
        telegram_chat_id,
    )
    db.commit()
    log_event(db, "BOT", "Settings updated from dashboard")
    if active_tab not in {"general", "notifications"}:
        active_tab = "general"
    return RedirectResponse(f"/settings?tab={active_tab}", status_code=303)


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


@router.post("/ai-settings/test-secondary", response_class=HTMLResponse)
def test_secondary_ai_settings(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
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
    return templates.TemplateResponse("ai_settings.html", {"request": request, "settings": settings, "test_result": test_result})


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


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    return templates.TemplateResponse("logs.html", {"request": request, "logs": latest_logs(db, 200)})


@router.get("/ai-reviews", response_class=HTMLResponse)
def ai_reviews_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    review_date: str = "",
    strategy: str = "",
    provider: str = "",
    decision: str = "",
    trade_result: str = "",
    page: int = 1,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    query = ai_reviews_query_for_filter(review_date, strategy, provider, decision, trade_result)

    filtered = query.subquery()
    summary = db.execute(
        select(
            func.count(filtered.c.id),
            func.sum(case((filtered.c.decision == "APPROVE", 1), else_=0)),
            func.sum(case((filtered.c.decision == "WATCH", 1), else_=0)),
            func.sum(case((filtered.c.decision == "REJECT", 1), else_=0)),
            func.avg(filtered.c.confidence),
            func.avg(filtered.c.latency_ms),
        )
    ).one()
    page_size = 25
    total = int(summary[0] or 0)
    pages = max((total + page_size - 1) // page_size, 1)
    page = min(max(page, 1), pages)
    reviews = list(db.scalars(query.order_by(AITradeReview.created_at.desc()).offset((page - 1) * page_size).limit(page_size)))
    groups: list[dict[str, object]] = []
    for review in reviews:
        day_label = to_ist(review.created_at).strftime("%d %b %Y")
        if not groups or groups[-1]["date"] != day_label:
            groups.append({"date": day_label, "reviews": []})
        context_log = get_context_log_for_review(db, review.trade_id, review.signal)
        groups[-1]["reviews"].append(
            {
                "review": review,
                "reason_to_buy": _review_list(review.reason_to_buy),
                "reason_not_to_buy": _review_list(review.reason_not_to_buy),
                "context_log": context_log,
                "context_json": _context_json(context_log.context_json) if context_log is not None else {},
                "context_missing_fields": _review_list(context_log.missing_fields) if context_log is not None else [],
            }
        )
    filters = {"review_date": review_date, "strategy": strategy, "provider": provider, "decision": decision, "trade_result": trade_result}
    export_query = urlencode({key: value for key, value in filters.items() if value})
    export_url = "/api/ai-reviews/export" + (f"?{export_query}" if export_query else "")
    return templates.TemplateResponse(
        "ai_reviews.html",
        {
            "request": request,
            "groups": groups,
            "summary": {"reviews": total, "approved": summary[1] or 0, "watch": summary[2] or 0, "rejected": summary[3] or 0, "confidence": round(summary[4] or 0, 2), "latency": round(summary[5] or 0, 2)},
            "strategies": list(db.scalars(select(AITradeReview.strategy).distinct().order_by(AITradeReview.strategy))),
            "providers": list(db.scalars(select(AITradeReview.provider).distinct().order_by(AITradeReview.provider))),
            "filters": filters,
            "export_url": export_url,
            "page": page,
            "pages": pages,
            "previous_url": str(request.url.include_query_params(page=page - 1)),
            "next_url": str(request.url.include_query_params(page=page + 1)),
        },
    )


@router.get("/ai-context-inspector", response_class=HTMLResponse)
@router.get("/ai-context-inspector", response_class=HTMLResponse)
def ai_context_inspector_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    review_date: str = "",
    context_id: str | None = None,
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    normalized_review_date = review_date.strip() if review_date else ""
    if normalized_review_date and "-" in normalized_review_date:
        parts = normalized_review_date.split("-")
        if len(parts) == 3 and len(parts[0]) != 4:
            normalized_review_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
    try:
        day = date.fromisoformat(normalized_review_date) if normalized_review_date else datetime.now(IST).date()
    except ValueError:
        day = datetime.now(IST).date()
    review_date = day.isoformat()

    start = datetime.combine(day, time.min, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.combine(day, time.max, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
    day_contexts = list(
        db.scalars(
            select(AIContextLog)
            .where(AIContextLog.created_at.between(start, end))
            .order_by(AIContextLog.created_at.desc())
        )
    )

    parsed_context_seq: int | None = None
    if context_id is not None:
        candidate = context_id.strip()
        if candidate:
            try:
                parsed_context_seq = int(candidate)
            except ValueError:
                parsed_context_seq = None

    day_contexts_by_time = sorted(day_contexts, key=lambda row: row.created_at)
    context_log = None
    if day_contexts_by_time:
        if parsed_context_seq is None:
            parsed_context_seq = 1
        if parsed_context_seq > 0:
            selected_index = parsed_context_seq - 1
            if selected_index < len(day_contexts_by_time):
                context_log = day_contexts_by_time[selected_index]
        if context_log is None:
            context_log = day_contexts_by_time[0]
            parsed_context_seq = 1
    else:
        parsed_context_seq = 0

    context_json = _context_json(context_log.context_json) if context_log is not None else {}
    tradingview = context_json.get("tradingview") if isinstance(context_json.get("tradingview"), dict) else {}
    tradingview_indicators = tradingview.get("indicators") if isinstance(tradingview.get("indicators"), dict) else {}
    tradingview_trend = tradingview.get("trend") if isinstance(tradingview.get("trend"), dict) else {}
    tradingview_strategy_filters = tradingview.get("strategy_filters") if isinstance(tradingview.get("strategy_filters"), dict) else {}
    source_breakdown = context_json.get("source_breakdown") if isinstance(context_json.get("source_breakdown"), dict) else {}
    return templates.TemplateResponse(
        "ai_context_inspector.html",
        {
            "request": request,
            "context_log": context_log,
            "context_json": context_json,
            "request_json": _context_json(context_log.request_json) if context_log is not None else {},
            "missing_fields": _review_list(context_log.missing_fields) if context_log is not None else [],
            "reason_to_buy": _review_list(context_log.reason_to_buy) if context_log is not None else [],
            "reason_not_to_buy": _review_list(context_log.reason_not_to_buy) if context_log is not None else [],
            "source_breakdown": source_breakdown,
            "tradingview_indicators": _section_values(
                {
                    **tradingview_indicators,
                    **{f"trend.{k}": v for k, v in tradingview_trend.items()},
                    **{f"strategy_filters.{k}": v for k, v in tradingview_strategy_filters.items()},
                },
                (
                    "ema9", "ema20", "ema21", "ema_gap", "vwap", "rsi", "atr", "adx", "di_plus", "di_minus",
                    "supertrend", "volume_ratio", "orb_high", "orb_low", "filters", "rr_ratio",
                    "trend.trend_direction", "trend.breakout", "trend.strong_candle", "trend.sideways_filter", "trend.htf_confirmation",
                    "strategy_filters.ema_filter", "strategy_filters.supertrend_filter", "strategy_filters.adx_filter",
                    "strategy_filters.session_filter", "strategy_filters.trade_limit_filter",
                ),
            ),
            "tradingview_market_data": _section_values(
                tradingview.get("market_data") if isinstance(tradingview.get("market_data"), dict) else {},
                ("banknifty_price", "open", "high", "low", "close", "timeframe", "volume", "timestamp"),
            ),
            "trade_data": _section_values(
                context_json.get("trade_data") if isinstance(context_json.get("trade_data"), dict) else {},
                (
                    "entry_price", "current_premium", "stop_loss", "target", "running_pnl", "holding_minutes",
                    "position", "position_state", "trade_number", "signal", "strategy",
                ),
                empty_label="-",
            ),
            "broker_data": _section_values(
                context_json.get("broker_data") if isinstance(context_json.get("broker_data"), dict) else {},
                ("banknifty_price", "option_price", "strike", "expiry", "option_type"),
                empty_label="-",
            ),
            "review_date": review_date,
            "context_id": parsed_context_seq,
            "day_contexts": day_contexts,
        },
    )
def _review_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value) if value else []
    except Exception:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)] if parsed else []


def _context_json(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value) if value else {}
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _section_values(values: dict[str, object], fields: tuple[str, ...], empty_label: str = "NOT PROVIDED BY WEBHOOK") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for field in fields:
        value = values.get(field)
        if value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (dict, list)) and not value):
            rows.append({"field": field, "value": empty_label})
        else:
            rows.append({"field": field, "value": value})
    return rows


def apply_settings(
    settings: PlatformSettings,
    square_off_time: str,
    telegram_bot_token: str,
    telegram_chat_id: str,
) -> None:
    settings.square_off_time = square_off_time
    settings.telegram_bot_token = telegram_bot_token
    settings.telegram_chat_id = telegram_chat_id





