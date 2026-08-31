"""Validated Signal -- a deterministic, non-LLM, paper-only experimental
strategy built from the single most evidence-backed finding this project's
two years of backtesting have produced, and nothing else.

WHY THIS EXISTS (31 Aug 2026)
------------------------------
Requested directly: "build a new strategy which can win... I give you a free
hand... just build best strategy." That request cannot be honestly delivered
as literally stated -- nothing in this project's history (two years of
backtests, a spent holdout, dozens of candidate gates, most rejected) has
found a strategy proven to win. What this module is instead: the ONE entry
signal that has actually survived every check this project has run, wired up
as its own small, fully isolated, paper-only strategy so it can be watched
against real live conditions -- explicitly NOT a guarantee, NOT validated at
this exact construction, and reported as such everywhere it's shown.

THE SIGNAL (not new -- see CLAUDE.md "Indicator setups showed a fit-window
edge" and "Walk-forward revised this")
------------------------------------------------------------------------
31 Jul 2026's six-window walk-forward found EMA_STACK / ST_ALIGNED /
ORB_BREAK / PDH_PDL_BREAK, direction-matched, between 11:00-14:00 IST,
replicated a positive forward-index edge on both indices -- the only
candidate in this project's entire backtest history to clear a Bonferroni
threshold at all. This module trades exactly that combination, using
app.market_context's own already-computed setups dict (the same one AI
Origination's prompt already shows the model every cycle) -- no new
indicator math, no new setup definitions.

WHY THIS IS NOT JUST THE HOLDOUT STRATEGY REPLAYED LIVE
---------------------------------------------------------
The 31 Jul holdout test replayed this exact signal as a rule-based strategy
with an 8%-activate/5%-width trail and a 20% target, and it failed --  not on
win rate (52-59%, fine) but on win/loss RATIO (0.53-0.68): the trail/target
exits capped winners around ~6% while losers ran to the full ~9-11% stop,
because the trail closed positions well before the wider fixed stop could.
This module deliberately does NOT use a trail. sl_mode=FIXED with an origin
that is neither "SIGNAL" nor "AI_ORIGIN_*" means app.multi_strategy's shared
monitor_open_trades branch gives it a plain stop/target/time-exit only (see
that function -- trailing and STALL_EXIT are both explicitly gated to
trade.origin.startswith("AI_ORIGIN_")) -- confirmed by reading that function,
not assumed. A winner here runs uninterrupted until it hits the target or the
session ends, which directly removes the mechanism the holdout blamed. This
is a real, reasoned change from what already failed, not a resubmission of it
-- but the exact stop/target numbers below are still a starting choice, not a
backtested pair, and are named as such.

WHERE THIS RUNS
-----------------
Hooked into AI Origination's existing 5-minute cycle (see the call to
check_validated_signal() in app.ai.originator.run_origination_checks, right
after the [AI][ORIGIN][CTX] log line) rather than a separate poller -- it
reuses the market_context already built that cycle, so this costs zero
additional SmartAPI calls. The hook is wrapped in try/except at the call
site specifically so a failure here can never take down AI Origination's own
per-provider decision loop that follows it -- same isolation principle
CLAUDE.md's "origin field is the isolation mechanism" section already states
for every other subsystem in this app.

ISOLATION
----------
origin="VALIDATED_SIGNAL" -- neither "SIGNAL" (so it never touches risk
locks, stats, or Telegram) nor "AI_ORIGIN_*"/"AI_ALT_*" (so it is never
counted as an AI Origination or AI Alternatives trade in any existing
report, backtest, or dashboard filter). It is its own fourth-plus population,
fully separate, exactly the discipline this project already applies to every
other trade type.

STRUCTURALLY PAPER-ONLY
-------------------------
Unlike AI Origination's two-key live-trading gate, this module has NO live
order code path at all -- mode is hardcoded to TradingMode.PAPER and
smartapi.place_market_order is never called from here, anywhere. This is
deliberately stricter than the existing two-key pattern: this construction
has not earned the option to risk real money yet, so the option does not
exist in the code, not just "off by default."

WHAT IS NOT VALIDATED HERE, STATED PLAINLY
---------------------------------------------
- The 12%/20% stop/target pair below is a reasoned starting point (12%
  matches the user's own current AI Origination max-stop setting; 20% matches
  the holdout's own target so the *change* under test is isolated to "no
  early trail-clip", not also a different target), not a backtested pair.
- The entry signal's own live correlation with outcome is itself still being
  measured separately (scripts/validated_setup_window_backtest.py) -- this
  module trades the same combination that script measures, so its own real
  results ARE that measurement, accumulating live.
- No sample size exists yet. Every number this strategy produces should be
  read with the same "not yet enough evidence" standard this project applies
  to everything else, not as proof the construction works.
"""

from __future__ import annotations

import json
import logging
from datetime import time as dtime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import IndexConfig, SLMode, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_context import MarketContext
from app.models import Signal
from app.option_finder import OptionFinder
from app.platform import log_event
from app.premium_model import days_to_expiry, symmetric_premium_percent
from app.smartapi_client import SmartAPIClient
from app.time_utils import format_ist, to_ist, utc_now

logger = logging.getLogger("validated_signal")

ORIGIN = "VALIDATED_SIGNAL"

# Exact window from the 31 Jul 2026 walk-forward finding -- see module
# docstring. Inclusive start, exclusive end, matching
# scripts/validated_setup_window_backtest.py's own WINDOW_START/WINDOW_END so
# this strategy and the script measuring it agree on the boundary.
WINDOW_START = dtime(11, 0)
WINDOW_END = dtime(14, 0)

# PDH_BREAK/PDL_BREAK carry no _UP/_DOWN suffix in app.market_context -- each
# is inherently one direction (previous-day-high break is bullish,
# previous-day-low break is bearish). Identical set to the backtest script's
# own UP_SETUPS/DOWN_SETUPS.
UP_SETUPS = {"EMA_STACK_UP", "ST_ALIGNED_UP", "ORB_BREAK_UP", "PDH_BREAK"}
DOWN_SETUPS = {"EMA_STACK_DOWN", "ST_ALIGNED_DOWN", "ORB_BREAK_DOWN", "PDL_BREAK"}

# Mirrors AI Origination's own _MIN_DTE_TO_TRADE -- same noise-survivability
# finding applies here (a fixed percentage stop is a wider index distance,
# and therefore more noise-resistant, at longer DTE). find_atm_contract
# rolls forward to satisfy this rather than skipping the trade.
_MIN_DTE_TO_TRADE = 5

# See module docstring's "WHAT IS NOT VALIDATED HERE" -- a reasoned starting
# point, not a backtested pair. Rescaled per CE/PE via symmetric_premium_
# percent so a put isn't stopped by a materially smaller index move than a
# call under the same nominal number (see app.premium_model's own docstring).
_STOP_PERCENT_NOMINAL = 12.0
_TARGET_PERCENT_NOMINAL = 20.0


def _in_window(now_ist) -> bool:
    return WINDOW_START <= now_ist.time() < WINDOW_END


def validated_action(market_context: MarketContext | None, now_ist) -> str | None:
    """"BUY_CE" / "BUY_PE" if the validated setup+window combination is
    active right now, else None. Fails closed on every ambiguous case:
    no market_context, outside the window, no direction-matched setup
    active, or -- deliberately -- both directions active at once (a
    genuinely conflicting read, not something to guess through)."""
    if market_context is None or not _in_window(now_ist):
        return None
    active = {name for name, on in market_context.setups.items() if on}
    up = bool(UP_SETUPS & active)
    down = bool(DOWN_SETUPS & active)
    if up and down:
        return None
    if up:
        return "BUY_CE"
    if down:
        return "BUY_PE"
    return None


def _has_open_validated_trade(db: Session, index_symbol: str) -> bool:
    """One position at a time per index -- this is a single deterministic
    engine, not multiple independent providers each entitled to their own
    slot the way AI Origination's Claude/OpenAI split works."""
    return (
        db.scalar(
            select(StrategyTrade.id)
            .where(
                StrategyTrade.index_symbol == index_symbol,
                StrategyTrade.origin == ORIGIN,
                StrategyTrade.status == TradeStatus.OPEN,
            )
            .limit(1)
        )
        is not None
    )


def open_validated_trade(
    db: Session,
    index: IndexConfig,
    action: str,
    market_context: MarketContext,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> StrategyTrade | None:
    option_type = "CE" if action == "BUY_CE" else "PE"
    signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE

    try:
        contract = option_finder.find_atm_contract(signal, index, 0, min_dte=_MIN_DTE_TO_TRADE)
    except Exception as exc:
        logger.info("[VALIDATED_SIGNAL] %s: Skipped, could not resolve contract (%s)", index.symbol, exc)
        return None

    dte = days_to_expiry(contract.expiry, to_ist(utc_now()).date())
    if dte < _MIN_DTE_TO_TRADE:
        logger.info(
            "[VALIDATED_SIGNAL] %s: no expiry at least %s DTE out (nearest %s at %s DTE) -- skipping",
            index.symbol, _MIN_DTE_TO_TRADE, contract.expiry, dte,
        )
        return None

    try:
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[VALIDATED_SIGNAL] %s: Skipped, could not resolve price (%s)", index.symbol, exc)
        return None
    if not entry_price:
        logger.info("[VALIDATED_SIGNAL] %s: Skipped, LTP came back empty", index.symbol)
        return None

    stop_percent, stop_matched = symmetric_premium_percent(
        _STOP_PERCENT_NOMINAL, index.symbol, contract.option_type, dte
    )
    target_percent, _ = symmetric_premium_percent(
        _TARGET_PERCENT_NOMINAL, index.symbol, contract.option_type, dte
    )
    stoploss = round(entry_price * (1 - stop_percent / 100), 2)
    target = round(entry_price * (1 + target_percent / 100), 2)

    active_setups = {name for name, on in market_context.setups.items() if on}
    matched = sorted((UP_SETUPS if action == "BUY_CE" else DOWN_SETUPS) & active_setups)
    strategy_name = f"Validated Signal - {index.display_name or index.symbol}"

    trade = StrategyTrade(
        trade_id=uuid4().hex,
        strategy_name=strategy_name,
        signal=signal.value,
        index_symbol=index.symbol,
        exchange=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        symboltoken=contract.symboltoken,
        strike=contract.strike,
        expiry=contract.expiry,
        option_type=contract.option_type,
        quantity=contract.lot_size,
        investment_amount=round(entry_price * contract.lot_size, 2),
        entry_price=round(entry_price, 2),
        current_premium=round(entry_price, 2),
        stoploss=stoploss,
        target=target,
        entry_time=utc_now(),
        # Structurally paper-only -- see module docstring. No live order path
        # exists anywhere in this module.
        mode=TradingMode.PAPER,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        highest_price=round(entry_price, 2),
        lowest_price=round(entry_price, 2),
        trailing_active=False,
        # FIXED + this origin (not AI_ORIGIN_*) means app.multi_strategy's
        # shared monitor_open_trades branch gives this a plain stop/target/
        # time-exit only -- no trailing, no STALL_EXIT, both gated in that
        # function to trade.origin.startswith("AI_ORIGIN_"). This is the
        # deliberate difference from the holdout construction -- see module
        # docstring.
        sl_mode=SLMode.FIXED,
        market_context_json=json.dumps(market_context.as_dict()),
        calibration_bucket_matched=stop_matched,
        origin=ORIGIN,
        ai_action=action,
        ai_reasoning=(
            f"Deterministic entry: 11:00-14:00 IST window, matched setups={matched}. "
            "Not an AI decision -- no model call was made for this trade."
        ),
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    log_event(
        db,
        "VALIDATED_SIGNAL",
        f"[{strategy_name}] Validated Signal originated: {signal.value} @ strike {trade.strike}",
        payload={
            "trade_id": trade.trade_id,
            "matched_setups": matched,
            "entry_time_ist": format_ist(trade.entry_time),
        },
    )
    logger.info(
        "[VALIDATED_SIGNAL] %s opened %s for %s (matched=%s)",
        ORIGIN, signal.value, index.symbol, matched,
    )
    return trade


def check_validated_signal(
    db: Session,
    index: IndexConfig,
    market_context: MarketContext | None,
    now_ist,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> StrategyTrade | None:
    """Entry point for one index, called once per AI Origination cycle --
    see the hook in app.ai.originator.run_origination_checks, right after
    the [AI][ORIGIN][CTX] log line. Reuses the market_context already built
    that cycle: zero additional SmartAPI calls of this module's own beyond
    the contract-resolution/LTP calls a real entry actually needs."""
    if _has_open_validated_trade(db, index.symbol):
        return None
    action = validated_action(market_context, now_ist)
    if action is None:
        return None
    return open_validated_trade(db, index, action, market_context, smartapi, option_finder)
