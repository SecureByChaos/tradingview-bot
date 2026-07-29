"""Technical indicators over OHLC bar series.

Pure functions, no database and no I/O, so the live path and the backtest run
identical code. That equivalence is the point: a parameter fitted offline is
worthless if live computes it even slightly differently.

Deliberately stdlib-only rather than pandas. pandas is already a dependency
(app/option_finder.py uses it for the scrip master) so this isn't about
avoiding the import -- it's that these run on the 5-minute scheduler tick over
a few hundred bars, where building and tearing down DataFrames costs more than
the arithmetic. The backtest, which processes ~100k bars, may reasonably use
pandas instead; if it does, it must be checked against these on an overlapping
window before its numbers are trusted.

Every function returns a list aligned to the input bars, with None for
positions where there isn't enough history yet. Callers must handle None
rather than assuming a warm series -- a silently-zero indicator is worse than
an absent one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.market_data import Bar


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with an SMA of the first `period`
    values -- the conventional seeding, and what TradingView's ta.ema does, so
    these agree with the Pine scripts the rule-based strategies use."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    previous = seed
    for i in range(period, len(values)):
        previous = (values[i] - previous) * multiplier + previous
        out[i] = previous
    return out


def true_range(bars: list[Bar]) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for i, bar in enumerate(bars):
        if i == 0:
            out[i] = bar.high - bar.low
            continue
        previous_close = bars[i - 1].close
        out[i] = max(
            bar.high - bar.low,
            abs(bar.high - previous_close),
            abs(bar.low - previous_close),
        )
    return out


def atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    """Wilder's ATR (RMA smoothing, not a simple EMA). Wilder smoothing uses
    1/period rather than 2/(period+1); using the wrong one makes ATR-based
    distances and Supertrend bands disagree with every charting package."""
    ranges = true_range(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period:
        return out
    seed = sum(r for r in ranges[:period] if r is not None) / period
    out[period - 1] = seed
    previous = seed
    for i in range(period, len(bars)):
        current = ranges[i] or 0.0
        previous = (previous * (period - 1) + current) / period
        out[i] = previous
    return out


def rsi(bars: list[Bar], period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(bars)
    if len(bars) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(bars)):
        change = bars[i].close - bars[i - 1].close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass(frozen=True)
class ADXPoint:
    adx: float
    plus_di: float
    minus_di: float


def adx(bars: list[Bar], period: int = 14) -> list[ADXPoint | None]:
    """Wilder's ADX with +DI/-DI.

    This is the single most important indicator for the diagnosed failure
    mode. The model has been reading "price is at the session high" as bullish
    evidence; ADX is what distinguishes a session high inside a real trend from
    one at the top of a range. Below 20 there is no trend to continue.

    Note it is deliberately lagging -- ADX typically crosses 20 well after a
    move is underway. It works as a filter against trading in chop, not as an
    entry trigger.
    """
    out: list[ADXPoint | None] = [None] * len(bars)
    # Needs `period` bars to seed the DM/TR smoothing, then a further `period`
    # DX values before the first ADX exists.
    if len(bars) < period * 2:
        return out

    # Index j in these arrays corresponds to bar index j + 1.
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    ranges: list[float] = []
    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        previous_close = bars[i - 1].close
        ranges.append(
            max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - previous_close),
                abs(bars[i].low - previous_close),
            )
        )

    # Wilder seeds with a plain sum of the first `period` values, then smooths.
    smooth_tr = sum(ranges[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])

    dx_values: list[float] = []
    adx_value: float | None = None

    for j in range(period - 1, len(ranges)):
        if j > period - 1:
            smooth_tr = smooth_tr - (smooth_tr / period) + ranges[j]
            smooth_plus = smooth_plus - (smooth_plus / period) + plus_dm[j]
            smooth_minus = smooth_minus - (smooth_minus / period) + minus_dm[j]

        if smooth_tr == 0:
            plus_di = minus_di = 0.0
        else:
            plus_di = 100.0 * smooth_plus / smooth_tr
            minus_di = 100.0 * smooth_minus / smooth_tr

        di_sum = plus_di + minus_di
        dx = 0.0 if di_sum == 0 else 100.0 * abs(plus_di - minus_di) / di_sum
        dx_values.append(dx)

        if len(dx_values) == period:
            adx_value = sum(dx_values) / period
        elif len(dx_values) > period:
            adx_value = ((adx_value or 0.0) * (period - 1) + dx) / period

        if adx_value is not None:
            out[j + 1] = ADXPoint(adx=adx_value, plus_di=plus_di, minus_di=minus_di)
    return out


@dataclass(frozen=True)
class SupertrendPoint:
    value: float
    direction: int  # 1 = uptrend (support below price), -1 = downtrend


def supertrend(bars: list[Bar], period: int = 10, multiplier: float = 3.0) -> list[SupertrendPoint | None]:
    """Supertrend over ATR bands.

    Trend-following of this kind performs in trends and produces false signals
    in ranges -- which is exactly why it is paired with ADX and the CPR regime
    classifier rather than used alone.
    """
    atr_series = atr(bars, period)
    out: list[SupertrendPoint | None] = [None] * len(bars)

    final_upper: float | None = None
    final_lower: float | None = None
    direction = 1

    for i, bar in enumerate(bars):
        current_atr = atr_series[i]
        if current_atr is None:
            continue
        mid = (bar.high + bar.low) / 2
        basic_upper = mid + multiplier * current_atr
        basic_lower = mid - multiplier * current_atr

        previous_close = bars[i - 1].close if i > 0 else bar.close
        if final_upper is None or basic_upper < final_upper or previous_close > final_upper:
            final_upper = basic_upper
        if final_lower is None or basic_lower > final_lower or previous_close < final_lower:
            final_lower = basic_lower

        if bar.close > final_upper:
            direction = 1
        elif bar.close < final_lower:
            direction = -1

        out[i] = SupertrendPoint(
            value=final_lower if direction == 1 else final_upper,
            direction=direction,
        )
    return out


def last_valid(series: list) -> object | None:
    """Most recent non-None value, or None if the series never warmed up."""
    for value in reversed(series):
        if value is not None:
            return value
    return None
