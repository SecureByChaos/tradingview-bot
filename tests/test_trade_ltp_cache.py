from __future__ import annotations

from app.dashboard_routes import _cached_ltp, _trade_ltp_cache, _TRADE_LTP_CACHE_TTL_SECONDS


class FakeSmartAPI:
    def __init__(self, price: float) -> None:
        self.price = price
        self.calls = 0

    def get_ltp(self, exchange, tradingsymbol, symboltoken) -> float:
        self.calls += 1
        return self.price


def setup_function(_fn) -> None:
    _trade_ltp_cache.clear()


def test_repeated_calls_within_ttl_hit_cache():
    smartapi = FakeSmartAPI(123.45)
    for _ in range(10):
        price = _cached_ltp(smartapi, "NFO", "BANKNIFTY25AUGCE", "TOKEN1")
        assert price == 123.45
    assert smartapi.calls == 1


def test_different_contracts_do_not_share_cache_entries():
    smartapi = FakeSmartAPI(100.0)
    _cached_ltp(smartapi, "NFO", "SYM_A", "TOKEN_A")
    smartapi.price = 200.0
    price_b = _cached_ltp(smartapi, "NFO", "SYM_B", "TOKEN_B")
    assert price_b == 200.0
    assert smartapi.calls == 2


def test_refreshes_after_ttl_expiry():
    smartapi = FakeSmartAPI(100.0)
    _cached_ltp(smartapi, "NFO", "SYM_A", "TOKEN_A")
    assert smartapi.calls == 1
    # Force staleness without a real sleep.
    price, fetched_at = _trade_ltp_cache["TOKEN_A"]
    _trade_ltp_cache["TOKEN_A"] = (price, fetched_at - _TRADE_LTP_CACHE_TTL_SECONDS - 1)
    smartapi.price = 150.0
    price = _cached_ltp(smartapi, "NFO", "SYM_A", "TOKEN_A")
    assert price == 150.0
    assert smartapi.calls == 2
