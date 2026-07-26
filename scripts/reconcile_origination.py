"""Reconcile the AI Origination trade population: 95 rows on the 7-day view
versus the 43 trades the analysis treated as valid.

The roadmap assumed the gap is 20 Jul's sub-1%-stop trades plus ~33 phantom
0.00% rows. This prints the exact split rather than taking that on trust,
because the "below breakeven net" conclusion rests on which rows were excluded
and how much P&L went with them.

Groups reported:
  PHANTOM        exit == entry, i.e. opened and closed at the same premium with
                 zero movement. These are almost certainly the sub-second
                 open/close pairs from before the 15:15 entry gate existed.
  PRE_VALIDATION trades whose configured stop was under 1% of entry -- these
                 predate the 5-50%% sanity band, so their exits are not
                 comparable to anything after it.
  VALID          everything else.

Usage:
    python -m scripts.reconcile_origination
    python -m scripts.reconcile_origination --start 2026-07-20 --end 2026-07-26
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.db_models import StrategyTrade, TradeStatus
from app.time_utils import to_ist
from app.trade_costs import estimate_round_trip_cost

PHANTOM = "PHANTOM (exit == entry)"
PRE_VALIDATION = "PRE_VALIDATION (stop < 1%)"
VALID = "VALID"


def _classify(trade: StrategyTrade) -> str:
    if trade.exit_price is not None and trade.entry_price and abs(trade.exit_price - trade.entry_price) < 1e-9:
        return PHANTOM
    if trade.entry_price:
        sl_percent = abs((trade.entry_price - trade.stoploss) / trade.entry_price) * 100
        if sl_percent < 1.0:
            return PRE_VALIDATION
    return VALID


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2026-07-20")
    parser.add_argument("--end", default="2026-07-26")
    args = parser.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    with SessionLocal() as session:
        trades = [
            trade
            for trade in session.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.origin.like("AI_ORIGIN_%"),
                    StrategyTrade.status == TradeStatus.CLOSED,
                )
            )
            if (entry := to_ist(trade.entry_time)) is not None and start <= entry.date() <= end
        ]

    if not trades:
        print(f"No closed AI Origination trades between {start} and {end}.")
        return 1

    groups: dict[str, list[StrategyTrade]] = defaultdict(list)
    by_day: dict[Any, list[StrategyTrade]] = defaultdict(list)
    for trade in trades:
        groups[_classify(trade)].append(trade)
        by_day[to_ist(trade.entry_time).date()].append(trade)

    def _summarise(label: str, rows: list[StrategyTrade]) -> None:
        if not rows:
            print(f"  {label:<28} 0 trades")
            return
        gross = sum(t.profit_loss or 0 for t in rows)
        cost = sum(
            t.estimated_cost or estimate_round_trip_cost(t.entry_price, t.exit_price, t.quantity).total
            for t in rows
        )
        wins = sum(1 for t in rows if (t.pnl_percent or 0) > 0)
        losses = sum(1 for t in rows if (t.pnl_percent or 0) < 0)
        flat = len(rows) - wins - losses
        print(
            f"  {label:<28} {len(rows):>3} trades | gross Rs {gross:>10,.2f} | "
            f"cost Rs {cost:>8,.2f} | net Rs {gross - cost:>10,.2f} | "
            f"{wins}W/{losses}L/{flat}flat"
        )

    print(f"\nAI Origination closed trades, {start} to {end}: {len(trades)} total\n")
    print("By classification:")
    for label in (PHANTOM, PRE_VALIDATION, VALID):
        _summarise(label, groups[label])

    print("\nBy day (all classifications):")
    for day in sorted(by_day):
        _summarise(day.isoformat(), by_day[day])

    print("\nValid set only, by day:")
    valid_by_day: dict[Any, list[StrategyTrade]] = defaultdict(list)
    for trade in groups[VALID]:
        valid_by_day[to_ist(trade.entry_time).date()].append(trade)
    for day in sorted(valid_by_day):
        _summarise(day.isoformat(), valid_by_day[day])

    # The single-outlier check: the roadmap noted Rs 2,578 of 20 Jul's ~Rs 3,000
    # came from one +25.32% trade. Worth seeing explicitly, because a conclusion
    # that flips on one trade is a conclusion about that trade.
    print("\nLargest 5 absolute P&L contributors (all classifications):")
    for trade in sorted(trades, key=lambda t: abs(t.profit_loss or 0), reverse=True)[:5]:
        print(
            f"  {to_ist(trade.entry_time).strftime('%d-%b %H:%M')} {trade.origin:<20} "
            f"{trade.index_symbol:<10} {trade.signal:<7} {trade.strike:<7} "
            f"{trade.pnl_percent:>7.2f}% Rs {trade.profit_loss:>10,.2f}  [{_classify(trade)}]"
        )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
