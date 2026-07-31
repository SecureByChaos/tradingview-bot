"""Exit-outcome precompute.

THE KEY OPTIMISATION. An exit outcome depends only on the entry bar and the
risk parameters -- never on which gate selected that entry. So outcomes are
computed once per (risk combo, direction, bar), and every gate combination
afterwards is a boolean mask over those arrays plus a .mean().

That turns O(gates x risk x bars x forward) into O(risk x bars x forward) once,
plus masking. Hundreds of gate combinations become seconds rather than hours.

Memory: 64 risk combos x 2 directions x 37,000 bars x 4 bytes = ~19 MB per
array. With three arrays (pnl, reason, mfe) that is ~57 MB, which is more than
this box wants to hold at once, so callers should stream risk combos in
batches rather than materialising all 64 (see iter_risk_outcomes).

The forward-window simulation is chunked: a full (37,000 x 75) matrix is ~11 MB
per intermediate and there are several. Chunks of ~2,000 entry bars keep each
intermediate near 1 MB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np

from scripts.backtest.data import IndexArrays, forward_window_bounds

# Exit reason codes. Small ints rather than strings -- 37,000 x 128 Python
# strings is not something this box should be asked to hold.
REASON_OPEN = 0
REASON_STOP = 1
REASON_TARGET = 2
REASON_TRAIL = 3
REASON_EOD = 4

REASON_NAMES = {
    REASON_OPEN: "open",
    REASON_STOP: "stop",
    REASON_TARGET: "target",
    REASON_TRAIL: "trail",
    REASON_EOD: "eod",
}

# One full session at 5-minute resolution.
MAX_FORWARD_BARS = 75
ENTRY_CHUNK = 2000


@dataclass(frozen=True)
class RiskCombo:
    stop_pct: float
    target_pct: float
    trail_activate_pct: float | None
    trail_width_pct: float | None

    @property
    def label(self) -> tuple:
        return (self.stop_pct, self.target_pct, self.trail_activate_pct, self.trail_width_pct)


@dataclass
class Outcomes:
    """Per-bar outcome for one risk combo and one direction."""

    pnl_pct: np.ndarray     # float32, premium-space % return
    reason: np.ndarray      # int8
    mfe_pct: np.ndarray     # float32
    mae_pct: np.ndarray     # float32
    bars_held: np.ndarray   # int16


def compute_outcomes(
    arrays: IndexArrays,
    combo: RiskCombo,
    direction: int,
    premium_multiplier: float,
    max_forward_bars: int = MAX_FORWARD_BARS,
    theta_per_minute: float = 0.0,
    minutes_per_bar: int = 5,
) -> Outcomes:
    """Simulate the exit engine for every bar as a hypothetical entry.

    direction: +1 for a long call (profits when the index rises), -1 for a long
    put. Both are LONG option positions -- the sign describes which way the
    index must move, not a short.

    premium_multiplier converts an index % move into a premium % move. It is
    fitted from real option candles (see scripts/backtest/premium.py), never
    hardcoded, because the whole result scales linearly with it.

    Exit precedence mirrors the live engine in app/multi_strategy.py: trailing
    stop first once armed, then the original stop, then target, then
    end-of-session. Getting this order wrong here would make the backtest
    disagree with production in exactly the cases that matter most.
    """
    n = len(arrays)
    close = arrays.close.astype(np.float64)
    high = arrays.high.astype(np.float64)
    low = arrays.low.astype(np.float64)
    bounds = forward_window_bounds(arrays, max_forward_bars)

    pnl = np.zeros(n, dtype=np.float32)
    reason = np.full(n, REASON_OPEN, dtype=np.int8)
    mfe = np.zeros(n, dtype=np.float32)
    mae = np.zeros(n, dtype=np.float32)
    held = np.zeros(n, dtype=np.int16)

    use_trail = combo.trail_activate_pct is not None and combo.trail_width_pct is not None

    for chunk_start in range(0, n, ENTRY_CHUNK):
        chunk_end = min(chunk_start + ENTRY_CHUNK, n)
        for i in range(chunk_start, chunk_end):
            entry = close[i]
            if entry <= 0:
                continue
            last = int(bounds[i])
            if last <= i:
                continue

            # Premium-space running state.
            best = 0.0          # highest favourable premium % seen
            worst = 0.0         # deepest adverse premium % seen
            trail_armed = False
            exit_pnl = 0.0
            exit_reason = REASON_EOD
            exit_bar = last

            for j in range(i + 1, last + 1):
                # Favourable and adverse index extremes within the bar,
                # oriented by direction.
                if direction == 1:
                    fav_index_pct = (high[j] - entry) / entry * 100.0
                    adv_index_pct = (low[j] - entry) / entry * 100.0
                else:
                    fav_index_pct = (entry - low[j]) / entry * 100.0
                    adv_index_pct = (entry - high[j]) / entry * 100.0

                # Time decay, applied to both sides. A long option bleeds
                # premium whether or not the index moves, so leaving this at
                # zero makes every result optimistic -- most at 0-1 DTE, least
                # at 27. theta_per_minute is normally negative, so this shifts
                # the favourable side down and the adverse side further down.
                decay = theta_per_minute * (j - i) * minutes_per_bar
                fav = fav_index_pct * premium_multiplier + decay
                adv = adv_index_pct * premium_multiplier + decay
                best = max(best, fav)
                worst = min(worst, adv)

                # Trailing stop, armed once the position has proven itself.
                if use_trail and not trail_armed and best >= combo.trail_activate_pct:
                    trail_armed = True

                # Precedence: trail, stop, target. Within a single bar the
                # true sequence is unknowable from OHLC alone, so the adverse
                # side is checked first -- the pessimistic assumption, and the
                # right default when the alternative is flattering the result.
                if trail_armed:
                    trail_level = best - float(combo.trail_width_pct)
                    if adv <= trail_level:
                        exit_pnl, exit_reason, exit_bar = trail_level, REASON_TRAIL, j
                        break
                if adv <= -combo.stop_pct:
                    exit_pnl, exit_reason, exit_bar = -combo.stop_pct, REASON_STOP, j
                    break
                if fav >= combo.target_pct:
                    exit_pnl, exit_reason, exit_bar = combo.target_pct, REASON_TARGET, j
                    break
            else:
                # Never triggered: mark to the last close in the window.
                final = close[last]
                move = (final - entry) / entry * 100.0 * (1 if direction == 1 else -1)
                exit_pnl = move * premium_multiplier

            pnl[i] = exit_pnl
            reason[i] = exit_reason
            mfe[i] = best
            mae[i] = worst
            held[i] = exit_bar - i

    return Outcomes(pnl_pct=pnl, reason=reason, mfe_pct=mfe, mae_pct=mae, bars_held=held)


def iter_risk_outcomes(
    arrays: IndexArrays,
    combos: list[RiskCombo],
    premium_multiplier: float,
    directions: tuple[int, ...] = (1, -1),
) -> Iterator[tuple[RiskCombo, int, Outcomes]]:
    """Yield outcomes one combo/direction at a time so the caller can consume
    and discard. Materialising all 64 combos at once is ~57 MB, which this box
    would rather not spend."""
    for combo in combos:
        for direction in directions:
            yield combo, direction, compute_outcomes(arrays, combo, direction, premium_multiplier)
