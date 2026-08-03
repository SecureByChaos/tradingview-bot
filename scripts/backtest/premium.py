"""Index-move to premium-move calibration.

Every backtest number scales linearly with this coefficient, so it is fitted
from real option candles rather than assumed. Hardcoding delta = 0.5 would make
the entire sweep a restatement of that guess.

Delta 0.5 is used only as a smoke test. A real trade on 29 July ran
Rs 115.35 -> Rs 137.20 on roughly 44 Nifty points, which is consistent with
~0.5 -- that validates the method, not the coefficient.

DTE BUCKETING IS NOT COSMETIC
-----------------------------
Elasticity falls sharply with time to expiry -- Bank Nifty ATM CE measures
~65 at 2-5 DTE against ~25 at 11+, because a longer-dated contract carries far
more premium and the same index move is a smaller fraction of it. A bucket that
spans too wide a DTE range therefore averages together contracts that behave
differently, and the average fits neither end.

The buckets were originally 0-1 / 2-5 / 6-10 / 11+, which was adequate only
while the archive stopped at 10 DTE. Once the Bank Nifty monthly was archived,
"11+" spanned 11 to ~30 days in one fit while live positions sat at 22 -- so
11-20 and 21+ are now separated. Anything that changes _dte_bucket must be
mirrored in app/premium_model.py; tests/test_premium_buckets.py enforces that.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

OPTION_CANDLE_DIR = Path("data/option_candles")
INSTRUMENT_CACHE = Path("data/instruments.json")

# Minutes in an NSE equity trading session (09:15-15:30).
SESSION_MINUTES = 375


def theoretical_theta_per_minute(dte: int) -> float:
    """Stated-assumption decay for an ATM option, as premium percent per minute.

    Used when the empirical joint fit fails its plausibility check, which on a
    1-minute archive it generally will -- the elapsed-time column has almost no
    independent variation, so the coefficient absorbs the mean residual instead
    of decay.

    An at-the-money option is essentially all time value, and that value decays
    roughly as sqrt(T). The fractional loss per day is therefore about 1/(2T),
    so:

        per-day fraction   = 1 / (2 * dte)
        per-minute percent = -100 / (2 * dte * 375)

    At 4 DTE that is ~12.5%/day, or ~1.5% over a 45-minute hold -- an eighth of
    a 12% stop, and material. At 8 DTE it halves.

    This is an ASSUMPTION, not a measurement. Every result computed with it
    should also be reported without it, so the size of the assumption stays
    visible rather than baked in.
    """
    effective_dte = max(dte, 1)
    return -100.0 / (2.0 * effective_dte * SESSION_MINUTES)


# Nominal DTE used to represent each bucket when a single number is needed
# (the stated-assumption theta, mainly). Midpoint of the range, except "21+"
# which is anchored on ~27 -- the Bank Nifty monthly cycle that actually
# populates it -- rather than an unbounded upper edge.
DTE_BUCKET_ORDER = ("0-1", "2-5", "6-10", "11-20", "21+")
DTE_BUCKET_NOMINAL = {"0-1": 1, "2-5": 3, "6-10": 8, "11-20": 15, "21+": 27}


def _dte_bucket(dte: int) -> str:
    """DTE -> bucket label.

    MIRRORED in app/premium_model.py, which cannot import this module because
    the live app must not pull numpy into its import graph (~15 MB on a 414 MB
    box). tests/test_premium_buckets.py asserts the two stay identical -- a
    silent divergence would have the calibration writing one bucket name and
    the live lookup asking for another, which surfaces only as every contract
    falling back to "extrapolated" for no visible reason.
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


# Moneyness buckets, in strike-intervals away from spot.
MONEYNESS_BUCKETS = ((-2, -1), (-1, 1), (1, 2))


@dataclass(frozen=True)
class PremiumFit:
    index_symbol: str
    option_type: str
    dte_bucket: str
    moneyness_bucket: str
    multiplier: float       # premium %% move per 1%% index move
    r_squared: float
    n_samples: int
    extrapolated: bool
    # Joint fit adding an elapsed-time term:
    #     premium_ret = lambda * index_ret + theta * minutes
    # theta is premium PERCENT lost per MINUTE held, and it is normally
    # negative -- a long option bleeds time value whether or not the index
    # moves. The directional-only fit above has no intercept, so any systematic
    # decay is currently absorbed into the slope rather than reported, which
    # makes every backtest number mildly optimistic.
    theta_per_minute: float | None = None
    multiplier_joint: float | None = None
    r_squared_joint: float | None = None
    # Median premium and spot LEVELS observed in this bucket. Not used by the
    # fit -- they exist so the first-principles smoke check can compare against
    # what this bucket actually contained, instead of a premium hardcoded from
    # one archive period. See expected_lambda below.
    median_premium: float | None = None
    median_spot: float | None = None

    def expected_lambda(self, assumed_delta: float = 0.5) -> float | None:
        """First-principles elasticity for this bucket: delta * spot / premium.

        Derived from the bucket's OWN median levels rather than a fixed
        reference, which is what makes the check valid for every bucket
        including ones added later. An earlier version hardcoded ATM premiums
        observed in the 20-24 July archive and then flagged a perfectly good
        6-10 DTE fit as 44% off, purely because that period held cheaper
        contracts than the bucket being checked.

        Only delta stays assumed. That is the honest residual: 0.5 is right for
        a true ATM option and drifts up slightly for longer-dated ones, so a
        long-dated bucket reading ~5-10% above expectation is the assumption
        showing, not a fault in the fit.
        """
        if not self.median_premium or not self.median_spot:
            return None
        return assumed_delta * self.median_spot / self.median_premium

    @property
    def theta_per_45min(self) -> float | None:
        """Decay over a typical hold, which is the figure worth reasoning about
        against a 12%% stop."""
        return None if self.theta_per_minute is None else self.theta_per_minute * 45

    @property
    def theta_is_plausible(self) -> bool:
        """Whether the fitted theta can be decay at all.

        A long option loses time value; theta must be NEGATIVE. A positive
        coefficient means the term is absorbing something else -- with a
        1-minute archive nearly every gap is exactly one minute, so
        `theta * minutes` behaves as an intercept and soaks up the mean
        residual (gamma convexity, sample drift) rather than measuring decay.

        This is a validity check, not a preference. An implausible sign means
        the coefficient is uninterpretable and must not be used.
        """
        return self.theta_per_minute is not None and self.theta_per_minute < 0

    def describe(self) -> str:
        flag = "  [EXTRAPOLATED]" if self.extrapolated else ""
        theta = (
            f" theta/min={self.theta_per_minute:+.4f} (45m {self.theta_per_45min:+.2f}%)"
            if self.theta_per_minute is not None else ""
        )
        return (
            f"{self.index_symbol:<10} {self.option_type} dte={self.dte_bucket:<6} "
            f"money={self.moneyness_bucket:<8} mult={self.multiplier:8.2f} "
            f"r2={self.r_squared:5.3f} n={self.n_samples:<6}{theta}{flag}"
        )


def _load_instrument_lookup() -> dict[str, dict]:
    """token -> {name, expiry, strike, symbol} from the cached scrip master.

    Parsing the tradingsymbol with a regex is tempting and wrong: Angel's
    option symbols concatenate expiry and strike with no delimiter, so
    'BANKNIFTY28JUL2657100CE' is genuinely ambiguous. The scrip master is
    authoritative.
    """
    if not INSTRUMENT_CACHE.exists():
        # Not fatal: filename parsing covers every expired contract anyway, and
        # expired contracts are the whole archive.
        logger.info("No instrument cache at %s; relying on filename parsing", INSTRUMENT_CACHE)
        return {}
    lookup: dict[str, dict] = {}
    for item in json.loads(INSTRUMENT_CACHE.read_text(encoding="utf-8")):
        token = str(item.get("token") or "")
        if token:
            lookup[token] = item
    return lookup


# BANKNIFTY28JUL2655900CE -> name, 28JUL26 expiry, 55900 strike, CE.
# The expiry field is fixed-width (2 digits, 3 letters, 2 digits), which is what
# makes this unambiguous -- everything between it and the CE/PE suffix is the
# strike. A general "split the digits" parse would NOT be safe here.
_SYMBOL_RE = re.compile(
    r"^(?P<name>[A-Z]+?)(?P<day>\d{2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})(?P<strike>\d+)(?P<opt>CE|PE)$"
)


def _parse_symbol_filename(stem: str) -> dict | None:
    """Recover contract metadata from the archived filename.

    Needed because the scrip master only lists LIVE instruments. Once a
    contract expires it drops out, so any archive older than the current expiry
    cycle cannot be looked up by token -- which is the normal case for exactly
    the historical data this calibration depends on. The filename is the only
    surviving record, and pull_option_candles.py writes it as
    <TRADINGSYMBOL>_<TOKEN>.csv precisely so this remains possible.
    """
    symbol = stem.rsplit("_", 1)[0].upper()
    match = _SYMBOL_RE.match(symbol)
    if not match:
        return None
    return {
        "name": match.group("name"),
        "expiry": f"{match.group('day')}{match.group('mon')}20{match.group('yy')}",
        "strike": float(match.group("strike")),
        "symbol": symbol,
        "_from_filename": True,
    }


def _parse_expiry(raw: str) -> datetime | None:
    """Parse an expiry string.

    The format string must NOT be uppercased. An earlier version did
    `fmt.upper()`, turning "%d%b%Y" into "%D%B%Y" -- Python rejects %D, so every
    parse failed silently and DTE fell back to 99, collapsing every bucket into
    "99+". Only the VALUE is uppercased, to match "28JUL2026".
    """
    text = str(raw).strip().upper()
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _load_option_series(path: Path) -> list[tuple[datetime, float]]:
    series: list[tuple[datetime, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                ts = datetime.fromisoformat(str(row["timestamp_ist"]).replace("Z", "+00:00")).replace(tzinfo=None)
                series.append((ts, float(row["close"])))
            except (KeyError, ValueError):
                continue
    return series


def fit_premium_model(
    index_series: dict[str, dict[datetime, float]],
    strike_intervals: dict[str, int],
    candle_dir: Path = OPTION_CANDLE_DIR,
) -> list[PremiumFit]:
    """Regress premium %% returns on index %% returns, bucketed by index, DTE
    and moneyness.

    index_series: index_symbol -> {timestamp -> close}, from the candle store,
    at the same 1-minute resolution as the option archive.
    """
    if not candle_dir.exists():
        raise SystemExit(
            f"Option candle archive not found at {candle_dir}. "
            "Run scripts/pull_option_candles.py while the contracts are still live."
        )

    lookup = _load_instrument_lookup()
    buckets: dict[tuple, list[tuple[float, float]]] = {}
    from_filename = 0

    for path in sorted(candle_dir.glob("*.csv")):
        token = path.stem.rsplit("_", 1)[-1]
        meta = lookup.get(token)
        if meta is None:
            # Expected, not exceptional: an expired contract is absent from the
            # scrip master by design, and every archive worth calibrating on is
            # of expired contracts. Fall back to the filename.
            meta = _parse_symbol_filename(path.stem)
            if meta is None:
                logger.warning("Skipping %s: not in scrip master and filename unparseable", path.name)
                continue
            from_filename += 1

        index_symbol = str(meta.get("name", "")).upper()
        option_type = "CE" if str(meta.get("symbol", "")).upper().endswith("CE") else "PE"
        spot_map = index_series.get(index_symbol)
        if not spot_map:
            continue
        expiry = _parse_expiry(meta.get("expiry", ""))
        strike_raw = float(meta.get("strike") or 0)
        strike = strike_raw / 100 if strike_raw > 100000 else strike_raw
        interval = strike_intervals.get(index_symbol, 100)

        series = _load_option_series(path)
        for k in range(1, len(series)):
            ts, premium = series[k]
            prev_ts, prev_premium = series[k - 1]
            spot = spot_map.get(ts)
            prev_spot = spot_map.get(prev_ts)
            if not spot or not prev_spot or prev_premium <= 0 or prev_spot <= 0:
                continue
            index_ret = (spot - prev_spot) / prev_spot * 100.0
            premium_ret = (premium - prev_premium) / prev_premium * 100.0
            # Drop zero-movement rows: they carry no information about the
            # relationship and would pull the fitted slope toward zero.
            if abs(index_ret) < 1e-6:
                continue

            dte = (expiry.date() - ts.date()).days if expiry else -1
            dte_bucket = _dte_bucket(dte)
            steps = (strike - spot) / interval if interval else 0.0
            money_bucket = "OTM"
            for low, high in MONEYNESS_BUCKETS:
                if low <= steps < high:
                    money_bucket = "ATM" if (low, high) == (-1, 1) else f"{low}:{high}"
                    break

            # Option type MUST be part of the key. A call gains when the index
            # rises and a put loses; pooling them into one regression drives the
            # coefficient toward zero or negative, which is exactly what the
            # first run produced (-7.44, -10.07, -16.65 with r2 ~ 0.002-0.074).
            # Elapsed minutes carried alongside so theta can be fitted jointly.
            # Mostly 1.0 (the archive is 1-minute), but gaps exist where a
            # strike did not trade, and those longer intervals are what give
            # the time term any leverage at all.
            minutes = max((ts - prev_ts).total_seconds() / 60.0, 0.0)
            # premium and spot LEVELS ride along unused by the regression, so
            # the smoke check can reconstruct delta * spot / premium from this
            # bucket's own contents rather than a hardcoded reference.
            buckets.setdefault(
                (index_symbol, option_type, dte_bucket, money_bucket), []
            ).append((index_ret, premium_ret, minutes, premium, spot))

    if from_filename:
        logger.info("Recovered metadata from filename for %s expired contracts", from_filename)
    logger.info(
        "Matched %s buckets; sample counts: %s",
        len(buckets), {k: len(v) for k, v in sorted(buckets.items())},
    )

    fits: list[PremiumFit] = []
    for (index_symbol, option_type, dte_bucket, money_bucket), pairs in sorted(buckets.items()):
        if len(pairs) < 50:
            continue
        x = np.array([p[0] for p in pairs], dtype=np.float64)
        y = np.array([p[1] for p in pairs], dtype=np.float64)
        m = np.array([p[2] for p in pairs], dtype=np.float64)
        # Median, not mean: premium levels are right-skewed and a handful of
        # deep-value rows would drag a mean well away from what the bucket
        # typically held.
        median_premium = float(np.median(np.array([p[3] for p in pairs], dtype=np.float64)))
        median_spot = float(np.median(np.array([p[4] for p in pairs], dtype=np.float64)))
        # Through the origin: a zero index move should imply a zero premium
        # move. An intercept here would silently absorb theta decay into what
        # is meant to be a directional sensitivity.
        slope = float(np.sum(x * y) / np.sum(x * x))
        residual = y - slope * x
        ss_total = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - float(np.sum(residual**2)) / ss_total if ss_total > 0 else 0.0

        # Joint fit: y = lambda*x + theta*m, solved from the 2x2 normal
        # equations. Reported alongside rather than replacing the directional
        # fit, so results can be run both ways and the optimism of ignoring
        # decay stays visible instead of hidden.
        theta = multiplier_joint = r2_joint = None
        sxx, sxm, smm = float(x @ x), float(x @ m), float(m @ m)
        sxy, smy = float(x @ y), float(m @ y)
        det = sxx * smm - sxm * sxm
        # A near-singular system means the elapsed-time column carries almost no
        # independent variation (nearly every gap is exactly one minute). Report
        # nothing rather than a coefficient the data cannot support.
        if abs(det) > 1e-9 and smm > 0:
            lam_j = (sxy * smm - smy * sxm) / det
            theta_j = (smy * sxx - sxy * sxm) / det
            residual_j = y - lam_j * x - theta_j * m
            r2_joint = 1.0 - float(np.sum(residual_j**2)) / ss_total if ss_total > 0 else 0.0
            multiplier_joint = float(lam_j)
            theta = float(theta_j)
        fits.append(
            PremiumFit(
                index_symbol=index_symbol,
                option_type=option_type,
                dte_bucket=dte_bucket,
                moneyness_bucket=money_bucket,
                multiplier=slope,
                r_squared=r2,
                n_samples=len(pairs),
                # A FITTED bucket is measured, by definition -- every bucket
                # here cleared the 50-sample minimum against real option
                # candles. "11+" was hardcoded as extrapolated when the archive
                # held nothing past 10 DTE; once the Bank Nifty monthly was
                # archived that became wrong, and actively harmful: with "11+"
                # marked extrapolated, select_multiplier could not match Bank
                # Nifty's ~27 DTE contract and fell back to the 6-10 bucket at
                # lambda ~-67 instead of the correct ~-28 -- a 2.4x error in
                # every derived stop distance.
                #
                # Extrapolation is a property of a LOOKUP that finds no bucket
                # for the DTE it was asked about, not of the fit. select_multiplier
                # already reports that separately.
                extrapolated=dte_bucket == "unknown",
                theta_per_minute=theta,
                multiplier_joint=multiplier_joint,
                r_squared_joint=r2_joint,
                median_premium=median_premium,
                median_spot=median_spot,
            )
        )
    return fits


def select_multiplier(
    fits: list[PremiumFit], index_symbol: str, dte: int, option_type: str = "CE"
) -> tuple[float, bool]:
    """Pick the ATM multiplier for an index, DTE and option type.

    Returns (multiplier, extrapolated). Callers MUST surface the extrapolated
    flag -- a Bank Nifty 27-DTE result carrying a 6-10 DTE coefficient is a
    materially different claim from a measured one.
    """
    candidates = [
        f for f in fits
        if f.index_symbol == index_symbol
        and f.moneyness_bucket == "ATM"
        and f.option_type == option_type
    ]
    if not candidates:
        raise ValueError(f"No ATM {option_type} premium fit for {index_symbol}")
    wanted = _dte_bucket(dte)
    for fit in candidates:
        if fit.dte_bucket == wanted and not fit.extrapolated:
            return fit.multiplier, False
    # Outside the fitted range: fall back to the best-sampled fit and mark it
    # extrapolated rather than pretending it was measured.
    best = max(candidates, key=lambda f: f.n_samples)
    return best.multiplier, True
