"""Quick Scalp -- a deterministic, no-AI, paper-only strategy built for one
explicit request: quick trades that bank a small profit, cut losses fast.
Discussed and scoped with the user before building (see CLAUDE.md's own
dated entry for the full back-and-forth) -- no LLM, fixed 5% target, fixed
3% stop, no trailing.

WHY NO TRAILING
-----------------
Two separate pieces of this project's own history argue against a trailing
mechanism at this scale, both surfaced in the design discussion before this
was built:

1. The 31 Jul holdout test on an entirely different signal found a trail
   (8% activate / 5% width against a 20% target) capped winners around ~6%
   while losses ran to the full ~9-11% stop -- the trail closing positions
   before the wider stop could. app.validated_signal was built specifically
   to avoid repeating this, and this module follows the same reasoning.
2. scripts/scalp_stop_sweep.py's own design (10 Aug 2026, "scalping-horizon"
   scoping pass, tooling built but never run) explicitly tracks "a noise-hit
   rate on stop-outs specifically" as a named risk at small stop distances --
   at a 3% stop, ordinary bid-ask/tick noise is a much larger fraction of the
   stop distance than it is at AI Origination's 12%+ stops, so a trail
   layered on top of an already-tight band has more opportunity to fire on
   noise, not a real reversal.

Given both, the target is fixed at the TOP of the requested 1-5% range (not
clipped early at the bottom) and the stop is a plain, immediately-enforced
3% -- "let it grind toward 5%, cut losses fast" without adding a mechanism
this project has already found reason to distrust at this scale.

THE SIGNAL
-----------
EMA_RSI_CROSS (scripts/backtest/setups.py, built 10 Aug 2026 for the
scalping-horizon investigation, never backtested against real data): EMA9
crosses EMA21 on a 1-minute bar, confirmed by RSI(14) > 55 (bullish) or < 45
(bearish) on that same bar. quick_scalp_action() below is a live
re-implementation of the exact same rule (same warm-up requirement, same RSI
thresholds) over app.indicators' pure ema()/rsi() functions -- the same
functions the 5-minute AI cycles already use, so this isn't new indicator
math, just a new, faster-cadence caller of it.

**Not validated.** scripts/scalp_breakeven.py -- built the same day as
EMA_RSI_CROSS, specifically to answer "what's the real cost floor a scalp
signal needs to clear after round-trip costs and slippage" -- has also never
been run. Recommended before trusting this module's own results: run it
against real history first, since a 3%/5% band is small enough that
transaction costs alone could matter more than the signal.

    python -m scripts.scalp_breakeven --candles data/option_candles

CADENCE
--------
Own scheduler job at 1-minute resolution (app.scheduler's "quick-scalp-
check"), tighter than every other AI-adjacent job in this app (all on 5
minutes) -- there is no LLM cost here to amortize against a slower cadence,
so "quick" scalping gets a genuinely quick decision loop. This does mean its
own dedicated candle refresh (a real SmartAPI get_candles call, same pattern
originator.py's own _load_market_context already uses) fires once a minute
per enabled index rather than once per 5 minutes -- still well inside
_throttle_quote_call()'s shared 1.3s-minimum-spacing budget (a 60-second
window has room for far more than the handful of calls this adds), not a
new unthrottled call, and not competing for anything more than ordinary
minute-to-minute spacing.

MAX HOLD TIME
---------------
A trade that hits neither the 3% stop nor the 5% target within
_MAX_HOLD_MINUTES is squared off unconditionally (ExitReason.MAX_HOLD_EXIT)
-- enforced entirely inside this module's own cycle, since nothing in the
shared monitor_open_trades knows about a per-strategy max-hold concept for
this origin. "Quick" is enforced on the time axis, not just the price axis.

STRUCTURALLY PAPER-ONLY
-------------------------
Same construction as every other experimental strategy in this project:
mode is hardcoded to TradingMode.PAPER, smartapi.place_market_order is never
called anywhere in this module.

NO ADMIN KILL SWITCH YET -- A KNOWN, DELIBERATE GAP
-------------------------------------------------------
Every other strategy in this app has some admin-facing enable/disable
(StrategyConfig.enabled for rule-based strategies, AISettings.enabled
transitively for the AI-driven ones). Quick Scalp has no AI dependency at
all, so it inherits no such toggle -- today the only way to stop it is a
code deploy or disabling the index in Settings > Instruments. Scoped out of
this pass deliberately to keep the build bounded, same reasoning this
project has used before recorded to defer a real gap explicitly rather than
build around it silently.

ISOLATION
----------
origin="QUICK_SCALP", matched with ==, its own population, never counted in
any other strategy's report/backtest/dashboard filter. One open position at
a time per index, same reasoning as every other single-decision-engine
strategy already in this app.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.db_models import IndexConfig, SLMode, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.indicators import ema, rsi
from app.market_data import ONE_MINUTE, Bar, load_bars, parse_smartapi_row, store_bars
from app.models import ExitReason, Signal
from app.option_finder import OptionFinder
from app.platform import list_index_configs, log_event
from app.premium_model import days_to_expiry, symmetric_premium_percent
from app.signal_validation import check_market_hours
from app.smartapi_client import SmartAPIClient
from app.time_utils import to_ist, utc_now

logger = logging.getLogger(__name__)

ORIGIN = "QUICK_SCALP"

_MIN_DTE_TO_TRADE = 5

# See module docstring's "WHY NO TRAILING" -- fixed, not backtested, a
# reasoned starting point matching what was explicitly requested (1-5%
# profit range, max 3% stop). Target set at the top of the requested range
# on purpose: "grind on profits" means not clipping a winner early.
_STOP_PERCENT_NOMINAL = 3.0
_TARGET_PERCENT_NOMINAL = 5.0

# "Quick" enforced on the time axis too, not just price -- a trade that
# hits neither level in this window is squared off regardless of P&L.
_MAX_HOLD_MINUTES = 15

# Matches scripts/backtest/setups.py's EMA_RSI_CROSS exactly, so a live
# decision here means the same thing its own (never-run) backtest measured.
_EMA_FAST = 9
_EMA_SLOW = 21
_RSI_PERIOD = 14
_RSI_BULL = 55.0
_RSI_BEAR = 45.0

# Wide enough to warm up EMA21/RSI14 (needs ~21+ bars) with real margin, not
# so wide that the refresh call pulls more than it needs every minute.
_CANDLE_LOOKBACK_MINUTES = 180


def quick_scalp_action(bars: list[Bar]) -> str | None:
    """"BUY_CE" / "BUY_PE" if EMA9 just crossed EMA21 on the latest bar,
    confirmed by RSI(14), else None. Pure function over an ascending-time
    bar series -- no DB, no network, directly testable. Mirrors
    scripts/backtest/setups.py's EMA_RSI_CROSS (entry_offset=0) exactly:
    same crossover definition, same RSI thresholds. Fails closed on
    anything not fully warmed up -- a partial indicator series is not
    "no signal", it's "can't tell yet"."""
    if len(bars) < 2:
        return None
    closes = [bar.close for bar in bars]
    ema_fast = ema(closes, _EMA_FAST)
    ema_slow = ema(closes, _EMA_SLOW)
    rsi_values = rsi(bars, _RSI_PERIOD)
    fast_now, fast_prev = ema_fast[-1], ema_fast[-2]
    slow_now, slow_prev = ema_slow[-1], ema_slow[-2]
    rsi_now = rsi_values[-1]
    if None in (fast_now, fast_prev, slow_now, slow_prev, rsi_now):
        return None
    crossed_up = fast_prev <= slow_prev and fast_now > slow_now
    crossed_down = fast_prev >= slow_prev and fast_now < slow_now
    if crossed_up and rsi_now > _RSI_BULL:
        return "BUY_CE"
    if crossed_down and rsi_now < _RSI_BEAR:
        return "BUY_PE"
    return None


def _refresh_bars(db: Session, index: IndexConfig, smartapi: SmartAPIClient, now_ist) -> list[Bar]:
    """Own candle refresh, same pattern app.ai.originator's own
    _load_market_context uses -- a rolling window pull, upserted, so
    overlapping minutes are free and a transient failure just means this
    cycle works from whatever is already stored. Returns whatever is stored
    even on a failed refresh (fail-soft, not fail-closed) since a few
    minutes of staleness on a scalping signal is still better than no
    signal at all -- unlike AI Origination, there's no LLM call to waste on
    stale data if the trade never opens because the signal doesn't fire."""
    if index.spot_token:
        try:
            from_dt = (now_ist - timedelta(minutes=_CANDLE_LOOKBACK_MINUTES)).strftime("%Y-%m-%d %H:%M")
            to_dt = now_ist.strftime("%Y-%m-%d %H:%M")
            rows = smartapi.get_candles(
                exchange=index.spot_exchange, symboltoken=index.spot_token,
                interval=ONE_MINUTE, from_dt=from_dt, to_dt=to_dt,
            )
            if rows:
                store_bars(db, index.symbol, ONE_MINUTE, [parse_smartapi_row(row) for row in rows])
        except Exception as exc:
            logger.info("[QUICK_SCALP] %s: candle refresh failed (%s), using stored history", index.symbol, exc)
    return load_bars(db, index.symbol, ONE_MINUTE, limit=_CANDLE_LOOKBACK_MINUTES + 30)


def _has_open_quick_scalp_trade(db: Session, index_symbol: str) -> bool:
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


def open_quick_scalp_trade(
    db: Session,
    index: IndexConfig,
    action: str,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> Optional[StrategyTrade]:
    option_type = "CE" if action == "BUY_CE" else "PE"
    signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE

    try:
        contract = option_finder.find_atm_contract(signal, index, 0, min_dte=_MIN_DTE_TO_TRADE)
    except Exception as exc:
        logger.info("[QUICK_SCALP] %s: Skipped, could not resolve contract (%s)", index.symbol, exc)
        return None

    dte = days_to_expiry(contract.expiry, to_ist(utc_now()).date())
    if dte < _MIN_DTE_TO_TRADE:
        logger.info(
            "[QUICK_SCALP] %s: no expiry at least %s DTE out (nearest %s at %s DTE) -- skipping",
            index.symbol, _MIN_DTE_TO_TRADE, contract.expiry, dte,
        )
        return None

    try:
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[QUICK_SCALP] %s: Skipped, could not resolve price (%s)", index.symbol, exc)
        return None
    if not entry_price:
        logger.info("[QUICK_SCALP] %s: Skipped, LTP came back empty", index.symbol)
        return None

    stop_percent, stop_matched = symmetric_premium_percent(
        _STOP_PERCENT_NOMINAL, index.symbol, contract.option_type, dte
    )
    target_percent, _ = symmetric_premium_percent(
        _TARGET_PERCENT_NOMINAL, index.symbol, contract.option_type, dte
    )
    stoploss = round(entry_price * (1 - stop_percent / 100), 2)
    target = round(entry_price * (1 + target_percent / 100), 2)
    strategy_name = f"Quick Scalp - {index.display_name or index.symbol}"

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
        # FIXED + this origin (not AI_ORIGIN_*) means monitor_open_trades'
        # shared branch gives this a plain stop/target/time-exit only -- no
        # trailing, no STALL_EXIT. See module docstring's "WHY NO TRAILING".
        sl_mode=SLMode.FIXED,
        calibration_bucket_matched=stop_matched,
        origin=ORIGIN,
        ai_action=action,
        ai_reasoning=f"EMA_RSI_CROSS: EMA{_EMA_FAST}/EMA{_EMA_SLOW} crossover confirmed by RSI({_RSI_PERIOD}).",
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    log_event(
        db, "QUICK_SCALP",
        f"[{strategy_name}] Quick Scalp opened {signal.value} @ strike {trade.strike}",
        payload={"trade_id": trade.trade_id},
    )
    logger.info("[QUICK_SCALP] %s opened %s for %s", ORIGIN, signal.value, index.symbol)
    return trade


def check_quick_scalp_exits(db: Session, trade_manager) -> None:
    """Squares off any open QUICK_SCALP trade that has been held at least
    _MAX_HOLD_MINUTES without hitting the mechanical stop/target -- see
    module docstring's "MAX HOLD TIME". Reuses trade_manager.close_trade
    (the same helper the mechanical backstop uses) so both paths record a
    close identically."""
    trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == ORIGIN,
            )
        )
    )
    now_ist = to_ist(utc_now())
    for trade in trades:
        try:
            if trade.current_premium is None:
                continue
            entry_ist = to_ist(trade.entry_time)
            if entry_ist is None:
                continue
            held_minutes = (now_ist - entry_ist).total_seconds() / 60
            if held_minutes >= _MAX_HOLD_MINUTES:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.MAX_HOLD_EXIT)
                log_event(
                    db, "QUICK_SCALP",
                    f"[{trade.strategy_name}] squared off, held {held_minutes:.0f} min with neither level hit",
                    payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
                )
                logger.info(
                    "[QUICK_SCALP] %s squared off at max hold (%.0f min, %.2f%%)",
                    trade.trade_id, held_minutes, trade.pnl_percent or 0.0,
                )
        except Exception:
            logger.exception("[QUICK_SCALP] exit check failed for trade %s", trade.trade_id)


def check_quick_scalp_entry(
    db: Session,
    index: IndexConfig,
    bars: list[Bar],
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> Optional[StrategyTrade]:
    if _has_open_quick_scalp_trade(db, index.symbol):
        return None
    action = quick_scalp_action(bars)
    if action is None:
        return None
    logger.info("[QUICK_SCALP] %s -> %s", index.symbol, action)
    return open_quick_scalp_trade(db, index, action, smartapi, option_finder)


def run_quick_scalp_checks(
    smartapi: Optional[SmartAPIClient] = None,
    option_finder: Optional[OptionFinder] = None,
    trade_manager=None,
    db=None,
) -> None:
    """Scheduler entry point (see app.scheduler's "quick-scalp-check" job).
    Owns its own DB session when called from the scheduler; accepts an
    existing session in tests."""
    if smartapi is None or option_finder is None or trade_manager is None:
        logger.info("[QUICK_SCALP] Skipped: no smartapi/option_finder/trade_manager available in this context")
        return
    closed_reason = check_market_hours(utc_now())
    if closed_reason is not None:
        logger.info("[QUICK_SCALP] Cycle skipped -- %s", closed_reason.replace("Signal received ", "", 1))
        return
    owns_session = db is None
    session = db or SessionLocal()
    try:
        check_quick_scalp_exits(session, trade_manager)
        now_ist = to_ist(utc_now())
        for index in list_index_configs(session):
            if not index.enabled:
                continue
            try:
                bars = _refresh_bars(session, index, smartapi, now_ist)
                check_quick_scalp_entry(session, index, bars, smartapi, option_finder)
            except Exception:
                logger.exception("[QUICK_SCALP] entry check failed for %s", index.symbol)
    finally:
        if owns_session:
            session.close()
