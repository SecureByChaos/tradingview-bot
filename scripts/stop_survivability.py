"""Item 1: stop survivability, using FITTED per-bucket premium coefficients.

WHAT THIS CORRECTS
------------------
The earlier baseline reported "a 10% stop is breached by noise in 62.3% (BN) /
55.8% (NIFTY) of bars" using a flat multiplier of 105 for everything. That
figure drove the conclusion that the risk band is structurally broken, and it
is wrong in a way that matters:

  * 105 was a first-principles guess for a cheap short-dated Nifty contract.
    The fitted ATM values are 46.9 (BANKNIFTY CE, 6-10 DTE) to 68.0 (NIFTY CE,
    2-5 DTE).
  * A LOWER multiplier means a given premium stop corresponds to a LARGER index
    move, so noise reaches it LESS often. The old number overstated breach
    rates for calls.
  * The correction runs in OPPOSITE directions for calls and puts. Fitted put
    coefficients (-85 to -108) sit close to the old 105, so put breach rates
    barely move, while call rates fall sharply. Pooling CE and PE hid this
    entirely -- which is why this script never pools them.

Stops are also reported in index points and ATR multiples, because "10% stop"
is not a comparable quantity across CE and PE: puts are 1.28-1.53x more
responsive, so the same percentage is a materially tighter index distance.

Usage:
    python -m scripts.stop_survivability --db data/trading.db
    python -m scripts.stop_survivability --db data/trading.db --horizon 12
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, time
from pathlib import Path

import numpy as np

from scripts.backtest.data import build_arrays, forward_window_bounds, load_bars_sqlite
from scripts.backtest.premium import PremiumFit, fit_premium_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("stop_survivability")

TRADING_START = time(9, 45)
TRADING_END = time(15, 15)
STOP_LEVELS = (10.0, 12.0, 15.0, 18.0, 22.0, 25.0)
# The flat value the superseded run used, kept so the correction is visible
# rather than silently replacing a published number.
OLD_FLAT_MULTIPLIER = 105.0


def _eligible(arrays) -> np.ndarray:
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_window = np.array([TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool)
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21)
    return in_window & warm


def _excursions(arrays, horizon_bars: int) -> tuple[np.ndarray, np.ndarray]:
    """Worst and best INDEX percentage move within the forward window.

    Returned in index space, not premium space, so a single pass serves every
    multiplier. Windows are clipped at the session boundary -- an overnight gap
    is not an intraday excursion.
    """
    n = len(arrays)
    close = arrays.close.astype(np.float64)
    high = arrays.high.astype(np.float64)
    low = arrays.low.astype(np.float64)
    bounds = forward_window_bounds(arrays, horizon_bars)
    positions = np.arange(n)

    worst = np.zeros(n)
    best = np.zeros(n)
    for step in range(1, horizon_bars + 1):
        j = np.minimum(positions + step, bounds)
        with np.errstate(invalid="ignore", divide="ignore"):
            worst = np.minimum(worst, (low[j] - close) / close * 100.0)
            best = np.maximum(best, (high[j] - close) / close * 100.0)
    return worst, best


def _atm_fits(fits: list[PremiumFit], index_symbol: str) -> list[PremiumFit]:
    return [
        f for f in fits
        if f.index_symbol == index_symbol and f.moneyness_bucket == "ATM"
    ]


def _report(
    index_symbol: str, fit: PremiumFit, arrays, eligible: np.ndarray,
    worst_idx: np.ndarray, best_idx: np.ndarray, horizon_minutes: int,
) -> None:
    """One (option_type, dte_bucket) combination."""
    lam = fit.multiplier
    # Signed lambda applied to the signed index move gives premium move
    # directly, correct for both option types: a call gains on an index rise
    # (lam > 0), a put on an index fall (lam < 0). So the adverse premium
    # excursion is the worst index move for a call and the BEST for a put.
    if lam > 0:
        prem_mae = worst_idx * lam
        prem_mfe = best_idx * lam
        adverse_index = worst_idx
    else:
        prem_mae = best_idx * lam
        prem_mfe = worst_idx * lam
        adverse_index = best_idx

    mask = eligible & ~np.isnan(arrays.atr14)
    mae = prem_mae[mask]
    mfe = prem_mfe[mask]
    adverse = adverse_index[mask]
    spot = arrays.close[mask].astype(np.float64)
    atr = arrays.atr14[mask].astype(np.float64)
    if mae.size == 0:
        return

    logger.info(
        "  %s %s dte=%s  lambda=%+.1f (r2=%.2f, n=%d)",
        index_symbol, fit.option_type, fit.dte_bucket, lam, fit.r_squared, fit.n_samples,
    )
    # Reported as unsigned magnitudes: adverse is negative for a call (index
    # falls) and positive for a put (index rises), so signed percentiles would
    # mean opposite things in the two rows.
    adverse_size = np.abs(adverse)
    logger.info(
        "    index adverse excursion (magnitude): median %.3f%%  worst-25%% %.3f%%  worst-10%% %.3f%%",
        float(np.percentile(adverse_size, 50)),
        float(np.percentile(adverse_size, 75)),
        float(np.percentile(adverse_size, 90)),
    )
    logger.info(
        "    premium MAE: median %+.2f%%  worst-25%% %+.2f%%  worst-10%% %+.2f%%  |  "
        "MFE median %+.2f%%",
        float(np.percentile(mae, 50)), float(np.percentile(mae, 25)),
        float(np.percentile(mae, 10)), float(np.percentile(mfe, 50)),
    )
    logger.info(
        "    %-8s %10s %10s %10s   %10s   %s",
        "stop", "idx move", "idx pts", "ATR mult", "breached", "(old flat m=105)",
    )
    for stop in STOP_LEVELS:
        # Premium stop -> required index move -> points and ATR multiples.
        index_move_pct = stop / abs(lam)
        index_points = index_move_pct / 100.0 * float(np.median(spot))
        atr_multiple = index_points / float(np.median(atr)) if np.median(atr) > 0 else float("nan")
        breached = float(np.mean(mae <= -stop) * 100)
        breached_old = float(np.mean((adverse * OLD_FLAT_MULTIPLIER * (1 if lam > 0 else -1)) <= -stop) * 100)
        logger.info(
            "    %6.1f%% %9.3f%% %10.0f %10.2f   %8.1f%%   %14.1f%%",
            stop, index_move_pct, index_points, atr_multiple, breached, breached_old,
        )
    logger.info(
        "    horizon %d min, n=%d eligible bars", horizon_minutes, mae.size,
    )


def _load_index_series(db_path: str, table: str) -> dict[str, dict[datetime, float]]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            f"SELECT index_symbol, ts_ist, close FROM {table} WHERE interval = 'ONE_MINUTE'"
        ).fetchall()
    finally:
        connection.close()
    series: dict[str, dict[datetime, float]] = {}
    for symbol, ts_raw, close in rows:
        ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
        series.setdefault(str(symbol).upper(), {})[ts] = float(close)
    return series


def _load_strike_intervals(db_path: str) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT symbol, strike_interval FROM index_configs").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        connection.close()
    return {str(s).upper(): int(i or 100) for s, i in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--candles", default="data/option_candles")
    parser.add_argument("--horizon", type=int, default=12, help="Forward bars (12 = 60 min)")
    args = parser.parse_args()

    logger.info("Fitting premium coefficients from the option archive...")
    fits = fit_premium_model(
        _load_index_series(args.db, args.table),
        _load_strike_intervals(args.db),
        Path(args.candles),
    )
    if not fits:
        logger.error("No premium fits available; cannot proceed without measured coefficients.")
        return 1

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

    logger.info("=" * 96)
    logger.info(
        "Stop survivability with FITTED coefficients. Final column repeats the "
        "superseded flat m=%.0f result for comparison.", OLD_FLAT_MULTIPLIER,
    )

    for symbol in sorted(symbols):
        bars = load_bars_sqlite(args.db, args.table, symbol, args.interval)
        if len(bars) < 500:
            continue
        arrays = build_arrays(symbol, bars)
        eligible = _eligible(arrays)
        worst_idx, best_idx = _excursions(arrays, args.horizon)

        logger.info("=" * 96)
        logger.info("%s | %s bars | %s to %s", symbol, len(bars), bars[0].ts_ist.date(), bars[-1].ts_ist.date())
        atm = _atm_fits(fits, symbol)
        if not atm:
            logger.warning("  no ATM fits for %s", symbol)
            continue
        for fit in sorted(atm, key=lambda f: (f.option_type, f.dte_bucket)):
            _report(symbol, fit, arrays, eligible, worst_idx, best_idx, args.horizon * 5)

    logger.info("=" * 96)
    logger.info(
        "CE and PE are reported separately on purpose. Puts are 1.28-1.53x more "
        "responsive than calls, so an identical percentage stop is a materially "
        "tighter index distance on a PE -- see the 'idx pts' column. Pooling them "
        "averages away the effect that matters."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
