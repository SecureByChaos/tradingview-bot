from __future__ import annotations

import logging
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.ai.models import AlternativeCall
from app.db_models import StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import Signal
from app.option_finder import OptionFinder
from app.platform import get_index_config, log_event
from app.smartapi_client import SmartAPIClient
from app.time_utils import format_ist, utc_now

logger = logging.getLogger(__name__)

_DEFAULT_SL_PERCENT = 10.0
_DEFAULT_TARGET_PERCENT = 20.0
# Floor below which we don't bother paper-opening the alternative at all -- an
# alternative the model itself isn't confident about isn't useful evaluation
# data, it's just noise in the comparison.
_MIN_CONFIDENCE_TO_ACT = 0.3
# Defensive floor on the AI-proposed sl_percent/target_percent themselves --
# the prompt (app/ai/prompt_builder.py) states these are percentage points,
# not a 0-1 fraction, but LLMs aren't 100% reliable about following a stated
# scale. A too-tight band closes almost instantly on ordinary option-premium
# noise, not on the AI actually being wrong. Below this floor the proposed
# value is discarded and the _DEFAULT_* constants above are used instead.
_MIN_SL_TARGET_PERCENT = 5.0


def maybe_open_alternative_trade(
    db: Session,
    original_trade: Optional[StrategyTrade],
    provider: str,
    alternative: Optional[AlternativeCall],
    smartapi_client: Optional[SmartAPIClient],
    option_finder: Optional[OptionFinder],
) -> Optional[StrategyTrade]:
    """Paper-execute an AI-proposed alternative alongside the original signal trade.

    This is additive only -- the original signal's trade is never touched or
    replaced. Two hard safety rules apply, and are not configurable:

    1. Only ever acts when the original trade is itself in PAPER mode. Never
       opens a live order, regardless of any strategy/live_trading setting.
       This feature is evaluation-only until proven out with paper data.
    2. Never calls smartapi_client.place_market_order -- alternative trades
       are always simulated (entry_order_id stays None).
    """
    if alternative is None or alternative.action in ("", "NONE"):
        return None
    if original_trade is None:
        logger.info("[AI][ALT] Skipped: no original trade to attach an alternative to")
        return None
    if original_trade.origin != "SIGNAL":
        # Explicit guard, not just relying on the caller (shadow.py's entry-review
        # trade lookup) to always filter correctly -- an alternative must only ever
        # be generated for a real signal trade, never for another AI_ALT_* or
        # AI_ORIGIN_* trade. This should already be unreachable given how the
        # caller looks trades up, but this isolation guarantee is too important
        # to depend on that alone.
        logger.info(
            "[AI][ALT] Skipped: original trade %s has origin %s, not SIGNAL -- refusing to generate an alternative for a non-signal trade",
            original_trade.trade_id, original_trade.origin,
        )
        return None
    if original_trade.mode != TradingMode.PAPER:
        logger.info(
            "[AI][ALT] Skipped: original trade %s is not PAPER mode (alt calls never touch live)",
            original_trade.trade_id,
        )
        return None
    if smartapi_client is None or option_finder is None:
        logger.info("[AI][ALT] Skipped: no smartapi/option_finder available in this context")
        return None
    if alternative.confidence < _MIN_CONFIDENCE_TO_ACT:
        logger.info(
            "[AI][ALT] Skipped: %s alternative confidence %.2f below floor %.2f",
            provider, alternative.confidence, _MIN_CONFIDENCE_TO_ACT,
        )
        return None

    option_type = original_trade.option_type
    if alternative.action == "FLIP":
        option_type = "PE" if original_trade.option_type == "CE" else "CE"
    elif alternative.option_type in ("CE", "PE"):
        option_type = alternative.option_type

    signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE
    index = get_index_config(db, original_trade.index_symbol)
    try:
        # v1 scope: alternatives stay ATM, same as normal entries. Strike/expiry
        # selection isn't part of what the model can vary yet -- only side
        # (CE/PE) and risk terms (sl/target percent).
        contract = option_finder.find_atm_contract(signal, index, 0)
        entry_price = smartapi_client.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[AI][ALT] Skipped: could not resolve contract/price for %s alternative (%s)", provider, exc)
        return None

    sl_percent = (
        alternative.sl_percent
        if alternative.sl_percent and alternative.sl_percent >= _MIN_SL_TARGET_PERCENT
        else _DEFAULT_SL_PERCENT
    )
    target_percent = (
        alternative.target_percent
        if alternative.target_percent and alternative.target_percent >= _MIN_SL_TARGET_PERCENT
        else _DEFAULT_TARGET_PERCENT
    )
    stoploss = round(entry_price * (1 - sl_percent / 100), 2)
    target = round(entry_price * (1 + target_percent / 100), 2)

    origin = f"AI_ALT_{provider.strip().upper()}"
    trade = StrategyTrade(
        trade_id=uuid4().hex,
        strategy_name=original_trade.strategy_name,
        signal=signal.value,
        index_symbol=original_trade.index_symbol,
        exchange=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        symboltoken=contract.symboltoken,
        strike=contract.strike,
        expiry=contract.expiry,
        option_type=contract.option_type,
        quantity=original_trade.quantity,
        investment_amount=round(entry_price * original_trade.quantity, 2),
        entry_price=round(entry_price, 2),
        current_premium=round(entry_price, 2),
        stoploss=stoploss,
        target=target,
        entry_time=utc_now(),
        mode=TradingMode.PAPER,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        entry_order_id=None,
        highest_price=round(entry_price, 2),
        lowest_price=round(entry_price, 2),
        trailing_active=False,
        origin=origin,
        source_trade_id=original_trade.trade_id,
        ai_action=alternative.action,
        ai_confidence=alternative.confidence,
        ai_reasoning=alternative.reasoning,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    log_event(
        db,
        "AI_ALT",
        f"[{original_trade.strategy_name}] {origin} alternative opened: {signal.value}",
        payload={
            "trade_id": trade.trade_id,
            "source_trade_id": original_trade.trade_id,
            "action": alternative.action,
            "confidence": alternative.confidence,
            "reasoning": alternative.reasoning,
            "entry_time_ist": format_ist(trade.entry_time),
        },
    )
    logger.info(
        "[AI][ALT] %s opened %s alternative for %s (confidence=%.2f action=%s)",
        origin, signal.value, original_trade.strategy_name, alternative.confidence, alternative.action,
    )
    return trade
