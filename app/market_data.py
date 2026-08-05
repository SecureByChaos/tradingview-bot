"""Candle storage, retrieval and resampling.

The single source of market history for AI Origination's context layer. Reads
and writes app.db_models.Candle; everything above this (indicators, levels,
setups) works on the OHLCV dataclass this module returns, so none of it needs
to know whether a bar came from the exchange directly or from resampling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db_models import Candle

logger = logging.getLogger(__name__)

ONE_MINUTE = "ONE_MINUTE"
FIVE_MINUTE = "FIVE_MINUTE"
FIFTEEN_MINUTE = "FIFTEEN_MINUTE"

# Minutes per interval, for resampling. Kept explicit rather than parsed from
# the name so an unrecognised interval fails loudly instead of silently
# bucketing wrong.
INTERVAL_MINUTES = {
    ONE_MINUTE: 1,
    FIVE_MINUTE: 5,
    FIFTEEN_MINUTE: 15,
    "THIRTY_MINUTE": 30,
    "ONE_HOUR": 60,
}

MARKET_OPEN = (9, 15)

# Rows per INSERT. 8 columns x 100 rows = 800 bound variables, under even the
# conservative 999-variable SQLite build limit.
_INSERT_CHUNK_ROWS = 100


@dataclass(frozen=True)
class Bar:
    ts_ist: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def parse_smartapi_row(row: list) -> Bar:
    """SmartAPI candle row -> Bar.

    Rows arrive as [timestamp, open, high, low, close, volume] with the
    timestamp as an ISO string carrying an IST offset (e.g.
    "2026-07-24T09:15:00+05:30"). Stored naive-IST: the offset is always
    +05:30 for this exchange, so carrying it adds nothing and reintroduces the
    tz round-trip problem SQLite has with aware datetimes.
    """
    raw_ts = str(row[0])
    text = raw_ts.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    naive_ist = parsed.replace(tzinfo=None)
    return Bar(
        ts_ist=naive_ist,
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
    )


def store_bars(db: Session, index_symbol: str, interval: str, bars: list[Bar]) -> int:
    """Idempotent upsert. Re-pulling an overlapping range is safe and is the
    normal case -- backfills are chunked by day and the live path re-requests
    the current partial bar every cycle."""
    if not bars:
        return 0
    payload = [
        {
            "index_symbol": index_symbol,
            "interval": interval,
            "ts_ist": bar.ts_ist,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]
    # Chunked because SQLite caps the number of bound variables per statement
    # (999 on older builds, 32766 on newer). At 8 columns per row a two-year
    # 5-minute backfill is ~37,000 rows -- a single VALUES clause would blow
    # the limit and fail the whole insert.
    for start in range(0, len(payload), _INSERT_CHUNK_ROWS):
        chunk = payload[start : start + _INSERT_CHUNK_ROWS]
        statement = sqlite_insert(Candle).values(chunk)
        statement = statement.on_conflict_do_update(
            index_elements=[Candle.index_symbol, Candle.interval, Candle.ts_ist],
            set_={
                "open": statement.excluded.open,
                "high": statement.excluded.high,
                "low": statement.excluded.low,
                "close": statement.excluded.close,
                "volume": statement.excluded.volume,
            },
        )
        db.execute(statement)
    db.commit()
    return len(payload)


def capture_closing_auction(smartapi, session_factory) -> dict[str, float]:
    """Re-fetch today's candles after the Closing Auction Session has settled.

    WHY THIS JOB HAS TO EXIST
    -------------------------
    From 3 Aug 2026 the official closing value is determined by an auction that
    concludes around 15:35, and it is published as a single bar near 15:29-15:30
    after fifteen minutes with no bars at all. Measured on 3 Aug: Bank Nifty's
    last continuous bar was 57,680.90 at 15:14 and the published close was
    58,247.95 -- 567 points higher. Nifty: 24,573.55 against a 24,774.30 close.

    Nothing in this system was fetching candles after 15:15, because AI
    Origination stops there and it was the only live caller. So the last stored
    bar of each session was the ~15:13-15:15 value and the CAS close was never
    written. 3 Aug only has it by accident, from a manual backfill run that
    evening; 4 Aug's stored close is the 15:13 bar.

    That is not a cosmetic gap. market_context reads the previous session's
    close the next morning to classify CPR and to place the previous-day levels
    in the entry prompt, and a pivot is (H + L + C) / 3 -- so a close wrong by
    567 points moves every derived level. This is the one route by which the
    auction reaches live entry decisions, and it is silent: no error, no gap,
    just a slightly wrong number.

    Returns {index_symbol: stored close} for logging. Never raises -- a failure
    here leaves the previous behaviour (a 15:13 close) rather than breaking
    anything downstream.
    """
    from app.db_models import IndexConfig
    from app.time_utils import to_ist, utc_now

    captured: dict[str, float] = {}
    now_ist = to_ist(utc_now())
    with session_factory() as db:
        indexes = [
            index for index in db.scalars(select(IndexConfig))
            if index.enabled and index.spot_token
        ]
        for index in indexes:
            try:
                rows = smartapi.get_candles(
                    exchange=index.spot_exchange,
                    symboltoken=index.spot_token,
                    interval=ONE_MINUTE,
                    # Today only. The auction bar is the sole reason for this
                    # call, so there is no case for a wider window and every
                    # reason not to spend the rate-limit budget on one.
                    from_dt=now_ist.strftime("%Y-%m-%d 09:15"),
                    to_dt=now_ist.strftime("%Y-%m-%d 15:45"),
                )
            except Exception as exc:
                logger.warning("[CAS] %s: post-auction candle refresh failed: %s", index.symbol, exc)
                continue
            if not rows:
                logger.warning("[CAS] %s: post-auction refresh returned no candles", index.symbol)
                continue
            bars = [parse_smartapi_row(row) for row in rows]
            store_bars(db, index.symbol, ONE_MINUTE, bars)
            last = max(bars, key=lambda bar: bar.ts_ist)
            captured[index.symbol] = last.close
            logger.info(
                "[CAS] %s: session close captured as %.2f from the %s bar (%s bars refreshed)",
                index.symbol, last.close, last.ts_ist.strftime("%H:%M"), len(bars),
            )
    return captured


def load_bars(
    db: Session,
    index_symbol: str,
    interval: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> list[Bar]:
    """Stored bars in ascending time order."""
    query = select(Candle).where(
        Candle.index_symbol == index_symbol,
        Candle.interval == interval,
    )
    if start is not None:
        query = query.where(Candle.ts_ist >= start)
    if end is not None:
        query = query.where(Candle.ts_ist <= end)
    if limit is not None:
        # Newest N, then flipped back to ascending -- "the last 200 bars" is
        # the common need and pulling the whole history to slice it is wasteful
        # once this table holds years of data.
        rows = list(db.scalars(query.order_by(Candle.ts_ist.desc()).limit(limit)))
        rows.reverse()
    else:
        rows = list(db.scalars(query.order_by(Candle.ts_ist.asc())))
    return [
        Bar(row.ts_ist, row.open, row.high, row.low, row.close, row.volume)
        for row in rows
    ]


def _bucket_start(ts: datetime, minutes: int) -> datetime:
    """Bucket a timestamp to its interval start, anchored to the 09:15 open.

    Anchoring matters: naive floor-to-multiple-of-5 on wall-clock minutes would
    put the first bar of the day at 09:15 only by luck, and would misalign
    entirely for 15-minute bars. Aligning to the session open makes resampled
    bars line up with what the exchange itself reports for the same interval,
    which is what makes the equivalence check in scripts/backfill_candles.py
    meaningful rather than noise.
    """
    open_today = ts.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    if ts < open_today:
        open_today -= timedelta(days=1)
    elapsed = int((ts - open_today).total_seconds() // 60)
    return open_today + timedelta(minutes=(elapsed // minutes) * minutes)


def resample(bars: list[Bar], interval: str) -> list[Bar]:
    """1-minute bars -> a coarser interval. OHLC aggregation, volume summed.

    Incomplete trailing buckets are returned as-is rather than dropped: the
    live path genuinely needs the in-progress bar, and callers that require
    only completed bars can discard the last element (see
    `completed_only=True` in build_market_context)."""
    minutes = INTERVAL_MINUTES.get(interval)
    if minutes is None:
        raise ValueError(f"Unsupported resample interval: {interval}")
    if minutes == 1:
        return list(bars)

    out: list[Bar] = []
    bucket: list[Bar] = []
    bucket_ts: datetime | None = None
    for bar in bars:
        ts = _bucket_start(bar.ts_ist, minutes)
        if bucket_ts is None or ts != bucket_ts:
            if bucket:
                out.append(_aggregate(bucket, bucket_ts))
            bucket = [bar]
            bucket_ts = ts
        else:
            bucket.append(bar)
    if bucket and bucket_ts is not None:
        out.append(_aggregate(bucket, bucket_ts))
    return out


def _aggregate(bucket: list[Bar], ts: datetime) -> Bar:
    return Bar(
        ts_ist=ts,
        open=bucket[0].open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
        volume=sum(b.volume for b in bucket),
    )


def latest_bar_time(db: Session, index_symbol: str, interval: str) -> datetime | None:
    return db.scalar(
        select(Candle.ts_ist)
        .where(Candle.index_symbol == index_symbol, Candle.interval == interval)
        .order_by(Candle.ts_ist.desc())
        .limit(1)
    )


def prune_before(db: Session, cutoff: datetime, interval: str = ONE_MINUTE) -> int:
    """Drop 1-minute bars older than cutoff. Retention is a deliberate decision,
    not a default: keep at least 30 days so offline replay stays possible."""
    result = db.execute(
        delete(Candle).where(Candle.interval == interval, Candle.ts_ist < cutoff)
    )
    db.commit()
    return result.rowcount or 0
