"""Persist one row per AI Origination decision.

INSTRUMENTATION ONLY. Nothing here influences a decision, a trade, or a
parameter. It records what was already computed, sent and received.

WHY IT IS ITS OWN MODULE
------------------------
So the change to app/ai/originator.py is one import and one call. That file is
under a two-week behaviour freeze while the trend-age fix is observed, and the
smaller its diff, the easier it is to show the freeze was respected.

WHY IT SWALLOWS ITS OWN FAILURES
--------------------------------
A logging table must never be able to stop a trading cycle. If the write
fails -- schema drift, disk full, a locked database -- the exception is logged
and the cycle continues. This is the one place in this codebase where catching
broadly and moving on is the correct behaviour, and it is deliberate rather
than incidental.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, StrategyTrade
from app.time_utils import utc_now

logger = logging.getLogger(__name__)


def record_decision(
    db: Session,
    *,
    index_symbol: str,
    provider: str,
    provider_role: str,
    decision: Any,
    market_context: Any,
    data_stale: bool,
    trade: StrategyTrade | None = None,
) -> None:
    """Write one decision row. Never raises.

    Called for EVERY decision, including NONE and ERROR -- those are the rows
    that did not previously exist anywhere queryable, and they are the majority
    of what AI Origination produces.

    `trade` is the StrategyTrade if the decision opened one. The fields sourced
    from it (trade_id, correlation flags, risk_units_extrapolated) are null
    otherwise, because they are properties of an opened position and have no
    meaning for a decision that declined.
    """
    try:
        counts = getattr(market_context, "same_direction_entries_today", None) or {}
        setups = sorted(k for k, v in (getattr(market_context, "setups", None) or {}).items() if v)
        cpr = getattr(market_context, "cpr", None)

        row = AIOriginationLog(
            timestamp=utc_now(),
            index_name=index_symbol,
            provider=(provider or "").strip().lower(),
            provider_role=provider_role,
            decision=decision.action,
            confidence=decision.confidence,
            setup_quality=getattr(decision, "setup_quality", None),
            entry_quality=getattr(decision, "entry_quality", None),
            risk_quality=getattr(decision, "risk_quality", None),
            market_alignment=getattr(decision, "market_alignment", None),
            trade_id=trade.trade_id if trade else None,
            regime=getattr(market_context, "regime", None) or "UNKNOWN",
            adx=getattr(market_context, "adx", None),
            cpr=cpr.classification if cpr else None,
            setups=json.dumps(setups),
            trend_duration_bars=getattr(market_context, "trend_duration_bars", None),
            trend_duration_pct_of_session=getattr(market_context, "trend_duration_pct_of_session", None),
            move_extent_atr=getattr(market_context, "move_extent_atr", None),
            same_direction_entries_ce=counts.get("BUY_CE"),
            same_direction_entries_pe=counts.get("BUY_PE"),
            concurrent_correlated_entry=trade.concurrent_correlated_entry if trade else None,
            correlated_with_trade_id=trade.correlated_with_trade_id if trade else None,
            context_json=json.dumps(market_context.as_dict()) if market_context else "{}",
            model_response_json=json.dumps(
                {
                    "action": decision.action,
                    "confidence": decision.confidence,
                    "setup_quality": getattr(decision, "setup_quality", None),
                    "entry_quality": getattr(decision, "entry_quality", None),
                    "risk_quality": getattr(decision, "risk_quality", None),
                    "market_alignment": getattr(decision, "market_alignment", None),
                    "sl_percent": decision.sl_percent,
                    "target_percent": decision.target_percent,
                }
            ),
            # Captured whatever the decision -- the original gap was that a
            # NONE's stated reasoning existed only in a debug log line, so a
            # whole session of declines left no record of why.
            reasoning=decision.reasoning or None,
            data_stale=bool(data_stale),
            risk_units_extrapolated=trade.risk_units_extrapolated if trade else None,
            latency_ms=getattr(decision, "latency_ms", None),
            # On ERROR, decision.reasoning already carries the specific cause
            # built by the 5 Aug fix -- an HTTP status, a timeout, a parse
            # failure with the offending payload excerpt. Mirrored here so an
            # error is queryable without grepping the journal.
            error_detail=decision.reasoning if decision.action == "ERROR" else None,
        )
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        # Deliberately broad: see the module docstring. A failed log write must
        # not take down a trading cycle.
        logger.exception("[AI][ORIGIN] Failed to persist decision log (cycle continues)")
