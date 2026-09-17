"""Persist one row per Autonomous AI entry-decision cycle.

INSTRUMENTATION ONLY. Nothing here influences a decision, a trade, or a
gate. It records what was already computed and decided.

WHY THIS EXISTS
---------------
Autonomous AI's own dominant output is NONE (325 of 402 raw decisions across
a real 30-day window sampled 17 Sep 2026), and until now a NONE decision
left no queryable trace at all -- only a bare `logger.info("... -> NONE")`
line with no reasoning attached, eventually rotated away by journald.
app.ai.origination_log already solved exactly this gap for AI Origination on
26 Aug 2026; this module gives Autonomous AI the same capability, following
the same isolated-module-plus-one-import shape so the change to
app/ai/autonomous.py stays small.

The concrete question this exists to answer: that same 30-day sample found
Autonomous AI's raw decisions skewed 10:1 toward BUY_PE (70) over BUY_CE (7),
against a real but comparatively modest -2.5% to -3.5% market move over the
same window on both indices -- a lean that looked disproportionate to the
move, but could not be checked further because the model's own stated
reasoning for its 325 NONE decisions in that window was never captured
anywhere. This table exists so that question, and the next one like it, is
answerable from stored data rather than an unrepeatable live grep.

WHY IT SWALLOWS ITS OWN FAILURES
---------------------------------
Same reasoning as app.ai.origination_log: a logging table must never be able
to stop a trading cycle. If the write fails, the exception is logged and the
cycle continues.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db_models import AutonomousAILog, StrategyTrade
from app.time_utils import utc_now

logger = logging.getLogger(__name__)


def record_entry_decision(
    db: Session,
    *,
    index_symbol: str,
    features: Any,
    raw_decision: str,
    confidence: Optional[float] = None,
    reasoning: Optional[str] = None,
    block_reason: Optional[str] = None,
    trade: Optional[StrategyTrade] = None,
    latency_ms: Optional[float] = None,
) -> None:
    """Write one entry-decision row. Never raises.

    Called for every cycle that reaches at least the feature-engine stage --
    a deterministic pre-call block (session phase, ADX floor), a provider
    error, a genuine model NONE, a model BUY_CE/BUY_PE overridden by the
    EMA-regime check, one that failed at contract/LTP resolution, or one
    that opened a real trade.

    `raw_decision` is always the model's own intended action where one
    exists (BUY_CE/BUY_PE/NONE/ERROR) -- never the post-override outcome --
    so a query for "what did the model actually want to do" is never
    contaminated by what a downstream gate did about it. `block_reason`
    records that separately: None means nothing intervened.
    """
    try:
        row = AutonomousAILog(
            timestamp=utc_now(),
            index_name=index_symbol,
            raw_decision=raw_decision,
            confidence=confidence,
            reasoning=reasoning or None,
            block_reason=block_reason,
            trade_id=trade.trade_id if trade else None,
            spot=getattr(features, "spot", None),
            vwap=getattr(features, "vwap", None),
            vwap_relation=getattr(features, "vwap_relation", None),
            fast_ema=getattr(features, "fast_ema", None),
            slow_ema=getattr(features, "slow_ema", None),
            trend_regime=getattr(features, "trend_regime", None),
            adx=getattr(features, "adx", None),
            dist_to_pdh=getattr(features, "dist_to_pdh", None),
            dist_to_pdl=getattr(features, "dist_to_pdl", None),
            session_phase=getattr(features, "session_phase", None),
            chop_efficiency_ratio=getattr(features, "chop_efficiency_ratio", None),
            recent_price_change_percent=getattr(features, "recent_price_change_percent", None),
            latency_ms=latency_ms,
        )
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        # Deliberately broad: see the module docstring. A failed log write
        # must not take down a trading cycle.
        logger.exception("[AUTONOMOUS_AI] Failed to persist decision log (cycle continues)")
