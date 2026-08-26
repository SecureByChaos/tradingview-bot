from __future__ import annotations

import json
import sqlite3

from scripts.freshness_resolution_check import (
    _context_contradicts_freshness,
    _mentions_freshness,
    run_check,
)


def _make_db():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE ai_origination_logs (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            index_name TEXT NOT NULL,
            decision TEXT NOT NULL,
            reasoning TEXT,
            trade_id TEXT,
            context_json TEXT
        )
        """
    )
    connection.commit()
    return connection


def _insert_log(connection, decision, reasoning, context, trade_id=None,
                 timestamp="2026-08-26T10:00:00", index_name="NIFTY"):
    connection.execute(
        "INSERT INTO ai_origination_logs (timestamp, index_name, decision, reasoning, trade_id, context_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (timestamp, index_name, decision, reasoning, trade_id, json.dumps(context)),
    )


def test_mentions_freshness_matches_the_named_keywords():
    assert _mentions_freshness("the fresh confirmed break makes this a continuation setup")
    assert _mentions_freshness("a newly confirmed breakdown below the range")
    assert not _mentions_freshness("the setup is clean, no conflicting signals")


def test_context_contradicts_freshness_on_high_trend_pct():
    assert _context_contradicts_freshness(json.dumps({"trend_duration_pct_of_session": 100.0}))
    assert not _context_contradicts_freshness(json.dumps({"trend_duration_pct_of_session": 40.0}))


def test_context_contradicts_freshness_on_large_move_extent():
    assert _context_contradicts_freshness(json.dumps({"move_extent_atr": 5.99}))
    assert not _context_contradicts_freshness(json.dumps({"move_extent_atr": 1.2}))


def test_context_missing_both_fields_does_not_contradict():
    assert not _context_contradicts_freshness(json.dumps({}))


def test_run_check_flags_the_exact_trigger_shape(caplog):
    connection = _make_db()
    # The 26 Aug trigger trade's own shape: freshness language + a
    # trend_duration_pct_of_session that directly contradicts it.
    _insert_log(
        connection, "BUY_PE",
        "The earlier 5.99 ATR run adds exhaustion risk, but the fresh confirmed "
        "break and continued negative drift make the bearish continuation case "
        "the clearest setup right now.",
        {"trend_duration_pct_of_session": 100.0, "move_extent_atr": 5.99},
        trade_id="t1",
    )
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "FLAGGED" in messages and ": 1" in messages
    assert "t1" in messages


def test_run_check_does_not_flag_freshness_language_with_low_trend_pct(caplog):
    connection = _make_db()
    _insert_log(
        connection, "BUY_CE",
        "A fresh confirmed breakout above the opening range, ADX developing.",
        {"trend_duration_pct_of_session": 15.0, "move_extent_atr": 0.8},
    )
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "FLAGGED" in messages
    assert "No candidate violations found" in messages


def test_run_check_does_not_flag_high_trend_pct_without_freshness_language(caplog):
    connection = _make_db()
    _insert_log(
        connection, "NONE",
        "The trend has run all session and there is no reason to override that caution.",
        {"trend_duration_pct_of_session": 100.0},
    )
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "No candidate violations found" in messages


def test_since_filter_excludes_earlier_rows(caplog):
    connection = _make_db()
    _insert_log(
        connection, "BUY_PE", "a fresh confirmed break",
        {"trend_duration_pct_of_session": 100.0}, timestamp="2026-08-10T10:00:00",
    )
    _insert_log(
        connection, "BUY_PE", "a fresh confirmed break",
        {"trend_duration_pct_of_session": 100.0}, timestamp="2026-08-27T10:00:00",
    )
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since="2026-08-26")

    messages = "\n".join(r.message for r in caplog.records)
    assert "Total decisions with reasoning: 1" in messages
