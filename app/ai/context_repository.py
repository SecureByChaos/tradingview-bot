from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import AIContextLog


def create_context_log(
    db: Session,
    *,
    timestamp: Any,
    strategy: str,
    signal: str,
    event_type: str,
    paper_live: str,
    trade_id: Optional[str],
    trade_number: int,
    session: str,
    context_data: dict[str, Any],
    request_data: dict[str, Any],
    payload_size: int,
    context_version: str,
    prompt_version: str,
    model: str,
    completeness_percent: float,
    missing_fields: list[str],
) -> AIContextLog:
    row = AIContextLog(
        timestamp=timestamp,
        strategy=strategy,
        signal=signal,
        event_type=event_type,
        paper_live=paper_live,
        trade_id=trade_id,
        trade_number=trade_number,
        session=session,
        context_json=json.dumps(context_data, default=str),
        request_json=json.dumps(request_data, default=str),
        payload_size=payload_size,
        context_version=context_version,
        prompt_version=prompt_version,
        model=model,
        completeness_percent=completeness_percent,
        missing_fields=json.dumps(missing_fields),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def finalize_context_log(
    db: Session,
    row_id: int,
    *,
    latency_ms: Optional[float],
    decision: str,
    confidence: Optional[float],
    reason_to_buy: list[str],
    reason_not_to_buy: list[str],
    summary: str,
) -> None:
    row = db.get(AIContextLog, row_id)
    if row is None:
        return
    row.latency_ms = latency_ms
    row.decision = decision
    row.confidence = confidence
    row.reason_to_buy = json.dumps(reason_to_buy)
    row.reason_not_to_buy = json.dumps(reason_not_to_buy)
    row.summary = summary
    db.commit()


def get_latest_context_log(db: Session) -> Optional[AIContextLog]:
    return db.scalar(select(AIContextLog).order_by(AIContextLog.created_at.desc()).limit(1))


def get_context_log_for_review(db: Session, trade_id: Optional[str], signal: str) -> Optional[AIContextLog]:
    if not trade_id:
        return None
    return db.scalar(
        select(AIContextLog)
        .where(AIContextLog.trade_id == trade_id, AIContextLog.signal == signal)
        .order_by(AIContextLog.created_at.desc())
        .limit(1)
    )
