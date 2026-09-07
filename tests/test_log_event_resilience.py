from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, PendingRollbackError
from sqlalchemy.orm import Session

from app.db_models import Base, LogEvent
from app.platform import log_event


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def test_log_event_normal_write_is_unaffected():
    db = _make_session()
    log_event(db, "TEST", "hello", payload={"a": 1})

    rows = db.query(LogEvent).all()
    assert len(rows) == 1
    assert rows[0].message == "hello"
    assert rows[0].event_type == "TEST"


def test_log_event_recovers_from_a_poisoned_session(monkeypatch):
    """7 Sep 2026 production incident: a job's own except-block calls this
    to record the error that just happened, but if the session's current
    transaction was already invalidated by an earlier DBAPI failure (a real
    SQLite "database is locked" contention, in the incident), SQLAlchemy
    refuses any further work on it until an explicit rollback -- so the old
    unconditional add()/commit() raised its OWN PendingRollbackError,
    hiding the real error behind "Also failed to log the above failure."
    Reproduces the exact exception SQLAlchemy raises for this
    (Connection._invalid_transaction, error code 8s2b) via a controlled
    fake rather than a real file-locking race, matching this codebase's
    established style for testing retry/resilience logic."""
    db = _make_session()
    calls = {"n": 0}
    real_commit = db.commit
    rollback_calls = {"n": 0}
    real_rollback = db.rollback

    def _flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise PendingRollbackError(
                "Can't reconnect until invalid transaction is rolled back.  "
                "Please rollback() fully before proceeding",
                code="8s2b",
            )
        return real_commit()

    def _counting_rollback():
        rollback_calls["n"] += 1
        return real_rollback()

    monkeypatch.setattr(db, "commit", _flaky_commit)
    monkeypatch.setattr(db, "rollback", _counting_rollback)

    log_event(db, "ERROR", "recovered after poisoning")

    assert rollback_calls["n"] == 1
    assert calls["n"] == 2
    rows = db.query(LogEvent).all()
    assert len(rows) == 1
    assert rows[0].message == "recovered after poisoning"


def test_log_event_also_recovers_from_a_raw_operational_error(monkeypatch):
    """The other real shape from the incident: log_event's OWN commit hits
    "database is locked" directly (not via a prior-statement-poisoned
    session) -- e.g. another connection still holds the write lock at the
    exact moment this tries to commit. Same rollback-and-retry path."""
    db = _make_session()
    calls = {"n": 0}
    real_commit = db.commit

    def _flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("database is locked", None, None)
        return real_commit()

    monkeypatch.setattr(db, "commit", _flaky_commit)

    log_event(db, "ERROR", "recovered from raw lock")

    rows = db.query(LogEvent).all()
    assert len(rows) == 1


def test_log_event_gives_up_quietly_if_the_retry_also_fails(monkeypatch, caplog):
    """This is the safety-net logging path itself -- it must never raise
    out of an except-block that's already handling a real failure, even if
    it genuinely can't recover (e.g. the connection is truly gone). Nothing
    ever reaches a durable commit in this case (both attempts fail before
    COMMIT), which is the correct, honest outcome -- the assertion here is
    only that the caller is never interrupted by a second exception."""
    db = _make_session()

    def _always_fails():
        raise PendingRollbackError("still broken", code="8s2b")

    monkeypatch.setattr(db, "commit", _always_fails)

    with caplog.at_level("ERROR"):
        log_event(db, "ERROR", "should not raise")  # must not raise

    assert "Failed to persist log event after retry" in caplog.text
