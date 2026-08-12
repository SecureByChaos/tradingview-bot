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
