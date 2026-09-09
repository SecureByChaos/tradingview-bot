from __future__ import annotations

import logging
import time

from app.database import _POOL_CHECKOUT_WARN_SECONDS, _wrap_pool_connect_with_timing, engine


def test_real_engine_pool_timeout_is_5_seconds():
    # 9 Sep 2026, Phase 2e of the "portal unresponsive during market hours"
    # investigation: the QueuePool default (30s) let pool exhaustion hang
    # silently for a long time before finally raising. 5s makes it fail
    # loudly and quickly instead -- deliberately not a pool_size increase,
    # which would only let the underlying contention hide for longer.
    assert engine.pool._timeout == 5


class _FakePool:
    def __init__(self) -> None:
        self.connect_calls = 0

    def connect(self, *args, **kwargs):
        self.connect_calls += 1
        return "a-connection"

    def checkedout(self) -> int:
        return 3

    def overflow(self) -> int:
        return 1


def test_wrap_pool_connect_does_not_log_when_checkout_is_fast(caplog):
    pool = _FakePool()
    _wrap_pool_connect_with_timing(pool, threshold_seconds=1.0)

    with caplog.at_level(logging.WARNING):
        result = pool.connect()

    assert result == "a-connection"
    assert pool.connect_calls == 1
    assert "DB_POOL" not in caplog.text


def test_wrap_pool_connect_logs_a_warning_when_checkout_is_slow(caplog, monkeypatch):
    pool = _FakePool()
    # Force the wrapper's own elapsed-time measurement past the threshold
    # without a real sleep, by making the underlying monotonic clock jump.
    import app.database as module

    times = iter([100.0, 100.0 + 2.5])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))

    _wrap_pool_connect_with_timing(pool, threshold_seconds=1.0)

    with caplog.at_level(logging.WARNING):
        result = pool.connect()

    assert result == "a-connection"
    assert "DB_POOL" in caplog.text
    assert "2.50s" in caplog.text
    assert "checked_out=3" in caplog.text
    assert "overflow=1" in caplog.text


def test_wrap_pool_connect_survives_a_pool_without_checkedout_or_overflow(caplog, monkeypatch):
    class _MinimalPool:
        def connect(self, *args, **kwargs):
            return "conn"

    pool = _MinimalPool()
    import app.database as module

    times = iter([0.0, 5.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))

    _wrap_pool_connect_with_timing(pool, threshold_seconds=1.0)

    with caplog.at_level(logging.WARNING):
        result = pool.connect()  # must not raise even without checkedout()/overflow()

    assert result == "conn"
    assert "DB_POOL" in caplog.text


def test_default_threshold_constant_is_one_second():
    assert _POOL_CHECKOUT_WARN_SECONDS == 1.0
