from __future__ import annotations

from sqlalchemy import create_engine, event, text


def _build_hardened_sqlite_engine(path: str):
    """Mirrors app.database's own engine construction exactly, against a
    scratch file, so this test doesn't depend on -- or risk interfering
    with -- the real module-level engine's shared file/connection pool."""
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    return engine


def test_sqlite_engine_applies_wal_and_busy_timeout(tmp_path):
    """7 Sep 2026: a real production incident (~20 minutes of cascading
    "database is locked" errors, ending in systemd SIGKILLing the process)
    traced to six-plus scheduler jobs -- including a new 5-second interval
    job -- all writing to one SQLite file with no busy_timeout and the
    default rollback-journal mode, which blocks every reader while a
    writer holds the lock. Confirms the fix actually takes effect on a real
    connection, not just that the code runs without raising."""
    engine = _build_hardened_sqlite_engine(str(tmp_path / "test.db"))
    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
        synchronous = conn.execute(text("PRAGMA synchronous")).scalar()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 10000
    assert synchronous == 1  # NORMAL


def test_sqlite_pragmas_applied_on_every_new_pooled_connection(tmp_path):
    """The event listener fires per-connection (SQLAlchemy's "connect"
    event), not once at engine creation -- confirms a second connection
    from the same engine (e.g. after the pool recycles one) still gets the
    same settings, not just the first one opened."""
    engine = _build_hardened_sqlite_engine(str(tmp_path / "test.db"))
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 10000


def test_real_app_database_engine_has_the_same_pragmas_applied():
    """Guards against the fix drifting out of sync with app.database's own
    real, live engine -- this is the object actually used in production,
    not just a reconstruction of it."""
    from app.database import engine

    if not str(engine.url).startswith("sqlite"):
        return  # this project only hardens the SQLite path; nothing to check otherwise
    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert journal_mode.lower() == "wal"
    assert busy_timeout == 10000
