"""Does a hard ADX floor predict better AI Origination outcomes?

THE TRIGGER (25 Aug, one trade, not evidence on its own)
----------------------------------------------------------
Nifty 50 BUY_PE, AI Origination/OpenAI, confidence 0.66. The dashboard's own
tradability read showed ADX 19.6 -- "Not tradable" -- at decision time. The
model's own reasoning named the same caution ("ADX is below 20... no fresh
breakout") and traded anyway, resolving it with a self-described structural
break (ORB_BREAK_DOWN + PDL_BREAK were both active). Asked directly: nothing
in _open_trade (app/ai/originator.py) currently gates on ADX at all --
_classify_tradability's TRENDING/MARGINAL/NOT_TRADABLE read
(app/platform.py) is explicitly informational-only, shown on the dashboard,
never consulted by the trading path. The only three real hard gates today
are the DTE floor, the same-direction consecutive-loss gate, and the 0.60
confidence floor.

This is the same class of question CLAUDE.md's "Trend-age caution moved to a
hard gate" entry (11 Aug) already flagged and deliberately left unresolved:
trend_duration_pct_of_session (closely related to ADX -- both describe "is
there a real trend") was kept as a soft prompt-only caution specifically
because a single day's anecdote is exactly the overfitting error this
project guards against, and a sweep range (80/90/95%) was left for a real
backtest rather than a committed number. Same standard applies here: build
the backtest, do not ship a hard gate from one trade.

WHAT THIS SCRIPT TESTS
-----------------------
Real AI Origination history (ai_origination_logs JOIN strategy_trades) --
the population an ADX gate would actually act on. ADX at decision time is
already logged per-row (AIOriginationLog.adx), so this is a filter on data
already collected, not a new computation.

Buckets on the exact bands _classify_tradability already uses (ADX_NO_TREND
= 20, ADX_TRENDING = 25, from app/market_context.py -- not reinvented here):
  <20    NOT_TRADABLE
  20-25  MARGINAL
  >=25   TRENDING

Two candidate hard-floor cuts are swept (block below 20, block below 25),
each via a bootstrap comparison of below-floor vs at-or-above-floor mean
P&L -- the same floor-bootstrap shape confidence_sizing_backtest.py already
used to validate the 0.60 confidence floor. A trade with no recorded ADX
(pre-dates the field, or a genuine gap) is reported in its own bucket and
excluded from every floor comparison rather than silently defaulted to a
side.

Per the project's standing rule (see break_confirmation_backtest.py,
trend_age_gate_backtest.py, same_direction_entries_backtest.py): report
what the data shows, including "not enough evidence" as an acceptable
outcome. No gate is added to app/ai/originator.py by this script -- that is
a deliberate follow-up decision, made only if a floor clears the trust
minimum with a bootstrap CI that excludes zero.

PART 3, ADDED 25 AUG: DI-DIRECTION AGREEMENT
----------------------------------------------
Prompted by a proposed ADX-gate design (external doc) with three legs: ADX
above a floor, +DI/-DI direction agreeing with the trade's own direction,
and ADX sloping upward. Only the DI leg is retroactively testable --
app/indicators.py's adx() already computes plus_di/minus_di alongside ADX,
and market_context.py already carries both into MarketContext.as_dict(),
which is exactly what ai_origination_logs.context_json stores verbatim.
So DI agreement at decision time can be reconstructed for every past trade
with no new logging.

ADX SLOPE IS DELIBERATELY NOT TESTED HERE. Nothing has ever recorded ADX's
trend over time -- only a single snapshot value per decision -- so there is
no historical series to compute a slope from. Same shape as
trend_duration_pct_of_session and same_direction_entries_today before
either was gated: log it going forward first, then backtest once real
history accumulates. Not built in this pass.

The doc's EMA/VWAP breakout trigger is not separately re-tested here either
-- VWAP has no index-instrument data in this codebase (see CLAUDE.md: index
candles report zero volume, the same wall BNV5.1/BNV6 hit), and the
EMA-alignment/structural-break alternative is what break_confirmation_
backtest.py already tested via EMA_STACK/ORB/PDH/PDL setups on 12 Aug --
verdict NOT SUPPORTED. Re-litigating that here would just duplicate it.

PART 4, ADDED 25 AUG: 2-YEAR INDEX-LEVEL FALLBACK
----------------------------------------------------
Real AI Origination history is inherently short (the feature has existed a
couple of months, ~45 closed trades as of 25 Aug) -- it cannot become "2
years of data" no matter how long paper trading runs today. What the
project DOES have at 2-year depth is the index-level candle archive
scripts/backtest/ already uses for exactly this kind of question (see
break_confirmation_backtest.py's PART 2, trend_age_gate_backtest.py). This
asks a related but not identical question from PARTS 1-3: among bars where
an already-registered setup fires (scripts/backtest/setups.py's
default_setups()), does forward index-direction edge differ between ADX <
floor and ADX >= floor, at HORIZON_BARS forward. It is index-direction-only
-- no real trades, no real premium P&L, no confidence score, the same
limitation every setup_significance-style script in this project already
has, stated here rather than glossed over.

IndexArrays.adx14 (scripts/backtest/data.py) is already computed for the
whole archive -- no new indicator code needed, only the threshold sweep.

Usage:
    python -m scripts.adx_gate_backtest --db data/trading.db
    python -m scripts.adx_gate_backtest --db data/trading.db --skip-live-history
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

from scripts.backtest.data import IndexArrays, build_arrays, forward_window_bounds, load_bars_sqlite
from scripts.backtest.setups import assert_causal, build_signals, default_setups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("adx_gate_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses

# PART 4 (2-year index-level fallback) constants -- mirrors trend_age_gate_
# backtest.py / break_confirmation_backtest.py's own conventions.
FUTURES_SUFFIX = "_FUT"
VOLUME_DEPENDENT_SETUPS = {"BNV6"}
INDEX_TRADING_START = time(9, 45)
INDEX_TRADING_END = time(15, 15)
HORIZON_BARS = 12  # 60 min at the default FIVE_MINUTE interval
MIN_SIGNALS_INDEX = 30
BOOTSTRAP_ITERATIONS_INDEX = 2000

# Matches app/market_context.py's ADX_NO_TREND / ADX_TRENDING exactly --
# not reinvented here. app/platform.py's _classify_tradability reads these
# same two constants for the dashboard's TRENDING/MARGINAL/NOT_TRADABLE label.
ADX_NO_TREND = 20.0
ADX_TRENDING = 25.0

ADX_BUCKETS = (
    (0.0, ADX_NO_TREND, "<20 (NOT_TRADABLE)"),
    (ADX_NO_TREND, ADX_TRENDING, "20-25 (MARGINAL)"),
    (ADX_TRENDING, 1000.0, ">=25 (TRENDING)"),
)

CANDIDATE_FLOORS = (ADX_NO_TREND, ADX_TRENDING)


@dataclass
class Entry:
    trade_id: str
    index_symbol: str
    decision: str
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool


def _load_entries(db_path: str) -> list[Entry]:
    """MFE/MAE come from strategy_trade_ticks, not StrategyTrade.highest_price/
    lowest_price -- same reasoning confidence_sizing_backtest.py's own
    _load_entries docstring already gives: those two columns are only
    reliably maintained going forward from the 24 Aug lowest_price fix, and
    this population spans trades opened both before and after that fix, so
    ticks stay the one source that's correct across the whole history."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                l.trade_id    AS trade_id,
                l.decision    AS decision,
                l.adx         AS adx,
                l.context_json AS context_json,
                t.index_symbol AS index_symbol,
                t.entry_price AS entry_price,
                t.pnl_percent AS pnl_percent,
                t.result      AS result
            FROM ai_origination_logs l
            JOIN strategy_trades t ON t.trade_id = l.trade_id
            WHERE l.decision IN ('BUY_CE', 'BUY_PE')
              AND l.trade_id IS NOT NULL
              AND t.status = 'CLOSED'
              AND t.pnl_percent IS NOT NULL
            """
        ).fetchall()
        tick_extremes = {
            row["trade_id"]: (row["low"], row["high"])
            for row in connection.execute(
                """
                SELECT trade_id, MIN(premium) AS low, MAX(premium) AS high
                FROM strategy_trade_ticks
                GROUP BY trade_id
                """
            ).fetchall()
        }
    finally:
        connection.close()

    entries: list[Entry] = []
    for row in rows:
        trade_id = str(row["trade_id"])
        entry_price = row["entry_price"]
        extremes = tick_extremes.get(trade_id)
        mfe = mae = None
        if extremes and entry_price:
            low, high = extremes
            if low is not None and high is not None:
                # Every AI Origination trade is long (BUY_CE/BUY_PE).
                mfe = (high - entry_price) / entry_price * 100.0
                mae = (low - entry_price) / entry_price * 100.0
        plus_di = minus_di = None
        try:
            context = json.loads(row["context_json"] or "{}")
            plus_di = context.get("plus_di")
            minus_di = context.get("minus_di")
        except (TypeError, ValueError):
            pass
        entries.append(Entry(
            trade_id=trade_id,
            index_symbol=str(row["index_symbol"]),
            decision=str(row["decision"]),
            adx=row["adx"],
            plus_di=plus_di,
            minus_di=minus_di,
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))
    return entries


def _di_agrees(decision: str, plus_di: float | None, minus_di: float | None) -> bool | None:
    """True if +DI/-DI direction agrees with the trade's own direction (the
    doc's rule: BUY_CE wants +DI > -DI, BUY_PE wants -DI > +DI). None when
    either DI value is missing -- not defaulted to a side."""
    if plus_di is None or minus_di is None:
        return None
    if decision == "BUY_CE":
        return plus_di > minus_di
    if decision == "BUY_PE":
        return minus_di > plus_di
    return None


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260825)
    diffs = []
    for _ in range(rounds):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        diffs.append(sum(sample_a) / len(sample_a) - sum(sample_b) / len(sample_b))
    diffs.sort()
    lo = diffs[int(0.05 * rounds)]
    hi = diffs[int(0.95 * rounds) - 1]
    return lo, hi


def _report_bucket(label: str, entries: list[Entry]) -> None:
    if not entries:
        logger.info("  %-24s n=0", label)
        return
    n = len(entries)
    wins = sum(1 for e in entries if e.is_win)
    mean_pnl = sum(e.pnl_percent for e in entries) / n
    mfes = [e.mfe_percent for e in entries if e.mfe_percent is not None]
    maes = [e.mae_percent for e in entries if e.mae_percent is not None]
    mfe_txt = f"{sum(mfes) / len(mfes):+.2f}%" if mfes else "n/a"
    mae_txt = f"{sum(maes) / len(maes):+.2f}%" if maes else "n/a"
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-24s n=%-4d win_rate=%5.1f%%  mean_pnl=%+6.2f%%  mean_mfe=%-8s mean_mae=%-8s%s",
        label, n, wins / n * 100.0, mean_pnl, mfe_txt, mae_txt, flag,
    )


def run_adx_buckets(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("PART 1: OUTCOME BY ADX BUCKET (all closed AI Origination trades)")
    logger.info("=" * 100)

    with_adx = [e for e in entries if e.adx is not None]
    no_adx = [e for e in entries if e.adx is None]
    if no_adx:
        logger.info(
            "%d of %d entries have no recorded ADX (predate the field, or a genuine gap) -- "
            "reported separately, excluded from every bucket/floor comparison below.",
            len(no_adx), len(entries),
        )
        _report_bucket("no ADX recorded", no_adx)
        logger.info("-" * 100)

    bucket_sizes = []
    for lo, hi, label in ADX_BUCKETS:
        bucket = [e for e in with_adx if lo <= e.adx < hi]
        bucket_sizes.append(len(bucket))
        _report_bucket(label, bucket)

    logger.info("-" * 100)
    logger.info("Per index:")
    for index_symbol in sorted({e.index_symbol for e in with_adx}):
        logger.info("  %s:", index_symbol)
        for lo, hi, label in ADX_BUCKETS:
            bucket = [e for e in with_adx if e.index_symbol == index_symbol and lo <= e.adx < hi]
            _report_bucket(f"    {label}", bucket)

    logger.info("-" * 100)
    logger.info("PART 2: CANDIDATE HARD-FLOOR CHECK")
    logger.info("=" * 100)
    for floor in CANDIDATE_FLOORS:
        below = [e for e in with_adx if e.adx < floor]
        at_or_above = [e for e in with_adx if e.adx >= floor]
        logger.info("Floor: block entries with ADX < %.0f", floor)
        _report_bucket("  below floor (blocked)", below)
        _report_bucket("  at/above floor (kept)", at_or_above)
        if len(below) >= 2 and len(at_or_above) >= 2:
            lo, hi = _bootstrap_mean_diff(
                [e.pnl_percent for e in below], [e.pnl_percent for e in at_or_above],
            )
            verdict = (
                f"blocked population reliably WORSE -- floor at {floor:.0f} is supported"
                if hi < 0
                else f"blocked population reliably BETTER -- floor at {floor:.0f} would hurt"
                if lo > 0
                else "no reliable difference at this sample size"
            )
            trust = (
                "" if min(len(below), len(at_or_above)) >= MIN_BUCKET_LIVE
                else "  [smaller bucket below the trust minimum -- read as suggestive, not confirmed]"
            )
            logger.info(
                "  bootstrap 90%% CI on mean_pnl(below) - mean_pnl(at/above): [%+.2f, %+.2f] -> %s%s",
                lo, hi, verdict, trust,
            )
        else:
            logger.info("  Too few observations on one side for a bootstrap comparison.")
        logger.info("-" * 100)

    if bucket_sizes and min(bucket_sizes) < MIN_BUCKET_LIVE:
        logger.info(
            "At least one ADX bucket is below the %s-observation trust minimum. Per this "
            "project's standing rule, that is an expected, reportable state at the current "
            "history size -- not a reason to pick a floor from it.",
            MIN_BUCKET_LIVE,
        )


def run_di_direction_check(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("PART 3: DI-DIRECTION AGREEMENT (does +DI/-DI matching the trade's own direction predict outcome?)")
    logger.info("=" * 100)

    with_di = [e for e in entries if _di_agrees(e.decision, e.plus_di, e.minus_di) is not None]
    with_di_ids = {e.trade_id for e in with_di}
    no_di = [e for e in entries if e.trade_id not in with_di_ids]
    if no_di:
        logger.info(
            "%d of %d entries have no plus_di/minus_di in their stored context_json -- "
            "reported separately, excluded from the comparison below.",
            len(no_di), len(entries),
        )
        _report_bucket("no DI recorded", no_di)
        logger.info("-" * 100)

    agrees = [e for e in with_di if _di_agrees(e.decision, e.plus_di, e.minus_di)]
    disagrees = [e for e in with_di if not _di_agrees(e.decision, e.plus_di, e.minus_di)]
    _report_bucket("DI agrees with direction", agrees)
    _report_bucket("DI disagrees with direction", disagrees)
    if len(agrees) >= 2 and len(disagrees) >= 2:
        lo, hi = _bootstrap_mean_diff(
            [e.pnl_percent for e in disagrees], [e.pnl_percent for e in agrees],
        )
        verdict = (
            "disagreeing trades reliably WORSE -- DI-agreement rule is supported" if hi < 0
            else "disagreeing trades reliably BETTER -- DI-agreement rule would hurt" if lo > 0
            else "no reliable difference at this sample size"
        )
        trust = (
            "" if min(len(agrees), len(disagrees)) >= MIN_BUCKET_LIVE
            else "  [smaller bucket below the trust minimum -- read as suggestive, not confirmed]"
        )
        logger.info(
            "bootstrap 90%% CI on mean_pnl(disagrees) - mean_pnl(agrees): [%+.2f, %+.2f] -> %s%s",
            lo, hi, verdict, trust,
        )
    else:
        logger.info("Too few observations on one side for a bootstrap comparison.")

    # The doc's full entry condition is ADX >= floor AND DI agrees -- check
    # the combined gate too, not just each leg in isolation.
    logger.info("-" * 100)
    logger.info("Combined check: ADX >= 20 AND DI agrees, vs. everything else")
    combined_pass = [e for e in with_di if e.adx is not None and e.adx >= ADX_NO_TREND and _di_agrees(e.decision, e.plus_di, e.minus_di)]
    combined_pass_ids = {e.trade_id for e in combined_pass}
    combined_fail = [e for e in with_di if e.trade_id not in combined_pass_ids]
    _report_bucket("combined gate PASSES", combined_pass)
    _report_bucket("combined gate FAILS", combined_fail)
    if len(combined_pass) >= 2 and len(combined_fail) >= 2:
        lo, hi = _bootstrap_mean_diff(
            [e.pnl_percent for e in combined_fail], [e.pnl_percent for e in combined_pass],
        )
        verdict = (
            "failing trades reliably WORSE -- combined gate is supported" if hi < 0
            else "failing trades reliably BETTER -- combined gate would hurt" if lo > 0
            else "no reliable difference at this sample size"
        )
        trust = (
            "" if min(len(combined_pass), len(combined_fail)) >= MIN_BUCKET_LIVE
            else "  [smaller bucket below the trust minimum -- read as suggestive, not confirmed]"
        )
        logger.info(
            "bootstrap 90%% CI on mean_pnl(fails) - mean_pnl(passes): [%+.2f, %+.2f] -> %s%s",
            lo, hi, verdict, trust,
        )
    else:
        logger.info("Too few observations on one side for a bootstrap comparison.")

    logger.info("-" * 100)
    logger.info(
        "ADX SLOPE (rising/falling) is not tested above -- no historical series exists to compute "
        "it from. It would need to be logged going forward (a new AIOriginationLog field) before "
        "it can ever be backtested, the same path trend_duration_pct_of_session and "
        "same_direction_entries_today both took before either was gated."
    )


# --- PART 4: 2-year index-level fallback -------------------------------------

def _is_futures(symbol: str) -> bool:
    return symbol.upper().endswith(FUTURES_SUFFIX)


def _eligible_index(arrays: IndexArrays) -> np.ndarray:
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_window = np.array(
        [INDEX_TRADING_START <= t.time() <= INDEX_TRADING_END for t in hours], dtype=bool,
    )
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21) & ~np.isnan(arrays.adx14)
    return in_window & warm


def _edge_index(wins: float, ups: float, longs: float, n: float) -> float:
    if n == 0:
        return 0.0
    up_rate = ups / n
    base = (longs * up_rate + (n - longs) * (1.0 - up_rate)) / n
    return (wins / n - base) * 100.0


def _evaluate_index(
    arrays: IndexArrays, mask: np.ndarray, direction: np.ndarray, forward_bars: int, rng,
) -> tuple[int, float, float, float]:
    """(n_signals, edge, ci_low, ci_high) via session-block bootstrap. Same
    shape as trend_age_gate_backtest.py/break_confirmation_backtest.py's own
    _evaluate -- duplicated per this project's established per-script
    convention, not shared."""
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
    edge = _edge_index(float(win.sum()), float(up.sum()), float(is_long.sum()), float(idx.size))

    sessions = arrays.session_id[idx]
    _, session_index = np.unique(sessions, return_inverse=True)
    size = session_index.max() + 1
    per_n = np.bincount(session_index, minlength=size).astype(np.float64)
    per_win = np.bincount(session_index, weights=win.astype(np.float64), minlength=size)
    per_up = np.bincount(session_index, weights=up.astype(np.float64), minlength=size)
    per_long = np.bincount(session_index, weights=is_long.astype(np.float64), minlength=size)

    edges = np.empty(BOOTSTRAP_ITERATIONS_INDEX)
    for b in range(BOOTSTRAP_ITERATIONS_INDEX):
        pick = rng.integers(0, size, size=size)
        total = per_n[pick].sum()
        edges[b] = _edge_index(per_win[pick].sum(), per_up[pick].sum(), per_long[pick].sum(), total) if total else 0.0
    ci_low, ci_high = np.percentile(edges, [5, 95])
    return int(idx.size), edge, float(ci_low), float(ci_high)


def run_index_fallback(db_path: str, table: str, interval: str) -> None:
    logger.info("=" * 108)
    logger.info("PART 4: 2-YEAR INDEX-LEVEL FALLBACK (registered setups, ADX < floor vs ADX >= floor)")
    logger.info("=" * 108)

    connection = sqlite3.connect(db_path)
    try:
        symbols = [
            row[0] for row in connection.execute(
                f"SELECT DISTINCT index_symbol FROM {table} WHERE interval = ?", (interval,),
            )
        ]
    finally:
        connection.close()

    rng = np.random.default_rng(20260825)
    setups = default_setups()
    any_result = False
    logger.info(
        "  %-10s %-24s %-13s %-13s %6s %9s  %-18s %s",
        "index", "setup", "adx_floor", "bucket", "n", "edge", "bootstrap 90% CI", "verdict",
    )
    for symbol in sorted(symbols):
        bars = load_bars_sqlite(db_path, table, symbol, interval)
        if len(bars) < 500:
            continue
        arrays = build_arrays(symbol, bars)
        eligible = _eligible_index(arrays)
        is_futures = _is_futures(symbol)

        for setup in setups:
            needs_volume = setup.name.upper() in VOLUME_DEPENDENT_SETUPS
            if needs_volume != is_futures:
                continue
            direction = build_signals(arrays, setup)
            assert_causal(arrays, setup, direction)

            for floor in CANDIDATE_FLOORS:
                below = eligible & (arrays.adx14 < floor)
                at_or_above = eligible & (arrays.adx14 >= floor)
                for bucket_name, bucket_mask in (("below", below), ("at_or_above", at_or_above)):
                    n, edge, ci_low, ci_high = _evaluate_index(arrays, bucket_mask, direction, HORIZON_BARS, rng)
                    if n < MIN_SIGNALS_INDEX:
                        continue
                    any_result = True
                    verdict = "POSITIVE" if ci_low > 0 else ("BACKWARDS" if ci_high < 0 else "-")
                    logger.info(
                        "  %-10s %-24s <%-12.0f %-13s %6d %+8.2fpp  [%+6.2f, %+6.2f]  %s",
                        symbol, setup.label, floor, bucket_name, n, edge, ci_low, ci_high, verdict,
                    )

    if not any_result:
        logger.error(
            "No (index, setup, floor, bucket) cell reached %s signals. Nothing to report -- most "
            "likely no real candle data in this environment (expected in this sandbox).",
            MIN_SIGNALS_INDEX,
        )
        return

    logger.info("-" * 108)
    logger.info(
        "Read this as a related but not identical question from PARTS 1-3: forward index-direction "
        "edge, not real trades or premium P&L. A (setup, floor) combination where 'below' is "
        "reliably worse than 'at_or_above' -- on BOTH indices -- is the kind of consistency this "
        "project's own standard treats as real rather than noise (see setup_significance.py's own "
        "docstring). A single-index or mixed-direction result is not that, even with a CI excluding "
        "zero."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument(
        "--skip-live-history", action="store_true",
        help="Skip PARTS 1-3 (real AI Origination trades) and only run PART 4 (2-year index archive).",
    )
    args = parser.parse_args()

    if not args.skip_live_history:
        entries = _load_entries(args.db)
        if not entries:
            logger.error(
                "No closed AI Origination entries with a joinable ai_origination_logs row found. "
                "Either data/trading.db has no history yet, or this sandbox has no real data at "
                "all (expected here -- see CLAUDE.md). Run this on the machine with real trade history."
            )
        else:
            run_adx_buckets(entries)
            run_di_direction_check(entries)

    run_index_fallback(args.db, args.table, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
