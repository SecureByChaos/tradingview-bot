"""Scalping-horizon target/stop sweep -- roadmap item 2b.

WHAT THIS ANSWERS
------------------
Given a directional signal (EMA_RSI_CROSS or ORB_BREAK) and a holding-period
cap (3/5/10/15 minutes), and given the stated requirement "multiple trades a
day, target 3-5%, stop tight" -- which stop distance, if any, actually
separates "tight enough to cap loss size" from "tight enough that it's just
harvesting ordinary noise repeatedly"?

Sweeps stop distance against two fixed targets (3% -> 1/1.5/2/2.5/3% stops,
5% -> 1.5/2/2.5/3/4% stops), and for every (setup, index, holding period,
target, stop) cell reports:

  - win rate: target hit before stop/timeout
  - noise-hit rate: of the STOP exits specifically, what fraction never moved
    favorably at all (MFE within NOISE_MFE_FRACTION of the stop distance) --
    a stop catching noise looks identical to a stop catching a real adverse
    move in raw win/loss counts alone; MFE is what tells them apart
  - realized reward:risk -- mean(winning pnl) / mean(|losing pnl|), the
    ACHIEVED ratio, not the nominal target:stop one a noise-prone stop will
    never actually deliver
  - net expectancy after costs, using the real fitted premium multiplier
    (scripts/backtest/premium.py) and app/trade_costs.py's cost model, not a
    flat assumption

Reuses scripts/backtest/outcomes.py's compute_outcomes() (the exact
target/stop/trail simulator scripts/stall_exit_backtest.py and others already
trust) rather than a second, parallel simulator -- masked down to whichever
bars carry a signal from the setup under test.

REQUIRES DATA NOT PRESENT IN THIS DEVELOPMENT ENVIRONMENT: the real 1-minute
option-candle archive (data/option_candles/, built by
scripts/pull_option_candles.py) and 1-minute index candles (interval=
ONE_MINUTE in the candles table). Neither exists in this sandbox -- built and
unit-tested against synthetic data (tests/test_scalp_stop_sweep.py); run this
against the real archive on the machine that has it.

Usage:
    python -m scripts.scalp_stop_sweep --db data/trading.db --setups EMA_RSI_CROSS
    python -m scripts.scalp_stop_sweep --db data/trading.db --setups ORB_BREAK --holding-minutes 5,10,15
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import numpy as np

from app.trade_costs import estimate_round_trip_cost
from scripts.backtest.data import build_arrays, load_bars_sqlite
from scripts.backtest.outcomes import REASON_STOP, REASON_TARGET, RiskCombo, compute_outcomes
from scripts.backtest.premium import (
    OPTION_CANDLE_DIR,
    load_option_series,
    parse_symbol_filename,
    fit_premium_model,
    select_multiplier,
    theoretical_theta_per_minute,
)
from scripts.backtest.setups import Setup, assert_causal, build_signals, default_setups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scalp_stop_sweep")

TRADING_START = time(9, 45)
TRADING_END = time(15, 15)

# Target/stop grid, exactly as specified: fix the target, sweep the stop.
TARGET_STOP_GRID: tuple[tuple[float, tuple[float, ...]], ...] = (
    (3.0, (1.0, 1.5, 2.0, 2.5, 3.0)),
    (5.0, (1.5, 2.0, 2.5, 3.0, 4.0)),
)

DEFAULT_HOLDING_MINUTES = (3, 5, 10, 15)

# A stop-out whose MFE never exceeded this fraction of the stop distance is
# counted as a noise hit -- the position never moved favorably in any real
# sense before getting stopped. 20% is a judgment call, not a measured
# threshold: report it explicitly so a reviewer can argue a different cutoff
# rather than inheriting an invisible one.
NOISE_MFE_FRACTION = 0.20

# Representative option DTE for premium-multiplier selection and theta.
# AI Origination's own floor is 5 DTE (app/ai/originator.py); this sweep
# tests the same "typical live contract" case rather than a monthly-expiry
# outlier that would understate decay.
DEFAULT_DTE = 6


@dataclass
class SweepResult:
    index_symbol: str
    setup_label: str
    holding_minutes: int
    target_pct: float
    stop_pct: float
    n_trades: int
    win_rate: float
    noise_hit_rate: float
    realized_rr: float | None
    mean_pnl_pct: float
    cost_pct: float
    net_expectancy_pct: float
    trades_per_session: float


def _eligible(arrays) -> np.ndarray:
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_window = np.array([TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool)
    return in_window


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


def _representative_premium(candle_dir: Path, index_symbol: str) -> float:
    """Median close across every archived contract for this index -- the real
    premium level costs get computed against, not an assumed one. Median
    rather than mean since a handful of far-dated or deep-ITM contracts in
    the archive would otherwise skew a small sample hard."""
    if not candle_dir.exists():
        return 0.0
    closes: list[float] = []
    for path in candle_dir.glob("*.csv"):
        meta = parse_symbol_filename(path.stem)
        if not meta or meta["name"].upper() != index_symbol.upper():
            continue
        closes.extend(price for _, price in load_option_series(path))
    return float(np.median(closes)) if closes else 0.0


def _load_lot_sizes(db_path: str) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT symbol, lot_size FROM index_configs").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        connection.close()
    return {str(s).upper(): int(size or 35) for s, size in rows}


def _load_strike_intervals(db_path: str) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT symbol, strike_interval FROM index_configs").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        connection.close()
    return {str(s).upper(): int(i or 100) for s, i in rows}


def _cost_pct(avg_premium: float, lot_size: int) -> float:
    """Round-trip cost as a percentage of premium, computed from the real
    cost model (app/trade_costs.py) against the archive's own average
    premium -- not the flat ~0.56-0.6% figure quoted elsewhere, though it
    should land close to it for a typical ATM contract."""
    if avg_premium <= 0 or lot_size <= 0:
        return 0.0
    breakdown = estimate_round_trip_cost(avg_premium, avg_premium, lot_size)
    return breakdown.total / (avg_premium * lot_size) * 100.0


def _sweep_one(
    arrays, eligible: np.ndarray, setup: Setup, index_symbol: str, premium_multiplier: float,
    dte: int, cost_pct: float, holding_minutes: int, minutes_per_bar: int,
) -> list[SweepResult]:
    signals = build_signals(arrays, setup)
    assert_causal(arrays, setup, signals)

    n_sessions = len(np.unique(arrays.session_id))
    holding_bars = max(1, holding_minutes // minutes_per_bar)
    theta = theoretical_theta_per_minute(dte)

    results: list[SweepResult] = []
    for target_pct, stop_candidates in TARGET_STOP_GRID:
        for stop_pct in stop_candidates:
            combo = RiskCombo(stop_pct=stop_pct, target_pct=target_pct, trail_activate_pct=None, trail_width_pct=None)
            pnl_all = np.zeros(len(arrays), dtype=np.float64)
            reason_all = np.zeros(len(arrays), dtype=np.int8)
            mfe_all = np.zeros(len(arrays), dtype=np.float64)
            mask = np.zeros(len(arrays), dtype=bool)

            for direction in (1, -1):
                outcomes = compute_outcomes(
                    arrays, combo, direction, premium_multiplier,
                    max_forward_bars=holding_bars, theta_per_minute=theta, minutes_per_bar=minutes_per_bar,
                )
                dir_mask = eligible & (signals == direction)
                pnl_all[dir_mask] = outcomes.pnl_pct[dir_mask]
                reason_all[dir_mask] = outcomes.reason[dir_mask]
                mfe_all[dir_mask] = outcomes.mfe_pct[dir_mask]
                mask |= dir_mask

            n_trades = int(mask.sum())
            if n_trades == 0:
                results.append(SweepResult(
                    index_symbol, setup.label, holding_minutes, target_pct, stop_pct,
                    0, 0.0, 0.0, None, 0.0, cost_pct, -cost_pct, 0.0,
                ))
                continue

            pnl = pnl_all[mask]
            reason = reason_all[mask]
            mfe = mfe_all[mask]

            is_win = reason == REASON_TARGET
            is_stop = reason == REASON_STOP
            win_rate = float(is_win.mean())

            n_stops = int(is_stop.sum())
            noise_hits = int((is_stop & (mfe < stop_pct * NOISE_MFE_FRACTION)).sum()) if n_stops else 0
            noise_hit_rate = (noise_hits / n_stops) if n_stops else 0.0

            wins = pnl[is_win]
            losses = pnl[~is_win & (pnl < 0)]
            realized_rr = (
                float(wins.mean() / abs(losses.mean())) if wins.size and losses.size and losses.mean() != 0 else None
            )

            mean_pnl = float(pnl.mean())
            net_expectancy = mean_pnl - cost_pct

            results.append(SweepResult(
                index_symbol, setup.label, holding_minutes, target_pct, stop_pct,
                n_trades, win_rate, noise_hit_rate, realized_rr, mean_pnl, cost_pct, net_expectancy,
                n_trades / n_sessions if n_sessions else 0.0,
            ))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--candles", default="data/option_candles")
    parser.add_argument(
        "--setups", default="EMA_RSI_CROSS",
        help="Comma-separated setup names from default_setups() to test (matches by name, all "
             "param variants included), e.g. EMA_RSI_CROSS,ORB_BREAK",
    )
    parser.add_argument(
        "--holding-minutes", default=",".join(str(m) for m in DEFAULT_HOLDING_MINUTES),
        help="Comma-separated holding-period caps in minutes",
    )
    parser.add_argument("--minutes-per-bar", type=int, default=1, help="Bar resolution loaded from --db")
    parser.add_argument("--dte", type=int, default=DEFAULT_DTE)
    args = parser.parse_args()

    wanted = {n.strip().upper() for n in args.setups.split(",") if n.strip()}
    setups = [s for s in default_setups() if s.name.upper() in wanted]
    if not setups:
        logger.error("No declared setup matches --setups %s", args.setups)
        return 1
    holding_minutes_list = [int(m.strip()) for m in args.holding_minutes.split(",") if m.strip()]

    logger.info("Fitting premium coefficients from the option archive...")
    fits = fit_premium_model(
        _load_index_series(args.db, args.table),
        _load_strike_intervals(args.db),
        Path(args.candles),
    )
    if not fits:
        logger.error("No premium fits available; cannot proceed without measured coefficients.")
        return 1

    interval = "ONE_MINUTE" if args.minutes_per_bar == 1 else f"{args.minutes_per_bar}_MINUTE"
    connection = sqlite3.connect(args.db)
    try:
        symbols = [
            row[0] for row in connection.execute(
                f"SELECT DISTINCT index_symbol FROM {args.table} WHERE interval = ?", (interval,)
            )
        ]
    finally:
        connection.close()
    lot_sizes = _load_lot_sizes(args.db)

    logger.info("=" * 100)
    logger.info(
        "%-10s %-24s %4s %6s %5s %7s %9s %7s %9s %7s %6s %8s %8s",
        "index", "setup", "hold", "target", "stop", "n", "trades/day", "win%", "noise%", "R:R", "mean%", "cost%", "net%",
    )

    all_results: list[SweepResult] = []
    for symbol in sorted(symbols):
        bars = load_bars_sqlite(args.db, args.table, symbol, interval)
        if len(bars) < 500:
            logger.warning("%s: only %s bars at %s, skipping", symbol, len(bars), interval)
            continue
        arrays = build_arrays(symbol, bars)
        eligible = _eligible(arrays)

        try:
            multiplier, extrapolated = select_multiplier(fits, symbol, args.dte, "CE")
        except ValueError as exc:
            logger.warning("%s: %s, skipping", symbol, exc)
            continue
        if extrapolated:
            logger.warning("%s: premium multiplier is EXTRAPOLATED for DTE=%s, not measured", symbol, args.dte)

        avg_premium = _representative_premium(Path(args.candles), symbol)
        if avg_premium <= 0:
            logger.warning("%s: no archived option premiums found under %s, cost_pct will read 0", symbol, args.candles)
        lot_size = lot_sizes.get(symbol, 35)
        cost_pct = _cost_pct(avg_premium, lot_size)

        for setup in setups:
            for holding_minutes in holding_minutes_list:
                results = _sweep_one(
                    arrays, eligible, setup, symbol, multiplier, args.dte, cost_pct,
                    holding_minutes, args.minutes_per_bar,
                )
                for r in results:
                    all_results.append(r)
                    logger.info(
                        "%-10s %-24s %4s %6.1f %5.1f %7s %9.1f %7.1f %9.1f %7s %6.2f %8.2f %8.2f",
                        r.index_symbol, r.setup_label, r.holding_minutes, r.target_pct, r.stop_pct,
                        r.n_trades, r.trades_per_session, r.win_rate * 100, r.noise_hit_rate * 100,
                        f"{r.realized_rr:.2f}" if r.realized_rr is not None else "n/a",
                        r.mean_pnl_pct, r.cost_pct, r.net_expectancy_pct,
                    )

    if not all_results:
        logger.error("No results produced -- check --db has ONE_MINUTE candles and the option archive is populated.")
        return 1

    viable = [r for r in all_results if r.net_expectancy_pct > 0 and r.noise_hit_rate < 0.5]
    logger.info("=" * 100)
    if viable:
        logger.info(
            "%s cell(s) clear both bars (net expectancy > 0, noise-hit rate < 50%%) -- "
            "still needs walk-forward + holdout validation before treating as a result.",
            len(viable),
        )
    else:
        logger.info(
            "NO cell in the tested grid clears both the noise-hit-rate bar and the cost-adjusted "
            "expectancy bar simultaneously. That is the direct answer to whether 'multiple trades, "
            "target 3-5%%, tight stop' is achievable with this signal at these holding periods."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
