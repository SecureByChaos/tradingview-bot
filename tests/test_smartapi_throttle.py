from __future__ import annotations

import time

from app.config import Settings
from app.smartapi_client import _MIN_QUOTE_INTERVAL_SECONDS, SmartAPIClient


def _make_client() -> SmartAPIClient:
    return SmartAPIClient(Settings(
        smartapi_api_key="x", smartapi_client_id="x", smartapi_pin="x", smartapi_totp_secret="x",
    ))


def test_throttle_enforces_minimum_interval_between_calls():
    client = _make_client()
    client._throttle_quote_call()
    started = time.monotonic()
    client._throttle_quote_call()
    elapsed = time.monotonic() - started
    assert elapsed >= _MIN_QUOTE_INTERVAL_SECONDS - 0.01  # small tolerance for scheduler jitter


def test_throttle_does_not_wait_when_calls_are_already_spaced_out():
    client = _make_client()
    client._throttle_quote_call()
    time.sleep(_MIN_QUOTE_INTERVAL_SECONDS + 0.05)
    started = time.monotonic()
    client._throttle_quote_call()
    elapsed = time.monotonic() - started
    assert elapsed < 0.05  # no extra sleep needed, call proceeds immediately


def test_throttle_is_shared_across_every_quote_call_site():
    # The 12 Aug incident's real cause: confirm the lock/timestamp are
    # instance attributes, not per-method state, so get_ltp and get_candles
    # (or any other quote-family call) contend for the SAME gate rather than
    # throttling themselves independently. app/main.py relies on exactly one
    # SmartAPIClient instance being shared by every caller for this to matter
    # in production.
    client = _make_client()
    client._throttle_quote_call()
    first_timestamp = client._last_quote_call_monotonic
    client._throttle_quote_call()
    assert client._last_quote_call_monotonic > first_timestamp


def test_min_quote_interval_has_real_margin_above_the_1s_broker_limit():
    # The 12 Aug incident measured a REAL rejection at a 1.073s gap -- the
    # margin must clear that with room to spare, not just barely exceed it.
    assert _MIN_QUOTE_INTERVAL_SECONDS >= 1.2


def test_throttle_logs_the_measured_gap_and_wait(caplog):
    # 13 Aug ground-truth instrumentation: this is the log line that would
    # have shown the retry-bypass bug directly instead of needing static
    # analysis to find it. Keep this test as a guard that the line survives.
    client = _make_client()
    client._throttle_quote_call()
    with caplog.at_level("INFO"):
        client._throttle_quote_call()
    messages = [r.message for r in caplog.records if "[THROTTLE]" in r.message]
    assert len(messages) == 1
    assert "sleeping" in messages[0]


def test_throttle_logs_no_wait_needed_when_already_spaced_out(caplog):
    client = _make_client()
    client._throttle_quote_call()
    time.sleep(_MIN_QUOTE_INTERVAL_SECONDS + 0.05)
    with caplog.at_level("INFO"):
        client._throttle_quote_call()
    messages = [r.message for r in caplog.records if "[THROTTLE]" in r.message]
    assert len(messages) == 1
    assert "no wait needed" in messages[0]


RATE_LIMITED_RESPONSE = {"status": False, "message": "Access denied because of exceeding access rate"}
OK_RESPONSE = {"status": True, "data": {"ltp": "100.0"}}


def test_retry_rate_limited_invokes_throttle_before_every_dispatch(monkeypatch):
    # The 13 Aug fix: every retry attempt is a real dispatch to the same
    # rate-limited endpoint, so a quote-family caller's throttle must gate
    # and record each one, not just the initial call.
    monkeypatch.setattr("app.smartapi_client.time.sleep", lambda _seconds: None)
    client = _make_client()
    responses = [RATE_LIMITED_RESPONSE, RATE_LIMITED_RESPONSE, OK_RESPONSE]
    calls = {"n": 0}

    def fake_func():
        calls["n"] += 1
        return responses[calls["n"] - 1]

    throttle_calls = []
    result = client._retry_rate_limited(fake_func, None, throttle=lambda: throttle_calls.append(1))

    assert result == OK_RESPONSE
    assert calls["n"] == 3
    assert len(throttle_calls) == 3  # one throttle() call per retry dispatch, including the successful one


def test_retry_rate_limited_without_throttle_still_works(monkeypatch):
    # Non-quote callers (order placement) pass no throttle -- must not break.
    monkeypatch.setattr("app.smartapi_client.time.sleep", lambda _seconds: None)
    client = _make_client()

    def fake_func():
        return OK_RESPONSE

    result = client._retry_rate_limited(fake_func, None)
    assert result == OK_RESPONSE


def test_call_with_reauth_passes_throttle_into_the_rate_limit_retry(monkeypatch):
    monkeypatch.setattr("app.smartapi_client.time.sleep", lambda _seconds: None)
    client = _make_client()
    responses = [RATE_LIMITED_RESPONSE, OK_RESPONSE]
    calls = {"n": 0}

    def fake_func():
        calls["n"] += 1
        return responses[calls["n"] - 1]

    fake_func.__name__ = "fakeMethod"
    throttle_calls = []
    result = client._call_with_reauth(fake_func, throttle=lambda: throttle_calls.append(1))

    assert result == OK_RESPONSE
    assert calls["n"] == 2
    assert len(throttle_calls) == 1  # the one retry dispatch was gated through throttle


def test_call_with_reauth_without_throttle_is_unaffected(monkeypatch):
    # Order placement calls _call_with_reauth with no throttle -- confirm the
    # rate-limit retry path still functions exactly as before this change.
    monkeypatch.setattr("app.smartapi_client.time.sleep", lambda _seconds: None)
    client = _make_client()
    responses = [RATE_LIMITED_RESPONSE, OK_RESPONSE]
    calls = {"n": 0}

    def fake_func():
        calls["n"] += 1
        return responses[calls["n"] - 1]

    fake_func.__name__ = "fakeMethod"
    result = client._call_with_reauth(fake_func)

    assert result == OK_RESPONSE
    assert calls["n"] == 2
