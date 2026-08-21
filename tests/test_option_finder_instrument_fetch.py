from __future__ import annotations

import json

import pytest

import app.option_finder as option_finder_module
from app.config import Settings
from app.option_finder import OptionFinder


def _make_finder(tmp_path) -> OptionFinder:
    settings = Settings(
        smartapi_api_key="x",
        smartapi_client_id="x",
        smartapi_pin="x",
        smartapi_totp_secret="x",
        data_dir=tmp_path,
        instrument_cache_path=tmp_path / "instruments.json",
    )
    return OptionFinder(settings, smartapi=None)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_succeeds_on_first_attempt_and_writes_cache(monkeypatch, tmp_path):
    finder = _make_finder(tmp_path)
    calls = {"n": 0}

    def fake_get(url, timeout):
        calls["n"] += 1
        return _FakeResponse([{"token": "1"}])

    monkeypatch.setattr(option_finder_module.requests, "get", fake_get)

    payload = finder._fetch_instruments_with_fallback(finder.settings.instrument_cache_path)

    assert payload == [{"token": "1"}]
    assert calls["n"] == 1
    assert json.loads(finder.settings.instrument_cache_path.read_text()) == [{"token": "1"}]


def test_fetch_retries_once_then_succeeds(monkeypatch, tmp_path):
    finder = _make_finder(tmp_path)
    calls = {"n": 0}
    slept = []

    def fake_get(url, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("Read timed out.")
        return _FakeResponse([{"token": "2"}])

    monkeypatch.setattr(option_finder_module.requests, "get", fake_get)
    monkeypatch.setattr(option_finder_module.time, "sleep", lambda s: slept.append(s))

    payload = finder._fetch_instruments_with_fallback(finder.settings.instrument_cache_path)

    assert payload == [{"token": "2"}]
    assert calls["n"] == 2
    assert slept == [option_finder_module._INSTRUMENT_FETCH_BACKOFF_SECONDS]


def test_fetch_falls_back_to_stale_cache_when_every_attempt_fails(monkeypatch, tmp_path):
    finder = _make_finder(tmp_path)
    cache_path = finder.settings.instrument_cache_path
    cache_path.write_text(json.dumps([{"token": "stale"}]), encoding="utf-8")

    def fake_get(url, timeout):
        raise TimeoutError("Read timed out.")

    monkeypatch.setattr(option_finder_module.requests, "get", fake_get)
    monkeypatch.setattr(option_finder_module.time, "sleep", lambda s: None)

    payload = finder._fetch_instruments_with_fallback(cache_path)

    assert payload == [{"token": "stale"}]


def test_fetch_reraises_when_every_attempt_fails_and_no_cache_exists(monkeypatch, tmp_path):
    finder = _make_finder(tmp_path)
    cache_path = finder.settings.instrument_cache_path
    assert not cache_path.exists()

    def fake_get(url, timeout):
        raise TimeoutError("Read timed out.")

    monkeypatch.setattr(option_finder_module.requests, "get", fake_get)
    monkeypatch.setattr(option_finder_module.time, "sleep", lambda s: None)

    with pytest.raises(TimeoutError):
        finder._fetch_instruments_with_fallback(cache_path)


def test_load_instruments_never_calls_network_when_cache_is_fresh(monkeypatch, tmp_path):
    finder = _make_finder(tmp_path)
    cache_path = finder.settings.instrument_cache_path
    cache_path.write_text(json.dumps([{"token": "fresh"}]), encoding="utf-8")

    def exploding_get(*args, **kwargs):
        raise AssertionError("requests.get should never be called when the cache is fresh")

    monkeypatch.setattr(option_finder_module.requests, "get", exploding_get)

    frame = finder._load_instruments()

    assert frame.iloc[0]["token"] == "fresh"
