"""Would STALL_EXIT trades have gone on to win?

STEP 1 (and 2) OF THE STALL_EXIT QUESTION, AND DELIBERATELY NOT 3-5.
See the blockers section below for why the walk-forward and holdout steps of
the spec cannot be run as written.

PEAK-MFE-CONDITIONED EXEMPTION SWEEP (added 12 Aug)
----------------------------------------------------
Prompted by a real giveback measurement: winning STALL_EXIT trades give back
~63% of their peak MFE on average, worse than TRAIL_EXIT's ~50% -- and by
construction (STALL_EXIT requires `not trade.trailing_active`), 100% of that
giveback happens on trades that never armed the full trailing mechanism, so
none of it is trailing's doing. STALL_EXIT's band is measured against ENTRY
price, with no awareness of how far a trade actually ran before settling
back -- a trade that peaked at +8% and eased to +4% reads identically to one
that never moved, and both get closed on the same 60-minute/+-5% rule.

The baseline analysis above answers "what if STALL_EXIT never fired at all,
unconditionally" (6 Aug finding: net -8.54%/trade, protective). This is a
different, narrower question: what if STALL_EXIT still fires for trades that
genuinely went nowhere, but is EXEMPTED once a trade's own peak (MFE at the
moment it would have stalled) already cleared some floor below full trailing
activation? Reuses the exact same real-premium replay this file already does
for every STALL_EXIT trade -- the only new thing is bucketing the results
that were already being computed by whether each trade's own `highest_price`
at exit cleared a candidate floor, and reporting the net effect of adopting
each floor as an exemption threshold. Every trade in the STALL_EXIT
population has mfe_at_stall below ITS OWN trail_activate_percent by
construction (arming exempts a trade from stalling in the first place), so
the floors swept here are meaningful headroom below whatever each trade's
own activation happened to be, not an arbitrary constant.

THE COUNTERFACTUAL IS RECONSTRUCTED FROM REAL PREMIUM, NOT SIMULATED
--------------------------------------------------------------------
Knowing what a stalled-out position would have done requires its premium path
AFTER the exit, which the trade record does not contain -- strategy_trade_ticks
stops when the trade closes. Two sources could supply it:

  * data/option_candles/<SYMBOL>_<TOKEN>.csv -- the actual 1-minute premium of
    the actual contract. Exact, but only for archived contracts.
  * the premium elasticity model, converting index moves into premium moves.
    Available for everything, but it is a model, and one with known Epps
    attenuation of 3-20% depending on DTE.

This script uses ONLY the first. A question about whether a 5% band cut a
winner short cannot be answered by a model whose own error bar is comparable
to the effect size. Trades whose contract is not archived are counted as
UNRECONSTRUCTIBLE and reported, never quietly dropped -- the coverage fraction
is part of the result.

THE REPLAY MIRRORS monitor_open_trades EXACTLY
---------------------------------------------
Same order of checks, same per-trade trail parameters, same 15:15 TIME_EXIT.
At a STALL_EXIT the trail is by construction not armed (arming exempts a trade
from stalling), so the replay starts with trailing_active False and the
high-water mark carried from the live portion.

Usage:
    python -m scripts.stall_exit_backtest --db data/trading.db
    python -m scripts.stall_exit_backtest --db data/trading.db --verbose
    python -m scripts.stall_exit_backtest --db data/trading.db --peak-floors 2,3,4,5,6,7,8
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("stall_exit_backtest")

OPTION_CANDLE_DIR = Path("data/option_candles")
SQUARE_OFF = time(15, 15)

# Mirrors app/multi_strategy.py. Duplicated rather than imported because that
# module drags the live trading stack in; if the live constants change, this
# must follow.
_AI_ORIGIN_TRAIL_ACTIVATION_PERCENT = 8.0
_AI_ORIGIN_TRAIL_OFFSET_PERCENT = 5.0

# Below this the split is arithmetic, not evidence. Named rather than implied so
# a thin result cannot be quoted as a finding.
MIN_SAMPLE_FOR_SPLIT = 20


@dataclass
class Replay:
    trade_id: str
    provider: str
    index_symbol: str
    entry_day: str
    actual_pnl_percent: float
    counterfactual_reason: str
    counterfactual_pnl_percent: float
    adx: float | None
    cpr: str | None
    minutes_held_after: int
    # Peak reached (highest_price vs entry) as of the moment STALL_EXIT
    # actually fired -- what a peak-relative exemption floor would compare
    # against. Always below this trade's own trail_activate_percent, since
    # arming trailing exempts a trade from stalling in the first place.
    mfe_at_stall_percent: float

    @property
    def delta(self) -> float:
        return self.counterfactual_pnl_percent - self.actual_pnl_percent


@dataclass
class Coverage:
    total: int = 0
    reconstructed: int = 0
    no_archive: list[str] = field(default_factory=list)
    no_bars_after: list[str] = field(default_factory=list)


def _load_premium_series(tradingsymbol: str, symboltoken: str) -> list[tuple[datetime, float, float, float]]:
    """(ts, high, low, close) per minute for one contract, or []."""
    path = OPTION_CANDLE_DIR / f"{tradingsymbol}_{symboltoken}.csv"
    if not path.exists():
        return []
    series = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                ts = datetime.fromisoformat(str(row["timestamp_ist"])).replace(tzinfo=None)
                series.append((ts, float(row["high"]), float(row["low"]), float(row["close"])))
            except (KeyError, ValueError):
                continue
    return sorted(series)


def _replay_forward(
    bars: list[tuple[datetime, float, float, float]],
    entry_price: float,
    stoploss: float,
    target: float,
    trail_activate_percent: float,
    trail_width_percent: float,
    high_water: float,
) -> tuple[str, float]:
    """Continue the position past the stall. Returns (exit_reason, exit_premium).

    Check order mirrors monitor_open_trades: trail first once armed, then the
    fixed stop, then target. Intrabar, the ADVERSE extreme is tested before the
    favourable one -- a bar that touched both the stop and the target is scored
    as a loss. That is the pessimistic assumption, and it is the right one for a
    study whose hypothesis is "we are leaving money on the table": it makes the
    finding harder to reach, not easier.
    """
    activation_price = entry_price * (1 + trail_activate_percent / 100)
    trail_offset = entry_price * (trail_width_percent / 100)
    trailing_active = False
    trailing_stop = None

    for ts, high, low, close in bars:
        if ts.time() >= SQUARE_OFF:
            return "TIME_EXIT", close
        high_water = max(high_water, high)
        if not trailing_active and high >= activation_price:
            trailing_active = True
        if trailing_active:
            trailing_stop = round(high_water - trail_offset, 2)

        if trailing_active and trailing_stop is not None and low <= trailing_stop:
            return "TRAIL_EXIT", trailing_stop
        if low <= stoploss:
            return "STOPLOSS", stoploss
        if high >= target:
            return "TARGET", target
    # Ran out of archived bars before 15:15 -- the contract's archive ends
    # mid-session. Reported separately rather than counted as a time exit.
    return "INCOMPLETE", bars[-1][3] if bars else 0.0


def _regime(context_json: str | None) -> tuple[float | None, str | None]:
    """ADX and CPR classification AT ENTRY.

    NOT at the moment STALL_EXIT fired, which is what the question really wants
    -- market_context is snapshotted once, at entry, and a stall fires at least
    60 minutes later. Over an hour ADX can cross a threshold in either
    direction, so this is a proxy and the regime split inherits that slack.
    Recording the limitation rather than the alternative of silently presenting
    entry regime as trigger regime.
    """
    if not context_json:
        return None, None
    try:
        data = json.loads(context_json)
    except (TypeError, ValueError):
        return None, None
    cpr = (data.get("cpr") or {}).get("classification")
    return data.get("adx"), cpr


DEFAULT_PEAK_FLOORS = (3.0, 4.0, 5.0, 6.0, 7.0)
# Thin at this population size, but always reported with its n rather than
# hidden -- the STALL_EXIT population itself is small (tens of trades), so a
# per-floor exemption bucket is smaller still. Below the existing
# MIN_SAMPLE_FOR_SPLIT deliberately: that gate protects a 2-way regime split,
# this is a single count meant to be read alongside its own n, not treated as
# pass/fail.
MIN_SAMPLE_FOR_FLOOR_CELL = 5


@dataclass
class FloorResult:
    floor: float
    n_exempt: int
    n_total: int
    exempt_mean_actual: float
    exempt_mean_counterfactual: float
    portfolio_mean_baseline: float
    portfolio_mean_if_adopted: float

    @property
    def exempt_mean_delta(self) -> float:
        return self.exempt_mean_counterfactual - self.exempt_mean_actual

    @property
    def portfolio_delta(self) -> float:
        return self.portfolio_mean_if_adopted - self.portfolio_mean_baseline


def _sweep_peak_floors(replays: list[Replay], floors: tuple[float, ...]) -> list[FloorResult]:
    """For each candidate floor, split the SAME reconstructed replays into
    "would be exempted from STALL_EXIT" (mfe_at_stall >= floor -- outcome
    becomes the real-premium counterfactual already computed above) and
    "still stalls" (outcome unchanged, the real actual_pnl_percent). Reports
    both the exempted subgroup's own delta and the whole population's mean
    P&L under that floor versus today's actual baseline. No new premium
    reconstruction -- this only re-buckets results _replay_forward already
    produced."""
    baseline_mean = sum(r.actual_pnl_percent for r in replays) / len(replays)
    results: list[FloorResult] = []
    for floor in floors:
        exempt = [r for r in replays if r.mfe_at_stall_percent >= floor]
        still_stalls = [r for r in replays if r.mfe_at_stall_percent < floor]
        if_adopted = [r.counterfactual_pnl_percent for r in exempt] + [r.actual_pnl_percent for r in still_stalls]
        results.append(FloorResult(
            floor=floor,
            n_exempt=len(exempt),
            n_total=len(replays),
            exempt_mean_actual=(sum(r.actual_pnl_percent for r in exempt) / len(exempt)) if exempt else 0.0,
            exempt_mean_counterfactual=(sum(r.counterfactual_pnl_percent for r in exempt) / len(exempt)) if exempt else 0.0,
            portfolio_mean_baseline=baseline_mean,
            portfolio_mean_if_adopted=(sum(if_adopted) / len(if_adopted)) if if_adopted else baseline_mean,
        ))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--verbose", action="store_true", help="Print every reconstructed trade")
    parser.add_argument(
        "--peak-floors", default=",".join(str(f) for f in DEFAULT_PEAK_FLOORS),
        help="Comma-separated candidate peak-MFE exemption floors to sweep, in percent.",
    )
    args = parser.parse_args()
    peak_floors = tuple(float(f.strip()) for f in args.peak_floors.split(",") if f.strip())

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, origin, index_symbol, tradingsymbol, symboltoken,
                   entry_price, exit_price, stoploss, target, entry_time, exit_time,
                   trail_activate_percent, trail_width_percent, highest_price,
                   market_context_json
            FROM strategy_trades
            WHERE origin LIKE 'AI_ORIGIN_%' AND exit_reason = 'STALL_EXIT'
            ORDER BY entry_time
            """
        ).fetchall()
        total_origin = connection.execute(
            "SELECT count(*) FROM strategy_trades WHERE origin LIKE 'AI_ORIGIN_%' AND exit_price IS NOT NULL"
        ).fetchone()[0]
    finally:
        connection.close()

    logger.info("=" * 84)
    logger.info("STALL_EXIT counterfactual: what would these trades have done?")
    logger.info("=" * 84)
    logger.info("  STALL_EXIT closures: %s of %s closed AI Origination trades (%.0f%%)",
                len(rows), total_origin, (len(rows) / total_origin * 100) if total_origin else 0)

    if not rows:
        logger.error("No STALL_EXIT closures found. Nothing to reconstruct.")
        return 1

    coverage = Coverage(total=len(rows))
    replays: list[Replay] = []

    for row in rows:
        label = f"{row['trade_id'][:8]} {row['index_symbol']} {row['tradingsymbol']}"
        series = _load_premium_series(row["tradingsymbol"], row["symboltoken"])
        if not series:
            coverage.no_archive.append(label)
            continue
        exit_ts = datetime.fromisoformat(str(row["exit_time"]).replace("Z", "+00:00"))
        # Stored UTC-aware; the archive is naive IST.
        exit_ist = exit_ts.replace(tzinfo=None) + (
            datetime(2000, 1, 1, 5, 30) - datetime(2000, 1, 1, 0, 0)
        ) if exit_ts.tzinfo else exit_ts.replace(tzinfo=None)
        after = [bar for bar in series if bar[0] > exit_ist and bar[0].date() == exit_ist.date()]
        if not after:
            coverage.no_bars_after.append(label)
            continue

        entry_price = float(row["entry_price"])
        reason, exit_premium = _replay_forward(
            after,
            entry_price=entry_price,
            stoploss=float(row["stoploss"]),
            target=float(row["target"]),
            trail_activate_percent=row["trail_activate_percent"] or _AI_ORIGIN_TRAIL_ACTIVATION_PERCENT,
            trail_width_percent=row["trail_width_percent"] or _AI_ORIGIN_TRAIL_OFFSET_PERCENT,
            high_water=float(row["highest_price"] or entry_price),
        )
        adx, cpr = _regime(row["market_context_json"])
        coverage.reconstructed += 1
        highest_price = float(row["highest_price"] or entry_price)
        replays.append(
            Replay(
                trade_id=row["trade_id"],
                provider=str(row["origin"]).replace("AI_ORIGIN_", ""),
                index_symbol=str(row["index_symbol"]),
                entry_day=str(row["entry_time"])[:10],
                actual_pnl_percent=(float(row["exit_price"]) - entry_price) / entry_price * 100,
                counterfactual_reason=reason,
                counterfactual_pnl_percent=(exit_premium - entry_price) / entry_price * 100,
                adx=adx,
                cpr=cpr,
                minutes_held_after=int((after[-1][0] - exit_ist).total_seconds() // 60),
                mfe_at_stall_percent=(highest_price - entry_price) / entry_price * 100,
            )
        )

    logger.info("  Reconstructed from real option candles: %s of %s", coverage.reconstructed, coverage.total)
    if coverage.no_archive:
        logger.warning(
            "  %s not reconstructible -- contract not in data/option_candles/. These are not "
            "zeros; they are unknowns, and the result below describes only the %s that could be "
            "reconstructed.", len(coverage.no_archive), coverage.reconstructed,
        )
        if args.verbose:
            for label in coverage.no_archive:
                logger.info("      %s", label)
    if coverage.no_bars_after:
        logger.warning("  %s had no archived bars after the exit", len(coverage.no_bars_after))

    if not replays:
        logger.error(
            "Nothing could be reconstructed. The option-candle archive does not cover any "
            "contract that stalled out. Archive the contracts AI Origination is currently "
            "trading (scripts/pull_option_candles.py) and re-run after they have stall events."
        )
        return 1

    logger.info("")
    logger.info("BASELINE -- what STALL_EXIT trades would have done if left open:")
    buckets: dict[str, list[Replay]] = {}
    for replay in replays:
        buckets.setdefault(replay.counterfactual_reason, []).append(replay)
    for reason in sorted(buckets):
        group = buckets[reason]
        logger.info(
            "  %-12s %2s/%s (%3.0f%%)  mean counterfactual %+6.2f%%  vs actual %+6.2f%%  "
            "-> mean delta %+6.2f%%",
            reason, len(group), len(replays), len(group) / len(replays) * 100,
            sum(r.counterfactual_pnl_percent for r in group) / len(group),
            sum(r.actual_pnl_percent for r in group) / len(group),
            sum(r.delta for r in group) / len(group),
        )

    net_delta = sum(r.delta for r in replays) / len(replays)
    better = sum(1 for r in replays if r.delta > 0)
    logger.info("")
    logger.info(
        "  NET: holding on would have been better in %s of %s cases, mean delta %+.2f%% per trade",
        better, len(replays), net_delta,
    )
    if net_delta > 0:
        logger.info(
            "  Positive: STALL_EXIT cost P&L on this sample. Note the replay resolves "
            "stop-and-target-in-one-bar as a LOSS, so this is the conservative reading."
        )
    else:
        logger.info(
            "  Negative or flat: STALL_EXIT protected P&L on this sample. Conditioning it away "
            "would make results worse, and 6 Aug was a non-representative session."
        )

    logger.info("")
    logger.info("=" * 84)
    logger.info("PEAK-MFE-CONDITIONED EXEMPTION SWEEP")
    logger.info(
        "  Exempt a STALL_EXIT trade once ITS OWN peak (mfe_at_stall, i.e. highest_price at the "
        "moment it actually stalled) clears the floor -- exempted trades take the real-premium "
        "counterfactual computed above, everything else keeps its real STALL_EXIT outcome. Full "
        "surface below; no floor is picked as a winner here."
    )
    for result in _sweep_peak_floors(replays, peak_floors):
        flag = "" if result.n_exempt >= MIN_SAMPLE_FOR_FLOOR_CELL else "  [THIN -- below trust minimum, read as anecdote]"
        logger.info(
            "  floor=%4.1f%%  exempt=%2d/%-2d  exempt-only: actual %+6.2f%% vs held %+6.2f%% (delta %+6.2f%%)  "
            "portfolio: %+6.2f%% -> %+6.2f%% (%+.2f%%)%s",
            result.floor, result.n_exempt, result.n_total,
            result.exempt_mean_actual, result.exempt_mean_counterfactual, result.exempt_mean_delta,
            result.portfolio_mean_baseline, result.portfolio_mean_if_adopted, result.portfolio_delta,
            flag,
        )
    logger.info(
        "  A floor is only worth taking seriously if its portfolio delta is positive AND its "
        "exempt-only n clears the trust minimum -- a positive delta from 2-3 trades is exactly "
        "the single-anecdote error this whole investigation started from."
    )

    if args.verbose:
        logger.info("")
        for replay in sorted(replays, key=lambda r: r.entry_day):
            logger.info(
                "    %s %-10s %-7s actual %+6.2f%% -> %-10s %+6.2f%% (delta %+6.2f%%) adx=%s cpr=%s",
                replay.entry_day, replay.index_symbol, replay.provider,
                replay.actual_pnl_percent, replay.counterfactual_reason,
                replay.counterfactual_pnl_percent, replay.delta,
                f"{replay.adx:.1f}" if replay.adx is not None else "-", replay.cpr or "-",
            )

    logger.info("")
    logger.info("=" * 84)
    if len(replays) < MIN_SAMPLE_FOR_SPLIT:
        logger.warning(
            "REGIME SPLIT NOT RUN: %s reconstructed trades is below the %s minimum. Splitting "
            "this by ADX and CPR would produce cells of two or three trades and a confident-"
            "looking table carrying no information. The baseline above is the whole result.",
            len(replays), MIN_SAMPLE_FOR_SPLIT,
        )
        return 0

    logger.info("BY REGIME AT ENTRY (proxy -- see _regime docstring, this is not trigger-time):")
    trending = [r for r in replays if r.adx is not None and r.adx >= 25 and r.cpr == "NARROW"]
    ranging = [r for r in replays if r.adx is not None and (r.adx < 20 or r.cpr == "WIDE")]
    for label, group in (("TRENDING (ADX>=25, narrow CPR)", trending), ("RANGING (ADX<20 or wide CPR)", ranging)):
        if not group:
            logger.info("  %-32s no trades", label)
            continue
        target_rate = sum(1 for r in group if r.counterfactual_reason == "TARGET") / len(group) * 100
        logger.info(
            "  %-32s n=%-3s would-hit-target %3.0f%%  mean delta %+6.2f%%",
            label, len(group), target_rate, sum(r.delta for r in group) / len(group),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
