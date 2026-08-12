"""Does a confirmed structural break predict better AI Origination outcomes?

THE TRIGGER (12 Aug, one trade, not evidence on its own)
----------------------------------------------------------
Bank Nifty 57700 CE, AI Origination/OpenAI, lost -10.61% (STOPLOSS), MFE only
1.78%. same_direction_entries_today was 0 -- the repeat-entry gate shipped
after 11 Aug had nothing to catch here. The model's own reasoning: "the move
has already run for most of the session... price is still inside the opening
range" -- trend_duration_pct_of_session=100.0 and still inside the opening
range are close to contradictory (a move that genuinely trended for 49 bars
should, near-definitionally, be outside the range that measures its own first
15-30 minutes). The model stated both facts and did not act on the tension.

The hypothesis this script tests: requiring a completed close beyond a
structural level (opening range high/low, or previous-day high/low) before a
continuation entry is allowed would filter out exactly this kind of trade.
ONE trade is not evidence -- this exists because the project's standing rule
is that a plausible-sounding fix gets tested before it ships, not after (see
the trend-age gate's own note on the same discipline, CLAUDE.md 11 Aug entry).

TWO SOURCES, IN PRIORITY ORDER
-------------------------------
1. Real AI Origination history (ai_origination_logs JOIN strategy_trades).
   The setups a decision actually saw are already logged per-row; whether
   ORB_BREAK_UP/DOWN or PDH_BREAK/PDL_BREAK (matching the trade's own
   direction) was active is a filter on data already collected, not a new
   computation. This is the population the gate would actually act on, so
   it is the primary source -- but as of 12 Aug it is roughly two months of
   AI Origination trades, a much smaller sample than the two-year index
   archive.
2. The 2-year index-level archive (scripts/backtest/), as a fallback / cross-
   check when (1) is too thin. ORB_BREAK and PDH_PDL_BREAK are already
   registered setups there. This asks a related but not identical question:
   among bars where a continuation-style setup (ST_ALIGNED, EMA_STACK) is
   active, does ALSO having a same-direction structural break active change
   the forward edge? This cannot see AI Origination's actual entries or
   real premium P&L -- it is index-direction-only, the same limitation every
   other setup_significance-style script in this project has.

Report both. Do not average them into one number -- they measure different
things at different sample sizes, and the roadmap's own instruction is to
report what each source actually shows, plainly, including "not enough data
to tell" as an acceptable answer.

Usage:
    python -m scripts.break_confirmation_backtest --db data/trading.db
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import time

import numpy as np

from scripts.backtest.data import build_arrays, forward_window_bounds, load_bars_sqlite
from scripts.backtest.setups import Setup, assert_causal, build_signals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("break_confirmation_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20    # AI Origination history: below this, report but flag as untrustworthy
MIN_BUCKET_INDEX = 30   # index-level: mirrors trend_age_gate_backtest.py's MIN_SIGNALS

# Direction-matched break setups, already computed by app/market_context.py
# and already logged verbatim into ai_origination_logs.setups.
BREAK_SETUPS_FOR_DECISION = {
    "BUY_CE": ("ORB_BREAK_UP", "PDH_BREAK"),
    "BUY_PE": ("ORB_BREAK_DOWN", "PDL_BREAK"),
}

TRADING_START = time(9, 45)
TRADING_END = time(15, 15)
HORIZON_BARS = 12  # 60 min at the default FIVE_MINUTE interval
CONTINUATION_SETUPS = ("ST_ALIGNED", "EMA_STACK")


# --- Part 1: real AI Origination history ------------------------------------

@dataclass
class LiveEntry:
    index_symbol: str
    decision: str
    confirmed: bool
    pnl_percent: float
    mfe_percent: float | None
    is_win: bool


def _load_live_entries(db_path: str) -> list[LiveEntry]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                l.index_name    AS index_name,
                l.decision      AS decision,
                l.setups        AS setups_json,
                t.entry_price   AS entry_price,
                t.highest_price AS highest_price,
                t.pnl_percent   AS pnl_percent,
                t.result        AS result
            FROM ai_origination_logs l
            JOIN strategy_trades t ON t.trade_id = l.trade_id
            WHERE l.decision IN ('BUY_CE', 'BUY_PE')
              AND l.trade_id IS NOT NULL
              AND t.status = 'CLOSED'
            """
        ).fetchall()
    finally:
        connection.close()

    entries: list[LiveEntry] = []
    for row in rows:
        if row["pnl_percent"] is None:
            continue
        try:
            setups = set(json.loads(row["setups_json"] or "[]"))
        except (TypeError, ValueError):
            setups = set()
        wanted = BREAK_SETUPS_FOR_DECISION.get(row["decision"], ())
        confirmed = any(name in setups for name in wanted)
        entry_price = row["entry_price"]
        highest = row["highest_price"]
        mfe = (
            (highest - entry_price) / entry_price * 100.0
            if entry_price and highest is not None else None
        )
        entries.append(LiveEntry(
            index_symbol=str(row["index_name"]),
            decision=str(row["decision"]),
            confirmed=confirmed,
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            is_win=(row["result"] == "WIN"),
        ))
    return entries


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260812)
    diffs = []
    for _ in range(rounds):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        diffs.append(sum(sample_a) / len(sample_a) - sum(sample_b) / len(sample_b))
    diffs.sort()
    lo = diffs[int(0.05 * rounds)]
    hi = diffs[int(0.95 * rounds) - 1]
    return lo, hi


def _report_bucket(label: str, entries: list[LiveEntry]) -> None:
    if not entries:
        logger.info("  %-28s n=0", label)
        return
    n = len(entries)
    wins = sum(1 for e in entries if e.is_win)
    mean_pnl = sum(e.pnl_percent for e in entries) / n
    mfes = [e.mfe_percent for e in entries if e.mfe_percent is not None]
    mean_mfe = sum(mfes) / len(mfes) if mfes else None
    mfe_txt = f"{mean_mfe:+.2f}%" if mean_mfe is not None else "n/a"
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-28s n=%-4d win_rate=%5.1f%%  mean_pnl=%+6.2f%%  mean_mfe=%s%s",
        label, n, wins / n * 100.0, mean_pnl, mfe_txt, flag,
    )


def run_live_history(db_path: str) -> None:
    entries = _load_live_entries(db_path)
    logger.info("=" * 100)
    logger.info("PART 1: REAL AI ORIGINATION HISTORY (ai_origination_logs JOIN strategy_trades)")
    logger.info("=" * 100)
    if not entries:
        logger.error(
            "No closed AI Origination entries found. Either data/trading.db has no AI "
            "Origination history yet, or this sandbox has no real data at all (expected "
            "here -- see CLAUDE.md). Run this on the machine with real trade history."
        )
        return

    confirmed = [e for e in entries if e.confirmed]
    unconfirmed = [e for e in entries if not e.confirmed]
    logger.info("Overall (both indices, both directions):")
    _report_bucket("confirmed (break active)", confirmed)
    _report_bucket("unconfirmed (no break active)", unconfirmed)

    if len(confirmed) >= 2 and len(unconfirmed) >= 2:
        lo, hi = _bootstrap_mean_diff(
            [e.pnl_percent for e in unconfirmed], [e.pnl_percent for e in confirmed],
        )
        verdict = "unconfirmed reliably WORSE" if hi < 0 else ("unconfirmed reliably BETTER" if lo > 0 else "no reliable difference")
        logger.info(
            "  bootstrap 90%% CI on mean_pnl(unconfirmed) - mean_pnl(confirmed): [%+.2f, %+.2f]  -> %s",
            lo, hi, verdict,
        )
    else:
        logger.info("  Too few observations in one bucket for a bootstrap comparison.")

    logger.info("-" * 100)
    logger.info("Per index:")
    for index_symbol in sorted({e.index_symbol for e in entries}):
        logger.info("  %s:", index_symbol)
        idx_confirmed = [e for e in confirmed if e.index_symbol == index_symbol]
        idx_unconfirmed = [e for e in unconfirmed if e.index_symbol == index_symbol]
        _report_bucket("    confirmed", idx_confirmed)
        _report_bucket("    unconfirmed", idx_unconfirmed)

    total_min = min(len(confirmed), len(unconfirmed))
    if total_min < MIN_BUCKET_LIVE:
        logger.info("-" * 100)
        logger.info(
            "Smaller bucket has only %s observations (minimum treated as trustworthy: %s). "
            "This is the expected state at ~2 months of AI Origination history -- read "
            "PART 2 below, and re-run this part as more history accumulates.",
            total_min, MIN_BUCKET_LIVE,
        )


# --- Part 2: 2-year index-level fallback -------------------------------------

def _eligible(arrays) -> np.ndarray:
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_window = np.array([TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool)
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21)
    return in_window & warm


def _edge(wins: float, ups: float, longs: float, n: float) -> float:
    if n == 0:
        return 0.0
    up_rate = ups / n
    base = (longs * up_rate + (n - longs) * (1.0 - up_rate)) / n
    return (wins / n - base) * 100.0


def _evaluate(arrays, mask: np.ndarray, direction: np.ndarray, forward_bars: int, rng) -> tuple[int, float, float, float]:
    n_bars = len(arrays)
    close = arrays.close.astype(np.float64)
    bounds = forward_window_bounds(arrays, forward_bars)
    positions = np.arange(n_bars)
    target = np.minimum(positions + forward_bars, bounds)

    valid = mask & (direction != 0) & (target > positions)
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        return 0, 0.0, 0.0, 0.0

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

    edges = np.empty(2000)
    for b in range(2000):
        pick = rng.integers(0, size, size=size)
        total = per_n[pick].sum()
        edges[b] = _edge(per_win[pick].sum(), per_up[pick].sum(), per_long[pick].sum(), total) if total else 0.0
    ci_low, ci_high = np.percentile(edges, [5, 95])
    return int(idx.size), edge, float(ci_low), float(ci_high)


def _break_confirmed_mask(direction: np.ndarray, orb_dir: np.ndarray, pdhpdl_dir: np.ndarray) -> np.ndarray:
    """True where a continuation setup's direction agrees with either break
    setup's direction at the same bar -- a same-direction structural break is
    active at the same time the continuation setup fires."""
    return (direction != 0) & ((orb_dir == direction) | (pdhpdl_dir == direction))


def run_index_fallback(db_path: str, table: str, interval: str) -> None:
    logger.info("=" * 100)
    logger.info("PART 2: 2-YEAR INDEX-LEVEL FALLBACK (continuation setup, WITH vs WITHOUT a same-direction break)")
    logger.info("=" * 100)

    connection = sqlite3.connect(db_path)
    try:
        symbols = [
            row[0] for row in connection.execute(
                f"SELECT DISTINCT index_symbol FROM {table} WHERE interval = ?", (interval,),
            )
        ]
    finally:
        connection.close()
    symbols = [s for s in symbols if not s.upper().endswith("_FUT")]

    rng = np.random.default_rng(20260812)
    any_result = False
    logger.info(
        "  %-10s %-10s %-13s %6s %9s  %-18s %s",
        "index", "setup", "bucket", "n", "edge", "bootstrap 90% CI", "verdict",
    )
    for symbol in sorted(symbols):
        bars = load_bars_sqlite(db_path, table, symbol, interval)
        if len(bars) < 500:
            continue
        arrays = build_arrays(symbol, bars)
        eligible = _eligible(arrays)

        orb_setup = Setup("ORB_BREAK", {"hold": 1})
        pdhpdl_setup = Setup("PDH_PDL_BREAK", {"hold": 1})
        orb_dir = build_signals(arrays, orb_setup)
        pdhpdl_dir = build_signals(arrays, pdhpdl_setup)
        assert_causal(arrays, orb_setup, orb_dir)
        assert_causal(arrays, pdhpdl_setup, pdhpdl_dir)

        for setup_name in CONTINUATION_SETUPS:
            setup = Setup(setup_name, {})
            direction = build_signals(arrays, setup)
            assert_causal(arrays, setup, direction)

            break_matches = _break_confirmed_mask(direction, orb_dir, pdhpdl_dir)
            confirmed_mask = eligible & break_matches
            unconfirmed_mask = eligible & (direction != 0) & ~break_matches

            for bucket_name, mask in (("confirmed", confirmed_mask), ("unconfirmed", unconfirmed_mask)):
                n, edge, ci_low, ci_high = _evaluate(arrays, mask, direction, HORIZON_BARS, rng)
                if n < MIN_BUCKET_INDEX:
                    continue
                any_result = True
                verdict = "POSITIVE" if ci_low > 0 else ("BACKWARDS" if ci_high < 0 else "-")
                logger.info(
                    "  %-10s %-10s %-13s %6d %+8.2fpp  [%+6.2f, %+6.2f]  %s",
                    symbol, setup_name, bucket_name, n, edge, ci_low, ci_high, verdict,
                )

    if not any_result:
        logger.error(
            "No (index, setup, bucket) cell reached %s signals. Nothing to report -- most "
            "likely no real candle data in this environment (expected in this sandbox).",
            MIN_BUCKET_INDEX,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    args = parser.parse_args()

    run_live_history(args.db)
    run_index_fallback(args.db, args.table, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
