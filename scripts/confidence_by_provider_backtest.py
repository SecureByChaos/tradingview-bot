"""Does AI confidence predict AI Origination outcomes -- per provider?

THE TRIGGER
-----------
The shared 0.60 confidence floor (shipped 14 Aug 2026, see CLAUDE.md's "AI Origination
confidence floor raised 0.55 -> 0.60, backtested" entry) was validated against a POOLED
sample of both providers. Live decision logs since it shipped show it is not filtering
evenly:

    Claude: 393/394 decisions (99.7%) fall below 0.60, mean confidence 0.304
    OpenAI:   2/258 decisions (0.8%)  fall below 0.60, mean confidence 0.827

Claude's confidence is not stuck -- it ranges 0.10 to 0.75 across 9+ distinct values, so
it is genuinely discriminating between situations. But even Claude's observed MAXIMUM
(0.75) sits barely above OpenAI's AVERAGE (0.827): Claude's entire operating range is
compressed into what would be OpenAI's low-to-mid band. The two providers are not
reporting confidence on a comparable scale, so a single shared threshold is structurally
miscalibrated for whichever provider's scale sits lower -- here, Claude.

WHAT THIS SCRIPT ANSWERS
-------------------------
1. Does the original pooled 0.60-floor result still hold for OpenAI specifically, using
   the exact same bucket boundaries (<0.60, 0.60-0.75, 0.75-0.85, >0.85) the original
   backtest used? Expected: yes, since OpenAI's higher volume and higher scores likely
   drove most of the original pooled signal.
2. Does Claude show a real outcome gradient WITHIN ITS OWN observed range, using bins
   sized to that range (<0.20, 0.20-0.35, 0.35-0.50, >=0.50) rather than reusing bins
   built around OpenAI's distribution (meaningless for Claude -- almost nothing it
   produces exceeds 0.60)? If a gradient exists, this also sweeps candidate floor cuts
   (0.20, 0.35, 0.50) and reports which ones the data can support, same "most relaxed
   floor that still excludes the clearly-broken bucket" logic as the original 0.60 pick.
   If no gradient exists at any cut, that's a different, more serious finding: Claude's
   confidence field may not carry outcome-predictive information for this task at all,
   independent of scale -- a floor isn't the right lever for Claude in that case.

Same MIN_BUCKET_LIVE / bootstrap-CI machinery as confidence_sizing_backtest.py,
deliberately duplicated rather than imported -- this project's established per-script
convention (see that script's own docstring for the same reasoning).

Usage:
    python -m scripts.confidence_by_provider_backtest --db data/trading.db
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("confidence_by_provider_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this
                       # project uses (confidence_sizing_backtest.py, break_confirmation_
                       # backtest.py, same_direction_entries_backtest.py)

# Unchanged from the original pooled backtest -- used only for the OpenAI
# reconfirmation, so any drift from these exact bounds would make the two
# results non-comparable.
OPENAI_BUCKETS = (
    (0.0, 0.6, "<0.60"),
    (0.6, 0.75, "0.60-0.75"),
    (0.75, 0.85, "0.75-0.85"),
    (0.85, 1.01, ">0.85"),
)

# Sized to Claude's own observed 0.10-0.75 range, per the roadmap's suggested bins --
# NOT the OpenAI bins, which would put almost every Claude decision in one bucket.
CLAUDE_BUCKETS = (
    (0.0, 0.20, "<0.20"),
    (0.20, 0.35, "0.20-0.35"),
    (0.35, 0.50, "0.35-0.50"),
    (0.50, 1.01, ">=0.50"),
)

# Candidate floor cuts to sweep for Claude, since (unlike OpenAI's pre-chosen 0.60)
# there is no single number under test yet -- report the full surface, pick nothing
# unilaterally. Matches this project's established sweep pattern (e.g.
# stall_exit_backtest.py's peak-MFE exemption sweep).
CLAUDE_CANDIDATE_FLOORS = (0.20, 0.35, 0.50)

OPENAI_FLOOR_UNDER_TEST = 0.60


@dataclass
class Entry:
    trade_id: str
    provider: str
    index_symbol: str
    confidence: float
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool


def _provider_from_origin(origin: str) -> str | None:
    origin = (origin or "").upper()
    if origin == "AI_ORIGIN_OPENAI":
        return "openai"
    if origin == "AI_ORIGIN_CLAUDE":
        return "claude"
    return None


def _load_entries(db_path: str) -> list[Entry]:
    """MFE/MAE from strategy_trade_ticks, not StrategyTrade.highest_price/lowest_price --
    see confidence_sizing_backtest.py's _load_entries docstring for why lowest_price is
    deterministically wrong for this always-long population. Mirrored here rather than
    reading the stored columns."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                trade_id, origin, index_symbol, ai_confidence,
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
    skipped_unknown_provider = 0
    for row in rows:
        provider = _provider_from_origin(str(row["origin"]))
        if provider is None:
            # AI_ALT_* / AI_ORIGIN_* with an unrecognised suffix -- should not
            # happen given the origin LIKE filter above plus the two known
            # providers, but fail loud-in-aggregate rather than silently
            # miscounting if a third provider is ever added.
            skipped_unknown_provider += 1
            continue
        trade_id = str(row["trade_id"])
        entry_price = row["entry_price"]
        extremes = tick_extremes.get(trade_id)
        mfe = mae = None
        if extremes and entry_price:
            low, high = extremes
            if low is not None and high is not None:
                mfe = (high - entry_price) / entry_price * 100.0
                mae = (low - entry_price) / entry_price * 100.0
        entries.append(Entry(
            trade_id=trade_id,
            provider=provider,
            index_symbol=str(row["index_symbol"]),
            confidence=float(row["ai_confidence"]),
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))
    if skipped_unknown_provider:
        logger.warning(
            "Skipped %d row(s) with an origin matching 'AI_ORIGIN_%%' but not "
            "AI_ORIGIN_OPENAI/AI_ORIGIN_CLAUDE -- check for a new provider.",
            skipped_unknown_provider,
        )
    return entries


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
    rng = random.Random(20260819)
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
    rng = random.Random(20260819)
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


def _floor_bootstrap(entries: list[Entry], floor: float, label_prefix: str = "") -> None:
    below = [e for e in entries if e.confidence < floor]
    at_or_above = [e for e in entries if e.confidence >= floor]
    if len(below) < 2 or len(at_or_above) < 2:
        logger.info(
            "%sfloor=%.2f: too few observations below/above for a bootstrap comparison (n=%d vs n=%d).",
            label_prefix, floor, len(below), len(at_or_above),
        )
        return
    lo, hi = _bootstrap_mean_diff(
        [e.pnl_percent for e in below], [e.pnl_percent for e in at_or_above],
    )
    trust = "" if min(len(below), len(at_or_above)) >= MIN_BUCKET_LIVE else "  [below trust minimum on the thinner side]"
    verdict = (
        "reliably WORSE below floor" if hi < 0
        else "reliably BETTER below floor" if lo > 0
        else "no reliable difference at this sample size"
    )
    logger.info(
        "%sfloor=%.2f: bootstrap 90%% CI on mean_pnl(<floor) - mean_pnl(>=floor): [%+.2f, %+.2f] -> %s (n=%d vs n=%d)%s",
        label_prefix, floor, lo, hi, verdict, len(below), len(at_or_above), trust,
    )


def run_openai_reconfirmation(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("OPENAI: RECONFIRM THE ORIGINAL 0.60 FLOOR ON OPENAI-ONLY DATA")
    logger.info("=" * 100)
    openai_entries = [e for e in entries if e.provider == "openai"]
    logger.info("OpenAI closed trades with recorded confidence: n=%d", len(openai_entries))
    if not openai_entries:
        logger.info("No OpenAI entries -- nothing to reconfirm.")
        return

    for lo, hi, label in OPENAI_BUCKETS:
        bucket = [e for e in openai_entries if lo <= e.confidence < hi]
        _report_bucket(label, bucket)

    logger.info("-" * 100)
    if len(openai_entries) >= 4:
        xs = [e.confidence for e in openai_entries]
        ys = [e.pnl_percent for e in openai_entries]
        r = _pearson(xs, ys)
        lo, hi = _bootstrap_correlation(xs, ys)
        verdict = (
            "reliably POSITIVE" if lo > 0 else "reliably NEGATIVE" if hi < 0
            else "no reliable relationship at this sample size"
        )
        logger.info("Pearson r(confidence, pnl_percent) = %+.3f, 90%% CI [%+.3f, %+.3f] -> %s", r, lo, hi, verdict)
    _floor_bootstrap(openai_entries, OPENAI_FLOOR_UNDER_TEST, label_prefix="OpenAI ")


def run_claude_analysis(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("CLAUDE: OUTCOME GRADIENT WITHIN CLAUDE'S OWN OBSERVED RANGE")
    logger.info("=" * 100)
    claude_entries = [e for e in entries if e.provider == "claude"]
    logger.info("Claude closed trades with recorded confidence: n=%d", len(claude_entries))
    if not claude_entries:
        logger.info("No Claude entries at all -- cannot assess.")
        return
    if claude_entries:
        confs = sorted(e.confidence for e in claude_entries)
        logger.info(
            "Claude confidence range in this population: min=%.2f max=%.2f distinct_values=%d",
            confs[0], confs[-1], len(set(confs)),
        )
    if len(claude_entries) < MIN_BUCKET_LIVE:
        logger.warning(
            "Claude's ENTIRE population (n=%d) is below the %d-observation trust minimum. "
            "Post-floor, Claude opens almost no new trades (394 decisions -> ~1 trade), so "
            "this population is effectively fixed at its pre-floor size and will not grow "
            "meaningfully from further paper trading alone -- same structural constraint "
            "noted in CLAUDE.md's same_direction_entries backtest entry. Read every result "
            "below as a preliminary read, not a validated threshold.",
            len(claude_entries), MIN_BUCKET_LIVE,
        )

    bucket_sizes = []
    for lo, hi, label in CLAUDE_BUCKETS:
        bucket = [e for e in claude_entries if lo <= e.confidence < hi]
        bucket_sizes.append(len(bucket))
        _report_bucket(label, bucket)

    logger.info("-" * 100)
    if len(claude_entries) >= 4:
        xs = [e.confidence for e in claude_entries]
        ys = [e.pnl_percent for e in claude_entries]
        r = _pearson(xs, ys)
        lo, hi = _bootstrap_correlation(xs, ys)
        verdict = (
            "reliably POSITIVE (higher Claude confidence -> better outcome)" if lo > 0
            else "reliably NEGATIVE" if hi < 0
            else "no reliable relationship at this sample size"
        )
        logger.info("Pearson r(confidence, pnl_percent) = %+.3f, 90%% CI [%+.3f, %+.3f] -> %s", r, lo, hi, verdict)
    else:
        logger.info("Too few observations (n=%d) for a correlation estimate.", len(claude_entries))

    logger.info("-" * 100)
    logger.info("Candidate floor sweep (most relaxed floor that still excludes a clearly-broken bucket):")
    for floor in CLAUDE_CANDIDATE_FLOORS:
        _floor_bootstrap(claude_entries, floor, label_prefix="Claude ")

    logger.info("-" * 100)
    if all(size < MIN_BUCKET_LIVE for size in bucket_sizes):
        logger.info(
            "Every Claude bucket is below the %d-observation trust minimum. Per this project's "
            "own standard, that is an expected outcome for this population size, not a reason "
            "to force a verdict -- report 'insufficient evidence, keep watching' rather than "
            "picking a floor from it.",
            MIN_BUCKET_LIVE,
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

    run_openai_reconfirmation(entries)
    run_claude_analysis(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
