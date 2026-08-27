"""Does the same-direction consecutive-loss gate ever block a real trade once
conditions have genuinely diverged from the losses that triggered it?

THE TRIGGER
------------
27 Aug 2026: the gate correctly blocked BUY_PE on both indices after 2
consecutive losses, per its own design (17 Aug). Hours later, both indices'
chop_efficiency_ratio had climbed from CHOPPY (<0.3) into CLEAN (>=0.5) and
confidence/sub-scores were reading high 70s%, genuinely different conditions
from the losing entries -- yet the gate kept blocking every BUY_PE decision
regardless, since it only counts a loss streak, blind to whether the setup
that produced it still looks anything like the current one. Asked directly:
is that ever costing a real trade, or does relaxing it just let the same
failure back in wearing a better-looking prompt?

METHODOLOGY, AND ITS REAL LIMITATION -- READ THIS BEFORE THE NUMBERS BELOW
------------------------------------------------------------------------------
A gate-blocked decision never opens a trade, so there is no real premium P&L
to read -- unlike every other gate backtest in this project (ADX, freshness,
hedge), which analyze real closed trades. This measures forward INDEX
direction only, from the live 1-minute Candle archive (app/db_models.py,
populated continuously by AI Origination's own candle refresh), as a proxy
for "would the thesis have been directionally right". It is explicitly NOT
premium P&L, and this project has repeatedly found index continuation is not
premium continuation (see CLAUDE.md's STALL_EXIT entry, 6 Aug). Read every
number here as evidence about the DIRECTIONAL thesis only, not a confirmed
trading outcome -- a real premium-reconstruction version (replaying the
option-candle archive the way stall_exit_backtest.py/stop_distance_
backtest.py already do for trades that DID open) is a larger follow-up, not
built here: a blocked decision never resolves a strike/contract, since
_open_trade's gates are checked before contract resolution, so there is no
real contract to look up archived premium for without independently
re-deriving strike selection -- meaningfully more machinery than this pass.

WHICH DECISIONS COUNT AS "BLOCKED BY THIS GATE"
--------------------------------------------------
A decision counts if: it chose BUY_CE/BUY_PE, its own confidence cleared the
0.60 floor (checked in run_origination_checks BEFORE _open_trade is ever
called -- a floor-fail is a different, unrelated block and must not be
folded in here), it never opened a trade (trade_id IS NULL), AND
reconstructing _same_direction_consecutive_losses's own logic (app/ai/
originator.py) as of that exact decision's timestamp -- not "now" -- shows
the streak was already at or above the configured threshold
(AISettings.ai_origination_max_same_direction_losses, default 2). A
BUY_CE/BUY_PE non-opener that doesn't reconstruct this way (a DTE floor with
no future expiry, a live order failure) is reported separately as
"unexplained", never silently folded in as gate-caused.

DIVERGENCE FROM THE TRIGGERING LOSSES
----------------------------------------
For each gate-blocked decision, compares its own chop_efficiency_ratio and
confidence against the mean of the SAME losing trades that produced the
streak blocking it, read from THEIR OWN ai_origination_logs row via
trade_id -- what the gate is protecting against, in the model's own terms
at the time it failed. "Diverged" if chop reads at least CHOP_DIVERGENCE_
FLOOR higher or confidence at least CONFIDENCE_DIVERGENCE_FLOOR higher than
that mean -- starting points, not validated, same status every new
threshold in this project gets before a backtest looks at it. Losses that
predate chop_efficiency_ratio's own existence (27 Aug) have no chop reading
to compare against by construction, not a gap in this script.

Usage:
    python -m scripts.loss_gate_override_backtest --db data/trading.db
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("loss_gate_override_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses
_IST_OFFSET = timedelta(hours=5, minutes=30)
_DEFAULT_MAX_SAME_DIRECTION_LOSSES = 2  # mirrors app/ai/originator.py's own constant
_MIN_CONFIDENCE_TO_ACT = 0.60  # mirrors app/ai/originator.py's own constant
HORIZON_MINUTES = 60
CHOP_DIVERGENCE_FLOOR = 0.15
CONFIDENCE_DIVERGENCE_FLOOR = 0.10


def db_timestamp_to_ist(raw: str) -> datetime:
    """Same conversion as scripts/stall_exit_backtest.py's own helper,
    duplicated rather than imported per this project's per-script
    convention -- see that function's own docstring for why the naive
    +5:30 shift is always correct against this app's real data (plain
    sqlite3 reads a DateTime(timezone=True) column back with no offset
    marker at all)."""
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    naive_utc = parsed.replace(tzinfo=None) if parsed.tzinfo is None else (parsed - parsed.utcoffset()).replace(tzinfo=None)
    return naive_utc + _IST_OFFSET


def _max_same_direction_losses(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT ai_origination_max_same_direction_losses FROM ai_settings LIMIT 1").fetchone()
    if row is None or row[0] is None:
        return _DEFAULT_MAX_SAME_DIRECTION_LOSSES
    return int(row[0])


def _reconstruct_loss_streak(
    connection: sqlite3.Connection, index_symbol: str, action: str, decision_raw_timestamp: str
) -> list[str]:
    """Mirrors app/ai/originator.py's _same_direction_consecutive_losses
    exactly, evaluated as of a specific past decision instead of live 'now'.
    Returns the trade_ids making up the streak, newest first. The explicit
    `entry_time < decision_raw_timestamp` guard has no equivalent in the
    live function -- it's never needed there, since it always runs in real
    time and no future trade can exist yet; reconstructing the past requires
    it explicitly to avoid a look-ahead bug."""
    decision_ist = db_timestamp_to_ist(decision_raw_timestamp)
    today = decision_ist.date()
    rows = connection.execute(
        """
        SELECT trade_id, entry_time, result FROM strategy_trades
        WHERE origin LIKE 'AI_ORIGIN_%' AND index_symbol = ? AND signal = ? AND status = 'CLOSED'
          AND entry_time < ?
        ORDER BY entry_time DESC
        """,
        (index_symbol, action, decision_raw_timestamp),
    ).fetchall()
    streak_ids: list[str] = []
    for trade_id, entry_time, result in rows:
        entry_ist = db_timestamp_to_ist(entry_time)
        if entry_ist.date() != today:
            break
        if result != "LOSS":
            break
        streak_ids.append(trade_id)
    return streak_ids


def _losing_streak_own_readings(connection: sqlite3.Connection, streak_ids: list[str]) -> tuple[float | None, float | None]:
    """Mean chop_efficiency_ratio/confidence the triggering losses were
    themselves logged with, read from THEIR OWN ai_origination_logs row --
    None for a value if none of the streak's trades have it (predates the
    field, or a genuine gap), not defaulted to a side."""
    if not streak_ids:
        return None, None
    placeholders = ",".join("?" for _ in streak_ids)
    rows = connection.execute(
        f"SELECT chop_efficiency_ratio, confidence FROM ai_origination_logs WHERE trade_id IN ({placeholders})",
        streak_ids,
    ).fetchall()
    chops = [r[0] for r in rows if r[0] is not None]
    confs = [r[1] for r in rows if r[1] is not None]
    return (
        sum(chops) / len(chops) if chops else None,
        sum(confs) / len(confs) if confs else None,
    )


def _forward_index_return(
    connection: sqlite3.Connection, index_symbol: str, decision_ist: datetime, horizon_minutes: int
) -> float | None:
    """Forward index % return from the nearest 1-minute candle at/after the
    decision to the nearest one at/after decision + horizon_minutes. None
    when either candle is missing (before this index's archive coverage
    starts, or a genuine gap) -- not fabricated as 0."""
    start = connection.execute(
        "SELECT ts_ist, close FROM candles WHERE index_symbol = ? AND interval = 'ONE_MINUTE' AND ts_ist >= ? "
        "ORDER BY ts_ist ASC LIMIT 1",
        (index_symbol, decision_ist.isoformat(sep=" ")),
    ).fetchone()
    if start is None:
        return None
    start_ts_raw, start_close = start
    start_ts = datetime.fromisoformat(start_ts_raw)
    target_ts = (start_ts + timedelta(minutes=horizon_minutes)).isoformat(sep=" ")
    end = connection.execute(
        "SELECT close FROM candles WHERE index_symbol = ? AND interval = 'ONE_MINUTE' AND ts_ist >= ? "
        "ORDER BY ts_ist ASC LIMIT 1",
        (index_symbol, target_ts),
    ).fetchone()
    if end is None or not start_close:
        return None
    return (end[0] - start_close) / start_close * 100.0


@dataclass
class BlockedDecision:
    index_symbol: str
    action: str
    decision_ist: datetime
    chop_efficiency_ratio: float | None
    confidence: float | None
    losses_chop: float | None
    losses_confidence: float | None
    forward_return: float | None  # favorable-signed: positive means the index moved the way the decision wanted

    @property
    def diverged(self) -> bool:
        chop_diverged = (
            self.chop_efficiency_ratio is not None and self.losses_chop is not None
            and self.chop_efficiency_ratio - self.losses_chop >= CHOP_DIVERGENCE_FLOOR
        )
        confidence_diverged = (
            self.confidence is not None and self.losses_confidence is not None
            and self.confidence - self.losses_confidence >= CONFIDENCE_DIVERGENCE_FLOOR
        )
        return chop_diverged or confidence_diverged


def _load_blocked_decisions(connection: sqlite3.Connection) -> tuple[list[BlockedDecision], int]:
    rows = connection.execute(
        """
        SELECT timestamp, index_name, decision, confidence, chop_efficiency_ratio
        FROM ai_origination_logs
        WHERE decision IN ('BUY_CE', 'BUY_PE') AND trade_id IS NULL AND confidence >= ?
        ORDER BY timestamp ASC
        """,
        (_MIN_CONFIDENCE_TO_ACT,),
    ).fetchall()
    threshold = _max_same_direction_losses(connection)

    entries: list[BlockedDecision] = []
    unexplained = 0
    for timestamp, index_name, action, confidence, chop in rows:
        streak_ids = _reconstruct_loss_streak(connection, index_name, action, timestamp)
        if len(streak_ids) < threshold:
            unexplained += 1
            continue
        losses_chop, losses_confidence = _losing_streak_own_readings(connection, streak_ids)
        decision_ist = db_timestamp_to_ist(timestamp)
        raw_return = _forward_index_return(connection, index_name, decision_ist, HORIZON_MINUTES)
        favorable_return = None
        if raw_return is not None:
            favorable_return = raw_return if action == "BUY_CE" else -raw_return
        entries.append(BlockedDecision(
            index_symbol=index_name, action=action, decision_ist=decision_ist,
            chop_efficiency_ratio=chop, confidence=confidence,
            losses_chop=losses_chop, losses_confidence=losses_confidence,
            forward_return=favorable_return,
        ))
    return entries, unexplained


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260827)
    diffs = []
    for _ in range(rounds):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        diffs.append(sum(sample_a) / len(sample_a) - sum(sample_b) / len(sample_b))
    diffs.sort()
    lo = diffs[int(0.05 * rounds)]
    hi = diffs[int(0.95 * rounds) - 1]
    return lo, hi


def _report_bucket(label: str, entries: list[BlockedDecision]) -> None:
    with_return = [e for e in entries if e.forward_return is not None]
    if not with_return:
        logger.info("  %-24s n=0 (no forward candle data)", label)
        return
    n = len(with_return)
    favorable = sum(1 for e in with_return if e.forward_return > 0)
    mean_return = sum(e.forward_return for e in with_return) / n
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-24s n=%-4d directionally favorable=%5.1f%%  mean forward return=%+6.2f%%%s",
        label, n, favorable / n * 100.0, mean_return, flag,
    )


def run_backtest(entries: list[BlockedDecision], unexplained: int) -> None:
    logger.info("=" * 100)
    logger.info(
        "LOSS-GATE OVERRIDE BACKTEST: %d decisions blocked by the same-direction consecutive-loss gate "
        "(index direction only, %d-min horizon -- see module docstring's METHODOLOGY section before "
        "trusting these as trading outcomes)",
        len(entries), HORIZON_MINUTES,
    )
    logger.info("=" * 100)
    if unexplained:
        logger.info(
            "%d additional BUY_CE/BUY_PE decisions never opened a trade but did NOT reconstruct as "
            "blocked by this gate (a DTE floor with no future expiry, a live order failure, or a "
            "genuine gap) -- excluded from every bucket below rather than assumed gate-caused.",
            unexplained,
        )
        logger.info("-" * 100)

    if not entries:
        logger.info("No decisions reconstructed as blocked by this gate in this window.")
        return

    diverged = [e for e in entries if e.diverged]
    similar = [e for e in entries if not e.diverged]
    logger.info("DIVERGED (chop and/or confidence read meaningfully better than the triggering losses):")
    _report_bucket("  diverged", diverged)
    logger.info("SIMILAR (no real difference from the losses that triggered the block):")
    _report_bucket("  similar", similar)

    diverged_returns = [e.forward_return for e in diverged if e.forward_return is not None]
    similar_returns = [e.forward_return for e in similar if e.forward_return is not None]
    if len(diverged_returns) >= 2 and len(similar_returns) >= 2:
        lo, hi = _bootstrap_mean_diff(diverged_returns, similar_returns)
        trust = (
            "" if min(len(diverged_returns), len(similar_returns)) >= MIN_BUCKET_LIVE
            else "  [below trust minimum -- read as suggestive, not confirmed]"
        )
        verdict = (
            "diverged reliably BETTER -- an override for clearly-changed conditions has real support"
            if lo > 0
            else "diverged reliably WORSE -- do not build an override on this evidence"
            if hi < 0
            else "no reliable difference at this sample size"
        )
        logger.info(
            "bootstrap 90%% CI on mean_return(diverged) - mean_return(similar): [%+.2f, %+.2f] -> %s%s",
            lo, hi, verdict, trust,
        )
    else:
        logger.info("Too few observations with forward candle data in one bucket for a bootstrap comparison.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        connection.execute("SELECT COUNT(*) FROM ai_origination_logs").fetchone()
    except sqlite3.OperationalError:
        logger.error(
            "No ai_origination_logs table found in %s. Either this sandbox has no real "
            "AI Origination history yet, or the wrong --db path was given.", args.db,
        )
        return 0
    finally:
        connection.close()

    connection = sqlite3.connect(args.db)
    try:
        entries, unexplained = _load_blocked_decisions(connection)
        run_backtest(entries, unexplained)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
