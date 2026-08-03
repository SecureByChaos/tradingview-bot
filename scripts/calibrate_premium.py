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
    DTE_BUCKET_NOMINAL,
    DTE_BUCKET_ORDER,
    SESSION_MINUTES,
    fit_premium_model,
    select_multiplier,
    theoretical_theta_per_minute,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("calibrate_premium")

# First-principles expectation, NOT a single observed fill:
#
#     lambda = delta * spot / premium
#
# The premium term is why both a single trade AND a fixed reference are bad
# baselines: elasticity scales inversely with premium, so a cheap 0-DTE
# contract and a 27-DTE monthly on the same index have very different lambdas.
# Two earlier versions were wrong in exactly this way -- one hardcoded 105 from
# a single 29-July fill, the next hardcoded ATM premiums from the 20-24 July
# archive -- and both flagged perfectly good fits as suspect whenever the
# bucket under test held different contracts than the reference did.
#
# So the expectation is now rebuilt per bucket from that bucket's own median
# premium and spot (PremiumFit.expected_lambda). Delta is the only thing still
# assumed, and it is the only thing that legitimately can be.
ASSUMED_ATM_DELTA = 0.5
# A good fit lands inside ~15% of expectation. Past 25% is worth investigating
# rather than waving through -- though see the long-dated note in the output:
# assuming delta = 0.5 flat costs a few percent at long expiry, where a true
# ATM call sits nearer 0.53.
SMOKE_TOLERANCE = 0.25


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
    for bucket in DTE_BUCKET_ORDER:
        dte = DTE_BUCKET_NOMINAL[bucket]
        per_min = theoretical_theta_per_minute(dte)
        logger.info(
            "    dte=%-6s (nominal %2d) -> %+.4f%%/min, %+.2f%% over a 45-minute hold%s",
            bucket, dte, per_min, per_min * 45,
            "  (Bank Nifty monthly)" if bucket == "21+" else "",
        )
    logger.info(
        "  At 3 DTE that is -2.0%% over 45 minutes -- a sixth of a 12%% stop, and the "
        "amount every backtest result so far has been optimistic by."
    )

    # Sign alone is not sufficient. Real theta is MORE negative at shorter
    # expiry; if a bucket that came out negative still orders backwards against
    # its longer-dated sibling, it is negative by accident and equally unusable.
    # Checked across EVERY adjacent pair of buckets present, not just 2-5 vs
    # 6-10. With 11-20 and 21+ now fitted, a check hardcoded to the two
    # shortest buckets would pass while the long end ordered backwards.
    ordering_ok = True
    for index_symbol in sorted({f.index_symbol for f in fitted_theta}):
        for option_type in ("CE", "PE"):
            present = [
                next((f for f in fitted_theta if f.index_symbol == index_symbol
                      and f.option_type == option_type and f.dte_bucket == bucket), None)
                for bucket in DTE_BUCKET_ORDER
            ]
            ladder = [f for f in present if f is not None]
            for shorter, longer in zip(ladder, ladder[1:]):
                # Real theta is MORE negative at shorter expiry, so walking the
                # ladder outward theta must strictly increase.
                if shorter.theta_per_minute >= longer.theta_per_minute:
                    ordering_ok = False
    theta_usable = ordering_ok and all(f.theta_is_plausible for f in fitted_theta)
    logger.info("")
    logger.info(
        "  EMPIRICAL THETA USABLE: %s%s",
        "yes" if theta_usable else "NO",
        "" if theta_usable else
        " -- signs and/or DTE ordering fail. Buckets that happen to be negative are "
        "negative by accident, not by measurement. Use theta_assumed_per_minute.",
    )

    logger.info("=" * 78)
    logger.info(
        "Smoke check against first principles (delta %.2f * median spot / median premium,"
        " both measured per bucket):", ASSUMED_ATM_DELTA,
    )
    # EVERY ATM CE bucket, not a hand-picked pair. A check that only ever looks
    # at 2-5 DTE cannot catch a units error in a long-dated bucket, and the
    # long-dated buckets are the ones carrying live Bank Nifty positions.
    suspect_buckets = []
    smoke_candidates = [
        f for f in fits if f.option_type == "CE" and f.moneyness_bucket == "ATM"
    ]
    for fit in sorted(
        smoke_candidates,
        key=lambda f: (f.index_symbol, DTE_BUCKET_ORDER.index(f.dte_bucket)
                       if f.dte_bucket in DTE_BUCKET_ORDER else 99),
    ):
        expected = fit.expected_lambda(ASSUMED_ATM_DELTA)
        if expected is None or expected <= 0:
            logger.warning(
                "  %-10s ATM CE dte=%-6s no premium levels recorded, cannot check",
                fit.index_symbol, fit.dte_bucket,
            )
            continue
        deviation = abs(fit.multiplier - expected) / expected
        verdict = "OK" if deviation <= SMOKE_TOLERANCE else "SUSPECT"
        logger.info(
            "  %-10s ATM CE dte=%-6s fitted=%6.1f expected=%6.1f "
            "(spot %.0f / prem %.0f, n=%s) deviation=%2.0f%% -> %s",
            fit.index_symbol, fit.dte_bucket, fit.multiplier, expected,
            fit.median_spot or 0.0, fit.median_premium or 0.0, fit.n_samples,
            deviation * 100, verdict,
        )
        if verdict == "SUSPECT":
            suspect_buckets.append(fit)
    if suspect_buckets:
        logger.warning(
            "  %s bucket(s) outside %.0f%%. A gap this size usually means a units error "
            "(delta vs elasticity) or timestamp misalignment between the two series, not "
            "a real market property. Resolve before trusting backtest output.",
            len(suspect_buckets), SMOKE_TOLERANCE * 100,
        )
        logger.warning(
            "  One benign exception: at long expiry a true ATM call's delta is nearer "
            "0.53 than 0.50, so a 21+ bucket reading a few percent ABOVE expectation is "
            "the flat-delta assumption showing, not a broken fit. A bucket reading far "
            "BELOW, or any short-dated bucket off by this much, is not."
        )

    # Put/call asymmetry is a real property, not an artefact, and it matters for
    # risk sizing: an identical percentage stop on a PE corresponds to a smaller
    # index move than on a CE.
    #
    # Reported PER DTE BUCKET as well as pooled, because the asymmetry is not
    # constant: Bank Nifty ATM runs ~1.29 at 2-5 DTE but ~1.12 at 21+. Pooling
    # across buckets averages those into a number that describes neither, and
    # the rescale in app/premium_model.py applies per bucket, so the per-bucket
    # figures are the ones that describe what live trading actually does.
    for index_symbol in sorted({f.index_symbol for f in fits}):
        atm = [f for f in fits if f.index_symbol == index_symbol and f.moneyness_bucket == "ATM"]
        ce = [f for f in atm if f.option_type == "CE"]
        pe = [f for f in atm if f.option_type == "PE"]
        if not (ce and pe):
            continue
        ce_mean = sum(f.multiplier for f in ce) / len(ce)
        pe_mean = sum(abs(f.multiplier) for f in pe) / len(pe)
        logger.info(
            "  %-10s ATM |PE|/CE sensitivity ratio = %.2f (PE %.1f vs CE %.1f), pooled",
            index_symbol, pe_mean / ce_mean if ce_mean else 0.0, pe_mean, ce_mean,
        )
        for bucket in DTE_BUCKET_ORDER:
            ce_b = next((f for f in ce if f.dte_bucket == bucket), None)
            pe_b = next((f for f in pe if f.dte_bucket == bucket), None)
            if not (ce_b and pe_b and ce_b.multiplier):
                continue
            logger.info(
                "               dte=%-6s ratio = %.2f (PE %.1f vs CE %.1f)",
                bucket, abs(pe_b.multiplier) / abs(ce_b.multiplier),
                abs(pe_b.multiplier), ce_b.multiplier,
            )

    logger.info("=" * 78)
    logger.info("DTE coverage per index (ATM CE, which is what the rescale keys off):")
    for index_symbol in sorted({f.index_symbol for f in fits}):
        covered = {
            f.dte_bucket for f in fits
            if f.index_symbol == index_symbol and f.moneyness_bucket == "ATM"
        }
        gaps = [b for b in DTE_BUCKET_ORDER if b not in covered]
        logger.info(
            "  %-10s covered: %s%s",
            index_symbol,
            ", ".join(b for b in DTE_BUCKET_ORDER if b in covered) or "(none)",
            f"   MISSING: {', '.join(gaps)}" if gaps else "",
        )
        if gaps:
            logger.warning(
                "  %s has no fit for %s. Any contract landing in those buckets falls back "
                "to the best-sampled bucket and is flagged extrapolated -- which across "
                "this DTE range is a factor-level error, not a rounding one. Archive "
                "option candles covering those DTEs before trusting derived stops there: "
                "python -m scripts.pull_option_candles --index %s --expiry <DDMMMYYYY> "
                "--start <date> --end <date> --strike-band 3",
                index_symbol, ", ".join(gaps), index_symbol,
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
            "theta_empirical_usable": theta_usable,
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
                            DTE_BUCKET_NOMINAL.get(f.dte_bucket, 27)
                        ), 6,
                    ),
                    # Levels the bucket was fitted on. Carried so the smoke
                    # check is reproducible from the JSON alone, without
                    # re-reading the option archive.
                    "median_premium": None if f.median_premium is None else round(f.median_premium, 2),
                    "median_spot": None if f.median_spot is None else round(f.median_spot, 2),
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
