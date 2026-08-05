"""Did the AI's alternative beat the signal trade it rejected?

WHY THIS IS WORTH RUNNING AT n=17
---------------------------------
Every other analysis in this project has been blocked by sample size, and 17
trades would normally be nowhere near enough. This one is different, because
the comparison is PAIRED.

Each AI_ALT_* trade carries source_trade_id pointing at the exact SIGNAL trade
it was proposed against. Both are open on the same index, in the same regime,
within minutes of each other. So almost everything that makes unpaired options
P&L noisy -- which day it was, whether the market trended, which contract was
expensive -- is differenced away. What remains is the thing actually being
asked: given this setup, was the model's alternative better than the trade it
declined?

A paired sign test on 17 observations has real power. 13 of 17 in one direction
is p ~ 0.05 two-sided; 14 is p ~ 0.013. That is a genuinely reachable result at
this sample size, unlike everything the entry-signal work ran into.

WHAT IT DOES NOT ANSWER
-----------------------
Only rejected signals get an alternative, so this is conditional on rejection
throughout. It says nothing about whether the reviewer rejects the right
signals -- a reviewer that rejected at random could still propose good
alternatives, and one that rejected perfectly could propose bad ones. Those are
separate questions and this measures only the second.

Timing caveat: the alternative opens when the review completes, which is after
the signal trade's own entry. Same regime, but not the same instant, and on a
fast move that difference is not nothing.

Usage:
    python -m scripts.alternative_vs_signal --db data/trading.db
    python -m scripts.alternative_vs_signal --db data/trading.db --net
"""

from __future__ import annotations

import argparse
import logging
import math
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("alternative_vs_signal")

BOOTSTRAP_ROUNDS = 10000


@dataclass
class Pair:
    provider: str
    index_symbol: str
    strategy: str
    action: str
    confidence: float | None
    signal_pnl: float
    alt_pnl: float
    entry_day: str
    # The source SIGNAL trade. Both providers produce an alternative for the
    # SAME rejected signal, so pairs are CLUSTERED on this -- two rows sharing
    # a source_id are not two independent observations, and a sign test that
    # treats them as such is anti-conservative.
    source_id: str
    # Configured stop distance on each arm, as a percent of entry. Recorded
    # because the arms do not use the same risk construction: alternatives
    # default to 10% SL / 20% target while the signal trade runs its
    # strategy's own. If those differ, the comparison confounds "was the
    # alternative a better trade" with "was the stop tighter", and the second
    # explains a lot of what the first appears to show.
    signal_sl_percent: float | None
    alt_sl_percent: float | None

    @property
    def difference(self) -> float:
        return self.alt_pnl - self.signal_pnl


def _pnl_percent(pnl: float | None, entry: float | None, quantity: int | None) -> float | None:
    """Percent of premium at risk, not rupees.

    Position sizing is one lot, so rupee P&L mostly reflects which contract
    happened to be expensive -- which is exactly the nuisance the pairing is
    meant to remove, so it must not be reintroduced by the unit choice.
    """
    if pnl is None or not entry or not quantity:
        return None
    return pnl / (entry * quantity) * 100.0


def _load(db_path: str, use_net: bool) -> list[Pair]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                a.origin          AS alt_origin,
                a.ai_action       AS action,
                a.ai_confidence   AS confidence,
                a.index_symbol    AS index_symbol,
                a.strategy_name   AS strategy,
                a.entry_time      AS alt_entry_time,
                a.profit_loss     AS alt_gross,
                a.net_pnl         AS alt_net,
                a.entry_price     AS alt_entry,
                a.quantity        AS alt_qty,
                a.stoploss        AS alt_sl,
                a.source_trade_id AS source_id,
                s.profit_loss     AS sig_gross,
                s.net_pnl         AS sig_net,
                s.entry_price     AS sig_entry,
                s.quantity        AS sig_qty,
                s.stoploss        AS sig_sl
            FROM strategy_trades a
            JOIN strategy_trades s ON s.trade_id = a.source_trade_id
            WHERE a.origin LIKE 'AI_ALT_%'
              AND a.exit_price IS NOT NULL
              AND s.exit_price IS NOT NULL
            """
        ).fetchall()
    finally:
        connection.close()

    pairs: list[Pair] = []
    for row in rows:
        alt = _pnl_percent(
            row["alt_net"] if (use_net and row["alt_net"] is not None) else row["alt_gross"],
            row["alt_entry"], row["alt_qty"],
        )
        sig = _pnl_percent(
            row["sig_net"] if (use_net and row["sig_net"] is not None) else row["sig_gross"],
            row["sig_entry"], row["sig_qty"],
        )
        if alt is None or sig is None:
            continue

        def _sl_percent(stoploss, entry) -> float | None:
            if not stoploss or not entry:
                return None
            return (entry - stoploss) / entry * 100.0

        pairs.append(
            Pair(
                provider=str(row["alt_origin"]).replace("AI_ALT_", ""),
                index_symbol=str(row["index_symbol"]),
                strategy=str(row["strategy"]),
                action=str(row["action"] or ""),
                confidence=row["confidence"],
                signal_pnl=sig,
                alt_pnl=alt,
                entry_day=str(row["alt_entry_time"])[:10],
                source_id=str(row["source_id"]),
                signal_sl_percent=_sl_percent(row["sig_sl"], row["sig_entry"]),
                alt_sl_percent=_sl_percent(row["alt_sl"], row["alt_entry"]),
            )
        )
    return pairs


def _sign_test(wins: int, total: int) -> float:
    """Exact two-sided binomial p-value against a fair coin.

    Exact rather than normal-approximated: at n=17 the approximation is poor
    precisely where the answer matters. stdlib math.comb, so this stays out of
    numpy's way and runs anywhere.
    """
    if total == 0:
        return 1.0
    def _tail(k: int) -> float:
        return sum(math.comb(total, i) for i in range(k, total + 1)) / (2 ** total)
    extreme = max(wins, total - wins)
    return min(1.0, 2 * _tail(extreme))


def _bootstrap(differences: list[float], rounds: int) -> tuple[float, float]:
    rng = random.Random(20260805)
    means = []
    for _ in range(rounds):
        sample = [rng.choice(differences) for _ in differences]
        means.append(sum(sample) / len(sample))
    means.sort()
    return means[int(0.025 * rounds)], means[int(0.975 * rounds)]


def _report(label: str, pairs: list[Pair]) -> None:
    if not pairs:
        logger.info("  %-22s no closed pairs", label)
        return
    differences = [p.difference for p in pairs]
    wins = sum(1 for d in differences if d > 0)
    ties = sum(1 for d in differences if d == 0)
    mean_difference = sum(differences) / len(differences)
    logger.info(
        "  %-22s n=%-3s  alt better %s/%s  mean diff %+.2f%%  (alt %+.2f%% vs signal %+.2f%%)",
        label, len(pairs), wins, len(pairs), mean_difference,
        sum(p.alt_pnl for p in pairs) / len(pairs),
        sum(p.signal_pnl for p in pairs) / len(pairs),
    )
    if ties:
        logger.info("  %-22s (%s exact ties, counted as losses by the sign test)", "", ties)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--net", action="store_true", help="Use net_pnl (after costs) instead of gross")
    parser.add_argument("--show-pairs", action="store_true", help="Print every pair")
    args = parser.parse_args()

    pairs = _load(args.db, args.net)
    basis = "NET (after costs)" if args.net else "GROSS"

    logger.info("=" * 84)
    logger.info("AI Alternative vs the SIGNAL trade it rejected -- PAIRED, %s", basis)
    logger.info("=" * 84)

    if not pairs:
        logger.error(
            "No closed AI_ALT trade with a closed source SIGNAL trade. Alternatives exist only "
            "where source_trade_id resolves and BOTH sides have exited -- open trades on either "
            "side are excluded, since a pair is only comparable once both are done."
        )
        return 1

    if args.show_pairs:
        logger.info("%-10s %-10s %-6s %-6s %8s %8s %8s", "provider", "index", "action", "conf", "signal", "alt", "diff")
        for pair in sorted(pairs, key=lambda p: p.entry_day):
            logger.info(
                "%-10s %-10s %-6s %-6s %+7.2f%% %+7.2f%% %+7.2f%%",
                pair.provider, pair.index_symbol, pair.action,
                f"{pair.confidence:.2f}" if pair.confidence is not None else "-",
                pair.signal_pnl, pair.alt_pnl, pair.difference,
            )
        logger.info("")

    _report("ALL", pairs)
    for provider in sorted({p.provider for p in pairs}):
        _report(f"  {provider}", [p for p in pairs if p.provider == provider])
    for action in sorted({p.action for p in pairs if p.action}):
        _report(f"  action={action}", [p for p in pairs if p.action == action])

    # CLUSTER on the source signal. Both providers review the same rejected
    # signal, so two rows can share a source_trade_id -- and they share its
    # entire market context, not just its identity. Treating them as two
    # independent observations overstates n and makes the sign test
    # anti-conservative. Collapsed by averaging the alternatives' differences
    # against their common source, so each rejected signal counts once.
    by_source: dict[str, list[float]] = {}
    for pair in pairs:
        by_source.setdefault(pair.source_id, []).append(pair.difference)
    clustered = [sum(values) / len(values) for values in by_source.values()]

    differences = [p.difference for p in pairs]
    wins = sum(1 for d in differences if d > 0)
    p_value = _sign_test(wins, len(differences))
    c_wins = sum(1 for d in clustered if d > 0)
    c_p = _sign_test(c_wins, len(clustered))
    low, high = _bootstrap(clustered, BOOTSTRAP_ROUNDS)

    logger.info("")
    if len(clustered) < len(pairs):
        logger.info(
            "  %s pairs come from %s distinct rejected signals (%s reviewed by both providers).",
            len(pairs), len(clustered), len(pairs) - len(clustered),
        )
    logger.info("  Naive per-pair sign test:  alt better %s/%s -> p = %.4f  [OVERSTATES n if clustered]",
                wins, len(differences), p_value)
    logger.info("  CLUSTERED sign test:       alt better %s/%s -> p = %.4f  <-- the one to read",
                c_wins, len(clustered), c_p)
    logger.info("  Mean paired difference 95%% CI (clustered): [%+.2f%%, %+.2f%%]", low, high)

    # THE CONFOUND. Report it before any verdict, because it can generate the
    # entire apparent effect on its own.
    sig_sls = [p.signal_sl_percent for p in pairs if p.signal_sl_percent is not None]
    alt_sls = [p.alt_sl_percent for p in pairs if p.alt_sl_percent is not None]
    if sig_sls and alt_sls:
        sig_mean = sum(sig_sls) / len(sig_sls)
        alt_mean = sum(alt_sls) / len(alt_sls)
        logger.info("")
        logger.info("  Configured stop distance: signal %.1f%% vs alternative %.1f%%", sig_mean, alt_mean)
        if abs(sig_mean - alt_mean) > 1.0:
            logger.warning(
                "  THE TWO ARMS DO NOT USE THE SAME RISK CONSTRUCTION. Alternatives default to "
                "10%% SL / 20%% target (alternative_trader.py); the signal trade runs its "
                "strategy's own. A tighter stop caps losses AND clips winners, which produces "
                "exactly the pattern of 'better on losing signals, worse on winning ones' -- "
                "with no difference in judgment required."
            )
            logger.warning(
                "  So this does NOT cleanly measure whether the model picks better trades. What "
                "it DOES measure, on matched setups in matched regimes, is one risk construction "
                "against another -- which is the open question the walk-forward analysis "
                "landed on. Read it as an exit experiment, not a selection experiment."
            )
        by_outcome = [
            ("signal LOST", [p for p in pairs if p.signal_pnl < 0]),
            ("signal WON", [p for p in pairs if p.signal_pnl >= 0]),
        ]
        for label, subset in by_outcome:
            if subset:
                sub_wins = sum(1 for p in subset if p.difference > 0)
                logger.info(
                    "    %-12s n=%-3s alt better %s/%s  mean diff %+.2f%%",
                    label, len(subset), sub_wins, len(subset),
                    sum(p.difference for p in subset) / len(subset),
                )
    p_value = c_p

    logger.info("")
    if p_value < 0.05 and wins > len(differences) / 2:
        logger.info(
            "  The alternatives beat the trades they replaced more often than chance. Note what "
            "this does and does not license: it is evidence the model proposes better trades "
            "GIVEN it has decided to reject, not that it rejects the right signals."
        )
    elif p_value < 0.05:
        logger.info(
            "  The alternatives did WORSE than the trades they replaced, beyond chance. Taking "
            "the rejected signal would have been better -- which is a usable finding, just not "
            "the hoped-for one."
        )
    else:
        # The honest framing at n=17: what would it have taken.
        needed = next(
            (w for w in range(len(clustered), 0, -1) if _sign_test(w, len(clustered)) >= 0.05),
            len(clustered),
        )
        logger.info(
            "  Not distinguishable from chance. At n=%s distinct signals the sign test needs %s+ "
            "wins to clear p<0.05; this has %s. That is a sample-size statement, not a verdict -- "
            "the pairing means each additional signal is worth more here than in any unpaired "
            "comparison.",
            len(clustered), needed + 1, c_wins,
        )

    days = {p.entry_day for p in pairs}
    if len(days) < 5:
        logger.warning(
            "  CAVEAT: these pairs span only %s day(s). Signals cluster, so several pairs can "
            "share one regime and the effective sample is smaller than n suggests.", len(days),
        )
    logger.info(
        "  Timing caveat: the alternative opens when the review completes, after the signal "
        "trade's own entry. Same regime, not the same instant."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
