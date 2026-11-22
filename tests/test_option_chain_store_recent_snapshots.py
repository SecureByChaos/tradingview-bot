"""recent_snapshots() -- the first real live consumer of the option-chain
archive (app.ai.agentic's Flow Confirmation Gate, 22 Nov 2026). Resets the
module's cached engine/session-factory globals around each test so this
file's scratch DATABASE never leaks into another test module's use of the
same archive -- see app.option_chain_store.get_session_factory's own
"lazily built" docstring for why those globals exist at all.
"""

from datetime import datetime, timedelta

import pytest

import app.option_chain_store as chain_store


@pytest.fixture(autouse=True)
def _scratch_chain_db(tmp_path, monkeypatch):
    db_path = tmp_path / "option_chain.db"
    monkeypatch.setenv("OPTION_CHAIN_DB_PATH", str(db_path))
    monkeypatch.setattr(chain_store, "_engine", None)
    monkeypatch.setattr(chain_store, "_SessionLocal", None)
    yield
    monkeypatch.setattr(chain_store, "_engine", None)
    monkeypatch.setattr(chain_store, "_SessionLocal", None)


def _row(snapshot_ts, strike, option_type, oi, symbol="BANKNIFTY", expiry="27NOV2026", spot=57000.0):
    return {
        "snapshot_ts": snapshot_ts,
        "index_symbol": symbol,
        "expiry": expiry,
        "strike": strike,
        "option_type": option_type,
        "symboltoken": f"{strike}{option_type}",
        "tradingsymbol": f"{symbol}{expiry}{strike}{option_type}",
        "ltp": 100.0,
        "open_interest": oi,
        "volume": 1000.0,
        "implied_volatility": 15.0,
        "spot": spot,
    }


def test_returns_empty_list_when_no_archive_file_exists(tmp_path, monkeypatch):
    missing_path = tmp_path / "does_not_exist.db"
    monkeypatch.setenv("OPTION_CHAIN_DB_PATH", str(missing_path))
    rows = chain_store.recent_snapshots("BANKNIFTY", datetime(2026, 11, 20), datetime(2026, 11, 20, 1))
    assert rows == []
    # Checking must not have created the file as a side effect.
    assert not missing_path.exists()


def test_returns_empty_list_when_table_has_no_matching_rows():
    now = datetime(2026, 11, 20, 10, 0)
    chain_store.store_snapshot([_row(now, 57000, "CE", 1000)])
    rows = chain_store.recent_snapshots("NIFTY", now - timedelta(hours=1), now + timedelta(hours=1))
    assert rows == []


def test_filters_by_index_symbol_and_time_window():
    t1 = datetime(2026, 11, 20, 10, 0)
    t2 = datetime(2026, 11, 20, 10, 5)
    chain_store.store_snapshot([
        _row(t1, 57000, "CE", 1000, symbol="BANKNIFTY"),
        _row(t1, 23000, "CE", 2000, symbol="NIFTY"),
        _row(t2 - timedelta(hours=2), 57000, "CE", 500, symbol="BANKNIFTY"),  # outside window
    ])
    rows = chain_store.recent_snapshots("BANKNIFTY", t1 - timedelta(minutes=1), t2)
    assert len(rows) == 1
    assert rows[0].index_symbol == "BANKNIFTY"
    assert rows[0].open_interest == 1000


def test_rows_returned_oldest_first():
    older = datetime(2026, 11, 20, 10, 0)
    newer = datetime(2026, 11, 20, 10, 5)
    chain_store.store_snapshot([_row(newer, 57000, "CE", 1200)])
    chain_store.store_snapshot([_row(older, 57000, "CE", 1000)])
    rows = chain_store.recent_snapshots("BANKNIFTY", older - timedelta(minutes=1), newer + timedelta(minutes=1))
    assert [r.snapshot_ts for r in rows] == [older, newer]
