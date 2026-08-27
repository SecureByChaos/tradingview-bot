"""build_market_context must actually thread compute_efficiency_ratio through
to the returned MarketContext -- the isolated unit tests in
tests/test_efficiency_ratio.py cover the formula itself, this covers the
live wiring."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.market_context import build_market_context, compute_efficiency_ratio
from app.market_data import Bar


def _bars(n: int, start: datetime, interval_minutes: int, base: float = 24000.0) -> list[Bar]:
    # A simple upward drift with small in-bar oscillation -- enough for
    # ADX/EMA/Supertrend to warm up without needing a hand-tuned path, since
    # this test only cares whether chop_efficiency_ratio reaches the result,
    # not what specific value it lands on.
    bars = []
    price = base
    for i in range(n):
        price += 1.5 if i % 3 != 0 else -0.5
        ts = start + timedelta(minutes=interval_minutes * i)
        bars.append(Bar(ts_ist=ts, open=price - 1, high=price + 1, low=price - 2, close=price))
    return bars


def test_chop_efficiency_ratio_reaches_the_returned_context():
    start = datetime(2026, 8, 27, 9, 15)
    bars_1m = _bars(80, start, 1)
    bars_5m = _bars(40, start, 5)
    bars_15m = _bars(15, start, 15)
    as_of = start + timedelta(minutes=195)  # well into the session

    context = build_market_context("BANKNIFTY", bars_1m, bars_5m, bars_15m, spot=bars_5m[-1].close, as_of=as_of)

    assert context is not None
    expected = compute_efficiency_ratio(bars_5m)
    assert expected is not None
    assert context.chop_efficiency_ratio == expected


def test_chop_efficiency_ratio_appears_in_as_dict():
    start = datetime(2026, 8, 27, 9, 15)
    bars_1m = _bars(80, start, 1)
    bars_5m = _bars(40, start, 5)
    bars_15m = _bars(15, start, 15)
    as_of = start + timedelta(minutes=195)

    context = build_market_context("BANKNIFTY", bars_1m, bars_5m, bars_15m, spot=bars_5m[-1].close, as_of=as_of)

    assert context is not None
    assert "chop_efficiency_ratio" in context.as_dict()
    assert context.as_dict()["chop_efficiency_ratio"] == context.chop_efficiency_ratio
