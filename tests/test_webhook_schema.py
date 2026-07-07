from __future__ import annotations

from app.models import Signal, WebhookPayload


def test_legacy_payload_passes() -> None:
    payload = WebhookPayload.model_validate({"strategy": "V5.1", "signal": "BUY_CE"})
    assert payload.strategy == "V5.1"
    assert payload.signal == Signal.BUY_CE
    assert payload.market_data is None
    assert payload.indicators is None
    assert payload.trend is None
    assert payload.strategy_filters is None
    assert payload.trade_state is None


def test_v6_payload_passes() -> None:
    payload = WebhookPayload.model_validate({"strategy": "V6", "signal": "SELL_PE"})
    assert payload.strategy == "V6"
    assert payload.signal == Signal.SELL_PE


def test_v7_enriched_payload_passes() -> None:
    payload = WebhookPayload.model_validate(
        {
            "context_version": "1.0",
            "strategy": "V7",
            "signal": "BUY_CE",
            "market_data": {"banknifty_price": 58125.4, "timeframe": "5m"},
            "indicators": {"ema9": 58092.8, "ema20": 58070.1, "rsi": 63.2},
            "trend": {"trend_direction": "UP", "breakout": True},
            "strategy_filters": {"ema_filter": True, "adx_filter": True},
            "trade_state": {"trade_number": 1, "daily_trade_count": 1, "position": 1},
        }
    )
    assert payload.context_version == "1.0"
    assert payload.market_data is not None and payload.market_data.banknifty_price == 58125.4
    assert payload.indicators is not None and payload.indicators.ema20 == 58070.1
    assert payload.trend is not None and payload.trend.breakout is True
    assert payload.strategy_filters is not None and payload.strategy_filters.ema_filter is True
    assert payload.trade_state is not None and payload.trade_state.position == 1


def test_future_v8_payload_with_unknown_fields_passes() -> None:
    payload = WebhookPayload.model_validate(
        {
            "context_version": "2.0",
            "strategy": "V8",
            "signal": "BUY_PE",
            "market_data": {"banknifty_price": 58200.0, "unknown_market_field": "ok"},
            "indicators": {"ema9": 58100.0, "future_indicator": 12.34},
            "trend": {"trend_direction": "DOWN", "future_trend_flag": "X"},
            "strategy_filters": {"ema_filter": False, "future_filter": "IGNORED"},
            "trade_state": {"position": 0, "future_state": {"nested": True}},
            "future_top_level": {"x": 1},
        }
    )
    assert payload.strategy == "V8"
    assert payload.signal == Signal.BUY_PE
