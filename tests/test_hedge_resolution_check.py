from __future__ import annotations

import sqlite3

from scripts.hedge_resolution_check import run_check


def _make_db():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE ai_origination_logs (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            decision TEXT NOT NULL,
            reasoning TEXT,
            trade_id TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE strategy_trades (
            trade_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            result TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _insert_log(connection, decision, reasoning, trade_id=None, timestamp="2026-08-19T10:00:00"):
    connection.execute(
        "INSERT INTO ai_origination_logs (timestamp, decision, reasoning, trade_id) VALUES (?, ?, ?, ?)",
        (timestamp, decision, reasoning, trade_id),
    )


def _insert_trade(connection, trade_id, status, result):
    connection.execute(
        "INSERT INTO strategy_trades (trade_id, status, result) VALUES (?, ?, ?)",
        (trade_id, status, result),
    )


def test_hedge_then_trade_rate_computed_correctly(caplog):
    connection = _make_db()
    # hedged and traded
    _insert_log(connection, "BUY_CE", "moderate rather than strong, but taking it", "t1")
    # hedged and declined
    _insert_log(connection, "NONE", "already extended, no fresh breakout")
    # not hedged at all
    _insert_log(connection, "NONE", "clean setup, no conflict")
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "Hedged decisions: 2" in messages
    assert "1/2 hedged decisions resulted in a trade" in messages


def test_no_hedged_decisions_reports_na(caplog):
    connection = _make_db()
    _insert_log(connection, "NONE", "clean confirmed setup, developing ADX")
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "HEDGE-THEN-TRADE RATE: n/a" in messages


def test_since_filter_excludes_earlier_rows(caplog):
    connection = _make_db()
    _insert_log(connection, "BUY_CE", "already extended", "t1", timestamp="2026-08-10T10:00:00")
    _insert_log(connection, "NONE", "already extended", timestamp="2026-08-20T10:00:00")
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since="2026-08-15")

    messages = "\n".join(r.message for r in caplog.records)
    assert "Total decisions with reasoning: 1" in messages


def test_win_rate_computed_only_on_closed_trades(caplog):
    connection = _make_db()
    _insert_log(connection, "BUY_CE", "clean setup", "t1")
    _insert_log(connection, "BUY_PE", "clean setup", "t2")
    _insert_log(connection, "BUY_CE", "clean setup", "t3")
    _insert_trade(connection, "t1", "CLOSED", "WIN")
    _insert_trade(connection, "t2", "CLOSED", "LOSS")
    _insert_trade(connection, "t3", "OPEN", "OPEN")
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "Win rate on closed trades from this window: 1/2 (50.0%)" in messages


def test_no_trades_in_window_reports_gracefully(caplog):
    connection = _make_db()
    _insert_log(connection, "NONE", "already extended")
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "No trades opened in this window." in messages


def test_empty_reasoning_excluded(caplog):
    connection = _make_db()
    _insert_log(connection, "NONE", "")
    _insert_log(connection, "NONE", "already extended")
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "Total decisions with reasoning: 1" in messages
