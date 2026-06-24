from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin_page
from app.database import get_db
from app.db_models import StrategyConfig, StrategyTrade, TradeStatus, TradingMode
from app.time_utils import duration_label, format_ist
from app.v7_manager import V7Manager

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["ist"] = format_ist
templates.env.filters["duration"] = duration_label
router = APIRouter()


def get_v7_manager() -> V7Manager:
    return router.v7_manager  # type: ignore[attr-defined]


@router.get("/v7", response_class=HTMLResponse)
def v7_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_admin_page)] = None,
) -> HTMLResponse:
    strategy = db.scalar(select(StrategyConfig).where(StrategyConfig.name == V7Manager.strategy_name))
    active_trades = list(
        db.scalars(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == V7Manager.strategy_name,
                StrategyTrade.status == TradeStatus.OPEN,
            )
            .order_by(StrategyTrade.entry_time.desc())
        )
    )
    recent_trades = list(
        db.scalars(
            select(StrategyTrade)
            .where(StrategyTrade.strategy_name == V7Manager.strategy_name)
            .order_by(StrategyTrade.entry_time.desc())
            .limit(20)
        )
    )
    mode = strategy.mode if strategy is not None else TradingMode.PAPER
    return templates.TemplateResponse(
        "v7.html",
        {
            "request": request,
            "status": "ACTIVE" if active_trades else "FLAT",
            "mode": mode,
            "active_trades": active_trades,
            "recent_trades": recent_trades,
        },
    )
