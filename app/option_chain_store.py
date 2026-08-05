"""Storage for raw option-chain snapshots.

WHAT THIS IS FOR
----------------
Two years of price history have been mined for a directional edge and have not
produced one that survives costs. Open interest, implied volatility and
put-call ratio are genuinely different information -- they describe positioning
and expected variance rather than realised direction -- and none of it has been
tested, because none of it has ever been recorded.

It cannot be backfilled. Angel One serves no option-chain history, so the only
way to have a year of it is to have started collecting a year ago. That makes
every week this isn't running a week permanently lost, which is the same
argument that governed the pre-expiry candle pulls.

This module is COLLECTION ONLY. Nothing here scores, thresholds, or interprets
anything, and nothing in the live trading path reads it. Evaluating it needs
months of accumulated history and the same significance machinery the price
setups went through.

WHY A SEPARATE SQLITE FILE
--------------------------
Volume. Two indices x two expiries x 21 strikes x CE/PE is ~168 rows per
snapshot; at 5-minute cadence that is ~12,600 rows per session and ~3M rows a
year, on the order of 300-400 MB with index overhead. The box has 414 MB of
RAM and the live trading database is a few MB.

Putting that in the trading DB would mean a pure-archive table dominating the
file that order placement, risk locks and the dashboard all depend on -- slower
backups, slower integrity checks, and a bloat failure mode that takes live
trading down with it. A separate file keeps the blast radius at "the archive
stopped growing", and makes rotation or offload a file move rather than a
migration.

The cost is that this data cannot be JOINed to trades in SQL. That is
acceptable: it is not meant to be queried alongside live trades, it is meant to
be pulled into a numpy backtest months from now.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("data/option_chain.db")

# SQLite caps bound variables per statement (999 on older builds). At 13
# columns a full snapshot would exceed that in one VALUES clause.
_INSERT_CHUNK_ROWS = 50


class ChainBase(DeclarativeBase):
    """Deliberately NOT app.database.Base.

    Sharing the declarative base would make init_db's create_all() build this
    table inside the trading database as well -- empty, but confusing, and one
    stray import away from being written to.
    """


class OptionChainSnapshot(ChainBase):
    """One strike, one side, at one moment. Raw as received.

    Every field is stored exactly as the broker reported it. No put-call ratio
    column: PCR is a ratio of summed OI and is trivially derived at query time,
    whereas a stored PCR would bake in a choice of strike range that a future
    analysis may not want.
    """

    __tablename__ = "option_chain_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Naive IST, matching the candle store. Truncated to the cycle minute so
    # every strike in one sweep shares a timestamp and a snapshot is a clean
    # WHERE clause rather than a time-window join.
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    index_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry: Mapped[str] = mapped_column(String(16), nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    symboltoken: Mapped[str] = mapped_column(String(32), nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)

    ltp: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Nullable because it comes from a different endpoint than everything else
    # and that endpoint may be unavailable. A row with OI but no IV is still
    # worth keeping, so IV absence must not discard the snapshot.
    implied_volatility: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Underlying at the moment of capture. Recorded per row rather than looked
    # up later because moneyness is only reconstructible against the spot that
    # was live at the time, and the 1-minute candle store may be resampled or
    # pruned before this archive is analysed.
    spot: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        # Makes a re-run of the same cycle idempotent rather than duplicating.
        UniqueConstraint("snapshot_ts", "symboltoken", name="uq_chain_snapshot_token"),
        # The access pattern for analysis is "give me one index's chain over a
        # date range", so lead on index and time.
        Index("ix_chain_index_ts", "index_symbol", "snapshot_ts"),
    )


@dataclass(frozen=True)
class ArchiveStatus:
    rows: int
    first_snapshot: datetime | None
    last_snapshot: datetime | None
    distinct_snapshots: int
    size_bytes: int


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def resolve_path() -> Path:
    """Archive location. Override with OPTION_CHAIN_DB_PATH.

    Worth overriding if data/ ever moves to a mounted volume -- this file grows
    without bound by design, and it is the one thing here that could fill a
    disk the trading system shares.
    """
    return Path(os.getenv("OPTION_CHAIN_DB_PATH", str(DEFAULT_PATH)))


_engine = None
_SessionLocal = None


def get_session_factory():
    """Lazily built so importing this module never creates a file.

    Matters because scripts import it to READ status, and a read-only caller
    should not bring an archive into existence as a side effect.
    """
    global _engine, _SessionLocal
    if _SessionLocal is None:
        path = resolve_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            _database_url(path),
            connect_args={"check_same_thread": False},
            future=True,
        )
        ChainBase.metadata.create_all(bind=_engine)
        _SessionLocal = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _SessionLocal


def store_snapshot(rows: list[dict]) -> int:
    """Persist one sweep. Idempotent on (snapshot_ts, symboltoken).

    Conflicts UPDATE rather than being ignored: a partial sweep that failed
    midway and was retried should end up with the complete, later reading
    rather than a mix.
    """
    if not rows:
        return 0
    factory = get_session_factory()
    with factory() as session:
        for start in range(0, len(rows), _INSERT_CHUNK_ROWS):
            chunk = rows[start : start + _INSERT_CHUNK_ROWS]
            statement = sqlite_insert(OptionChainSnapshot).values(chunk)
            statement = statement.on_conflict_do_update(
                index_elements=[
                    OptionChainSnapshot.snapshot_ts,
                    OptionChainSnapshot.symboltoken,
                ],
                set_={
                    "ltp": statement.excluded.ltp,
                    "open_interest": statement.excluded.open_interest,
                    "volume": statement.excluded.volume,
                    "implied_volatility": statement.excluded.implied_volatility,
                    "spot": statement.excluded.spot,
                },
            )
            session.execute(statement)
        session.commit()
    return len(rows)


def archive_status() -> ArchiveStatus:
    """Row counts and file size, for the status report.

    Size is read off the filesystem rather than estimated, because the point of
    tracking it is to notice growth that estimation would have missed.
    """
    path = resolve_path()
    if not path.exists():
        return ArchiveStatus(0, None, None, 0, 0)
    factory = get_session_factory()
    with factory() as session:
        rows = session.scalar(select(func.count()).select_from(OptionChainSnapshot)) or 0
        first = session.scalar(select(func.min(OptionChainSnapshot.snapshot_ts)))
        last = session.scalar(select(func.max(OptionChainSnapshot.snapshot_ts)))
        distinct = session.scalar(
            select(func.count(func.distinct(OptionChainSnapshot.snapshot_ts)))
        ) or 0
    return ArchiveStatus(
        rows=int(rows),
        first_snapshot=first,
        last_snapshot=last,
        distinct_snapshots=int(distinct),
        size_bytes=path.stat().st_size,
    )
