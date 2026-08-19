from __future__ import annotations

import sqlite3

from scripts.confidence_distribution_check import _report_provider


def _make_db():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE ai_origination_logs (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            provider TEXT NOT NULL,
            confidence REAL
        )
        """
    )
    connection.commit()
    return connection


def _insert(connection, provider, confidence, timestamp="2026-08-19T10:00:00"):
    connection.execute(
        "INSERT INTO ai_origination_logs (timestamp, provider, confidence) VALUES (?, ?, ?)",
        (timestamp, provider, confidence),
    )


def test_report_provider_computes_min_max_mean_distinct(caplog):
    connection = _make_db()
    for c in [0.10, 0.30, 0.30, 0.55, 0.75]:
        _insert(connection, "claude", c)
    connection.commit()

    with caplog.at_level("INFO"):
        _report_provider(connection, "claude", since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "min=0.10" in messages
    assert "max=0.75" in messages
    assert "distinct_values=4" in messages  # 0.10, 0.30, 0.55, 0.75


def test_report_provider_handles_no_rows(caplog):
    connection = _make_db()
    with caplog.at_level("INFO"):
        _report_provider(connection, "claude", since=None)
    messages = "\n".join(r.message for r in caplog.records)
    assert "n=0" in messages


def test_report_provider_respects_since_filter(caplog):
    connection = _make_db()
    _insert(connection, "claude", 0.10, timestamp="2026-08-10T10:00:00")  # before cutoff
    _insert(connection, "claude", 0.70, timestamp="2026-08-20T10:00:00")  # after cutoff
    connection.commit()

    with caplog.at_level("INFO"):
        _report_provider(connection, "claude", since="2026-08-15")

    messages = "\n".join(r.message for r in caplog.records)
    assert "n=1" in messages
    assert "min=0.70" in messages
    assert "max=0.70" in messages


def test_report_provider_ignores_null_confidence(caplog):
    connection = _make_db()
    _insert(connection, "claude", None)
    _insert(connection, "claude", 0.5)
    connection.commit()

    with caplog.at_level("INFO"):
        _report_provider(connection, "claude", since=None)

    messages = "\n".join(r.message for r in caplog.records)
    assert "n=1" in messages
