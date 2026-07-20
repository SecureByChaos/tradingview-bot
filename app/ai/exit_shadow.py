from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import AIClient
from app.ai.json_utils import extract_json_object
from app.ai.repository import get_settings
from app.database import SessionLocal
from app.db_models import AIExitCall, AISettings, StrategyTrade, TradeStatus
from app.time_utils import to_ist, utc_now

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an options exit-timing assistant running an independent, "
    "observation-only check on an already-open paper-trading options position. "
    "You are not responsible for entries and your decision never closes the "
    "real position -- you are tracked separately for comparison purposes only. "
    "Given the position's own numbers, decide whether to EXIT now or HOLD. "
    "Base this purely on the numbers given -- do not invent market data you "
    "were not given. Respond with a single valid JSON object only, no "
    "markdown, code fences, or extra text: "
    '{"decision": "EXIT"|"HOLD", "confidence": 0-1, "reasoning": "one or two sentences"}.'
)


@dataclass(frozen=True)
class _ProviderView:
    """Lightweight stand-in for AISettings so the secondary provider's columns
    can be passed through the same call path as the primary's."""

    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int


def _build_user_prompt(trade: StrategyTrade) -> str:
    entry_ist = to_ist(trade.entry_time)
    holding_minutes = None
    if entry_ist is not None:
        holding_minutes = max(int((utc_now() - trade.entry_time).total_seconds() // 60), 0)
    lines = [
        f"Strategy: {trade.strategy_name}",
        f"Index: {trade.index_symbol}",
        f"Position: {trade.option_type} ({trade.signal})",
        f"Mode: {trade.mode}",
        f"Entry premium: {trade.entry_price}",
        f"Current premium: {trade.current_premium}",
        f"Stop loss: {trade.stoploss}",
        f"Target: {trade.target}",
        f"Trailing active: {trade.trailing_active}"
        + (f" (trailing stop {trade.trailing_stop})" if trade.trailing_active and trade.trailing_stop else ""),
        f"Running P&L: {trade.pnl_percent}%",
        f"Holding time: {holding_minutes} minutes" if holding_minutes is not None else "Holding time: unknown",
    ]
    return "\n".join(lines) + "\n\nDecide: EXIT now, or HOLD?"


def _parse_response(text: str) -> tuple[str, float | None, str]:
    try:
        data = json.loads(extract_json_object(text)) if isinstance(text, str) else text
        if not isinstance(data, dict):
            return "ERROR", None, "Invalid AI response (not a JSON object)."
        decision = str(data.get("decision") or "").strip().upper()
        if decision not in {"EXIT", "HOLD"}:
            decision = "ERROR"
        confidence = data.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
                if confidence > 1.0:
                    confidence = confidence / 100.0
                confidence = min(1.0, max(0.0, confidence))
            except (TypeError, ValueError):
                confidence = None
        reasoning = str(data.get("reasoning") or "")
        return decision, confidence, reasoning
    except Exception:
        return "ERROR", None, "Invalid AI response."


def _call_openai(view: _ProviderView, user_prompt: str) -> tuple[str, float | None, str]:
    if not view.api_key or not view.model:
        return "ERROR", None, "OpenAI API key/model not configured."
    endpoint = (view.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    response = AIClient().send(
        endpoint=endpoint,
        headers={"Authorization": f"Bearer {view.api_key}", "Content-Type": "application/json"},
        payload={
            "model": view.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=view.timeout_seconds,
    )
    if response.error:
        return "ERROR", None, response.error
    try:
        content = response.response_body["choices"][0]["message"]["content"]
    except Exception:
        return "ERROR", None, "Unexpected OpenAI response shape."
    return _parse_response(content)


def _call_claude(view: _ProviderView, user_prompt: str) -> tuple[str, float | None, str]:
    if not view.api_key or not view.model:
        return "ERROR", None, "Claude API key/model not configured."
    endpoint = (view.base_url or "https://api.anthropic.com/v1").rstrip("/") + "/messages"
    response = AIClient().send(
        endpoint=endpoint,
        headers={
            "x-api-key": view.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload={
            "model": view.model,
            "max_tokens": 256,
            "system": SYSTEM_PROMPT + "\n\nRespond with JSON only, no markdown or code fences.",
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=view.timeout_seconds,
    )
    if response.error:
        return "ERROR", None, response.error
    try:
        blocks = response.response_body.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
    except Exception:
        return "ERROR", None, "Unexpected Claude response shape."
    return _parse_response(text)


def _call_provider(provider: str, view: _ProviderView, user_prompt: str) -> tuple[str, float | None, str] | None:
    normalized = (provider or "").strip().lower()
    if normalized == "claude":
        return _call_claude(view, user_prompt)
    if normalized == "openai":
        return _call_openai(view, user_prompt)
    # "dummy" (no real provider configured) or anything unrecognized -- skip
    # silently rather than writing an ERROR row every 3 minutes for every open
    # trade when AI is enabled but no real API key has been set up yet.
    return None


def _has_terminal_exit(db: Session, trade_id: str) -> bool:
    return (
        db.scalar(select(AIExitCall.id).where(AIExitCall.trade_id == trade_id, AIExitCall.decision == "EXIT").limit(1))
        is not None
    )


def _save_call(db: Session, trade: StrategyTrade, provider: str, model: str, result: tuple[str, float | None, str]) -> None:
    decision, confidence, reasoning = result
    db.add(
        AIExitCall(
            trade_id=trade.trade_id,
            strategy_name=trade.strategy_name,
            provider=provider,
            model=model,
            decision=decision,
            confidence=confidence,
            reasoning=reasoning,
            premium_at_check=trade.current_premium,
            pnl_percent_at_check=trade.pnl_percent,
            holding_minutes=max(int((utc_now() - trade.entry_time).total_seconds() // 60), 0) if trade.entry_time else None,
        )
    )
    db.commit()


def run_exit_shadow_checks(db: Session | None = None) -> None:
    """Independent, read-only AI exit-call check for the AI Exit Calls page.
    Never writes to StrategyTrade or anything the real trading logic reads --
    see app/db_models.py's AIExitCall docstring. Owns its own DB session when
    called from the scheduler (db=None); accepts an existing session in tests."""
    owns_session = db is None
    session = db or SessionLocal()
    try:
        settings: AISettings | None = get_settings(session)
        if settings is None or not settings.enabled or settings.mode == "DISABLED":
            logger.info("[AI][ExitCall] Skipped: AI disabled")
            return
        if (settings.provider or "").strip().lower() not in {"openai", "claude"}:
            logger.info("[AI][ExitCall] Skipped: no real provider configured (provider=%s)", settings.provider)
            return
        trades = list(
            session.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.origin == "SIGNAL",
                )
            )
        )
        for trade in trades:
            try:
                if _has_terminal_exit(session, trade.trade_id):
                    continue
                if trade.current_premium is None:
                    continue
                user_prompt = _build_user_prompt(trade)
                primary_view = _ProviderView(
                    provider=settings.provider,
                    model=settings.model,
                    api_key=settings.api_key,
                    base_url=settings.base_url,
                    timeout_seconds=settings.timeout_seconds,
                )
                result = _call_provider(settings.provider, primary_view, user_prompt)
                if result is not None:
                    _save_call(session, trade, settings.provider, settings.model, result)
                    logger.info("[AI][ExitCall] %s %s -> %s", trade.strategy_name, trade.trade_id, result[0])

                if (
                    settings.secondary_enabled
                    and settings.secondary_provider
                    and settings.secondary_provider.strip().lower() != settings.provider.strip().lower()
                ):
                    secondary_view = _ProviderView(
                        provider=settings.secondary_provider,
                        model=settings.secondary_model,
                        api_key=settings.secondary_api_key,
                        base_url=settings.secondary_base_url,
                        timeout_seconds=settings.timeout_seconds,
                    )
                    secondary_result = _call_provider(settings.secondary_provider, secondary_view, user_prompt)
                    if secondary_result is not None:
                        _save_call(session, trade, settings.secondary_provider, settings.secondary_model, secondary_result)
                        logger.info(
                            "[AI][ExitCall] %s %s (secondary %s) -> %s",
                            trade.strategy_name, trade.trade_id, settings.secondary_provider, secondary_result[0],
                        )
            except Exception:
                logger.exception("[AI][ExitCall] Check failed for trade %s", trade.trade_id)
    except Exception:
        logger.exception("[AI][ExitCall] run_exit_shadow_checks failed")
    finally:
        if owns_session:
            session.close()
