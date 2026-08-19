"""Does hedged AI Origination reasoning predict worse outcomes -- with a sharper detector?

THE TRIGGER
-----------
Five real trades on record where the model's own reasoning states a caution or a direct
contradiction and trades anyway -- the sentence shape is consistently [hedge/caution], "but"
[trades anyway]:

    "this is not an ideal fresh entry, but the bearish structure still outweighs the
    exhaustion risk" (19 Aug, confidence 0.78)
    "this is a moderate-confidence continuation rather than a high-conviction entry"
    (19 Aug, confidence 0.70)
    "this is a cautious momentum continuation rather than a strong breakout"
    (14 Aug, confidence 0.55)
    "the move is already extended and price is still inside the opening range" -- a
    stated contradiction, traded anyway (12 Aug)

THIS REVISITS ALREADY-TESTED GROUND -- SAID PLAINLY, NOT HIDDEN
-----------------------------------------------------------------
confidence_sizing_backtest.py's PART 2 already tested hedge language on 14 Aug with a flat
5-keyword match ("cautious," "moderate," "extended," "already run," "mature") across the
whole population and came back NOT reliable: hedged mean P&L -2.08% (n=77) vs not-hedged
-0.09% (n=108), bootstrap 90% CI [-4.68, +0.68], crosses zero. The point estimate matched
the intuition; the sample didn't clear the bar.

What's different and worth re-testing here is the DETECTOR, not the underlying question.
The old pass was a flat "does any of these five words appear anywhere" match -- diluted by
words like "moderate" showing up in unrelated contexts. This pass targets the specific
failure shape from the trigger examples: a stated risk/caution clause followed by a
contrastive conjunction ("but"/"however"/"although"/"despite") that the model then argues
past, plus direct hedge phrases and unresolved risk-acknowledgment phrases as two further
categories. A sharper detector on the same population could reveal a real effect the flat
keyword match diluted -- or could reproduce the same "not reliable" verdict on a cleaner
signal, which is itself a stronger, more trustworthy negative than the original.

Confidence was already shown not to predict outcome for either provider individually
(Pearson r ~ -0.04 for OpenAI). This tests something different: whether the QUALITATIVE
content of the reasoning carries information the numeric confidence score doesn't --
plausible given multiple trigger trades combined high confidence (0.70-0.92) with clearly
hedged language, i.e. the number and the words already disagree in specific cases.

DETECTOR DESIGN -- v1, explicitly a phrase pass, not a real classifier
------------------------------------------------------------------------
Three categories, matched case-insensitively as substrings (same pragmatic starting point
this project's other keyword-based checks use, e.g. HEDGE_KEYWORDS in
confidence_sizing_backtest.py):

  direct_hedge         -- "not ideal", "not a strong", "not a high-conviction",
                           "moderate rather than", "cautious rather than", "moderate-confidence"
  contradiction_marker -- "but", "however", "although", "despite"
  risk_acknowledgment   -- "the main caution is", "the main risk is", "already extended",
                           "already run", "no fresh breakout"

contradiction_marker is a SIMPLIFICATION of the requested "clause before states a risk,
clause after states a decision to proceed" -- true clause-role parsing needs real NLP,
which the request itself authorizes deferring ("start with a keyword/phrase pass since
it's cheap and can be validated quickly, then upgrade if needed"). This v1 flags the
conjunction's mere presence. That is deliberately coarser than the trigger examples'
actual shape and will overmatch some reasoning where "but" doesn't introduce a real
contradiction -- flagged here rather than quietly accepted, worth tightening if the
contradiction_marker category's own bucket (reported separately from the combined flag)
looks materially different from direct_hedge/risk_acknowledgment's.

A trade is reasoning_hedged=True if ANY category matches at least one phrase. Matched
phrases are retained per trade for auditability, per the request's own requirement that
hedge-triggered decisions stay inspectable.

Usage:
    python -m scripts.reasoning_hedge_backtest --db data/trading.db
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reasoning_hedge_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses

HEDGE_PHRASES: dict[str, tuple[str, ...]] = {
    "direct_hedge": (
        # "not an ideal" added alongside "not ideal" -- the trigger example itself
        # ("this is not an ideal fresh entry, but...") uses the "an" form, which
        # the request's own literal phrase list would have missed.
        "not ideal", "not an ideal", "not a strong", "not a high-conviction",
        "moderate rather than", "cautious rather than", "moderate-confidence",
    ),
    "contradiction_marker": ("but", "however", "although", "despite"),
    "risk_acknowledgment": (
        "the main caution is", "the main risk is", "already extended",
        "already run", "no fresh breakout",
    ),
}


@dataclass
class Entry:
    trade_id: str
    provider: str | None
    index_symbol: str
    reasoning: str
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool
    hedged: bool = field(init=False)
    matched: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.hedged, self.matched = classify_hedge(self.reasoning)


def classify_hedge(reasoning: str) -> tuple[bool, list[str]]:
    """(is_hedged, matched "category:phrase" list). See module docstring for the
    three categories and the documented simplification in contradiction_marker."""
    lowered = (reasoning or "").lower()
    matched: list[str] = []
    for category, phrases in HEDGE_PHRASES.items():
        for phrase in phrases:
            if phrase in lowered:
                matched.append(f"{category}:{phrase}")
    return (len(matched) > 0, matched)


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
            SELECT trade_id, origin, index_symbol, ai_reasoning, entry_price, pnl_percent, result
            FROM strategy_trades
            WHERE origin LIKE 'AI_ORIGIN_%'
              AND status = 'CLOSED'
              AND ai_reasoning IS NOT NULL
              AND ai_reasoning != ''
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
                mfe = (high - entry_price) / entry_price * 100.0
                mae = (low - entry_price) / entry_price * 100.0
        entries.append(Entry(
            trade_id=trade_id,
            provider=_provider_from_origin(str(row["origin"])),
            index_symbol=str(row["index_symbol"]),
            reasoning=str(row["ai_reasoning"] or ""),
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))
    return entries


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


def _report_bucket(label: str, entries: list[Entry]) -> None:
    if not entries:
        logger.info("  %-28s n=0", label)
        return
    n = len(entries)
    wins = sum(1 for e in entries if e.is_win)
    mean_pnl = sum(e.pnl_percent for e in entries) / n
    maes = [e.mae_percent for e in entries if e.mae_percent is not None]
    mae_txt = f"{sum(maes) / len(maes):+.2f}%" if maes else "n/a"
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-28s n=%-4d win_rate=%5.1f%%  mean_pnl=%+6.2f%%  mean_mae=%-8s%s",
        label, n, wins / n * 100.0, mean_pnl, mae_txt, flag,
    )


def _compare(label: str, entries: list[Entry]) -> None:
    hedged = [e for e in entries if e.hedged]
    not_hedged = [e for e in entries if not e.hedged]
    logger.info("--- %s (n=%d) ---", label, len(entries))
    _report_bucket("hedged", hedged)
    _report_bucket("not hedged", not_hedged)
    if len(hedged) >= 2 and len(not_hedged) >= 2:
        lo, hi = _bootstrap_mean_diff([e.pnl_percent for e in hedged], [e.pnl_percent for e in not_hedged])
        trust = "" if min(len(hedged), len(not_hedged)) >= MIN_BUCKET_LIVE else "  [below trust minimum on the thinner side]"
        verdict = (
            "hedged reliably WORSE" if hi < 0
            else "hedged reliably BETTER" if lo > 0
            else "no reliable difference at this sample size"
        )
        logger.info(
            "  bootstrap 90%% CI on mean_pnl(hedged) - mean_pnl(not hedged): [%+.2f, %+.2f] -> %s%s",
            lo, hi, verdict, trust,
        )
    else:
        logger.info("  Too few observations in one bucket for a bootstrap comparison.")


def run_backtest(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("REASONING HEDGE DETECTOR -- OUTCOME BACKTEST (%d category-matched entries)", len(entries))
    logger.info("=" * 100)

    _compare("ALL PROVIDERS", entries)
    logger.info("-" * 100)
    _compare("OPENAI ONLY", [e for e in entries if e.provider == "openai"])
    logger.info("-" * 100)
    _compare("CLAUDE ONLY", [e for e in entries if e.provider == "claude"])

    # Category-level breakdown -- so a positive/negative overall verdict can be
    # traced back to a specific category rather than treated as one monolithic
    # signal. Also the auditability the request asks for: which phrases are
    # actually driving the flag.
    logger.info("-" * 100)
    logger.info("MATCHED PHRASE FREQUENCY (across every entry the detector flagged):")
    counts: Counter[str] = Counter()
    for e in entries:
        counts.update(e.matched)
    for phrase, count in counts.most_common():
        logger.info("  %-45s %d", phrase, count)

    logger.info("-" * 100)
    logger.info("PER-CATEGORY OUTCOME (a trade counts in a category if ANY of its phrases matched,")
    logger.info("a trade can appear in more than one category):")
    for category in HEDGE_PHRASES:
        in_category = [e for e in entries if any(m.startswith(f"{category}:") for m in e.matched)]
        _report_bucket(category, in_category)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    args = parser.parse_args()

    entries = _load_entries(args.db)
    logger.info("Loaded %d closed AI Origination trade(s) with recorded reasoning.", len(entries))
    if not entries:
        logger.error(
            "No closed AI Origination entries with ai_reasoning found. Either data/trading.db "
            "has no AI Origination history yet, or this sandbox has no real data at all "
            "(expected here -- see CLAUDE.md). Run this on the machine with real trade history."
        )
        return 0

    run_backtest(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
