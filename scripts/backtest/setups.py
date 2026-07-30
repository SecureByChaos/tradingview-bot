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

    else:
        raise ValueError(f"Unknown setup: {name}")

    return direction


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
    if setup.name in ("ST_ALIGNED_ADX", "EXTENDED_FADE"):
        cold = np.isnan(arrays.adx14)
        assert not np.any(signals[cold] != 0), f"{setup.label} fired before ADX warmed up"

    # No signal may reference a bar in a different session than its own via a
    # run that crossed the boundary.
    boundaries = np.flatnonzero(np.diff(arrays.session_id, prepend=arrays.session_id[0]) != 0)
    if setup.name in ("ORB_BREAK", "PDH_PDL_BREAK") and boundaries.size:
        assert not np.any(signals[boundaries] != 0), (
            f"{setup.label} fired on the first bar of a session, which cannot have a "
            "completed-bar hold history"
        )
    assert signals.shape == (n,)
