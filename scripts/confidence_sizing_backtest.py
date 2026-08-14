"""Does AI confidence, or hedging language in the reasoning, predict AI Origination outcomes?

THE TRIGGER (three trades this cycle, a repeat pattern -- not a single anecdote)
---------------------------------------------------------------------------------
- 12 Aug, Bank Nifty CE (confidence 0.66, "cautious... rather than a strong breakout")
  -- trend already ran full session, still inside opening range. Lost.
- 14 Aug, Nifty CE (confidence 0.55) -- 5-min breakout but 15-min Supertrend still
  down, extended from EMA21. Lost, STALL_EXIT at -0.42% after only 3.84% MFE.
- 14 Aug, Bank Nifty PE (confidence 0.77) -- "the move is already extended and the
  trend is mature," trend_duration_pct_of_session=100.0. Lost.

Contrast with 14 Aug's one clean winner (Bank Nifty PE, confidence 0.71, developing
ADX, no self-flagged conflict in the reasoning).

All three losses had the model naming a real conflict in its own reasoning and
trading at full size anyway. This is a repeat-anecdote pattern, which is stronger
evidence than the single-trade cases checked earlier this cycle (the
break-confirmation hypothesis, tested in break_confirmation_backtest.py and found
NOT SUPPORTED from similarly compelling anecdotal grounds) -- but three trades is
still not the ~2 months of ai_origination_logs/strategy_trades history that would
let this be tested properly. Same discipline applies: backtest before shipping,
and "insufficient evidence, keep watching" is an acceptable outcome.

TWO CHECKS, IN PRIORITY ORDER
------------------------------
1. Confidence-bucketed backtest (<0.6, 0.6-0.75, 0.75-0.85, >0.85): win rate, mean
   P&L, mean MFE, mean MAE per bucket, plus a bootstrap CI on the correlation
   between the raw confidence score and pnl_percent across all trades. This is the
   direct test of the hypothesis as stated ("confidence predicts outcome").
2. Reasoning-text hedging-language check, meant to be read alongside (1), not only
   as a fallback when (1) is thin: does ai_reasoning containing hedging language
   ("cautious," "moderate," "extended," "already run," "mature") correlate with
   worse outcomes, independent of the raw confidence number? The 14 Aug Bank Nifty
   PE loss (confidence 0.77, hedged reasoning) is exactly a case where the score and
   the text disagree -- the cross-tab at the end of this check reports how often
   that happens, since the roadmap explicitly asks which of the two actually
   predicts outcomes.

Both draw from the same population: every closed AI Origination trade
(origin LIKE 'AI_ORIGIN_%') with a recorded ai_confidence. Report sample sizes
plainly; below MIN_BUCKET_LIVE a bucket is flagged, not hidden.

Usage:
    python -m scripts.confidence_sizing_backtest --db data/trading.db
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("confidence_sizing_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # below this, report but flag as untrustworthy -- same threshold
                       # break_confirmation_backtest.py uses for the same live-history population

CONFIDENCE_BUCKETS = (
    (0.0, 0.6, "<0.60"),
    (0.6, 0.75, "0.60-0.75"),
    (0.75, 0.85, "0.75-0.85"),
    (0.85, 1.01, ">0.85"),
)

# Exactly the words named in the roadmap -- not an invented, broader list. Matched
# case-insensitively as substrings against the stored reasoning text.
HEDGE_KEYWORDS = ("cautious", "moderate", "extended", "already run", "mature")


@dataclass
class Entry:
    trade_id: str
    index_symbol: str
    confidence: float
    reasoning: str
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool


def _load_entries(db_path: str) -> list[Entry]:
    """MFE/MAE come from strategy_trade_ticks (real 30s premium samples), not
    from StrategyTrade.highest_price/lowest_price. Those two stored columns
    feed the trailing-stop engine and are only maintained on the side the
    trailing logic needs: for a long trade (every AI Origination trade is
    BUY_CE/BUY_PE, i.e. long) monitor_open_trades updates highest_price but
    never touches lowest_price -- it stays pinned at its entry-time seed
    value forever, making a lowest_price-derived MAE deterministically 0.00%
    for every single trade, not a real adverse excursion. Confirmed 14 Aug:
    the first version of this script used lowest_price directly and every
    bucket in both PART 1 and PART 2 reported mean_mae=+0.00% -- not close to
    zero, exactly zero, which is the signature of this exact bug rather than
    a coincidence of real trading outcomes. dashboard_routes.py's CSV export
    already solved this the same way (its own _excursion helper); mirrored
    here rather than reading the stored columns."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                trade_id, index_symbol, ai_confidence, ai_reasoning,
                entry_price, pnl_percent, result
            FROM strategy_trades
            WHERE origin LIKE 'AI_ORIGIN_%'
              AND status = 'CLOSED'
              AND ai_confidence IS NOT NULL
              AND pnl_percent IS NOT NULL
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
                # Every AI Origination trade is long (BUY_CE/BUY_PE), so
                # favourable == high and adverse == low -- unlike
                # dashboard_routes.py's _excursion this population never
                # needs the SELL-direction flip.
                mfe = (high - entry_price) / entry_price * 100.0
                mae = (low - entry_price) / entry_price * 100.0
        entries.append(Entry(
            trade_id=trade_id,
            index_symbol=str(row["index_symbol"]),
            confidence=float(row["ai_confidence"]),
            reasoning=str(row["ai_reasoning"] or ""),
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))
    return entries


def _is_hedged(reasoning: str) -> bool:
    lowered = reasoning.lower()
    return any(keyword in lowered for keyword in HEDGE_KEYWORDS)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = (var_x * var_y) ** 0.5
    return cov / denom if denom else 0.0


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260815)
    diffs = []
    for _ in range(rounds):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        diffs.append(sum(sample_a) / len(sample_a) - sum(sample_b) / len(sample_b))
    diffs.sort()
    lo = diffs[int(0.05 * rounds)]
    hi = diffs[int(0.95 * rounds) - 1]
    return lo, hi


def _bootstrap_correlation(xs: list[float], ys: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on Pearson r(xs, ys) via paired resampling (preserves the x/y pairing)."""
    pairs = list(zip(xs, ys))
    n = len(pairs)
    rng = random.Random(20260815)
    corrs = []
    for _ in range(rounds):
        sample = [rng.choice(pairs) for _ in range(n)]
        corrs.append(_pearson([p[0] for p in sample], [p[1] for p in sample]))
    corrs.sort()
    lo = corrs[int(0.05 * rounds)]
    hi = corrs[int(0.95 * rounds) - 1]
    return lo, hi


def _report_bucket(label: str, entries: list[Entry]) -> None:
    if not entries:
        logger.info("  %-28s n=0", label)
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
        "  %-28s n=%-4d win_rate=%5.1f%%  mean_pnl=%+6.2f%%  mean_mfe=%-8s mean_mae=%-8s%s",
        label, n, wins / n * 100.0, mean_pnl, mfe_txt, mae_txt, flag,
    )


def run_confidence_buckets(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("PART 1: OUTCOME BY AI CONFIDENCE BUCKET")
    logger.info("=" * 100)
    bucket_sizes = []
    for lo, hi, label in CONFIDENCE_BUCKETS:
        bucket = [e for e in entries if lo <= e.confidence < hi]
        bucket_sizes.append(len(bucket))
        _report_bucket(label, bucket)

    logger.info("-" * 100)
    if len(entries) >= 4:
        xs = [e.confidence for e in entries]
        ys = [e.pnl_percent for e in entries]
        r = _pearson(xs, ys)
        lo, hi = _bootstrap_correlation(xs, ys)
        verdict = (
            "reliably POSITIVE (higher confidence -> better outcome)" if lo > 0
            else "reliably NEGATIVE" if hi < 0
            else "no reliable relationship at this sample size"
        )
        logger.info(
            "Pearson r(confidence, pnl_percent) = %+.3f, bootstrap 90%% CI [%+.3f, %+.3f] -> %s",
            r, lo, hi, verdict,
        )
    else:
        logger.info("Too few observations (n=%d) for a correlation estimate.", len(entries))

    # The correlation above tests a smooth, continuous relationship across the
    # whole range. A floor is a different, discrete claim ("below X is
    # unusually bad", not "less confidence is gradually worse throughout") and
    # needs its own comparison -- the two can disagree, and did in practice on
    # 14 Aug: the correlation CI included zero while the lowest bucket still
    # stood out sharply against every other bucket on point estimates alone.
    floor = CONFIDENCE_BUCKETS[0][1]
    below_floor = [e for e in entries if e.confidence < floor]
    at_or_above_floor = [e for e in entries if e.confidence >= floor]
    if len(below_floor) >= 2 and len(at_or_above_floor) >= 2:
        lo, hi = _bootstrap_mean_diff(
            [e.pnl_percent for e in below_floor], [e.pnl_percent for e in at_or_above_floor],
        )
        verdict = (
            f"reliably WORSE below {floor:.2f}" if hi < 0
            else f"reliably BETTER below {floor:.2f}" if lo > 0
            else "no reliable difference at this sample size"
        )
        logger.info(
            "bootstrap 90%% CI on mean_pnl(<%.2f) - mean_pnl(>=%.2f): [%+.2f, %+.2f] -> %s  (n=%d vs n=%d)",
            floor, floor, lo, hi, verdict, len(below_floor), len(at_or_above_floor),
        )
    else:
        logger.info("Too few observations below/above the %.2f floor for a bootstrap comparison.", floor)

    if bucket_sizes and min(bucket_sizes) < MIN_BUCKET_LIVE:
        logger.info(
            "At least one confidence bucket is below the %s-observation trust minimum -- "
            "read PART 2 (reasoning-text hedging check) alongside this, not instead of it.",
            MIN_BUCKET_LIVE,
        )


def run_hedging_check(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("PART 2: OUTCOME BY REASONING-TEXT HEDGING LANGUAGE")
    logger.info("=" * 100)
    logger.info("Keywords: %s", ", ".join(HEDGE_KEYWORDS))

    hedged = [e for e in entries if _is_hedged(e.reasoning)]
    not_hedged = [e for e in entries if not _is_hedged(e.reasoning)]
    _report_bucket("hedged (caveat in reasoning)", hedged)
    _report_bucket("not hedged", not_hedged)

    logger.info("-" * 100)
    if len(hedged) >= 2 and len(not_hedged) >= 2:
        lo, hi = _bootstrap_mean_diff(
            [e.pnl_percent for e in hedged], [e.pnl_percent for e in not_hedged],
        )
        verdict = (
            "hedged reliably WORSE" if hi < 0
            else "hedged reliably BETTER" if lo > 0
            else "no reliable difference at this sample size"
        )
        logger.info(
            "bootstrap 90%% CI on mean_pnl(hedged) - mean_pnl(not hedged): [%+.2f, %+.2f] -> %s",
            lo, hi, verdict,
        )
    else:
        logger.info("Too few observations in one bucket for a bootstrap comparison.")

    # Does the confidence NUMBER agree with the reasoning TEXT? The 14 Aug Bank
    # Nifty PE loss (confidence 0.77, "already extended... mature") is exactly a
    # case where they disagree -- report how often that happens across the whole
    # population, since the roadmap explicitly asks which signal is more
    # diagnostic rather than assuming they always move together.
    logger.info("-" * 100)
    logger.info("Does the confidence NUMBER agree with the reasoning TEXT?")
    high_conf_hedged = [e for e in hedged if e.confidence >= 0.75]
    low_conf_not_hedged = [e for e in not_hedged if e.confidence < 0.6]
    logger.info(
        "  High confidence (>=0.75) but hedged reasoning: %d trade(s)%s",
        len(high_conf_hedged),
        "" if not high_conf_hedged else " -- " + ", ".join(
            f"{e.trade_id}(conf={e.confidence:.2f}, pnl={e.pnl_percent:+.2f}%)" for e in high_conf_hedged
        ),
    )
    logger.info(
        "  Low confidence (<0.60) but no hedging language: %d trade(s)%s",
        len(low_conf_not_hedged),
        "" if not low_conf_not_hedged else " -- " + ", ".join(
            f"{e.trade_id}(conf={e.confidence:.2f}, pnl={e.pnl_percent:+.2f}%)" for e in low_conf_not_hedged
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    args = parser.parse_args()

    entries = _load_entries(args.db)
    logger.info("Loaded %d closed AI Origination trade(s) with a recorded confidence score.", len(entries))
    if not entries:
        logger.error(
            "No closed AI Origination entries with ai_confidence found. Either data/trading.db "
            "has no AI Origination history yet, or this sandbox has no real data at all "
            "(expected here -- see CLAUDE.md). Run this on the machine with real trade history."
        )
        return 0

    run_confidence_buckets(entries)
    run_hedging_check(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
