"""One-off diagnostic: replay today's Validated Signal session(s) against the real
1-minute candle history already stored on this box, using the exact production
functions (compute_levels, evaluate_intraday_signal) -- so a "no trade today" report
can be checked against what the module actually computed (boundaries, volume, the
candidate at every bar-close), rather than inferred from its own on-failure-only
logging (see CLAUDE.md's "pull everything from a single source" entry -- this module
now deliberately logs nothing at all when a cycle evaluates cleanly and finds no
signal, same as before that pass).

Read-only: makes no new SmartAPI calls, opens no trades, writes nothing. Reads
whatever ONE_MINUTE candles are already in the DB (spot and the synthetic futures
key) and resamples them locally, exactly as app.validated_signal._load_index_features
does on a live cycle.

Usage: python -m scripts.diagnose_validated_signal_today [--db data/trading.db]
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.market_context import compute_levels
from app.market_data import FIVE_MINUTE, FUTURES_CANDLE_SUFFIX, ONE_MINUTE, load_bars, resample
from app.time_utils import IST, to_ist, utc_now
from app.validated_signal import _CANDLE_LOAD_LIMIT, _SUPPORTED_INDEXES, evaluate_intraday_signal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/trading.db")
    args = parser.parse_args()

    engine = create_engine(f"sqlite:///{args.db}")
    db = Session(engine)

    now_ist = to_ist(utc_now())
    now_naive = now_ist.replace(tzinfo=None)
    today = now_ist.date()

    for symbol in sorted(_SUPPORTED_INDEXES):
        print(f"\n=== {symbol} ===")

        bars_1m = load_bars(db, symbol, ONE_MINUTE, limit=_CANDLE_LOAD_LIMIT)
        bars_5m = resample(bars_1m, FIVE_MINUTE)
        if bars_5m and bars_5m[-1].ts_ist + timedelta(minutes=5) > now_naive:
            bars_5m = bars_5m[:-1]  # drop the still-forming trailing bucket, same as live
        levels = compute_levels(bars_5m, today)
        session_bars = [b for b in bars_5m if b.ts_ist.date() == today]

        print(f"  1-min bars stored: {len(bars_1m)} "
              f"(latest: {bars_1m[-1].ts_ist if bars_1m else None})")
        print(f"  5-min session bars today (completed only): {len(session_bars)}")
        print(f"  PDH={levels.previous_day_high} PDL={levels.previous_day_low}")

        futures_key = f"{symbol}{FUTURES_CANDLE_SUFFIX}"
        fut_1m = load_bars(db, futures_key, ONE_MINUTE, limit=_CANDLE_LOAD_LIMIT)
        fut_5m = resample(fut_1m, FIVE_MINUTE)
        vol_by_ts = {b.ts_ist: b.volume for b in fut_5m if b.ts_ist.date() == today and b.volume}
        volumes = [vol_by_ts.get(b.ts_ist, 0.0) for b in session_bars]

        print(f"  futures 1-min bars stored: {len(fut_1m)} (key={futures_key})")
        print(f"  today's 5-min bars: "
              f"{[(b.ts_ist.strftime('%H:%M'), b.open, b.high, b.low, b.close) for b in session_bars]}")
        print(f"  today's substituted volumes: {volumes}")

        if not session_bars:
            print("  No session bars for today -- nothing to evaluate.")
            continue

        fired = False
        for i in range(len(session_bars)):
            check_bars = session_bars[: i + 1]
            check_volumes = volumes[: i + 1]
            as_of = check_bars[-1].ts_ist.replace(tzinfo=IST)
            candidate = evaluate_intraday_signal(
                check_bars, check_volumes, levels.previous_day_high, levels.previous_day_low,
                as_of, symbol,
            )
            if candidate is not None:
                fired = True
                print(f"  {as_of.time()}: CANDIDATE -> {candidate}")
        if not fired:
            print(f"  No candidate at any of the {len(session_bars)} bar-closes evaluated today.")


if __name__ == "__main__":
    main()
