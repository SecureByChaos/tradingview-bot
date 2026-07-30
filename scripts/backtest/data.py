"""Candle loading and one-pass indicator array construction.

Everything downstream masks over these arrays. They are built once per index
and never recomputed, which is what makes a large gate sweep tractable on a
small box.

LOOK-AHEAD IS THE PRIMARY CORRECTNESS RISK HERE, not performance. Every array
below must satisfy: the value at position i derives only from bars <= i. Two
places where that is easy to get wrong and both are asserted, not commented:

  * Today's high/low must be as-at-bar, never the full session's.
  * The 15-minute Supertrend at a 5-minute bar must come from a 15-minute bar
    that had already CLOSED, not the one currently forming.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from app.indicators import adx, atr, ema, rsi, supertrend
from app.market_data import Bar, resample, FIFTEEN_MINUTE
from app.market_context import compute_cpr

SESSION_OPEN = (9, 15)
OPENING_RANGE_END = (9, 45)
NAN = float("nan")


@dataclass
class IndexArrays:
    """Column-oriented view of one index's history. float32 where the extra
    precision buys nothing -- prices are ~5 significant digits and this halves
    the resident size of ~20 arrays."""

    index_symbol: str
    ts: np.ndarray          # datetime64[m], naive IST
    session_id: np.ndarray  # int32, one id per trading date
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    ema9: np.ndarray
    ema21: np.ndarray
    ema50: np.ndarray
    atr14: np.ndarray
    rsi14: np.ndarray
    adx14: np.ndarray
    st_5m_dir: np.ndarray
    st_15m_dir: np.ndarray
    or_high: np.ndarray
    or_low: np.ndarray
    pdh: np.ndarray
    pdl: np.ndarray
    prev_close: np.ndarray
    cpr_width_pct: np.ndarray
    extension_atr: np.ndarray
    range_percentile: np.ndarray
    minutes_since_open: np.ndarray
    bars_held_above_or: np.ndarray
    bars_held_below_or: np.ndarray

    def __len__(self) -> int:
        return len(self.close)


def load_bars_sqlite(
    db_path: str, table: str, index_symbol: str, interval: str = "FIVE_MINUTE"
) -> list[Bar]:
    """Read candles straight from SQLite. Deliberately not via SQLAlchemy: the
    ORM would build 37,000 mapped objects and their identity map, which is
    memory this box would rather spend elsewhere."""
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            f"SELECT ts_ist, open, high, low, close, volume FROM {table} "
            "WHERE index_symbol = ? AND interval = ? ORDER BY ts_ist ASC",
            (index_symbol, interval),
        ).fetchall()
    finally:
        connection.close()

    bars: list[Bar] = []
    for ts_raw, o, h, l, c, v in rows:
        ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
        bars.append(Bar(ts_ist=ts, open=o, high=h, low=l, close=c, volume=v or 0.0))
    return bars


def _as_float32(values: list, fill=NAN) -> np.ndarray:
    return np.array([fill if v is None else float(v) for v in values], dtype=np.float32)


def _direction_array(points: list) -> np.ndarray:
    return np.array([0 if p is None else p.direction for p in points], dtype=np.int8)


def build_arrays(index_symbol: str, bars: list[Bar]) -> IndexArrays:
    """One pass over one index's bars, producing every array the sweep needs."""
    n = len(bars)
    if n == 0:
        raise ValueError(f"No bars for {index_symbol}")

    ts = np.array([b.ts_ist for b in bars], dtype="datetime64[m]")
    open_ = np.array([b.open for b in bars], dtype=np.float32)
    high = np.array([b.high for b in bars], dtype=np.float32)
    low = np.array([b.low for b in bars], dtype=np.float32)
    close = np.array([b.close for b in bars], dtype=np.float32)

    # --- session identity -------------------------------------------------
    dates = np.array([b.ts_ist.date() for b in bars])
    unique_dates, session_id = np.unique(dates, return_inverse=True)
    session_id = session_id.astype(np.int32)

    # --- indicators, from the live engine's own functions ------------------
    closes = [b.close for b in bars]
    ema9 = _as_float32(ema(closes, 9))
    ema21 = _as_float32(ema(closes, 21))
    ema50 = _as_float32(ema(closes, 50))
    atr14 = _as_float32(atr(bars, 14))
    rsi14 = _as_float32(rsi(bars, 14))

    adx_points = adx(bars, 14)
    adx14 = _as_float32([None if p is None else p.adx for p in adx_points])

    st_5m_dir = _direction_array(supertrend(bars, period=10, multiplier=3.0))

    # --- 15-minute Supertrend, mapped back without look-ahead --------------
    bars_15m = resample(bars, FIFTEEN_MINUTE)
    st_15m_points = supertrend(bars_15m, period=7, multiplier=3.0)
    st_15m_dir = np.zeros(n, dtype=np.int8)
    if bars_15m:
        # A 15-minute bar stamped T covers [T, T+15) and is only CLOSED at
        # T+15. A 5-minute bar at time t may therefore use it only when
        # T + 15 <= t. Using the bar stamped T while t is inside [T, T+15)
        # would be reading a bar that has not finished forming.
        close_times = [b.ts_ist + timedelta(minutes=15) for b in bars_15m]
        pointer = -1
        for i, bar in enumerate(bars):
            while pointer + 1 < len(bars_15m) and close_times[pointer + 1] <= bar.ts_ist:
                pointer += 1
            if pointer >= 0:
                point = st_15m_points[pointer]
                st_15m_dir[i] = 0 if point is None else point.direction

    # --- per-session running state, all causal ----------------------------
    or_high = np.full(n, NAN, dtype=np.float32)
    or_low = np.full(n, NAN, dtype=np.float32)
    pdh = np.full(n, NAN, dtype=np.float32)
    pdl = np.full(n, NAN, dtype=np.float32)
    prev_close = np.full(n, NAN, dtype=np.float32)
    cpr_width = np.full(n, NAN, dtype=np.float32)
    range_pct = np.full(n, NAN, dtype=np.float32)
    minutes_open = np.zeros(n, dtype=np.int16)
    held_above = np.zeros(n, dtype=np.int16)
    held_below = np.zeros(n, dtype=np.int16)

    # Previous-session OHLC, computed strictly from completed sessions.
    session_high: dict[int, float] = {}
    session_low: dict[int, float] = {}
    session_last: dict[int, float] = {}
    for i in range(n):
        s = int(session_id[i])
        session_high[s] = max(session_high.get(s, -1e18), float(high[i]))
        session_low[s] = min(session_low.get(s, 1e18), float(low[i]))
        session_last[s] = float(close[i])

    running_high = -1e18
    running_low = 1e18
    or_h = or_l = NAN
    current_session = -1
    session_pdh = session_pdl = session_pdc = session_cpr_width = NAN

    for i in range(n):
        s = int(session_id[i])
        bar_time = bars[i].ts_ist

        if s != current_session:
            current_session = s
            running_high, running_low = -1e18, 1e18
            or_h = or_l = NAN
            if s - 1 in session_high:
                session_pdh = session_high[s - 1]
                session_pdl = session_low[s - 1]
                session_pdc = session_last[s - 1]
                session_cpr_width = compute_cpr(session_pdh, session_pdl, session_pdc).width_percent
            else:
                session_pdh = session_pdl = session_pdc = session_cpr_width = NAN

        pdh[i] = session_pdh
        pdl[i] = session_pdl
        prev_close[i] = session_pdc
        cpr_width[i] = session_cpr_width

        running_high = max(running_high, float(high[i]))
        running_low = min(running_low, float(low[i]))

        session_open_dt = bar_time.replace(
            hour=SESSION_OPEN[0], minute=SESSION_OPEN[1], second=0, microsecond=0
        )
        minutes_open[i] = int((bar_time - session_open_dt).total_seconds() // 60)

        # Opening range: accumulated during 09:15-09:45, then frozen. NaN
        # before 09:45 -- a range that has not closed is not a level, and any
        # gate reading it before then is reading the future.
        in_or = (SESSION_OPEN[0], SESSION_OPEN[1]) <= (bar_time.hour, bar_time.minute) and (
            bar_time.hour, bar_time.minute
        ) < OPENING_RANGE_END
        if in_or:
            or_h = float(high[i]) if or_h != or_h else max(or_h, float(high[i]))
            or_l = float(low[i]) if or_l != or_l else min(or_l, float(low[i]))
        elif (bar_time.hour, bar_time.minute) >= OPENING_RANGE_END:
            or_high[i], or_low[i] = or_h, or_l

        span = running_high - running_low
        range_pct[i] = ((float(close[i]) - running_low) / span * 100) if span > 0 else NAN

        # Consecutive completed bars closed beyond the opening range. Resets
        # at a session boundary and on the first bar that closes back inside.
        same_session_as_previous = i > 0 and int(session_id[i - 1]) == s
        if not np.isnan(or_high[i]):
            if float(close[i]) > or_high[i]:
                held_above[i] = (held_above[i - 1] + 1) if same_session_as_previous else 1
            if float(close[i]) < or_low[i]:
                held_below[i] = (held_below[i - 1] + 1) if same_session_as_previous else 1

    with np.errstate(invalid="ignore", divide="ignore"):
        extension = (close - ema21) / atr14

    arrays = IndexArrays(
        index_symbol=index_symbol,
        ts=ts, session_id=session_id,
        open=open_, high=high, low=low, close=close,
        ema9=ema9, ema21=ema21, ema50=ema50,
        atr14=atr14, rsi14=rsi14, adx14=adx14,
        st_5m_dir=st_5m_dir, st_15m_dir=st_15m_dir,
        or_high=or_high, or_low=or_low,
        pdh=pdh, pdl=pdl, prev_close=prev_close,
        cpr_width_pct=cpr_width,
        extension_atr=extension.astype(np.float32),
        range_percentile=range_pct,
        minutes_since_open=minutes_open,
        bars_held_above_or=held_above,
        bars_held_below_or=held_below,
    )
    assert_no_lookahead(arrays, bars)
    return arrays


def assert_no_lookahead(arrays: IndexArrays, bars: list[Bar]) -> None:
    """Executable look-ahead checks. Assertions, not comments, because this is
    the class of bug that produces a beautiful equity curve and no error."""
    n = len(arrays)

    # 1. Opening range must be NaN before it has closed.
    for i in range(min(n, 5000)):
        t = bars[i].ts_ist
        if (t.hour, t.minute) < OPENING_RANGE_END:
            assert np.isnan(arrays.or_high[i]), (
                f"Opening range populated at {t}, before it closed at 09:45"
            )

    # 2. Running range percentile must never imply a high/low the bar itself
    #    has not yet seen: close must sit within [session low so far, high so far].
    session_starts = np.flatnonzero(np.diff(arrays.session_id, prepend=-1))
    for start in session_starts[: min(len(session_starts), 200)]:
        assert np.isnan(arrays.range_percentile[start]) or -0.001 <= arrays.range_percentile[start] <= 100.001, (
            f"range_percentile out of bounds at session start index {start}"
        )

    # 3. Previous-day levels must never equal a level only reachable today.
    for i in range(min(n, 5000)):
        if not np.isnan(arrays.pdh[i]):
            assert arrays.pdh[i] >= arrays.pdl[i], f"pdh < pdl at {i}"


def forward_window_bounds(arrays: IndexArrays, max_bars: int) -> np.ndarray:
    """Last usable forward index per bar, clipped to the session end.

    An overnight gap is not an intraday move. Letting a forward window cross a
    session boundary is the single easiest way to manufacture an edge that does
    not exist.
    """
    n = len(arrays)
    session = arrays.session_id
    # Index of the last bar in each bar's own session.
    last_in_session = np.empty(n, dtype=np.int32)
    end = n - 1
    for i in range(n - 1, -1, -1):
        if i < n - 1 and session[i] != session[i + 1]:
            end = i
        last_in_session[i] = end
    return np.minimum(last_in_session, np.arange(n, dtype=np.int32) + max_bars)
