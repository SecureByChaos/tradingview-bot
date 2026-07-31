"""Fit the index-to-premium coefficient from the option archive.

This is Part 1 of the backtest spec, and it is the step that decides whether
Question 2 of the baseline means anything. Every premium-space number scales
linearly with these coefficients.

UNITS. The fitted coefficient (lambda) is premium PERCENT move per index
PERCENT move. It is NOT delta. Delta is premium RUPEES per index POINT, and the
two differ by spot/premium -- roughly 216x for Nifty, 140x for Bank Nifty.
Conflating them is a ~200x error that still produces plausible-looking output,
so the smoke check below exists to catch exactly that.

Usage:
    python -m scripts.calibrate_premium --db data/trading.db
    python -m scripts.calibrate_premium --db data/trading.db --candles data/option_candles
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from scripts.backtest.premium import (
    SESSION_MINUTES,
    fit_premium_model,
    select_multiplier,
    theoretical_theta_per_minute,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("calibrate_premium")

# First-principles expectation, NOT a single observed fill.
#
#     lambda = delta * spot / premium
#
# The premium term is why a single trade is a bad reference: elasticity scales
# inversely with premium, so a cheap 0-DTE contract and a 6-DTE contract on the
# same index have very different lambdas. An earlier version hardcoded 105 from
# one 29-July fill (Rs 115.35 -> Rs 137.20, +18.94% on 0.181% index) and then
# flagged a perfectly good 6-10 DTE fit as 44% off, because that fill was a
# cheaper contract than the archive holds.
#
# Reference premiums below are ATM levels observed in the 20-24 July archive
# period, so the expectation matches the data actually being fitted.
SMOKE_REFERENCE = {
    # index: (spot, atm_premium, dte_bucket)
    "NIFTY": (23950.0, 160.0, "2-5"),
    "BANKNIFTY": (56900.0, 420.0, "2-5"),
}
ASSUMED_ATM_DELTA = 0.5
# Tightened from 60%: with a correct reference, a good fit lands inside ~15%.
# Anything past 25% is worth investigating rather than waving through.
SMOKE_TOLERANCE = 0.25


def _expected_lambda(spot: float, premium: float) -> float:
    return ASSUMED_ATM_DELTA * spot / premium * 1.0


def _load_index_series(db_path: str, table: str) -> dict[str, dict[datetime, float]]:
    """index_symbol -> {timestamp -> close} at 1-minute resolution.

    Must be 1-minute: the option archive is 1-minute, and matching a 1-minute
    premium change against a 5-minute index change would understate the index
    move and inflate the fitted coefficient.
    """
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            f"SELECT index_symbol, ts_ist, close FROM {table} WHERE interval = 'ONE_MINUTE'"
        ).fetchall()
    finally:
        connection.close()

    series: dict[str, dict[datetime, float]] = {}
    for symbol, ts_raw, close in rows:
        ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
        series.setdefault(str(symbol).upper(), {})[ts] = float(close)
    return series


def _load_strike_intervals(db_path: str) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT symbol, strike_interval FROM index_configs").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        connection.close()
    return {str(s).upper(): int(i or 100) for s, i in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--candles", default="data/option_candles")
    parser.add_argument(
        "--write", action="store_true",
        help="Write fits to data/premium_coefficients.json for app/premium_model.py. "
             "That file is how the live app gets these numbers without importing numpy.",
    )
    args = parser.parse_args()

    index_series = _load_index_series(args.db, args.table)
    if not index_series:
        logger.error(
            "No ONE_MINUTE index candles in %s. The archive is 1-minute, so 1-minute "
            "index bars are required to match against it.", args.db,
        )
        return 1
    for symbol, points in index_series.items():
        logger.info("%s: %s one-minute index bars loaded", symbol, len(points))

    intervals = _load_strike_intervals(args.db)
    logger.info("Strike intervals: %s", intervals or "(defaulting to 100)")

    fits = fit_premium_model(index_series, intervals, Path(args.candles))
    if not fits:
        logger.error(
            "No bucket reached the 50-sample minimum. Check that option candle "
            "timestamps overlap the index candles -- a timezone or naive/aware "
            "mismatch produces exactly this symptom."
        )
        return 1

    logger.info("=" * 78)
    logger.info("Fitted coefficients (premium %% per index %%):")
    for fit in sorted(fits, key=lambda f: (f.index_symbol, f.dte_bucket, f.moneyness_bucket)):
        logger.info("  %s", fit.describe())

    logger.info("=" * 78)
    logger.info("Time decay (theta), from the joint fit y = lambda*x + theta*minutes:")
    fitted_theta = [f for f in fits if f.theta_per_minute is not None and f.moneyness_bucket == "ATM"]
    for f in sorted(fitted_theta, key=lambda x: (x.index_symbol, x.option_type, x.dte_bucket)):
        logger.info(
            "  %-10s %s dte=%-6s theta/min=%+.4f%%  over 45min=%+.2f%%  "
            "(lambda %.1f -> %.1f joint, r2 %.3f -> %.3f)%s",
            f.index_symbol, f.option_type, f.dte_bucket, f.theta_per_minute,
            f.theta_per_45min, f.multiplier, f.multiplier_joint or 0.0,
            f.r_squared, f.r_squared_joint or 0.0,
            "" if f.theta_is_plausible else "   <-- IMPLAUSIBLE (positive)",
        )

    implausible = [f for f in fitted_theta if not f.theta_is_plausible]
    if implausible:
        logger.warning("")
        logger.warning(
            "  %s of %s ATM buckets fitted a POSITIVE theta. A long option cannot gain "
            "value from time passing, so these coefficients are not measuring decay.",
            len(implausible), len(fitted_theta),
        )
        logger.warning(
            "  Cause: the archive is 1-minute, so nearly every gap is exactly one minute. "
            "With no intercept in the model, theta*minutes behaves as an intercept and "
            "absorbs the mean residual -- gamma convexity, sample drift -- rather than "
            "decay. The large shifts in lambda when the term is added (e.g. 68.0 -> 80.0) "
            "are the same collinearity showing up."
        )
        logger.warning(
            "  A second tell: theta should be MORE negative at shorter expiry. If the "
            "6-10 DTE bucket reads more negative than 2-5, the ordering is backwards and "
            "confirms the term is not decay."
        )
        logger.warning(
            "  DO NOT feed these into compute_outcomes. Use the stated-assumption "
            "fallback below and report every result both with and without it."
        )

    logger.info("")
    logger.info("  Stated-assumption decay (ATM, ~1/(2*DTE) per day over a %s-minute session):", SESSION_MINUTES)
    for dte in (1, 3, 8, 27):
        per_min = theoretical_theta_per_minute(dte)
        logger.info(
            "    %2d DTE -> %+.4f%%/min, %+.2f%% over a 45-minute hold%s",
            dte, per_min, per_min * 45,
            "  (Bank Nifty monthly)" if dte == 27 else "",
        )
    logger.info(
        "  At 3 DTE that is ~1.5%% over 45 minutes -- an eighth of a 12%% stop, and the "
        "amount every backtest result so far has been optimistic by."
    )

    logger.info("=" * 78)
    logger.info("Smoke check against first principles (delta * spot / premium):")
    for index_symbol, (spot, premium, dte_bucket) in SMOKE_REFERENCE.items():
        expected = _expected_lambda(spot, premium)
        matching = [
            f for f in fits
            if f.index_symbol == index_symbol
            and f.option_type == "CE"
            and f.moneyness_bucket == "ATM"
            and f.dte_bucket == dte_bucket
        ]
        if not matching:
            logger.warning("  %s: no ATM CE fit in the %s DTE bucket", index_symbol, dte_bucket)
            continue
        fit = matching[0]
        deviation = abs(fit.multiplier - expected) / expected
        verdict = "OK" if deviation <= SMOKE_TOLERANCE else "SUSPECT"
        logger.info(
            "  %-10s ATM CE dte=%s fitted=%6.1f expected=%6.1f (spot %.0f / prem %.0f) "
            "deviation=%.0f%% -> %s",
            index_symbol, dte_bucket, fit.multiplier, expected, spot, premium,
            deviation * 100, verdict,
        )
        if verdict == "SUSPECT":
            logger.warning(
                "  A coefficient this far off usually means a units error (delta vs "
                "elasticity) or timestamp misalignment between the two series, not a "
                "real market property. Resolve before trusting backtest output."
            )

    # Put/call asymmetry is a real property, not an artefact, and it matters for
    # risk sizing: an identical percentage stop on a PE corresponds to a smaller
    # index move than on a CE.
    for index_symbol in SMOKE_REFERENCE:
        ce = [f for f in fits if f.index_symbol == index_symbol and f.option_type == "CE" and f.moneyness_bucket == "ATM"]
        pe = [f for f in fits if f.index_symbol == index_symbol and f.option_type == "PE" and f.moneyness_bucket == "ATM"]
        if ce and pe:
            ce_mean = sum(f.multiplier for f in ce) / len(ce)
            pe_mean = sum(abs(f.multiplier) for f in pe) / len(pe)
            logger.info(
                "  %-10s ATM |PE|/CE sensitivity ratio = %.2f (PE %.1f vs CE %.1f)",
                index_symbol, pe_mean / ce_mean if ce_mean else 0.0, pe_mean, ce_mean,
            )

    logger.info("=" * 78)
    banknifty = [f for f in fits if f.index_symbol == "BANKNIFTY"]
    if banknifty and all(f.dte_bucket in ("0-1", "2-5", "6-10") for f in banknifty):
        logger.warning(
            "Bank Nifty fits cover 0-8 DTE only. Bank Nifty now trades the ~27 DTE "
            "monthly, which is outside this range -- gamma differs substantially. "
            "Either archive fresh option candles for the current monthly, or flag "
            "every Bank Nifty backtest result as extrapolated. Do not apply an "
            "8-DTE coefficient to a 27-DTE contract silently."
        )

    if args.write:
        out_path = Path("data/premium_coefficients.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": "scripts/calibrate_premium.py",
            "note": (
                "multiplier is premium PERCENT per index PERCENT (elasticity), NOT delta. "
                "Delta is premium rupees per index point; the two differ by spot/premium."
            ),
            "theta_note": (
                "theta_per_minute is the EMPIRICAL joint-fit coefficient and is generally "
                "NOT usable: on a 1-minute archive the elapsed-time column is nearly "
                "constant, so with no intercept the term absorbs the mean residual rather "
                "than measuring decay. Check theta_is_plausible (theta must be negative). "
                "When it is false, use theta_assumed_per_minute, which is a stated "
                "assumption of ~1/(2*DTE) per day, and report results both with and "
                "without it."
            ),
            "fits": [
                {
                    "index_symbol": f.index_symbol,
                    "option_type": f.option_type,
                    "dte_bucket": f.dte_bucket,
                    "moneyness_bucket": f.moneyness_bucket,
                    "multiplier": round(f.multiplier, 4),
                    "r_squared": round(f.r_squared, 4),
                    "n_samples": f.n_samples,
                    "extrapolated": f.extrapolated,
                    "theta_per_minute": None if f.theta_per_minute is None else round(f.theta_per_minute, 6),
                    "theta_is_plausible": f.theta_is_plausible,
                    "theta_assumed_per_minute": round(
                        theoretical_theta_per_minute(
                            {"0-1": 1, "2-5": 3, "6-10": 8}.get(f.dte_bucket, 27)
                        ), 6,
                    ),
                    "multiplier_joint": None if f.multiplier_joint is None else round(f.multiplier_joint, 4),
                    "r_squared_joint": None if f.r_squared_joint is None else round(f.r_squared_joint, 4),
                }
                for f in fits
            ],
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Wrote %s fits to %s", len(fits), out_path)

    logger.info("Pass the chosen value to the baseline, e.g.:")
    logger.info("  python -m scripts.backtest_baseline --db %s --multiplier <value>", args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
