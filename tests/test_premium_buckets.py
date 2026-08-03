"""The DTE bucket function exists twice, on purpose. This keeps the copies honest.

WHY THERE ARE TWO
-----------------
scripts/backtest/premium.py fits the coefficients and is numpy-based.
app/premium_model.py reads the fitted results at runtime and sits inside
app.main's import graph, which must never pull numpy in -- that is ~15 MB on a
414 MB box already running ~106 MB of live app. So the bucket function is
duplicated rather than imported.

WHY A DIVERGENCE WOULD BE EXPENSIVE
-----------------------------------
It fails silently. If the calibration writes "21+" while the live lookup asks
for "11+", no bucket ever matches, lambda_for falls through to its
best-sampled fallback, and every contract quietly gets an extrapolated
coefficient. Nothing errors. The only symptom is that derived stops are wrong
by a factor -- Bank Nifty ATM CE measures ~65 at 2-5 DTE against ~25 at 21+ --
which is precisely the class of bug that took a full cycle to notice last time.
"""

from __future__ import annotations

from app.premium_model import _dte_bucket as app_bucket
from scripts.backtest.premium import (
    DTE_BUCKET_NOMINAL,
    DTE_BUCKET_ORDER,
    _dte_bucket as script_bucket,
)


def test_bucket_functions_agree_across_the_full_range():
    """Every DTE a real contract could carry, plus the unparseable sentinel.

    400 days is well past any listed index option, so this covers the whole
    domain rather than sampling it.
    """
    for dte in range(-5, 400):
        assert app_bucket(dte) == script_bucket(dte), (
            f"DTE {dte}: app/premium_model.py says {app_bucket(dte)!r} but "
            f"scripts/backtest/premium.py says {script_bucket(dte)!r}. "
            "The two copies have diverged -- see this module's docstring."
        )


def test_negative_dte_is_unknown_not_a_real_bucket():
    """-1 is the sentinel for an unparseable expiry, not an expired contract.

    It must stay distinguishable from every fitted bucket, because "unknown" is
    the one label fit_premium_model still marks extrapolated.
    """
    assert app_bucket(-1) == "unknown"
    assert "unknown" not in DTE_BUCKET_ORDER


def test_declared_bucket_order_matches_what_the_function_produces():
    """DTE_BUCKET_ORDER drives the smoke check, the theta ladder and the
    coverage report. If a bucket is added to the function but not the tuple, it
    silently drops out of all three."""
    produced = {script_bucket(dte) for dte in range(0, 400)}
    assert produced == set(DTE_BUCKET_ORDER)


def test_every_bucket_has_a_nominal_dte():
    """The nominal DTE feeds the stated-assumption theta. A missing entry would
    fall through to the 27-day default and understate decay on a short-dated
    contract by roughly 9x."""
    assert set(DTE_BUCKET_NOMINAL) == set(DTE_BUCKET_ORDER)
    for bucket, nominal in DTE_BUCKET_NOMINAL.items():
        assert script_bucket(nominal) == bucket, (
            f"nominal DTE {nominal} for bucket {bucket!r} does not fall inside it"
        )


def test_buckets_are_ordered_by_increasing_dte():
    """DTE_BUCKET_ORDER is walked as a ladder by the theta ordering check, which
    is only meaningful if the tuple really is in ascending DTE order."""
    nominals = [DTE_BUCKET_NOMINAL[b] for b in DTE_BUCKET_ORDER]
    assert nominals == sorted(nominals)
