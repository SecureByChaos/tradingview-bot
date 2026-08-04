"""Do AI Origination trades opened on stale candle data do worse?

WHAT PROMPTED THIS
------------------
On 4 Aug both Nifty losses (-18.14%, -9.91%) carried Data Stale: YES and all
five wins carried NO. That is 7 trades. It is exactly the size of sample that
produces a confident wrong conclusion in either direction, so this script is
built to make the sample size impossible to ignore rather than to find an
effect.

WHY IT WOULD MATTER IF REAL
---------------------------
`_load_market_context` deliberately does NOT fail closed on a failed candle
refresh -- it falls back to stored history and sets data_stale=True. That was a
considered choice ("stored history is often still good enough"), and the flag
exists so the choice could be checked later rather than assumed. This is the
check. If stale-data trades are materially worse, the argument for fail-closed
on that path becomes an evidence-backed one instead of a preference.

Note the asymmetry in what a null result means. Finding no effect does NOT
vindicate the fallback -- it may only mean the sample is too small to see one.
Finding an effect on a handful of trades does not establish one either. The
honest output of a small sample is a wide interval, which is what this prints.

METHOD
------
Difference in mean P&L between the two groups, with a bootstrap interval over
TRADES. Not over days: unlike the entry-signal work, staleness is a per-cycle
broker condition rather than a property of the session, so trades on the same
day are not automatically one observation. That said, a rate-limit episode
lasts minutes to hours and can make several trades stale together -- so the
script also reports how many distinct days the stale trades come from, because
if the answer is "one", the effective sample is one incident and no interval
computed here means much.

Usage:
    python -m scripts.stale_data_correlation --db data/trading.db
    python -m scripts.stale_data_correlation --db data/trading.db --net
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("stale_data_correlation")

BOOTSTRAP_ROUNDS = 10000
# Below this, report the split and refuse to compute an interval. A bootstrap
# over 2 observations resamples the same 2 values and produces an interval that
# looks like a measurement but carries no information.
MIN_GROUP_FOR_INTERVAL = 8


@dataclass
class Group:
    label: str
    pnl_percent: list[float]
    days: set
    mfe: list[float]
    mae: list[float]

    @property
    def n(self) -> int:
        return len(self.pnl_percent)

    @property
    def wins(self) -> int:
        return sum(1 for value in self.pnl_percent if value > 0)

    @property
    def win_rate(self) -> float | None:
        return (self.wins / self.n * 100) if self.n else None

    @property
    def mean(self) -> float | None:
        return (sum(self.pnl_percent) / self.n) if self.n else None

    def mean_of(self, values: list[float]) -> float | None:
        return (sum(values) / len(values)) if values else None


def _load(db_path: str, use_net: bool) -> tuple[Group, Group]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        # MFE/MAE are NOT stored columns. highest_price/lowest_price exist but
        # only the side the trailing engine needs is maintained -- on a long
        # trade lowest_price stays at its entry-time seed -- so the stored low
        # is not a real adverse excursion. The tick table has every 30-second
        # sample in both directions. Same derivation the CSV export uses.
        rows = connection.execute(
            """
            SELECT t.data_stale, t.entry_price, t.exit_price, t.profit_loss, t.net_pnl,
                   t.quantity, t.entry_time, t.signal,
                   MIN(k.premium) AS premium_low,
                   MAX(k.premium) AS premium_high
            FROM strategy_trades t
            LEFT JOIN strategy_trade_ticks k ON k.trade_id = t.trade_id
            WHERE t.origin LIKE 'AI_ORIGIN_%'
              AND t.exit_price IS NOT NULL
              AND t.data_stale IS NOT NULL
            GROUP BY t.trade_id
            """
        ).fetchall()
    finally:
        connection.close()

    stale = Group("STALE (refresh failed)", [], set(), [], [])
    fresh = Group("FRESH", [], set(), [], [])
    for row in rows:
        entry = row["entry_price"]
        if not entry:
            continue
        # Percent of premium, not rupees: position size is always one lot, so
        # rupee P&L is dominated by which contract happened to be expensive.
        pnl = row["net_pnl"] if (use_net and row["net_pnl"] is not None) else row["profit_loss"]
        if pnl is None or not row["quantity"]:
            continue
        pnl_percent = pnl / (entry * row["quantity"]) * 100.0
        target = stale if row["data_stale"] else fresh
        target.pnl_percent.append(pnl_percent)
        if row["entry_time"]:
            target.days.add(str(row["entry_time"])[:10])

        low, high = row["premium_low"], row["premium_high"]
        if low is not None and high is not None:
            # Signed so favourable is always positive and adverse always
            # negative, whichever side the trade is on.
            direction = -1 if str(row["signal"] or "").startswith("SELL") else 1
            best = high if direction == 1 else low
            worst = low if direction == 1 else high
            target.mfe.append((best - entry) / entry * 100 * direction)
            target.mae.append((worst - entry) / entry * 100 * direction)
    return stale, fresh


def _bootstrap_difference(stale: list[float], fresh: list[float], rounds: int) -> tuple[float, float]:
    """95% interval on (mean stale - mean fresh), resampling each group."""
    rng = random.Random(20260804)
    diffs = []
    for _ in range(rounds):
        a = [rng.choice(stale) for _ in stale]
        b = [rng.choice(fresh) for _ in fresh]
        diffs.append(sum(a) / len(a) - sum(b) / len(b))
    diffs.sort()
    return diffs[int(0.025 * rounds)], diffs[int(0.975 * rounds)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--net", action="store_true", help="Use net_pnl (after costs) instead of gross profit_loss")
    args = parser.parse_args()

    stale, fresh = _load(args.db, args.net)
    basis = "NET (after costs)" if args.net else "GROSS"

    logger.info("=" * 78)
    logger.info("AI Origination: stale-data vs fresh-data outcomes, %s", basis)
    logger.info("=" * 78)

    if not stale.n and not fresh.n:
        logger.error(
            "No closed AI Origination trades with a data_stale value. The column is "
            "forward-only -- trades predating it are NULL and cannot be classified."
        )
        return 1

    for group in (stale, fresh):
        logger.info(
            "  %-24s n=%-4s win rate=%-6s mean P&L=%-8s over %s distinct day(s)",
            group.label, group.n,
            f"{group.win_rate:.0f}%" if group.win_rate is not None else "-",
            f"{group.mean:+.2f}%" if group.mean is not None else "-",
            len(group.days),
        )
        mfe, mae = group.mean_of(group.mfe), group.mean_of(group.mae)
        if mfe is not None or mae is not None:
            logger.info(
                "      mean MFE=%s  mean MAE=%s",
                f"{mfe:+.2f}%" if mfe is not None else "-",
                f"{mae:+.2f}%" if mae is not None else "-",
            )

    logger.info("")
    if stale.n < MIN_GROUP_FOR_INTERVAL or fresh.n < MIN_GROUP_FOR_INTERVAL:
        logger.warning(
            "  SAMPLE TOO SMALL TO CONCLUDE ANYTHING. Need at least %s trades in each "
            "group; have %s stale and %s fresh. The difference in means is printed above "
            "and should be read as an observation, not a result -- no interval is computed "
            "because a bootstrap over this many points would resample the same handful of "
            "values and produce a confident-looking interval carrying no information.",
            MIN_GROUP_FOR_INTERVAL, stale.n, fresh.n,
        )
        logger.info(
            "  Revisit once the stale group reaches %s+ trades across several separate "
            "incidents. Until then the fail-closed question stays open on its own merits, "
            "not on this evidence.", MIN_GROUP_FOR_INTERVAL,
        )
        return 0

    low, high = _bootstrap_difference(stale.pnl_percent, fresh.pnl_percent, BOOTSTRAP_ROUNDS)
    difference = (stale.mean or 0) - (fresh.mean or 0)
    logger.info(
        "  Difference (stale - fresh): %+.2f%%  95%% CI [%+.2f%%, %+.2f%%]",
        difference, low, high,
    )
    if high < 0:
        logger.info(
            "  Interval excludes zero and is negative: stale-data trades are worse. That is "
            "an evidence-backed argument for failing closed on a failed candle refresh in "
            "_load_market_context rather than falling back to stored history."
        )
    elif low > 0:
        logger.info(
            "  Interval excludes zero and is POSITIVE -- stale trades did better. Treat with "
            "suspicion rather than acting on it: there is no mechanism by which older data "
            "should improve a decision, so this more likely reflects what the market was "
            "doing during the rate-limit episodes than anything about the data."
        )
    else:
        logger.info(
            "  Interval spans zero: no detectable difference at this sample size. This does "
            "NOT vindicate the stored-history fallback -- an effect could exist and be "
            "invisible here. It only means today's pattern is not yet evidence."
        )

    if len(stale.days) <= 2:
        logger.warning(
            "  CAVEAT: the stale trades span only %s day(s). A rate-limit episode makes "
            "several trades stale at once, so the effective sample is closer to %s incident(s) "
            "than %s independent observations, and the interval above is correspondingly "
            "too narrow.", len(stale.days), len(stale.days), stale.n,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
