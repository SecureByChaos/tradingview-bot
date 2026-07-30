"""Index-move to premium-move calibration.

Every backtest number scales linearly with this coefficient, so it is fitted
from real option candles rather than assumed. Hardcoding delta = 0.5 would make
the entire sweep a restatement of that guess.

Delta 0.5 is used only as a smoke test. A real trade on 29 July ran
Rs 115.35 -> Rs 137.20 on roughly 44 Nifty points, which is consistent with
~0.5 -- that validates the method, not the coefficient.

KNOWN GAP, stated loudly because silently extrapolating it would be the most
expensive kind of quiet error: the option archive covers 0-8 DTE. Bank Nifty
now trades a ~27 DTE monthly, outside the fitted range. Applying an 8-DTE
coefficient to a 27-DTE contract would systematically misstate results, so
fit_premium_model refuses to do it and marks such buckets extrapolated.
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

# Beyond this the fit is extrapolation, not measurement.
MAX_FITTED_DTE = 10


def _dte_bucket(dte: int) -> str:
    if dte < 0:
        return "unknown"
    if dte <= 1:
        return "0-1"
    if dte <= 5:
        return "2-5"
    if dte <= 10:
        return "6-10"
    return "11+"
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

    def describe(self) -> str:
        flag = "  [EXTRAPOLATED]" if self.extrapolated else ""
        return (
            f"{self.index_symbol:<10} {self.option_type} dte={self.dte_bucket:<6} "
            f"money={self.moneyness_bucket:<8} mult={self.multiplier:8.2f} "
            f"r2={self.r_squared:5.3f} n={self.n_samples:<6}{flag}"
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
            buckets.setdefault(
                (index_symbol, option_type, dte_bucket, money_bucket), []
            ).append((index_ret, premium_ret))

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
        # Through the origin: a zero index move should imply a zero premium
        # move. An intercept here would silently absorb theta decay into what
        # is meant to be a directional sensitivity.
        slope = float(np.sum(x * y) / np.sum(x * x))
        residual = y - slope * x
        ss_total = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - float(np.sum(residual**2)) / ss_total if ss_total > 0 else 0.0
        fits.append(
            PremiumFit(
                index_symbol=index_symbol,
                option_type=option_type,
                dte_bucket=dte_bucket,
                moneyness_bucket=money_bucket,
                multiplier=slope,
                r_squared=r2,
                n_samples=len(pairs),
                extrapolated=dte_bucket in ("11+", "unknown"),
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
