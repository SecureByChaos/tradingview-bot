"""Quick Scalp -- would a wider premium stop, or an ADX trend filter, have
helped? Requested after the September cross-strategy review showed
`STOPLOSS` alone (-Rs8,366 across 15 trades) accounting for more than the
whole month's net loss (-Rs8,194) -- every other exit reason combined was
roughly flat. Two candidate fixes, tested independently rather than guessed
at:

  PART A -- would a WIDER stop have helped? Quick Scalp's current stop is a
  flat -2.5% of entry premium (app/quick_scalp.py's _STOP_PERCENT), tighter
  than every stop distance this project has previously tested for a
  different strategy (scripts/stop_distance_backtest.py swept 5-12% for AI
  Origination and found 5% already too tight there; scripts/scalp_stop_
  sweep.py found 1-4% stops net-negative after costs for the PRIOR, now-
  superseded EMA_RSI_CROSS scalp signal). Neither tested THIS strategy's own
  real trades at 2.5%, so that is what this does: every closed QUICK_SCALP
  FIXED-mode trade is replayed from its own real entry_price/entry_time,
  using its own real target held fixed, against swept stop distances --
  same reconstruction method, same pessimistic intrabar ordering, and the
  same load_premium_series/db_timestamp_to_ist helpers scripts.stop_
  distance_backtest.py already established and trusts (reused here, not
  reimplemented).

  NOT simulated here, named rather than silently folded in: the structural
  (index-level) stop and the 3-minute breakeven-trail/scratch mechanic --
  layering either on top would conflate the pure premium-stop-width question
  with two other, separately-tunable mechanisms and make the result harder
  to read, the same reasoning stop_distance_backtest.py already gives for
  leaving trailing/STALL_EXIT out of its own replay.

  PART B -- would an ADX trend filter have helped? Quick Scalp is a MEAN-
  REVERSION engine (VWAP 2-sigma band pierce + wick rejection, betting price
  reverts toward VWAP) -- it has no awareness of whether the broader index is
  actually trending underneath it. For every closed QUICK_SCALP trade (any
  exit reason), ADX(14) is recomputed from the index's own real FIVE_MINUTE
  candle history as of the entry bar -- the same indicator, same period, and
  the same ADX_NO_TREND/ADX_TRENDING bands (app/market_context.py) every
  other trend-aware part of this codebase already uses, not a new threshold
  invented for this question. Deliberately 5-minute bars, not Quick Scalp's
  own 1-minute feed bars -- a 14-period ADX on 1-minute data is only a
  14-minute lookback, too short to describe "is the broader market
  trending" the way this project's existing ADX convention already means it
  elsewhere. Buckets trades into NO_TREND (<20) / MARGINAL (20-25) / TRENDING
  (>=25) and reports win rate, mean P&L, and STOPLOSS-rate per bucket, plus a
  bootstrap 90% CI on whether the TRENDING bucket is reliably worse than
  NO_TREND -- the direct test of "should this mean-reversion engine refuse
  to fire while the index is clearly trending."

Per this project's standing discipline: nothing ships into app/quick_scalp.py
from this pass regardless of what either part finds -- this is measurement
only. A verdict of "neither clears the bar" is as reportable an outcome as
finding something that does.

REQUIRES DATA NOT PRESENT IN THIS SANDBOX: data/trading.db with real
strategy_trades/candles tables, and data/option_candles/ (built by
scripts/pull_option_candles.py) for PART A's premium replay. Neither exists
here -- built and unit-tested against synthetic data; run on the machine
that has both.

Usage:
    python -m scripts.quick_scalp_stop_trend_backtest --db data/trading.db
    python -m scripts.quick_scalp_stop_trend_backtest --db data/trading.db --stops 2.5,3.5,4,5,6
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time

from app.indicators import adx
from app.market_context import ADX_NO_TREND, ADX_TRENDING
from app.trade_costs import estimate_round_trip_cost
from scripts.backtest.data import load_bars_sqlite
from scripts.stall_exit_backtest import db_timestamp_to_ist, load_premium_series

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("quick_scalp_stop_trend_backtest")

SQUARE_OFF = time(15, 15)
DEFAULT_STOPS = (2.5, 3.5, 4.0, 5.0, 6.0)
ACTUAL_LABEL = "actual"
# Same value and meaning as stop_distance_backtest.py's NOISE_MFE_FRACTION --
# a documented judgment call, not a measured threshold, kept consistent
# across every script that uses it rather than re-derived per script.
NOISE_MFE_FRACTION = 0.20
MIN_SAMPLE = 10
MIN_BUCKET_LIVE = 20
BOOTSTRAP_ROUNDS = 2000


# ---------------------------------------------------------------------------
# PART A -- stop-width sweep
# ---------------------------------------------------------------------------


@dataclass
class SourceTrade:
    trade_id: str
    index_symbol: str
    option_type: str
    entry_price: float
    entry_time: str
    target_price: float
    actual_stoploss_price: float
    quantity: int
    tradingsymbol: str
    symboltoken: str


@dataclass
class ReplayResult:
    trade_id: str
    index_symbol: str
    option_type: str
    entry_day: str
    stop_label: str
    reason: str
    pnl_percent: float
    net_pnl_percent: float
    is_win: bool
    is_noise_hit: bool


def _load_stop_sweep_trades(db_path: str) -> list[SourceTrade]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, index_symbol, option_type, entry_price, entry_time,
                   target, stoploss, quantity, tradingsymbol, symboltoken
            FROM strategy_trades
            WHERE origin = 'QUICK_SCALP'
              AND exit_price IS NOT NULL
              AND sl_mode = 'FIXED'
              AND entry_price IS NOT NULL AND entry_price > 0
              AND target IS NOT NULL AND stoploss IS NOT NULL
            ORDER BY entry_time
            """
        ).fetchall()
    finally:
        connection.close()
    return [
        SourceTrade(
            trade_id=row["trade_id"], index_symbol=str(row["index_symbol"]),
            option_type=str(row["option_type"]), entry_price=float(row["entry_price"]),
            entry_time=str(row["entry_time"]), target_price=float(row["target"]),
            actual_stoploss_price=float(row["stoploss"]), quantity=int(row["quantity"]),
            tradingsymbol=str(row["tradingsymbol"]), symboltoken=str(row["symboltoken"]),
        )
        for row in rows
    ]


def _cost_pct(entry_price: float, exit_price: float, quantity: int) -> float:
    if entry_price <= 0 or quantity <= 0:
        return 0.0
    breakdown = estimate_round_trip_cost(entry_price, exit_price, quantity)
    return breakdown.total / (entry_price * quantity) * 100.0


def _replay(
    bars: list[tuple], entry_price: float, stop_price: float, target_price: float,
) -> tuple[str, float, float]:
    """(reason, exit_price, mfe_price). Pessimistic intrabar ordering: a bar
    that touches both stop and target in the same minute scores as a loss --
    same convention as stop_distance_backtest.py/stall_exit_backtest.py, for
    the same reason: it makes a "this distance is fine" finding harder to
    reach, not easier."""
    mfe_price = entry_price
    for ts, high, low, close in bars:
        if ts.time() >= SQUARE_OFF:
            return "TIME_EXIT", close, mfe_price
        mfe_price = max(mfe_price, high)
        if low <= stop_price:
            return "STOPLOSS", stop_price, mfe_price
        if high >= target_price:
            return "TARGET", target_price, mfe_price
    return "INCOMPLETE", bars[-1][3] if bars else entry_price, mfe_price


def _run_one(trade: SourceTrade, bars: list[tuple], stop_label: str, stop_price: float) -> ReplayResult:
    reason, exit_price, mfe_price = _replay(bars, trade.entry_price, stop_price, trade.target_price)
    pnl_percent = (exit_price - trade.entry_price) / trade.entry_price * 100.0
    cost_pct = _cost_pct(trade.entry_price, exit_price, trade.quantity)
    stop_distance = trade.entry_price - stop_price
    mfe_fraction = (mfe_price - trade.entry_price) / stop_distance if stop_distance > 0 else 0.0
    is_noise_hit = reason == "STOPLOSS" and mfe_fraction < NOISE_MFE_FRACTION
    return ReplayResult(
        trade_id=trade.trade_id, index_symbol=trade.index_symbol, option_type=trade.option_type,
        entry_day=trade.entry_time[:10], stop_label=stop_label, reason=reason,
        pnl_percent=pnl_percent, net_pnl_percent=pnl_percent - cost_pct,
        is_win=pnl_percent > 0, is_noise_hit=is_noise_hit,
    )


@dataclass
class StopCell:
    stop_label: str
    split: str
    n: int
    win_rate: float
    noise_hit_rate: float
    mean_pnl_percent: float
    mean_net_expectancy_percent: float


def _aggregate_stop_cell(results: list[ReplayResult], stop_label: str, split: str) -> StopCell | None:
    group = [r for r in results if r.stop_label == stop_label]
    if not group:
        return None
    n = len(group)
    stop_outs = [r for r in group if r.reason == "STOPLOSS"]
    noise_hit_rate = (sum(1 for r in stop_outs if r.is_noise_hit) / len(stop_outs)) if stop_outs else 0.0
    return StopCell(
        stop_label=stop_label, split=split, n=n,
        win_rate=sum(1 for r in group if r.is_win) / n * 100.0,
        noise_hit_rate=noise_hit_rate * 100.0,
        mean_pnl_percent=sum(r.pnl_percent for r in group) / n,
        mean_net_expectancy_percent=sum(r.net_pnl_percent for r in group) / n,
    )


def run_stop_sweep(db_path: str, stop_levels: tuple[float, ...], split_fraction: float) -> int:
    trades = _load_stop_sweep_trades(db_path)
    logger.info("=" * 100)
    logger.info("PART A -- Quick Scalp stop-width sweep: would a wider stop have helped?")
    logger.info("=" * 100)
    logger.info("  Closed QUICK_SCALP FIXED-mode trades: %s", len(trades))
    if not trades:
        logger.error("No closed QUICK_SCALP FIXED-mode trades found. Nothing to replay.")
        return 1

    reconstructed = 0
    no_archive: list[str] = []
    no_bars_from_entry: list[str] = []
    all_results: list[ReplayResult] = []
    for trade in trades:
        series = load_premium_series(trade.tradingsymbol, trade.symboltoken)
        if not series:
            no_archive.append(f"{trade.trade_id[:8]} {trade.index_symbol} {trade.tradingsymbol}")
            continue
        entry_ist = db_timestamp_to_ist(trade.entry_time)
        bars = [b for b in series if b[0] >= entry_ist and b[0].date() == entry_ist.date()]
        if not bars:
            no_bars_from_entry.append(f"{trade.trade_id[:8]} {trade.index_symbol} {trade.tradingsymbol}")
            continue
        reconstructed += 1

        for stop_pct in stop_levels:
            stop_price = round(trade.entry_price * (1 - stop_pct / 100.0), 2)
            label = f"{stop_pct:.1f}%"
            all_results.append(_run_one(trade, bars, label, stop_price))
        all_results.append(_run_one(trade, bars, ACTUAL_LABEL, trade.actual_stoploss_price))

    logger.info("  Reconstructed from real option candles: %s of %s", reconstructed, len(trades))
    if no_archive:
        logger.warning(
            "  %s not reconstructible -- contract not in data/option_candles/. Result below "
            "describes only the %s that could be reconstructed.", len(no_archive), reconstructed,
        )
    if no_bars_from_entry:
        logger.warning(
            "  %s had an archived contract but no bars on/after the real entry time (same "
            "calendar day) -- excluded rather than replayed from the wrong starting point.",
            len(no_bars_from_entry),
        )
    if not all_results:
        logger.error(
            "Nothing could be reconstructed. Archive the contracts Quick Scalp is currently "
            "trading (scripts/pull_option_candles.py) and re-run."
        )
        return 1

    distinct_days = sorted({r.entry_day for r in all_results})
    cutoff_idx = max(int(len(distinct_days) * split_fraction), 1)
    if len(distinct_days) < 2:
        splits = (("all", all_results),)
    else:
        cutoff_day = distinct_days[min(cutoff_idx, len(distinct_days) - 1)]
        in_sample = [r for r in all_results if r.entry_day < cutoff_day]
        out_of_sample = [r for r in all_results if r.entry_day >= cutoff_day]
        splits = (
            (("in_sample", in_sample), ("out_of_sample", out_of_sample))
            if in_sample and out_of_sample else (("all", all_results),)
        )

    labels = [f"{s:.1f}%" for s in stop_levels] + [ACTUAL_LABEL]
    logger.info("-" * 100)
    logger.info("  %-8s %-12s %6s %8s %10s %10s %12s", "stop", "split", "n", "win%", "noise-hit%", "mean_pnl%", "net_exp%")
    cells: list[StopCell] = []
    for label in labels:
        for split_name, split_results in splits:
            cell = _aggregate_stop_cell(split_results, label, split_name)
            if cell is None:
                continue
            cells.append(cell)
            flag = "" if cell.n >= MIN_SAMPLE else "  [THIN]"
            logger.info(
                "  %-8s %-12s %6d %7.1f%% %9.1f%% %9.2f%% %11.2f%%%s",
                label, split_name, cell.n, cell.win_rate, cell.noise_hit_rate,
                cell.mean_pnl_percent, cell.mean_net_expectancy_percent, flag,
            )

    logger.info("-" * 100)
    logger.info("RECOMMENDATION (PART A)")
    actual_cells = [c for c in cells if c.stop_label == ACTUAL_LABEL]
    actual_baseline = (
        sum(c.mean_net_expectancy_percent * c.n for c in actual_cells) / sum(c.n for c in actual_cells)
        if actual_cells else None
    )
    logger.info(
        "  current (actual, 2.5%%) stop baseline net expectancy: %s",
        f"{actual_baseline:+.2f}%" if actual_baseline is not None else "n/a",
    )
    for stop_pct in stop_levels:
        label = f"{stop_pct:.1f}%"
        label_cells = [c for c in cells if c.stop_label == label]
        if not label_cells:
            continue
        consistent_positive = all(c.mean_net_expectancy_percent > 0 for c in label_cells)
        consistent_below_noise_bar = all(c.noise_hit_rate < 50.0 for c in label_cells)
        thin = any(c.n < MIN_SAMPLE for c in label_cells)
        verdict = (
            "CLEARS both bars" if (consistent_positive and consistent_below_noise_bar and not thin)
            else ("thin sample -- not a basis for a decision" if thin else "does not clear both bars")
        )
        logger.info("    stop=%-6s -> %s", label, verdict)
    logger.info(
        "  A stop only 'clears both bars' if EVERY reported split shows net expectancy > 0 AND "
        "noise-hit rate < 50%%, with no split below the %s-trade minimum. Per this project's "
        "standing rule: if nothing clears, that is the expected, reportable answer, not a "
        "failure of this backtest.", MIN_SAMPLE,
    )
    return 0


# ---------------------------------------------------------------------------
# PART B -- ADX trend-filter check
# ---------------------------------------------------------------------------


@dataclass
class TrendEntry:
    trade_id: str
    index_symbol: str
    adx_band: str
    pnl_percent: float
    net_pnl_percent: float
    is_win: bool
    is_stoploss: bool


def _load_trend_trades(db_path: str) -> list[dict]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, index_symbol, entry_time, entry_price, exit_price,
                   quantity, pnl_percent, estimated_cost, investment_amount,
                   result, exit_reason
            FROM strategy_trades
            WHERE origin = 'QUICK_SCALP' AND exit_price IS NOT NULL
            ORDER BY entry_time
            """
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _adx_band(value: float) -> str:
    if value < ADX_NO_TREND:
        return "NO_TREND"
    if value < ADX_TRENDING:
        return "MARGINAL"
    return "TRENDING"


def _adx_at_entry(bars_cache: dict, db_path: str, index_symbol: str, entry_ist: datetime) -> float | None:
    """ADX(14) on real FIVE_MINUTE candles, read at the last completed bar at
    or before entry_ist. One candle load per index, cached across trades --
    same index queried repeatedly as trades accumulate through the day."""
    if index_symbol not in bars_cache:
        bars = load_bars_sqlite(db_path, "candles", index_symbol, "FIVE_MINUTE")
        bars_cache[index_symbol] = (bars, adx(bars, 14))
    bars, adx_points = bars_cache[index_symbol]
    idx = None
    for i, bar in enumerate(bars):
        if bar.ts_ist <= entry_ist:
            idx = i
        else:
            break
    if idx is None:
        return None
    point = adx_points[idx]
    return None if point is None else point.adx


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20261002)
    diffs = []
    for _ in range(rounds):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        diffs.append(sum(sample_a) / len(sample_a) - sum(sample_b) / len(sample_b))
    diffs.sort()
    lo = diffs[int(0.05 * rounds)]
    hi = diffs[int(0.95 * rounds) - 1]
    return lo, hi


def _report_trend_bucket(label: str, entries: list[TrendEntry]) -> None:
    if not entries:
        logger.info("  %-10s n=0", label)
        return
    n = len(entries)
    wins = sum(1 for e in entries if e.is_win)
    stoplosses = sum(1 for e in entries if e.is_stoploss)
    mean_pnl = sum(e.pnl_percent for e in entries) / n
    mean_net = sum(e.net_pnl_percent for e in entries) / n
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-10s n=%-4d win_rate=%5.1f%%  stoploss_rate=%5.1f%%  mean_pnl=%+6.2f%%  mean_net=%+6.2f%%%s",
        label, n, wins / n * 100.0, stoplosses / n * 100.0, mean_pnl, mean_net, flag,
    )


def run_trend_filter(db_path: str) -> int:
    rows = _load_trend_trades(db_path)
    logger.info("=" * 100)
    logger.info("PART B -- Quick Scalp ADX trend filter: does this mean-reversion engine")
    logger.info("          perform worse while the index is actually trending?")
    logger.info("=" * 100)
    logger.info("  Closed QUICK_SCALP trades (any exit reason): %s", len(rows))
    if not rows:
        logger.error("No closed QUICK_SCALP trades found. Nothing to classify.")
        return 1

    bars_cache: dict = {}
    entries: list[TrendEntry] = []
    no_adx = 0
    for row in rows:
        entry_ist = db_timestamp_to_ist(str(row["entry_time"]))
        adx_value = _adx_at_entry(bars_cache, db_path, str(row["index_symbol"]), entry_ist)
        if adx_value is None:
            no_adx += 1
            continue
        investment = float(row["investment_amount"] or 0.0)
        cost = float(row["estimated_cost"] or 0.0)
        net_pnl_percent = ((float(row["pnl_percent"] or 0.0) / 100.0 * investment) - cost) / investment * 100.0 \
            if investment > 0 else float(row["pnl_percent"] or 0.0)
        entries.append(TrendEntry(
            trade_id=str(row["trade_id"]), index_symbol=str(row["index_symbol"]),
            adx_band=_adx_band(adx_value), pnl_percent=float(row["pnl_percent"] or 0.0),
            net_pnl_percent=net_pnl_percent, is_win=str(row["result"]) == "WIN",
            is_stoploss=str(row["exit_reason"]) == "STOPLOSS",
        ))

    logger.info("  Classified with a real ADX reading: %s of %s", len(entries), len(rows))
    if no_adx:
        logger.warning(
            "  %s excluded -- no FIVE_MINUTE candle history at/before entry for that index "
            "(not defaulted to a band, since 'no reading yet' is not the same as any real band).",
            no_adx,
        )
    if not entries:
        logger.error("Nothing could be classified. Backfill FIVE_MINUTE candles and re-run.")
        return 1

    logger.info("-" * 100)
    for band in ("NO_TREND", "MARGINAL", "TRENDING"):
        _report_trend_bucket(band, [e for e in entries if e.adx_band == band])

    logger.info("-" * 100)
    logger.info("RECOMMENDATION (PART B)")
    no_trend = [e.net_pnl_percent for e in entries if e.adx_band == "NO_TREND"]
    trending = [e.net_pnl_percent for e in entries if e.adx_band == "TRENDING"]
    if len(no_trend) >= MIN_BUCKET_LIVE and len(trending) >= MIN_BUCKET_LIVE:
        lo, hi = _bootstrap_mean_diff(trending, no_trend)
        reliably_worse = hi < 0
        logger.info(
            "  TRENDING vs NO_TREND mean net P&L%% difference, 90%% CI: [%+.2f%%, %+.2f%%]", lo, hi,
        )
        logger.info(
            "  %s",
            "TRENDING is reliably worse -- real support for an ADX trend filter blocking new "
            "Quick Scalp entries above the TRENDING threshold." if reliably_worse
            else "CI does not exclude zero -- not yet reliable evidence either way.",
        )
    else:
        logger.info(
            "  NO_TREND (n=%d) and/or TRENDING (n=%d) below the %s-trade minimum -- "
            "not yet enough evidence to compare reliably. Not a failure of this check, "
            "the correct 'not yet enough evidence' outcome at this sample size.",
            len(no_trend), len(trending), MIN_BUCKET_LIVE,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--stops", default=",".join(str(s) for s in DEFAULT_STOPS))
    parser.add_argument("--split-fraction", type=float, default=0.7,
                         help="Fraction of distinct trading days (chronological) in the in-sample slice.")
    parser.add_argument("--part", choices=("a", "b", "both"), default="both",
                         help="Run only the stop sweep (a), only the trend filter (b), or both (default).")
    args = parser.parse_args()
    stop_levels = tuple(float(s.strip()) for s in args.stops.split(",") if s.strip())

    exit_code = 0
    if args.part in ("a", "both"):
        exit_code = run_stop_sweep(args.db, stop_levels, args.split_fraction) or exit_code
    if args.part in ("b", "both"):
        exit_code = run_trend_filter(args.db) or exit_code
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
