from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.freshness_resolution_check import (
    _bootstrap_mean_diff,
    _context_contradicts_freshness,
    _load_trade_entries,
    _mentions_freshness,
    run_check,
    run_outcome_backtest,
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
    connection.execute(
        """
        CREATE TABLE strategy_trades (
            trade_id TEXT PRIMARY KEY,
            entry_time TEXT NOT NULL,
            entry_price REAL,
            origin TEXT NOT NULL,
            status TEXT NOT NULL,
            ai_reasoning TEXT,
            market_context_json TEXT,
            pnl_percent REAL,
            result TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE strategy_trade_ticks (
            trade_id TEXT NOT NULL,
            premium REAL NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _insert_trade(
    connection, trade_id, reasoning, context, pnl_percent, result,
    entry_price=100.0, entry_time="2026-08-26T10:00:00", origin="AI_ORIGIN_OPENAI",
    ticks=None,
):
    connection.execute(
        "INSERT INTO strategy_trades "
        "(trade_id, entry_time, entry_price, origin, status, ai_reasoning, market_context_json, pnl_percent, result) "
        "VALUES (?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?)",
        (trade_id, entry_time, entry_price, origin, reasoning, json.dumps(context), pnl_percent, result),
    )
    for premium in (ticks or [entry_price]):
        connection.execute(
            "INSERT INTO strategy_trade_ticks (trade_id, premium) VALUES (?, ?)", (trade_id, premium)
        )


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


def test_run_check_does_not_flag_a_none_decision_that_negates_freshness(caplog):
    # 26 Aug 2026, first real production run: 38/46 "flagged" decisions were
    # every one a NONE decline whose reasoning used freshness language
    # NEGATED ("there is no fresh breakout") to correctly justify NOT
    # trading -- exactly the prompt working as intended, not a violation of
    # it. A bare "fresh" substring match can't distinguish "a fresh confirmed
    # break" (the real violation shape) from "no fresh breakout" (a correct
    # decline); requiring decision to be BUY_CE/BUY_PE is the fix, since a
    # NONE decision can never violate "don't trade on a contradicted fresh
    # framing" -- nothing was traded.
    connection = _make_db()
    _insert_log(
        connection, "NONE",
        "ADX and Supertrend are supportive, but there is no fresh breakout: price "
        "is still inside the opening range and the move has already run the "
        "whole session and 9.94 ATR, which makes continuation risky.",
        {"trend_duration_pct_of_session": 100.0, "move_extent_atr": 9.94},
    )
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "No candidate violations found" in messages


def test_run_check_excludes_none_and_error_decisions_from_the_trade_count(caplog):
    connection = _make_db()
    _insert_log(connection, "NONE", "a fresh confirmed break", {"trend_duration_pct_of_session": 100.0})
    _insert_log(connection, "ERROR", "a fresh confirmed break", {"trend_duration_pct_of_session": 100.0})
    _insert_log(
        connection, "BUY_CE", "a fresh confirmed break",
        {"trend_duration_pct_of_session": 100.0}, trade_id="t1",
    )
    connection.commit()

    with caplog.at_level("INFO"):
        run_check(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "Total decisions with reasoning: 3" in messages
    assert "decisions that opened a trade (BUY_CE/BUY_PE): 1" in messages
    assert "FLAGGED" in messages and ": 1" in messages


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


# ---------------------------------------------------------------------------
# Outcome backtest (26 Aug 2026 -- evaluate the flag against real P&L, not
# just count occurrences)
# ---------------------------------------------------------------------------

def test_load_trade_entries_flags_the_trigger_trade_shape():
    connection = _make_db()
    _insert_trade(
        connection, "t1",
        "the fresh confirmed break and continued negative drift make the bearish "
        "continuation case the clearest setup right now.",
        {"trend_duration_pct_of_session": 100.0, "move_extent_atr": 5.99},
        pnl_percent=-13.09, result="LOSS",
    )
    _insert_trade(
        connection, "t2", "a routine setup, no conflicting signals",
        {"trend_duration_pct_of_session": 20.0}, pnl_percent=4.2, result="WIN",
    )
    connection.commit()

    entries = {e.trade_id: e for e in _load_trade_entries(connection, since=None)}
    assert entries["t1"].flagged is True
    assert entries["t2"].flagged is False


def test_load_trade_entries_derives_mae_from_ticks():
    connection = _make_db()
    _insert_trade(
        connection, "t1", "a fresh confirmed break",
        {"trend_duration_pct_of_session": 100.0}, pnl_percent=-13.09, result="LOSS",
        entry_price=100.0, ticks=[100.0, 90.0, 105.0, 86.91],
    )
    connection.commit()

    entries = _load_trade_entries(connection, since=None)
    assert len(entries) == 1
    # low tick 86.91 vs entry 100.0 -> -13.09% MAE, matching the real trigger trade
    assert entries[0].mae_percent == pytest.approx(-13.09)


def test_load_trade_entries_excludes_non_ai_origination_and_open_trades():
    connection = _make_db()
    _insert_trade(
        connection, "signal-trade", "a fresh confirmed break",
        {"trend_duration_pct_of_session": 100.0}, pnl_percent=-5.0, result="LOSS",
        origin="SIGNAL",
    )
    connection.execute(
        "INSERT INTO strategy_trades (trade_id, entry_time, entry_price, origin, status, ai_reasoning, "
        "market_context_json, pnl_percent, result) VALUES "
        "('open-trade', '2026-08-26T10:00:00', 100.0, 'AI_ORIGIN_OPENAI', 'OPEN', 'a fresh confirmed break', "
        "'{}', NULL, NULL)"
    )
    connection.commit()

    assert _load_trade_entries(connection, since=None) == []


def test_load_trade_entries_since_filter():
    connection = _make_db()
    _insert_trade(
        connection, "old", "a fresh confirmed break", {"trend_duration_pct_of_session": 100.0},
        pnl_percent=-5.0, result="LOSS", entry_time="2026-08-10T10:00:00",
    )
    _insert_trade(
        connection, "new", "a fresh confirmed break", {"trend_duration_pct_of_session": 100.0},
        pnl_percent=-5.0, result="LOSS", entry_time="2026-08-27T10:00:00",
    )
    connection.commit()

    entries = _load_trade_entries(connection, since="2026-08-26")
    assert [e.trade_id for e in entries] == ["new"]


def test_bootstrap_mean_diff_detects_a_real_separated_gap():
    lo, hi = _bootstrap_mean_diff([-10.0, -10.0, -10.0], [10.0, 10.0, 10.0])
    assert lo == hi == -20.0


def test_bootstrap_mean_diff_no_gap_when_groups_overlap_identically():
    lo, hi = _bootstrap_mean_diff([5.0, -5.0, 5.0, -5.0], [5.0, -5.0, 5.0, -5.0])
    assert lo <= 0.0 <= hi


def test_run_outcome_backtest_reports_a_reliable_difference_but_flags_thin_sample(caplog):
    connection = _make_db()
    for i in range(3):
        _insert_trade(
            connection, f"flagged-{i}", "a fresh confirmed break",
            {"trend_duration_pct_of_session": 100.0}, pnl_percent=-10.0, result="LOSS",
        )
    for i in range(3):
        _insert_trade(
            connection, f"clean-{i}", "a routine setup, no conflicts",
            {"trend_duration_pct_of_session": 20.0}, pnl_percent=10.0, result="WIN",
        )
    connection.commit()

    with caplog.at_level("INFO"):
        run_outcome_backtest(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "flagged reliably WORSE" in messages
    assert "below trust minimum" in messages  # n=3 per bucket, well under MIN_BUCKET_LIVE=20


def test_run_outcome_backtest_reports_no_closed_trades_gracefully(caplog):
    connection = _make_db()
    connection.commit()

    with caplog.at_level("INFO"):
        run_outcome_backtest(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "No closed AI Origination trades with reasoning + market context in this window." in messages


def test_run_outcome_backtest_reports_too_few_for_bootstrap_when_one_bucket_is_empty(caplog):
    connection = _make_db()
    _insert_trade(
        connection, "t1", "a fresh confirmed break",
        {"trend_duration_pct_of_session": 100.0}, pnl_percent=-10.0, result="LOSS",
    )
    connection.commit()

    with caplog.at_level("INFO"):
        run_outcome_backtest(connection, since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "Too few observations in one bucket for a bootstrap comparison." in messages
