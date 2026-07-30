"""Step 4: the baseline. Run this before building the gate sweep.

Two questions, both of which can kill the rest of the plan:

  1. Does the current AI Origination rule -- 45-minute spot drift implies
     direction -- beat the unconditional base rate at all? If it is 50/50,
     the model is not being let down by thin context; the premise it reasons
     from has no signal in it, and a gate sweep is answering a different
     question.

  2. Is a stop of the size currently in use survivable? The live stops sit
     around 10-15% of premium, which at a fitted multiplier translates to a
     small number of index points. If normal intraday noise routinely exceeds
     that distance, trades are being stopped by noise rather than by being
     wrong, and no amount of entry filtering fixes it.

Usage:
    python -m scripts.backtest_baseline --db data/platform.sqlite3
    python -m scripts.backtest_baseline --db data/platform.sqlite3 --smoke

Run outside market hours. On the Lightsail box:
    systemd-run --scope -p MemoryMax=150M --nice=19 \
      python -m scripts.backtest_baseline --db data/platform.sqlite3
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from datetime import datetime, time

import numpy as np

from scripts.backtest.data import build_arrays, forward_window_bounds, load_bars_sqlite
from scripts.backtest.outcomes import REASON_NAMES, RiskCombo, compute_outcomes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backtest_baseline")

LOOKBACK_MINUTES = 45
BARS_PER_LOOKBACK = LOOKBACK_MINUTES // 5
TRADING_START = time(9, 45)
TRADING_END = time(15, 15)
FORWARD_CHECKS = (6, 12)  # 30 and 60 minutes at 5-minute bars

# Smoke-test coefficient ONLY. The real number comes from
# scripts/backtest/premium.py fitted against the option archive. Using this in
# a reported result would make that result a restatement of this guess.
SMOKE_MULTIPLIER = 0.5


def _tradeable_mask(arrays) -> np.ndarray:
    """Bars eligible as entries: inside the live trading window, with enough
    lookback and warm indicators."""
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_window = np.array(
        [TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool
    )
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21)
    enough_lookback = np.zeros(len(arrays), dtype=bool)
    enough_lookback[BARS_PER_LOOKBACK:] = (
        arrays.session_id[BARS_PER_LOOKBACK:] == arrays.session_id[:-BARS_PER_LOOKBACK]
    )
    return in_window & warm & enough_lookback


def _drift_direction(arrays) -> tuple[np.ndarray, np.ndarray]:
    """The production rule, mechanically: sign of the 45-minute spot drift.

    This is the proxy for what the model is handed. It does not reproduce the
    model's judgment, but it does reproduce the ONLY directional information
    the model receives -- so if this has no edge, neither does anything built
    on it.
    """
    n = len(arrays)
    direction = np.zeros(n, dtype=np.int8)
    close = arrays.close.astype(np.float64)
    past = np.empty(n, dtype=np.float64)
    past[:] = np.nan
    past[BARS_PER_LOOKBACK:] = close[:-BARS_PER_LOOKBACK]
    with np.errstate(invalid="ignore"):
        drift = (close - past) / past * 100.0
    direction[drift > 0] = 1
    direction[drift < 0] = -1
    return direction, drift


def _binomial_z(successes: int, n: int, p0: float = 0.5) -> tuple[float, float]:
    """Normal-approximation z and two-sided p, stdlib only (no scipy on this box)."""
    if n == 0:
        return 0.0, 1.0
    p_hat = successes / n
    se = math.sqrt(p0 * (1 - p0) / n)
    z = (p_hat - p0) / se if se > 0 else 0.0
    p = math.erfc(abs(z) / math.sqrt(2))
    return z, p


def _directional_baseline(arrays, eligible: np.ndarray, direction: np.ndarray, drift: np.ndarray) -> None:
    close = arrays.close.astype(np.float64)
    bounds = forward_window_bounds(arrays, max(FORWARD_CHECKS))
    n = len(arrays)

    for forward_bars in FORWARD_CHECKS:
        target_index = np.minimum(np.arange(n) + forward_bars, bounds)
        valid = eligible & (target_index > np.arange(n)) & (direction != 0)
        idx = np.flatnonzero(valid)
        if idx.size == 0:
            logger.warning("  no eligible bars for %s-bar forward window", forward_bars)
            continue
        entry = close[idx]
        exit_price = close[target_index[idx]]
        signed = (exit_price - entry) / entry * 100.0 * direction[idx]
        wins = int(np.sum(signed > 0))
        z, p = _binomial_z(wins, idx.size)
        logger.info(
            "  %2d min forward | n=%6d | hit=%.2f%% | mean=%+.4f%% | median=%+.4f%% | z=%+.2f p=%.4f",
            forward_bars * 5, idx.size, wins / idx.size * 100,
            float(np.mean(signed)), float(np.median(signed)), z, p,
        )

    # Does a bigger drift predict better? If the rule has any edge at all it
    # should concentrate here; a flat profile across magnitude is the
    # signature of noise.
    logger.info("  by drift magnitude (60 min forward):")
    target_index = np.minimum(np.arange(n) + 12, bounds)
    valid = eligible & (target_index > np.arange(n)) & (direction != 0)
    magnitudes = np.abs(drift)
    for low, high in ((0.0, 0.1), (0.1, 0.25), (0.25, 0.5), (0.5, 99.0)):
        band = valid & (magnitudes >= low) & (magnitudes < high)
        idx = np.flatnonzero(band)
        if idx.size < 50:
            continue
        entry = close[idx]
        signed = (close[target_index[idx]] - entry) / entry * 100.0 * direction[idx]
        wins = int(np.sum(signed > 0))
        z, p = _binomial_z(wins, idx.size)
        logger.info(
            "    drift %.2f-%.2f%% | n=%6d | hit=%.2f%% | mean=%+.4f%% | z=%+.2f p=%.4f",
            low, high, idx.size, wins / idx.size * 100, float(np.mean(signed)), z, p,
        )


def _stop_survivability(arrays, eligible: np.ndarray, direction: np.ndarray, multiplier: float) -> None:
    """How often does ordinary noise reach the stop before the trade is wrong?

    Reported as the adverse excursion distribution in index points and in
    premium %% at the given multiplier, against the stop sizes actually in use.
    """
    logger.info("  adverse excursion within 60 min (premium %%, multiplier=%.2f):", multiplier)
    combo = RiskCombo(stop_pct=999.0, target_pct=999.0, trail_activate_pct=None, trail_width_pct=None)
    for sign, label in ((1, "CE (index up)"), (-1, "PE (index down)")):
        outcomes = compute_outcomes(arrays, combo, sign, multiplier, max_forward_bars=12)
        mask = eligible & (direction == sign)
        mae = outcomes.mae_pct[mask]
        mfe = outcomes.mfe_pct[mask]
        if mae.size == 0:
            continue
        # MAE is negative, so the WORSE tail is the LOW percentile of the
        # value. Labelled by severity to avoid the ambiguity: "worst 10%"
        # means the 10th percentile of a negative distribution.
        logger.info(
            "    %-14s n=%6d | MAE median=%+.2f worst-25%%=%+.2f worst-10%%=%+.2f "
            "| MFE median=%+.2f best-10%%=%+.2f",
            label, mae.size,
            float(np.percentile(mae, 50)), float(np.percentile(mae, 25)),
            float(np.percentile(mae, 10)),
            float(np.percentile(mfe, 50)), float(np.percentile(mfe, 90)),
        )
        for stop in (10.0, 12.0, 15.0):
            hit = float(np.mean(mae <= -stop) * 100)
            logger.info("      stop %4.1f%% would be reached by noise in %.1f%% of bars", stop, hit)


def _risk_sanity(arrays, eligible: np.ndarray, direction: np.ndarray, multiplier: float) -> None:
    """Current live risk band, run over every eligible bar. Not a strategy --
    a floor. If the exit engine loses money on unconditional entries, the gate
    sweep is looking for a filter good enough to overcome the exits, which is
    a much harder ask than improving entries."""
    combo = RiskCombo(stop_pct=12.0, target_pct=20.0, trail_activate_pct=8.0, trail_width_pct=5.0)
    logger.info("  live risk band (stop 12%%, target 20%%, trail 8%%/5%%):")
    for sign, label in ((1, "CE"), (-1, "PE")):
        outcomes = compute_outcomes(arrays, combo, sign, multiplier)
        mask = eligible & (direction == sign)
        pnl = outcomes.pnl_pct[mask]
        if pnl.size == 0:
            continue
        reasons = outcomes.reason[mask]
        counts = {REASON_NAMES[int(r)]: int(np.sum(reasons == r)) for r in np.unique(reasons)}
        wins = float(np.mean(pnl > 0) * 100)
        logger.info(
            "    %s n=%6d | win=%.1f%% | mean=%+.3f%% | exits=%s",
            label, pnl.size, wins, float(np.mean(pnl)), counts,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/platform.sqlite3")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--multiplier", type=float, default=None,
                        help="Index->premium multiplier. Omit to use the smoke value and be told so.")
    parser.add_argument("--smoke", action="store_true", help="Last 30 sessions only")
    args = parser.parse_args()

    multiplier = args.multiplier if args.multiplier is not None else SMOKE_MULTIPLIER
    if args.multiplier is None:
        logger.warning(
            "No --multiplier given: using the smoke value %.2f. Premium-space numbers "
            "below are indicative only. Fit the real coefficient from the option "
            "archive before reporting anything.", SMOKE_MULTIPLIER,
        )

    connection = sqlite3.connect(args.db)
    try:
        symbols = [
            row[0] for row in connection.execute(
                f"SELECT DISTINCT index_symbol FROM {args.table} WHERE interval = ?",
                (args.interval,),
            )
        ]
    finally:
        connection.close()
    if not symbols:
        logger.error("No candles found in %s table %s at interval %s", args.db, args.table, args.interval)
        return 1

    for symbol in symbols:
        bars = load_bars_sqlite(args.db, args.table, symbol, args.interval)
        if args.smoke:
            cutoff_dates = sorted({b.ts_ist.date() for b in bars})[-30:]
            bars = [b for b in bars if b.ts_ist.date() in set(cutoff_dates)]
        if len(bars) < 200:
            logger.warning("%s: only %s bars, skipping", symbol, len(bars))
            continue

        logger.info("=" * 72)
        logger.info(
            "%s | %s bars | %s to %s",
            symbol, len(bars), bars[0].ts_ist.date(), bars[-1].ts_ist.date(),
        )
        arrays = build_arrays(symbol, bars)
        eligible = _tradeable_mask(arrays)
        direction, drift = _drift_direction(arrays)
        logger.info("  eligible entry bars: %s", int(np.sum(eligible)))

        logger.info("QUESTION 1 -- does 45-minute drift predict direction?")
        _directional_baseline(arrays, eligible, direction, drift)

        logger.info("QUESTION 2 -- is the current stop distance survivable?")
        _stop_survivability(arrays, eligible, direction, multiplier)
        _risk_sanity(arrays, eligible, direction, multiplier)

    logger.info("=" * 72)
    logger.info(
        "Read the hit rates against 50%%. A z below ~2 means the drift rule is "
        "indistinguishable from a coin flip on this sample, and the gate sweep "
        "would be tuning filters over a signal that isn't there."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
