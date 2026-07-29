"""Structural market context for AI Origination: levels, CPR regime, and setups.

PHASE 1 SCOPE. Everything here is computed, stored and logged, but is NOT fed
to the entry prompt yet. Phase 0 (the trailing stop) needs a clean week of
single-variable measurement first; adding market context to the prompt at the
same time would make it impossible to attribute any change in results. See
docs/ai-origination-roadmap.md.

What this addresses: the diagnosed failure mode is that the model reads *being
at an extreme* as directional evidence -- "price is at session highs with a
steady uptrend" preceded losses of -15.56%, -15.65%, -13.70% and -11.26%. Over
a rising 45-minute window the current price IS the window high by construction,
so that observation restates "price went up" and carries no information.
Structural levels (opening range, previous day, CPR) and a trend-existence
measure (ADX) are what distinguish a high inside a trend from a high at the top
of a range.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from app.indicators import ADXPoint, SupertrendPoint, adx, atr, ema, last_valid, rsi, supertrend
from app.market_data import Bar

logger = logging.getLogger(__name__)

# Opening range. The trading-start gate moved to 09:45 so that the range is
# always complete before any entry is considered -- previously entries began at
# 09:30, leaving a 15-minute window where every ORB-derived setup was undefined
# precisely when breakout logic matters most. Fewer trades with full structural
# context is the direction the cost arithmetic demands anyway.
OPENING_RANGE_START = time(9, 15)
OPENING_RANGE_END = time(9, 45)

# CPR width thresholds, as a percentage of the pivot. Starting values only --
# these MUST be calibrated against a real backtest before being trusted. They
# are the single most assumption-laden numbers in this module.
CPR_NARROW_MAX_PERCENT = 0.20
CPR_WIDE_MIN_PERCENT = 0.50

# ADX bands. Below 20 there is no trend to continue.
ADX_NO_TREND = 20.0
ADX_TRENDING = 25.0

# A breakout requires a completed bar to CLOSE beyond the level. A wick
# touching it does not qualify -- that distinction is most of the difference
# between a breakout and a failed breakout.
FAILED_BREAKOUT_LOOKBACK_BARS = 6
EXTENDED_ATR_MULTIPLE = 2.0


@dataclass
class Levels:
    opening_range_high: float | None = None
    opening_range_low: float | None = None
    opening_range_complete: bool = False
    previous_day_high: float | None = None
    previous_day_low: float | None = None
    previous_day_close: float | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None


@dataclass
class CPR:
    pivot: float
    top: float
    bottom: float
    width_percent: float
    classification: str  # NARROW / MODERATE / WIDE


@dataclass
class MarketContext:
    index_symbol: str
    as_of: datetime
    spot: float
    levels: Levels
    cpr: CPR | None
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    atr_value: float | None
    atr_percent: float | None
    rsi_value: float | None
    ema9: float | None
    ema21: float | None
    ema50: float | None
    supertrend_5m: int | None
    supertrend_15m: int | None
    htf_ema20: float | None
    htf_ema50: float | None
    distance_from_ema21_atr: float | None
    day_range_atr_multiple: float | None
    setups: dict[str, bool] = field(default_factory=dict)
    setup_strength: dict[str, float] = field(default_factory=dict)
    regime: str = "UNKNOWN"

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_symbol": self.index_symbol,
            "as_of": self.as_of.isoformat(),
            "spot": self.spot,
            "levels": {
                "opening_range_high": self.levels.opening_range_high,
                "opening_range_low": self.levels.opening_range_low,
                "opening_range_complete": self.levels.opening_range_complete,
                "previous_day_high": self.levels.previous_day_high,
                "previous_day_low": self.levels.previous_day_low,
                "previous_day_close": self.levels.previous_day_close,
                "day_open": self.levels.day_open,
                "day_high": self.levels.day_high,
                "day_low": self.levels.day_low,
            },
            "cpr": None if self.cpr is None else {
                "pivot": self.cpr.pivot,
                "top": self.cpr.top,
                "bottom": self.cpr.bottom,
                "width_percent": self.cpr.width_percent,
                "classification": self.cpr.classification,
            },
            "adx": self.adx,
            "plus_di": self.plus_di,
            "minus_di": self.minus_di,
            "atr": self.atr_value,
            "atr_percent": self.atr_percent,
            "rsi": self.rsi_value,
            "ema9": self.ema9,
            "ema21": self.ema21,
            "ema50": self.ema50,
            "supertrend_5m": self.supertrend_5m,
            "supertrend_15m": self.supertrend_15m,
            "htf_ema20": self.htf_ema20,
            "htf_ema50": self.htf_ema50,
            "distance_from_ema21_atr": self.distance_from_ema21_atr,
            "day_range_atr_multiple": self.day_range_atr_multiple,
            "regime": self.regime,
            "setups": {k: v for k, v in self.setups.items() if v},
            "setup_strength": self.setup_strength,
        }


def compute_cpr(prev_high: float, prev_low: float, prev_close: float) -> CPR:
    """Central Pivot Range from the previous session's OHLC.

    The one piece of context none of the existing BNV/NV strategies has, and it
    addresses the gap directly: nothing in the engine currently has any concept
    of "today is likely to chop, stop taking breakout signals."

    Narrow CPR is associated with trending days, wide CPR with range-bound
    ones. Costs nothing -- pure arithmetic on data already stored.
    """
    pivot = (prev_high + prev_low + prev_close) / 3
    bottom = (prev_high + prev_low) / 2
    top = (pivot - bottom) + pivot
    width_percent = abs(top - bottom) / pivot * 100 if pivot else 0.0
    if width_percent <= CPR_NARROW_MAX_PERCENT:
        classification = "NARROW"
    elif width_percent >= CPR_WIDE_MIN_PERCENT:
        classification = "WIDE"
    else:
        classification = "MODERATE"
    return CPR(
        pivot=round(pivot, 2),
        top=round(max(top, bottom), 2),
        bottom=round(min(top, bottom), 2),
        width_percent=round(width_percent, 4),
        classification=classification,
    )


def compute_levels(bars_5m: list[Bar], session_date: date) -> Levels:
    """Opening range, previous session and today-so-far levels from 5-min bars."""
    levels = Levels()
    today_bars = [b for b in bars_5m if b.ts_ist.date() == session_date]
    previous_bars = [b for b in bars_5m if b.ts_ist.date() < session_date]

    if previous_bars:
        last_day = max(b.ts_ist.date() for b in previous_bars)
        prev_day_bars = [b for b in previous_bars if b.ts_ist.date() == last_day]
        if prev_day_bars:
            levels.previous_day_high = round(max(b.high for b in prev_day_bars), 2)
            levels.previous_day_low = round(min(b.low for b in prev_day_bars), 2)
            levels.previous_day_close = round(prev_day_bars[-1].close, 2)

    if today_bars:
        levels.day_open = round(today_bars[0].open, 2)
        levels.day_high = round(max(b.high for b in today_bars), 2)
        levels.day_low = round(min(b.low for b in today_bars), 2)

        opening = [
            b for b in today_bars
            if OPENING_RANGE_START <= b.ts_ist.time() < OPENING_RANGE_END
        ]
        if opening:
            levels.opening_range_high = round(max(b.high for b in opening), 2)
            levels.opening_range_low = round(min(b.low for b in opening), 2)
            # Complete only once a bar starting at or after 09:45 exists --
            # i.e. the range has actually closed, not merely been sampled.
            levels.opening_range_complete = any(
                b.ts_ist.time() >= OPENING_RANGE_END for b in today_bars
            )
    return levels


def _bars_held_beyond(bars: list[Bar], level: float, above: bool) -> int:
    """How many consecutive completed bars have closed beyond a level.

    Follow-through, not a single close. One bar closing past a level is noise
    often enough that acting on it is most of what "false breakout" means.
    """
    count = 0
    for bar in reversed(bars):
        if (above and bar.close > level) or (not above and bar.close < level):
            count += 1
        else:
            break
    return count


def _failed_breakout(bars: list[Bar], level: float, above: bool, lookback: int) -> bool:
    """Closed beyond a level within the lookback, then closed back inside.

    This is the direct counter to the diagnosed defect: the model treats an
    extreme as continuation evidence, and this is the pattern that says the
    extreme was rejected.
    """
    if len(bars) < 2:
        return False
    window = bars[-lookback:]
    breached = any(
        (bar.close > level) if above else (bar.close < level)
        for bar in window[:-1]
    )
    if not breached:
        return False
    latest = window[-1]
    return (latest.close <= level) if above else (latest.close >= level)


def build_market_context(
    index_symbol: str,
    bars_1m: list[Bar],
    bars_5m: list[Bar],
    bars_15m: list[Bar],
    spot: float,
    as_of: datetime,
) -> MarketContext | None:
    """Assemble the full context. Returns None when there isn't enough history
    to compute it honestly.

    Fail-closed by design: a context with three of nine sections populated is
    worse than no context, because partial data still reads as authoritative.
    Callers treat None as "skip origination this cycle" rather than falling
    back to a thinner prompt.
    """
    if len(bars_5m) < 30 or len(bars_15m) < 10:
        logger.info(
            "[CONTEXT] %s: insufficient history (5m=%s, 15m=%s)",
            index_symbol, len(bars_5m), len(bars_15m),
        )
        return None

    closes_5m = [b.close for b in bars_5m]
    ema9_series = ema(closes_5m, 9)
    ema21_series = ema(closes_5m, 21)
    ema50_series = ema(closes_5m, 50)
    atr_series = atr(bars_5m, 14)
    rsi_series = rsi(bars_5m, 14)
    adx_series = adx(bars_5m, 14)
    st_fast = supertrend(bars_5m, period=10, multiplier=2.0)
    st_5m = supertrend(bars_5m, period=10, multiplier=3.0)
    st_15m = supertrend(bars_15m, period=7, multiplier=3.0)

    closes_15m = [b.close for b in bars_15m]
    htf_ema20 = last_valid(ema(closes_15m, 20))
    htf_ema50 = last_valid(ema(closes_15m, 50))

    adx_point: ADXPoint | None = last_valid(adx_series)  # type: ignore[assignment]
    st_5m_point: SupertrendPoint | None = last_valid(st_5m)  # type: ignore[assignment]
    st_15m_point: SupertrendPoint | None = last_valid(st_15m)  # type: ignore[assignment]
    st_fast_point: SupertrendPoint | None = last_valid(st_fast)  # type: ignore[assignment]

    ema9_value = last_valid(ema9_series)
    ema21_value = last_valid(ema21_series)
    ema50_value = last_valid(ema50_series)
    atr_value = last_valid(atr_series)
    rsi_value = last_valid(rsi_series)

    levels = compute_levels(bars_5m, as_of.date())
    cpr = None
    if levels.previous_day_high and levels.previous_day_low and levels.previous_day_close:
        cpr = compute_cpr(levels.previous_day_high, levels.previous_day_low, levels.previous_day_close)

    distance_atr = None
    if ema21_value and atr_value:
        distance_atr = round((spot - float(ema21_value)) / float(atr_value), 2)
    day_range_atr = None
    if levels.day_high and levels.day_low and atr_value:
        day_range_atr = round((levels.day_high - levels.day_low) / float(atr_value), 2)

    adx_now = round(adx_point.adx, 2) if adx_point else None
    completed = bars_5m[:-1] if len(bars_5m) > 1 else bars_5m

    setups: dict[str, bool] = {}
    strength: dict[str, float] = {}

    trend_ok = adx_now is not None and adx_now >= ADX_NO_TREND
    st_aligned_up = bool(st_5m_point and st_15m_point and st_5m_point.direction == 1 and st_15m_point.direction == 1)
    st_aligned_down = bool(st_5m_point and st_15m_point and st_5m_point.direction == -1 and st_15m_point.direction == -1)
    setups["ST_ALIGNED_UP"] = st_aligned_up and trend_ok
    setups["ST_ALIGNED_DOWN"] = st_aligned_down and trend_ok

    stack_up = bool(ema9_value and ema21_value and ema50_value and ema9_value > ema21_value > ema50_value)
    stack_down = bool(ema9_value and ema21_value and ema50_value and ema9_value < ema21_value < ema50_value)
    setups["EMA_STACK_UP"] = stack_up and trend_ok
    setups["EMA_STACK_DOWN"] = stack_down and trend_ok

    orh, orl = levels.opening_range_high, levels.opening_range_low
    if levels.opening_range_complete and orh and orl:
        held_up = _bars_held_beyond(completed, orh, above=True)
        held_down = _bars_held_beyond(completed, orl, above=False)
        setups["ORB_BREAK_UP"] = held_up > 0
        setups["ORB_BREAK_DOWN"] = held_down > 0
        strength["orb_bars_held_up"] = float(held_up)
        strength["orb_bars_held_down"] = float(held_down)
        setups["FAILED_BREAKOUT_UP"] = _failed_breakout(completed, orh, True, FAILED_BREAKOUT_LOOKBACK_BARS)
        setups["FAILED_BREAKOUT_DOWN"] = _failed_breakout(completed, orl, False, FAILED_BREAKOUT_LOOKBACK_BARS)

    pdh, pdl = levels.previous_day_high, levels.previous_day_low
    if pdh and pdl:
        held_pdh = _bars_held_beyond(completed, pdh, above=True)
        held_pdl = _bars_held_beyond(completed, pdl, above=False)
        setups["PDH_BREAK"] = held_pdh > 0
        setups["PDL_BREAK"] = held_pdl > 0
        strength["pdh_bars_held"] = float(held_pdh)
        strength["pdl_bars_held"] = float(held_pdl)

    setups["EXTENDED_FROM_MEAN"] = bool(
        distance_atr is not None
        and abs(distance_atr) >= EXTENDED_ATR_MULTIPLE
        and adx_now is not None
        and adx_now < ADX_NO_TREND
    )

    # Regime, not "day type": CPR is fixed at the open but ADX moves all day,
    # so this legitimately changes during the session and should not be read as
    # a morning verdict.
    regime = "UNKNOWN"
    if cpr and adx_now is not None:
        if cpr.classification == "WIDE" and adx_now < ADX_NO_TREND:
            regime = "RANGE"
        elif cpr.classification == "NARROW" and adx_now > ADX_TRENDING:
            regime = "TREND"
        else:
            regime = "MIXED"
    setups["RANGE_REGIME"] = regime == "RANGE"
    setups["TREND_REGIME"] = regime == "TREND"

    if adx_now is not None:
        strength["adx"] = adx_now
    if st_fast_point:
        strength["supertrend_fast_direction"] = float(st_fast_point.direction)

    return MarketContext(
        index_symbol=index_symbol,
        as_of=as_of,
        spot=spot,
        levels=levels,
        cpr=cpr,
        adx=adx_now,
        plus_di=round(adx_point.plus_di, 2) if adx_point else None,
        minus_di=round(adx_point.minus_di, 2) if adx_point else None,
        atr_value=round(float(atr_value), 2) if atr_value else None,
        atr_percent=round(float(atr_value) / spot * 100, 3) if atr_value and spot else None,
        rsi_value=round(float(rsi_value), 2) if rsi_value else None,
        ema9=round(float(ema9_value), 2) if ema9_value else None,
        ema21=round(float(ema21_value), 2) if ema21_value else None,
        ema50=round(float(ema50_value), 2) if ema50_value else None,
        supertrend_5m=st_5m_point.direction if st_5m_point else None,
        supertrend_15m=st_15m_point.direction if st_15m_point else None,
        htf_ema20=round(float(htf_ema20), 2) if htf_ema20 else None,
        htf_ema50=round(float(htf_ema50), 2) if htf_ema50 else None,
        distance_from_ema21_atr=distance_atr,
        day_range_atr_multiple=day_range_atr,
        setups=setups,
        setup_strength=strength,
        regime=regime,
    )
