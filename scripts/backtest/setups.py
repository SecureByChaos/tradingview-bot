"""Setup detection over the precomputed indicator arrays.

RELATIONSHIP TO app/market_context.py
-------------------------------------
The definitions here mirror `build_market_context` exactly -- same thresholds,
same close-beyond-and-held breakout rule, same failed-breakout window. They are
re-expressed as vectorised per-bar arrays because the live path needs one
answer for the current bar while the backtest needs 37,000 of them.

That duplication is a real risk (a drifting definition would make the backtest
describe a strategy nobody is running), so `assert_matches_live_context` below
spot-checks these arrays against `build_market_context` on sampled bars. Run it
whenever either side changes.

Constants are imported from app.market_context rather than redeclared, so a
threshold can only be changed in one place.

EVERY SIGNAL IS CAUSAL. A setup at bar i uses only bars <= i, and breakout
tests use only COMPLETED bars, i.e. strictly < i. Enforced by assertion, not
by comment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.market_context import (
    ADX_NO_TREND,
    EXTENDED_ATR_MULTIPLE,
    FAILED_BREAKOUT_LOOKBACK_BARS,
)
from scripts.backtest.data import IndexArrays

# Direction convention: +1 means the setup argues the index rises (buy CE),
# -1 means it argues the index falls (buy PE).
LONG = 1
SHORT = -1


@dataclass(frozen=True)
class Setup:
    """A named, parameterised directional signal."""

    name: str
    params: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        if not self.params:
            return self.name
        detail = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}[{detail}]"


def _held_beyond(close: np.ndarray, level: np.ndarray, above: bool, min_bars: int, session: np.ndarray) -> np.ndarray:
    """True where the last `min_bars` COMPLETED bars all closed beyond `level`.

    Shifted by one so bar i never inspects its own close -- a signal acted on
    at bar i must be knowable from bars strictly before it. Resets at session
    boundaries.
    """
    n = close.size
    beyond = np.zeros(n, dtype=bool)
    valid = ~np.isnan(level)
    if above:
        beyond[valid] = close[valid] > level[valid]
    else:
        beyond[valid] = close[valid] < level[valid]

    run = np.zeros(n, dtype=np.int32)
    for i in range(n):
        if beyond[i]:
            run[i] = run[i - 1] + 1 if (i > 0 and session[i - 1] == session[i]) else 1

    held = np.zeros(n, dtype=bool)
    held[1:] = run[:-1] >= min_bars          # shift: use the PREVIOUS bar's run
    held[0] = False
    same_session = np.zeros(n, dtype=bool)
    same_session[1:] = session[1:] == session[:-1]
    return held & same_session


def _failed_breakout(
    close: np.ndarray, level: np.ndarray, above: bool, lookback: int, session: np.ndarray
) -> np.ndarray:
    """Closed beyond a level within the lookback, then closed back inside.

    Fade signal: direction is opposite to the failed break. Uses completed bars
    only -- the "closed back inside" test is on bar i-1, not bar i.
    """
    n = close.size
    valid = ~np.isnan(level)
    beyond = np.zeros(n, dtype=bool)
    inside = np.zeros(n, dtype=bool)
    if above:
        beyond[valid] = close[valid] > level[valid]
        inside[valid] = close[valid] <= level[valid]
    else:
        beyond[valid] = close[valid] < level[valid]
        inside[valid] = close[valid] >= level[valid]

    out = np.zeros(n, dtype=bool)
    for i in range(1, n):
        if not inside[i - 1] or session[i] != session[i - 1]:
            continue
        start = max(0, i - 1 - lookback)
        window = slice(start, i - 1)
        if window.stop > window.start and beyond[window].any() and (session[window] == session[i]).all():
            out[i] = True
    return out


def build_signals(arrays: IndexArrays, setup: Setup) -> np.ndarray:
    """Per-bar direction array: +1 long, -1 short, 0 no signal."""
    n = len(arrays)
    close = arrays.close.astype(np.float64)
    session = arrays.session_id
    direction = np.zeros(n, dtype=np.int8)
    name = setup.name

    if name == "ORB_BREAK":
        hold = int(setup.params.get("hold", 1))
        up = _held_beyond(close, arrays.or_high.astype(np.float64), True, hold, session)
        down = _held_beyond(close, arrays.or_low.astype(np.float64), False, hold, session)
        direction[up] = LONG
        direction[down & ~up] = SHORT

    elif name == "PDH_PDL_BREAK":
        hold = int(setup.params.get("hold", 1))
        up = _held_beyond(close, arrays.pdh.astype(np.float64), True, hold, session)
        down = _held_beyond(close, arrays.pdl.astype(np.float64), False, hold, session)
        direction[up] = LONG
        direction[down & ~up] = SHORT

    elif name == "ST_ALIGNED":
        aligned_up = (arrays.st_5m_dir == 1) & (arrays.st_15m_dir == 1)
        aligned_down = (arrays.st_5m_dir == -1) & (arrays.st_15m_dir == -1)
        direction[aligned_up] = LONG
        direction[aligned_down] = SHORT

    elif name == "ST_ALIGNED_ADX":
        adx_min = float(setup.params.get("adx_min", ADX_NO_TREND))
        trend = ~np.isnan(arrays.adx14) & (arrays.adx14 >= adx_min)
        aligned_up = (arrays.st_5m_dir == 1) & (arrays.st_15m_dir == 1) & trend
        aligned_down = (arrays.st_5m_dir == -1) & (arrays.st_15m_dir == -1) & trend
        direction[aligned_up] = LONG
        direction[aligned_down] = SHORT

    elif name == "EMA_STACK":
        up = (arrays.ema9 > arrays.ema21) & (arrays.ema21 > arrays.ema50)
        down = (arrays.ema9 < arrays.ema21) & (arrays.ema21 < arrays.ema50)
        warm = ~np.isnan(arrays.ema50)
        direction[up & warm] = LONG
        direction[down & warm] = SHORT

    elif name == "FAILED_BREAKOUT":
        lookback = int(setup.params.get("lookback", FAILED_BREAKOUT_LOOKBACK_BARS))
        # A failed break ABOVE argues down, and vice versa -- this is a fade.
        failed_up = _failed_breakout(close, arrays.or_high.astype(np.float64), True, lookback, session)
        failed_down = _failed_breakout(close, arrays.or_low.astype(np.float64), False, lookback, session)
        direction[failed_up] = SHORT
        direction[failed_down & ~failed_up] = LONG

    elif name == "EXTENDED_FADE":
        atr_mult = float(setup.params.get("atr_mult", EXTENDED_ATR_MULTIPLE))
        adx_max = float(setup.params.get("adx_max", ADX_NO_TREND))
        weak = ~np.isnan(arrays.adx14) & (arrays.adx14 < adx_max)
        extension = arrays.extension_atr
        stretched_up = weak & (extension >= atr_mult)
        stretched_down = weak & (extension <= -atr_mult)
        # Extended above the mean argues down -- this is a fade.
        direction[stretched_up] = SHORT
        direction[stretched_down] = LONG

    elif name == "BNV7":
        direction = _bnv7_signals(arrays, setup.params)

    elif name == "NV1":
        direction = _nv1_signals(arrays, setup.params)

    elif name == "BNV6":
        direction = _bnv6_signals(arrays, setup.params)

    else:
        raise ValueError(f"Unknown setup: {name}")

    return direction


# ---------------------------------------------------------------------------
# Live strategy entry conditions, transcribed from the Pine sources.
#
# SCOPE: entry signals only. Exits, position state and sizing are owned by the
# bot (or, for BNV7, by v7_manager's own premium-percent engine), so what is
# reproduced here is the ENTRY QUALITY -- directly comparable to the indicator
# setups above under the same base-rate, bootstrap and direction-aware test.
#
# BNV5.1 is deliberately ABSENT -- tested separately, outside this sweep.
#
# BNV6 IS included below, now that FUTIDX candles exist
# (scripts/backfill_futures.py). It still only produces real signals when
# evaluated against a FUTIDX symbol's arrays: its VWAP condition is
# meaningless on spot index bars, where volume is identically zero, so
# arrays.vwap is NaN everywhere and _bnv6_signals simply never fires there.
# Reproducing it with the VWAP condition silently dropped -- rather than
# leaving it structurally unable to fire on the wrong instrument -- would
# test a different strategy and produce a confident, wrong comparison,
# exactly the failure the indicator-equivalence check exists to prevent.
# ---------------------------------------------------------------------------


def _session_hhmm(arrays: IndexArrays) -> np.ndarray:
    """Bar time as HHMM ints, for the strategies' own session windows."""
    times = arrays.ts.astype("datetime64[m]").astype(object)
    return np.array([t.hour * 100 + t.minute for t in times], dtype=np.int32)


def _apply_daily_cap(direction: np.ndarray, session: np.ndarray, max_per_day: int) -> np.ndarray:
    """Keep only the first `max_per_day` signals in each session.

    Both strategies cap daily trades. The cap does not change the edge of an
    individual signal, but it does change WHICH signals are taken, so leaving
    it out would measure a strategy nobody runs.
    """
    out = np.zeros_like(direction)
    count = 0
    current = -1
    for i in range(direction.size):
        if session[i] != current:
            current = int(session[i])
            count = 0
        if direction[i] != 0 and count < max_per_day:
            out[i] = direction[i]
            count += 1
    return out


def _bnv7_signals(arrays: IndexArrays, params: dict) -> np.ndarray:
    """BNV7: Supertrend cross + EMA20 filter + ADX, 09:30-14:30, max 3/day.

        buySignal  = crossover(close, supertrend) and close > ema and inSession
                     and adx > 18 and tradeCount < maxTrades
    """
    adx_min = float(params.get("adx_min", 18))
    max_trades = int(params.get("max_trades", 3))
    n = len(arrays)
    close = arrays.close.astype(np.float64)
    st_dir = arrays.st_5m_dir

    # ta.crossover(close, supertrend) is equivalent to the Supertrend direction
    # flipping to bullish, which is what st_5m_dir already encodes.
    flip_up = np.zeros(n, dtype=bool)
    flip_down = np.zeros(n, dtype=bool)
    flip_up[1:] = (st_dir[1:] == 1) & (st_dir[:-1] == -1)
    flip_down[1:] = (st_dir[1:] == -1) & (st_dir[:-1] == 1)

    hhmm = _session_hhmm(arrays)
    in_session = (hhmm >= 930) & (hhmm <= 1430)
    trending = ~np.isnan(arrays.adx14) & (arrays.adx14 > adx_min)
    warm = ~np.isnan(arrays.ema20)

    direction = np.zeros(n, dtype=np.int8)
    direction[flip_up & (close > arrays.ema20) & in_session & trending & warm] = LONG
    direction[flip_down & (close < arrays.ema20) & in_session & trending & warm] = SHORT
    return _apply_daily_cap(direction, arrays.session_id, max_trades)


def _nv1_signals(arrays: IndexArrays, params: dict) -> np.ndarray:
    """NV1: Supertrend flip held N bars + EMA20/50 trend + ADX + HTF agreement.

        entryReadyLong = bullHoldBars == confirmBars + 1
        longSignal = not longSwingUsed and inSession and underLimit and
                     not pastForceExit and entryReadyLong and trendUp and
                     adxOk and htfBull

    The `holdBars == confirmBars + 1` equality (not >=) makes this fire exactly
    once per swing, which is also what longSwingUsed enforces -- so the
    one-shot-per-swing behaviour falls out of the equality and needs no extra
    state here.
    """
    adx_min = float(params.get("adx_min", 22))
    confirm_bars = int(params.get("confirm_bars", 2))
    max_trades = int(params.get("max_trades", 3))
    n = len(arrays)
    st_dir = arrays.st_5m_dir

    # Consecutive bars in the current Supertrend direction.
    hold_bull = np.zeros(n, dtype=np.int32)
    hold_bear = np.zeros(n, dtype=np.int32)
    for i in range(n):
        if st_dir[i] == 1:
            hold_bull[i] = (hold_bull[i - 1] + 1) if i > 0 else 1
            hold_bear[i] = 0
        elif st_dir[i] == -1:
            hold_bear[i] = (hold_bear[i - 1] + 1) if i > 0 else 1
            hold_bull[i] = 0

    hhmm = _session_hhmm(arrays)
    in_session = (hhmm >= 920) & (hhmm <= 1445) & (hhmm < 1500)
    adx_ok = ~np.isnan(arrays.adx14) & (arrays.adx14 >= adx_min)
    warm = ~np.isnan(arrays.ema50) & ~np.isnan(arrays.htf_ema50)

    trend_up = arrays.ema20 > arrays.ema50
    trend_down = arrays.ema20 < arrays.ema50
    htf_bull = arrays.htf_ema20 > arrays.htf_ema50
    htf_bear = arrays.htf_ema20 < arrays.htf_ema50

    direction = np.zeros(n, dtype=np.int8)
    ready_long = hold_bull == (confirm_bars + 1)
    ready_short = hold_bear == (confirm_bars + 1)
    direction[ready_long & trend_up & adx_ok & htf_bull & in_session & warm] = LONG
    direction[ready_short & trend_down & adx_ok & htf_bear & in_session & warm] = SHORT
    return _apply_daily_cap(direction, arrays.session_id, max_trades)


def _apply_cooldown(direction: np.ndarray, cooldown_bars: int) -> np.ndarray:
    """Keep a signal only if at least `cooldown_bars` array positions have
    elapsed since the last kept signal (either direction), counted
    continuously across session boundaries -- NOT reset per session.

    This mirrors Pine's `bar_index - lastSignalBar >= cooldownBars` exactly:
    on an intraday-only chart, bar_index has no gap between the last bar of
    one session and the first bar of the next, so a cooldown armed late on
    one day can genuinely suppress a signal early the next. Distinct from
    _apply_daily_cap's per-session reset, which BNV7/NV1 use instead -- a
    different real mechanism in a different real strategy, not a stylistic
    choice to normalise away.
    """
    out = np.zeros_like(direction)
    last_signal = -(10**9)
    for i in range(direction.size):
        if direction[i] != 0 and (i - last_signal) >= cooldown_bars:
            out[i] = direction[i]
            last_signal = i
    return out


def _prior_rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    """max(values[i-window : i]) -- the window ending at the bar BEFORE i,
    never including bar i itself. Mirrors Pine's `ta.highest(series,
    window)[1]`. No session-reset: the source script doesn't gate this by
    session either, so a signal shortly after one session's open can
    legitimately reference the previous session's closing bars, exactly as
    the live indicator would."""
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    for i in range(window, n):
        out[i] = np.max(values[i - window:i])
    return out


def _prior_rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    """min(values[i-window : i]), see _prior_rolling_max."""
    n = values.size
    out = np.full(n, np.nan, dtype=np.float64)
    for i in range(window, n):
        out[i] = np.min(values[i - window:i])
    return out


def _bnv6_signals(arrays: IndexArrays, params: dict) -> np.ndarray:
    """BNV6.2 Momentum, transcribed verbatim from the Pine source:

        bull_trend    = ema9 > ema21 and close > ema9
        above_vwap    = close > ta.vwap(close)
        rsi_bull      = rsi > 55
        htf_bull      = ema9_15m > ema21_15m
        atr_ok        = atr > 80.0
        strong_trend  = abs(ema9 - ema21) > atr * 0.15
        bull_breakout = close > ta.highest(high, 3)[1]
        long_setup    = bull_trend and above_vwap and rsi_bull and htf_bull
                         and atr_ok and strong_trend and bull_breakout

    (mirrored for short_setup/bear_*/below_vwap/rsi_bear). A signal is kept
    only if at least `cooldown_bars` (default 24) bars have elapsed since the
    last kept signal in EITHER direction -- see _apply_cooldown.

    ta.vwap(close) is session-anchored VWAP using CLOSE as the source series,
    not the more common hlc3 -- an exact, deliberate detail of the source
    script, not a simplification. Needs real volume, so this only fires on
    FUTIDX arrays; see the module-level comment above and
    scripts/backfill_futures.py.
    """
    atr_threshold = float(params.get("atr_threshold", 80.0))
    trend_strength_factor = float(params.get("trend_strength_factor", 0.15))
    rsi_bull_level = float(params.get("rsi_bull_level", 55.0))
    rsi_bear_level = float(params.get("rsi_bear_level", 45.0))
    breakout_bars = int(params.get("breakout_bars", 3))
    cooldown_bars = int(params.get("cooldown_bars", 24))

    close = arrays.close.astype(np.float64)
    high = arrays.high.astype(np.float64)
    low = arrays.low.astype(np.float64)
    ema9 = arrays.ema9.astype(np.float64)
    ema21 = arrays.ema21.astype(np.float64)
    atr14 = arrays.atr14.astype(np.float64)
    rsi14 = arrays.rsi14.astype(np.float64)
    vwap = arrays.vwap.astype(np.float64)
    htf_ema9 = arrays.htf_ema9.astype(np.float64)
    htf_ema21 = arrays.htf_ema21.astype(np.float64)

    bull_trend = (ema9 > ema21) & (close > ema9)
    bear_trend = (ema9 < ema21) & (close < ema9)
    above_vwap = close > vwap
    below_vwap = close < vwap
    rsi_bull = rsi14 > rsi_bull_level
    rsi_bear = rsi14 < rsi_bear_level
    htf_bull = htf_ema9 > htf_ema21
    htf_bear = htf_ema9 < htf_ema21
    atr_ok = atr14 > atr_threshold
    ema_gap = np.abs(ema9 - ema21)
    strong_trend = ema_gap > (atr14 * trend_strength_factor)

    prior_high = _prior_rolling_max(high, breakout_bars)
    prior_low = _prior_rolling_min(low, breakout_bars)
    bull_breakout = close > prior_high
    bear_breakout = close < prior_low

    warm = (
        ~np.isnan(ema9) & ~np.isnan(ema21) & ~np.isnan(atr14) & ~np.isnan(rsi14)
        & ~np.isnan(vwap) & ~np.isnan(htf_ema9) & ~np.isnan(htf_ema21)
        & ~np.isnan(prior_high) & ~np.isnan(prior_low)
    )

    long_setup = warm & bull_trend & above_vwap & rsi_bull & htf_bull & atr_ok & strong_trend & bull_breakout
    short_setup = warm & bear_trend & below_vwap & rsi_bear & htf_bear & atr_ok & strong_trend & bear_breakout

    direction = np.zeros(len(arrays), dtype=np.int8)
    direction[long_setup] = LONG
    direction[short_setup & ~long_setup] = SHORT
    return _apply_cooldown(direction, cooldown_bars)


def default_setups() -> list[Setup]:
    """The full sweep, declared BEFORE any result is inspected.

    Fixing this list up front is what makes the multiple-comparison count
    honest. Adding a setup after seeing results, or quietly dropping one that
    underperformed, invalidates the correction.
    """
    setups: list[Setup] = []
    for hold in (1, 2, 3):
        setups.append(Setup("ORB_BREAK", {"hold": hold}))
        setups.append(Setup("PDH_PDL_BREAK", {"hold": hold}))
    setups.append(Setup("ST_ALIGNED"))
    for adx_min in (20, 22, 25, 28):
        setups.append(Setup("ST_ALIGNED_ADX", {"adx_min": adx_min}))
    setups.append(Setup("EMA_STACK"))
    for lookback in (3, 6):
        setups.append(Setup("FAILED_BREAKOUT", {"lookback": lookback}))
    for atr_mult in (1.5, 2.0):
        setups.append(Setup("EXTENDED_FADE", {"atr_mult": atr_mult}))
    # Live strategies, at their production parameters only -- no sweep. These
    # are being evaluated as configured, not optimised, so adding parameter
    # variants would both inflate the comparison count and answer a different
    # question.
    setups.append(Setup("BNV7"))
    setups.append(Setup("NV1"))
    setups.append(Setup("BNV6"))
    return setups


def assert_causal(arrays: IndexArrays, setup: Setup, signals: np.ndarray) -> None:
    """Look-ahead assertions, executable rather than aspirational."""
    n = len(arrays)

    # Opening-range setups cannot fire before the range has closed at 09:45.
    if setup.name in ("ORB_BREAK", "FAILED_BREAKOUT"):
        undefined = np.isnan(arrays.or_high)
        assert not np.any(signals[undefined] != 0), (
            f"{setup.label} fired on {int(np.sum(signals[undefined] != 0))} bars where the "
            "opening range was not yet defined"
        )

    # ADX-gated setups cannot fire before ADX has warmed up.
    if setup.name in ("ST_ALIGNED_ADX", "EXTENDED_FADE", "BNV7", "NV1"):
        cold = np.isnan(arrays.adx14)
        assert not np.any(signals[cold] != 0), f"{setup.label} fired before ADX warmed up"

    # NV1 requires the 15-minute HTF EMAs; it cannot fire before they exist.
    if setup.name == "NV1":
        cold_htf = np.isnan(arrays.htf_ema50)
        assert not np.any(signals[cold_htf] != 0), "NV1 fired before the HTF EMAs warmed up"

    # BNV6 requires VWAP (never warms up on spot index bars -- zero volume --
    # which is exactly the point) and its own 15-minute HTF EMA9/21 pair.
    if setup.name == "BNV6":
        cold_bnv6 = np.isnan(arrays.vwap) | np.isnan(arrays.htf_ema9) | np.isnan(arrays.htf_ema21)
        assert not np.any(signals[cold_bnv6] != 0), (
            f"{setup.label} fired before VWAP/HTF EMA9/21 warmed up"
        )

    # No signal may reference a bar in a different session than its own via a
    # run that crossed the boundary.
    boundaries = np.flatnonzero(np.diff(arrays.session_id, prepend=arrays.session_id[0]) != 0)
    if setup.name in ("ORB_BREAK", "PDH_PDL_BREAK") and boundaries.size:
        assert not np.any(signals[boundaries] != 0), (
            f"{setup.label} fired on the first bar of a session, which cannot have a "
            "completed-bar hold history"
        )
    assert signals.shape == (n,)
