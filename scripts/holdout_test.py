"""Item 5: the locked holdout. Single-use.

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER SCRIPT HERE
------------------------------------------------------
Everything else in scripts/ can be re-run freely, because re-running an
exploratory analysis costs nothing. This one cannot. The holdout's entire value
is that it has never influenced a decision; run it twice with different
candidates and it becomes just another slice of in-sample data, silently.

So this script:

  * requires candidates to be named EXPLICITLY on the command line -- no
    defaults, no "best from the last run". Naming them is the commitment.
  * refuses more than two.
  * records that it ran, to data/holdout_record.json, and refuses to run again
    without --force. Forcing is allowed but is logged permanently in that file,
    because a forced re-run is a fact about the result that anyone reading it
    later needs to know.

PREREQUISITE THAT IS EASY TO GET WRONG
--------------------------------------
Candidates must have been selected WITHOUT seeing the holdout window. The
earlier setup_significance runs covered the full two years including this
window, so selection was contaminated. Re-run selection with
`--end <holdout_start - 1 day>` and confirm the same candidates emerge before
trusting anything here. This script warns but cannot verify it for you.

WHAT IT REPORTS
---------------
Not just directional edge. The edge is the hypothesis; the decision needs net
P&L after costs and time decay, and the win/loss RATIO -- at symmetric payoffs
a 2pp edge is worth ~0.48% per trade against ~0.56% costs, i.e. nothing.

Usage:
    python -m scripts.holdout_test --db data/trading.db \\
        --candidate "EMA_STACK@1100_1400" \\
        --candidate "ORB_BREAK[hold=2]@1100_1400" \\
        --holdout-start 2026-05-29
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import numpy as np

from app.trade_costs import estimate_round_trip_cost
from scripts.backtest.data import build_arrays, load_bars_sqlite
from scripts.backtest.outcomes import REASON_NAMES, RiskCombo, compute_outcomes
from scripts.backtest.premium import theoretical_theta_per_minute
from scripts.backtest.setups import Setup, assert_causal, build_signals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("holdout_test")

RECORD_PATH = Path("data/holdout_record.json")
MAX_CANDIDATES = 2
TRADING_START = time(9, 45)
TRADING_END = time(15, 15)
BOOTSTRAP_ITERATIONS = 2000

REGIME_WINDOWS = {
    "0945_1100": (0, 105),
    "1100_1400": (105, 285),
    "1400_1515": (285, 10**6),
    "all": (0, 10**6),
}

# The live risk band, so the holdout measures the configuration actually in use
# rather than an optimised one. Optimising risk parameters ON the holdout would
# defeat its purpose entirely.
LIVE_RISK = RiskCombo(stop_pct=12.0, target_pct=20.0, trail_activate_pct=8.0, trail_width_pct=5.0)
ASSUMED_DTE = 3
HOLD_HORIZON_BARS = 12


@dataclass
class Candidate:
    setup: Setup
    regime: str

    @property
    def label(self) -> str:
        return f"{self.setup.label}@{self.regime}"


def _parse_candidate(text: str) -> Candidate:
    if "@" not in text:
        raise SystemExit(f"Candidate must be SETUP@REGIME, got: {text}")
    setup_text, regime = text.rsplit("@", 1)
    if regime not in REGIME_WINDOWS:
        raise SystemExit(f"Unknown regime {regime}; expected one of {sorted(REGIME_WINDOWS)}")
    params: dict = {}
    name = setup_text
    if "[" in setup_text and setup_text.endswith("]"):
        name, raw = setup_text[:-1].split("[", 1)
        for part in raw.split(","):
            key, _, value = part.partition("=")
            params[key.strip()] = float(value) if "." in value else int(value)
    return Candidate(Setup(name, params), regime)


def _eligible(arrays, regime: str) -> np.ndarray:
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_session = np.array([TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool)
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21)
    low, high = REGIME_WINDOWS[regime]
    minutes = arrays.minutes_since_open
    return in_session & warm & (minutes >= low) & (minutes < high)


def _edge(wins: float, ups: float, longs: float, n: float) -> float:
    if n == 0:
        return 0.0
    up_rate = ups / n
    base = (longs * up_rate + (n - longs) * (1.0 - up_rate)) / n
    return (wins / n - base) * 100.0


def _evaluate(arrays, mask, direction, rng) -> dict:
    """Directional edge plus a simulated P&L using the LIVE risk band."""
    n_bars = len(arrays)
    close = arrays.close.astype(np.float64)
    positions = np.arange(n_bars)

    from scripts.backtest.data import forward_window_bounds

    bounds = forward_window_bounds(arrays, HOLD_HORIZON_BARS)
    target = np.minimum(positions + HOLD_HORIZON_BARS, bounds)
    valid = mask & (direction != 0) & (target > positions)
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        return {"n": 0}

    raw = (close[target[idx]] - close[idx]) / close[idx] * 100.0
    win = (raw * direction[idx]) > 0
    up = raw > 0
    is_long = direction[idx] == 1
    edge = _edge(float(win.sum()), float(up.sum()), float(is_long.sum()), float(idx.size))

    sessions = arrays.session_id[idx]
    _, session_index = np.unique(sessions, return_inverse=True)
    size = session_index.max() + 1
    per_n = np.bincount(session_index, minlength=size).astype(np.float64)
    per_win = np.bincount(session_index, weights=win.astype(np.float64), minlength=size)
    per_up = np.bincount(session_index, weights=up.astype(np.float64), minlength=size)
    per_long = np.bincount(session_index, weights=is_long.astype(np.float64), minlength=size)
    edges = np.empty(BOOTSTRAP_ITERATIONS)
    for b in range(BOOTSTRAP_ITERATIONS):
        pick = rng.integers(0, size, size=size)
        total = per_n[pick].sum()
        edges[b] = _edge(per_win[pick].sum(), per_up[pick].sum(), per_long[pick].sum(), total) if total else 0.0
    ci_low, ci_high = np.percentile(edges, [5, 95])

    return {
        "n": int(idx.size),
        "edge": edge,
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
        "idx": idx,
        "direction": direction,
    }


def _simulate_pnl(arrays, idx, direction, multiplier: float, theta: float, label: str) -> dict:
    """Net P&L through the live risk band, with and without time decay.

    Reported as avg_win / avg_loss alongside win rate, because that ratio is
    where any viable result has to come from -- a 2pp hit-rate edge at
    symmetric payoffs is worth ~0.48% per trade against ~0.56% costs.
    """
    out: dict = {}
    for decay_label, decay in (("no_decay", 0.0), ("with_decay", theta)):
        gross_parts, reasons = [], []
        for sign in (1, -1):
            sub = idx[direction[idx] == sign]
            if sub.size == 0:
                continue
            outcomes = compute_outcomes(
                arrays, LIVE_RISK, sign, multiplier,
                max_forward_bars=HOLD_HORIZON_BARS, theta_per_minute=decay,
            )
            gross_parts.append(outcomes.pnl_pct[sub])
            reasons.append(outcomes.reason[sub])
        if not gross_parts:
            continue
        gross = np.concatenate(gross_parts)
        reason = np.concatenate(reasons)
        wins = gross[gross > 0]
        losses = gross[gross < 0]
        avg_win = float(wins.mean()) if wins.size else 0.0
        avg_loss = float(abs(losses.mean())) if losses.size else 0.0

        # Costs as a percentage of capital. Uses the app's own trade_costs so
        # the holdout is charged the same way live trades are.
        sample_cost = estimate_round_trip_cost(entry_price=160.0, exit_price=160.0, quantity=75)
        cost_pct = sample_cost.total / (160.0 * 75) * 100.0

        out[decay_label] = {
            "n": int(gross.size),
            "win_rate": float((gross > 0).mean() * 100),
            "avg_win_pct": round(avg_win, 3),
            "avg_loss_pct": round(avg_loss, 3),
            "win_loss_ratio": round(avg_win / avg_loss, 3) if avg_loss else None,
            "gross_mean_pct": round(float(gross.mean()), 4),
            "cost_pct": round(cost_pct, 4),
            "net_mean_pct": round(float(gross.mean()) - cost_pct, 4),
            "exits": {REASON_NAMES[int(r)]: int((reason == r).sum()) for r in np.unique(reason)},
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--candidate", action="append", default=[], required=True,
                        help="SETUP@REGIME, e.g. 'EMA_STACK@1100_1400'. Max two.")
    parser.add_argument("--holdout-start", required=True, help="First holdout session (YYYY-MM-DD)")
    parser.add_argument("--multiplier", type=float, default=68.0,
                        help="Premium elasticity. Default is the fitted Nifty ATM CE 2-5 DTE value.")
    parser.add_argument("--force", action="store_true",
                        help="Re-run despite a prior record. Permanently logged.")
    parser.add_argument("--seed", type=int, default=20260731)
    args = parser.parse_args()

    if len(args.candidate) > MAX_CANDIDATES:
        raise SystemExit(f"At most {MAX_CANDIDATES} candidates; got {len(args.candidate)}.")
    candidates = [_parse_candidate(c) for c in args.candidate]
    holdout_start = datetime.strptime(args.holdout_start, "%Y-%m-%d").date()

    prior = json.loads(RECORD_PATH.read_text(encoding="utf-8")) if RECORD_PATH.exists() else None
    if prior and not args.force:
        logger.error("=" * 92)
        logger.error("HOLDOUT ALREADY USED on %s for: %s", prior.get("run_at"),
                     ", ".join(prior.get("candidates", [])))
        logger.error(
            "The holdout is single-use. Running it again on different candidates turns it "
            "into in-sample data, and every result from it -- including the first one -- "
            "becomes uninterpretable."
        )
        logger.error("Pass --force only if you accept that. The forced run is logged permanently.")
        return 2

    logger.warning("=" * 92)
    logger.warning(
        "PREREQUISITE: these candidates must have been selected WITHOUT seeing data from "
        "%s onward. Earlier setup_significance runs covered the full two years, which "
        "INCLUDES this window. Re-run selection with --end %s and confirm the same "
        "candidates emerge, or this is not an out-of-sample test.",
        holdout_start, (holdout_start - __import__("datetime").timedelta(days=1)).isoformat(),
    )
    logger.warning("=" * 92)

    rng = np.random.default_rng(args.seed)
    theta = theoretical_theta_per_minute(ASSUMED_DTE)
    logger.info("Assumed decay: %+.4f%%/min at %s DTE (%.2f%% over a 60-minute hold)",
                theta, ASSUMED_DTE, theta * 60)

    connection = sqlite3.connect(args.db)
    try:
        symbols = [r[0] for r in connection.execute(
            f"SELECT DISTINCT index_symbol FROM {args.table} WHERE interval = ?", (args.interval,))]
    finally:
        connection.close()

    results: dict = {}
    for symbol in sorted(symbols):
        bars = [b for b in load_bars_sqlite(args.db, args.table, symbol, args.interval)
                if b.ts_ist.date() >= holdout_start]
        if len(bars) < 200:
            logger.warning("%s: only %s holdout bars, skipping", symbol, len(bars))
            continue
        logger.info("=" * 92)
        logger.info("%s | HOLDOUT %s to %s | %s bars",
                    symbol, bars[0].ts_ist.date(), bars[-1].ts_ist.date(), len(bars))
        arrays = build_arrays(symbol, bars)

        for cand in candidates:
            signals = build_signals(arrays, cand.setup)
            assert_causal(arrays, cand.setup, signals)
            stats = _evaluate(arrays, _eligible(arrays, cand.regime), signals, rng)
            if stats.get("n", 0) == 0:
                logger.warning("  %s: no signals in the holdout", cand.label)
                continue
            logger.info(
                "  %-32s n=%5d  edge %+6.2fpp  CI [%+6.2f, %+6.2f]  %s",
                cand.label, stats["n"], stats["edge"], stats["ci_low"], stats["ci_high"],
                "CONFIRMED" if stats["ci_low"] > 0 else "NOT CONFIRMED",
            )
            pnl = _simulate_pnl(arrays, stats["idx"], stats["direction"], args.multiplier, theta, cand.label)
            for decay_label, row in pnl.items():
                logger.info(
                    "      %-11s win %.1f%%  avg_win %+.2f%%  avg_loss %.2f%%  W/L %.2f  "
                    "gross %+.3f%%  cost %.3f%%  NET %+.3f%%",
                    decay_label, row["win_rate"], row["avg_win_pct"], row["avg_loss_pct"],
                    row["win_loss_ratio"] or 0.0, row["gross_mean_pct"], row["cost_pct"],
                    row["net_mean_pct"],
                )
            results.setdefault(symbol, {})[cand.label] = {"edge": stats["edge"],
                                                          "ci_low": stats["ci_low"],
                                                          "ci_high": stats["ci_high"],
                                                          "n": stats["n"], "pnl": pnl}

    logger.info("=" * 92)
    logger.info(
        "Read NET, not edge. A confirmed directional edge that is net negative after costs "
        "and decay is not a tradeable result -- and the with_decay row is the honest one."
    )

    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "candidates": [c.label for c in candidates],
        "holdout_start": args.holdout_start,
        "multiplier": args.multiplier,
        "assumed_theta_per_minute": theta,
        "forced": bool(args.force),
        "previous_runs": ([prior] if prior else []) + (prior.get("previous_runs", []) if prior else []),
        "results": results,
    }
    RECORD_PATH.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    logger.info("Holdout recorded at %s. It is now used.", RECORD_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
