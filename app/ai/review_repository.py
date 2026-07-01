from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from app.ai.models import ReviewResult
from app.db_models import AITradeReview, StrategyTrade, TradeStatus


def save_review(
    db: Session,
    trade_id: Optional[str],
    strategy: str,
    signal: str,
    result: ReviewResult,
    prompt_version: str,
    context_version: str,
    framework_version: str,
) -> AITradeReview:
    trade = db.scalar(select(StrategyTrade).where(StrategyTrade.trade_id == trade_id)) if trade_id else None
    review = AITradeReview(
        trade_id=trade_id,
        strategy=strategy,
        signal=signal,
        provider=result.provider,
        model=result.model or "",
        prompt_version=prompt_version,
        context_version=context_version,
        framework_version=framework_version,
        decision=result.decision,
        confidence=result.confidence,
        entry_quality=result.entry_quality or "",
        market_type=result.market_type or "",
        risk=result.risk or "",
        reason_to_buy=json.dumps(result.reason_to_buy),
        reason_not_to_buy=json.dumps(result.reason_not_to_buy),
        summary=result.summary,
        latency_ms=result.latency_ms,
        actual_result=trade.result if trade is not None and trade.status == TradeStatus.CLOSED else None,
        actual_pnl=trade.profit_loss if trade is not None and trade.status == TradeStatus.CLOSED else None,
        ai_correct=_is_correct(result.decision, trade.result) if trade is not None and trade.status == TradeStatus.CLOSED else None,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


def update_trade_result(db: Session, trade_id: str, actual_result: str, actual_pnl: float) -> None:
    reviews = list(db.scalars(select(AITradeReview).where(AITradeReview.trade_id == trade_id)))
    for review in reviews:
        review.actual_result = actual_result
        review.actual_pnl = actual_pnl
        review.ai_correct = _is_correct(review.decision, actual_result)
    db.commit()


def get_review(db: Session, trade_id: str) -> Optional[AITradeReview]:
    return db.scalar(
        select(AITradeReview).where(AITradeReview.trade_id == trade_id).order_by(AITradeReview.created_at.desc()).limit(1)
    )


def _is_correct(decision: str, actual_result: str) -> Optional[bool]:
    if decision == "APPROVE":
        return actual_result == "WIN"
    if decision == "REJECT":
        return actual_result == "LOSS"
    return None


@event.listens_for(StrategyTrade, "after_update")
def _update_review_after_trade_close(_, connection, trade: StrategyTrade) -> None:
    if trade.status != TradeStatus.CLOSED:
        return
    reviews = connection.execute(
        select(AITradeReview.id, AITradeReview.decision).where(AITradeReview.trade_id == trade.trade_id)
    )
    for review_id, decision in reviews:
        connection.execute(
            update(AITradeReview)
            .where(AITradeReview.id == review_id)
            .values(
                actual_result=trade.result,
                actual_pnl=trade.profit_loss,
                ai_correct=_is_correct(decision, trade.result),
            )
        )
