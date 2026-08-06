"""The AI Origination decision log must capture declines, not just trades.

NONE is the majority of what AI Origination produces, and until this table it
left no queryable trace -- only a debug log line. So the tests that matter are
the ones asserting a NONE and an ERROR write a complete row, not the happy path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.db_models import AIOriginationLog
from app.ai.origination_log import record_decision


@dataclass(frozen=True)
class FakeDecision:
    action: str
    confidence: float | None
    sl_percent: float | None
    target_percent: float | None
    reasoning: str
    latency_ms: float | None = None


@dataclass
class FakeCPR:
    classification: str = "NARROW"


@dataclass
class FakeContext:
    regime: str = "TREND"
    adx: float | None = 27.4
    cpr: FakeCPR | None = None
    setups: dict | None = None
    trend_duration_bars: int | None = 18
    trend_duration_pct_of_session: float | None = 41.5
    move_extent_atr: float | None = 3.2
    same_direction_entries_today: dict | None = None

    def as_dict(self):
        return {"regime": self.regime, "adx": self.adx}


def _context() -> FakeContext:
    return FakeContext(
        cpr=FakeCPR(),
        setups={"EMA_STACK_UP": True, "ORB_BREAK_UP": True, "PDH_BREAK": False},
        same_direction_entries_today={"BUY_CE": 3, "BUY_PE": 0},
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[AIOriginationLog.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        yield db


def _only_row(db) -> AIOriginationLog:
    rows = list(db.scalars(select(AIOriginationLog)))
    assert len(rows) == 1
    return rows[0]


def test_none_decision_is_recorded_with_its_reasoning(session):
    """The original gap. A declined cycle used to leave nothing behind, so a
    whole session of NONE was indistinguishable from a broken provider."""
    record_decision(
        session,
        index_symbol="BANKNIFTY",
        provider="claude",
        provider_role="secondary",
        decision=FakeDecision("NONE", 0.2, None, None, "Trend already mature; declining."),
        market_context=_context(),
        data_stale=False,
        trade=None,
    )
    row = _only_row(session)
    assert row.decision == "NONE"
    assert row.reasoning == "Trend already mature; declining."
    assert row.trade_id is None


def test_trend_age_fields_are_persisted(session):
    """The reason this table exists. Without these, a NONE during a genuine
    TREND regime cannot be attributed to the trend-age caution after the fact --
    which is exactly what happened on 6 Aug."""
    record_decision(
        session, index_symbol="BANKNIFTY", provider="claude", provider_role="primary",
        decision=FakeDecision("NONE", None, None, None, ""),
        market_context=_context(), data_stale=False, trade=None,
    )
    row = _only_row(session)
    assert row.trend_duration_bars == 18
    assert row.trend_duration_pct_of_session == 41.5
    assert row.move_extent_atr == 3.2


def test_entry_counts_are_stored_per_side(session):
    """Split by side rather than a single "same direction" integer: at decision
    time the direction is not yet known, so one number is undefined for the
    NONE rows this table exists to capture."""
    record_decision(
        session, index_symbol="NIFTY", provider="openai", provider_role="primary",
        decision=FakeDecision("NONE", None, None, None, ""),
        market_context=_context(), data_stale=False, trade=None,
    )
    row = _only_row(session)
    assert row.same_direction_entries_ce == 3
    assert row.same_direction_entries_pe == 0


def test_error_populates_error_detail_with_the_real_cause(session):
    """Mirrors the 5 Aug fix: the specific reason must be queryable, not just
    the word ERROR."""
    cause = "Claude returned no text content (stop_reason='max_tokens', usage={...})"
    record_decision(
        session, index_symbol="NIFTY", provider="claude", provider_role="secondary",
        decision=FakeDecision("ERROR", None, None, None, cause, latency_ms=1830.5),
        market_context=_context(), data_stale=True, trade=None,
    )
    row = _only_row(session)
    assert row.decision == "ERROR"
    assert row.error_detail == cause
    assert row.latency_ms == 1830.5
    assert row.data_stale is True


def test_only_active_setups_are_stored(session):
    """Matches what the [CTX] log line shows, so the table and the journal
    cannot disagree while both are being read during the observation window."""
    record_decision(
        session, index_symbol="BANKNIFTY", provider="openai", provider_role="primary",
        decision=FakeDecision("NONE", None, None, None, ""),
        market_context=_context(), data_stale=False, trade=None,
    )
    assert _only_row(session).setups == '["EMA_STACK_UP", "ORB_BREAK_UP"]'


def test_a_failed_write_does_not_raise_into_the_trading_cycle(session):
    """A logging table must never be able to stop a cycle. regime is NOT NULL,
    so a context missing it would violate the constraint -- and that must
    surface as a swallowed error, not an exception escaping into the caller."""
    broken = FakeContext(cpr=FakeCPR(), setups={}, same_direction_entries_today={})
    broken.as_dict = lambda: (_ for _ in ()).throw(ValueError("context blew up"))
    record_decision(
        session, index_symbol="NIFTY", provider="openai", provider_role="primary",
        decision=FakeDecision("NONE", None, None, None, ""),
        market_context=broken, data_stale=False, trade=None,
    )
    assert list(session.scalars(select(AIOriginationLog))) == []
