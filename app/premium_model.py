"""Premium sensitivity coefficients, for expressing risk in comparable units.

WHY THIS EXISTS
---------------
A "12% stop" is not a comparable quantity across option types. ATM puts are
1.28-1.53x more sensitive to index movement than calls, so the same percentage
is a materially tighter index distance on a PE:

    Nifty, 2-5 DTE, nominal 12% stop -> CE 43 index points (2.02 ATR)
                                     -> PE 27 index points (1.27 ATR)

Nobody chose that asymmetry and it persists under any entry rule. Risk
parameters should therefore be specified and reviewed in index points or ATR
multiples; premium percent is a label that hides the actual bet.

WHY IT LOADS FROM JSON RATHER THAN IMPORTING THE FIT
----------------------------------------------------
The coefficients are fitted in scripts/backtest/premium.py, which is numpy-
based. Nothing in app.main's import graph may pull numpy in -- that is ~15 MB
on a 414 MB box already running ~106 MB of live app. So the calibration script
writes its results to a data file and this module reads them with the stdlib.

Regenerate with:
    python -m scripts.calibrate_premium --db data/trading.db --write

If the file is absent or a bucket is missing, conversions return None. That is
deliberate: a fabricated coefficient would produce a confident, wrong index
distance, which is worse than no number at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

COEFFICIENTS_PATH = Path("data/premium_coefficients.json")


def _dte_bucket(dte: int) -> str:
    """DTE -> bucket label.

    MIRRORED from scripts/backtest/premium.py, which this module deliberately
    does not import -- that module is numpy-based and nothing in app.main's
    import graph may pull numpy in. tests/test_premium_buckets.py asserts the
    two copies agree.

    Divergence is the failure mode worth guarding against, because it is
    silent: the calibration would write "21+" while this lookup asked for
    "11+", no bucket would ever match, and every contract would quietly fall
    back to an extrapolated coefficient with nothing in the logs to say why.
    """
    if dte < 0:
        return "unknown"
    if dte <= 1:
        return "0-1"
    if dte <= 5:
        return "2-5"
    if dte <= 10:
        return "6-10"
    if dte <= 20:
        return "11-20"
    return "21+"


@dataclass(frozen=True)
class RiskUnits:
    """A risk level expressed three ways. index_points and atr_multiple are
    None when no fitted coefficient covers the contract."""

    premium_percent: float
    index_percent: float | None
    index_points: float | None
    atr_multiple: float | None
    extrapolated: bool


@lru_cache(maxsize=1)
def _load() -> dict:
    if not COEFFICIENTS_PATH.exists():
        logger.info(
            "No premium coefficients at %s; risk-unit conversion disabled. "
            "Run: python -m scripts.calibrate_premium --write",
            COEFFICIENTS_PATH,
        )
        return {}
    try:
        return json.loads(COEFFICIENTS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read premium coefficients: %s", exc)
        return {}


def reload_coefficients() -> None:
    """Drop the cache so a freshly written file is picked up without restart."""
    _load.cache_clear()


def lambda_for(
    index_symbol: str, option_type: str, dte: int, moneyness: str = "ATM"
) -> tuple[float, bool] | None:
    """(premium %% per index %%, extrapolated) for a contract, or None.

    The extrapolated flag matters and callers should surface it. Elasticity
    varies by more than 2x across the DTE range actually traded -- Bank Nifty
    ATM CE measures ~65 at 2-5 DTE against ~25 at 21+ -- so a coefficient
    borrowed from the wrong bucket does not misstate a derived stop slightly,
    it misstates it by a factor.
    """
    data = _load()
    if not data:
        return None
    fits = data.get("fits") or []
    wanted = _dte_bucket(dte)
    index_symbol = index_symbol.upper()
    option_type = option_type.upper()

    exact = [
        f for f in fits
        if f.get("index_symbol") == index_symbol
        and f.get("option_type") == option_type
        and f.get("moneyness_bucket") == moneyness
        and f.get("dte_bucket") == wanted
        and not f.get("extrapolated")
    ]
    if exact:
        return float(exact[0]["multiplier"]), False

    # No fit for this DTE: fall back to the best-sampled bucket for the same
    # index and option type, flagged as extrapolated rather than passed off as
    # measured.
    fallback = [
        f for f in fits
        if f.get("index_symbol") == index_symbol
        and f.get("option_type") == option_type
        and f.get("moneyness_bucket") == moneyness
    ]
    if not fallback:
        return None
    best = max(fallback, key=lambda f: f.get("n_samples", 0))
    return float(best["multiplier"]), True


def to_risk_units(
    premium_percent: float,
    index_symbol: str,
    option_type: str,
    dte: int,
    spot: float | None,
    atr: float | None,
    moneyness: str = "ATM",
) -> RiskUnits:
    """Convert a premium-percent risk level into index points and ATR multiples.

        index_percent = premium_percent / |lambda|
        index_points  = index_percent / 100 * spot
        atr_multiple  = index_points / atr
    """
    resolved = lambda_for(index_symbol, option_type, dte, moneyness)
    if resolved is None or not premium_percent:
        return RiskUnits(premium_percent, None, None, None, False)
    lam, extrapolated = resolved
    if not lam:
        return RiskUnits(premium_percent, None, None, None, extrapolated)

    index_percent = premium_percent / abs(lam)
    index_points = (index_percent / 100.0 * spot) if spot else None
    atr_multiple = (index_points / atr) if (index_points is not None and atr) else None
    return RiskUnits(
        premium_percent=round(premium_percent, 2),
        index_percent=round(index_percent, 4),
        index_points=round(index_points, 2) if index_points is not None else None,
        atr_multiple=round(atr_multiple, 2) if atr_multiple is not None else None,
        extrapolated=extrapolated,
    )


def symmetric_premium_percent(
    proposed_percent: float,
    index_symbol: str,
    option_type: str,
    dte: int,
    moneyness: str = "ATM",
) -> tuple[float, bool]:
    """Rescale a premium-percent risk level so CE and PE are the SAME bet.

    THE PROBLEM THIS SOLVES
    -----------------------
    A "12% stop" is not one setting, it is two different bets wearing one
    label. ATM puts are 1.28x (Bank Nifty) to 1.53x (Nifty) more sensitive to
    index movement than calls, so the identical percentage is a much tighter
    index distance on a put:

        Nifty, 2-5 DTE, nominal 12% stop -> CE 43 index points (2.02 ATR)
                                         -> PE 27 index points (1.27 ATR)

    Puts were therefore being stopped by materially smaller moves than calls
    under the same nominal setting, and nobody chose that.

    THE FIX
    -------
    Interpret the proposed percentage against a CALL reference, convert it to
    an index distance, then convert back using the ACTUAL contract's
    sensitivity:

        index_pct   = proposed_percent / |lambda_call|
        premium_pct = index_pct * |lambda_actual|

    For a call that is a no-op. For a put it WIDENS the premium stop by the
    sensitivity ratio, so the index distance matches. A 12% call stop becomes
    an ~18% put stop -- same bet, honestly labelled.

    Returns (adjusted_percent, bucket_matched). When no fitted coefficient
    covers this contract, returns the input unchanged with matched=False --
    deliberately NOT silently borrowing an unrelated bucket's coefficient,
    which would produce a confident wrong distance.
    """
    if not proposed_percent:
        return proposed_percent, False
    actual = lambda_for(index_symbol, option_type, dte, moneyness)
    reference = lambda_for(index_symbol, "CE", dte, moneyness)
    if actual is None or reference is None:
        logger.info(
            "[RISK] %s %s dte=%s: no fitted coefficient, keeping premium-percent behaviour",
            index_symbol, option_type, dte,
        )
        return proposed_percent, False
    lam_actual, _ = actual
    lam_reference, _ = reference
    if not lam_reference or not lam_actual:
        return proposed_percent, False
    adjusted = proposed_percent / abs(lam_reference) * abs(lam_actual)
    return round(adjusted, 2), True


def days_to_expiry(expiry: str, as_of_date) -> int:
    """Days between a stored expiry string and a date. -1 when unparseable."""
    from datetime import datetime

    text = str(expiry).strip().upper()
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return (datetime.strptime(text, fmt).date() - as_of_date).days
        except ValueError:
            continue
    return -1
