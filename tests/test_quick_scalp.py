from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_data import Bar
from app.models import ExitReason, OptionContract, Signal
from app.multi_strategy import MultiStrategyTradeManager
from app.time_utils import IST, to_ist, utc_now
from app.quick_scalp import (
    _BREAKEVEN_BUFFER_POINTS,
    _HARD_TIME_STOP_MINUTES,
    _MAX_INDEX_STOP_POINTS,
    _OPTION_SL_POINTS,
    _RSI_OVERBOUGHT,
    _RSI_OVERSOLD,
    _STRUCTURAL_BUFFER_POINTS,
    _TARGET1_OPTION_POINTS,
    _VWAP_SIGMA_MULTIPLIER,
    _WICK_REJECTION_RATIO,
    ORIGIN,
    _ScalpFeatures,
    _ScalpSignal,
    _compute_vwap_bands,
    _has_open_quick_scalp_trade,
    _sibling_trade_id,
    _square_off_all,
    _structural_stop_level,
    check_quick_scalp_entry,
    check_quick_scalp_exits,
    open_scalp_trade,
    run_quick_scalp_checks,
    vwap_scalp_action,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(
        symbol="NIFTY", display_name="Nifty 50", enabled=True,
        exchange_segment="NFO", instrument_name="NIFTY",
        spot_exchange="NSE", spot_symbol="Nifty 50", spot_token="26000", strike_interval=50,
    )


def _bar(ts: datetime, o: float, h: float, l: float, c: float, v: float = 0.0) -> Bar:
    return Bar(ts_ist=ts, open=o, high=h, low=l, close=c, volume=v)


def _add_trade(db, *, trade_id, index_symbol="NIFTY", origin=ORIGIN, status=TradeStatus.OPEN,
                current_premium=100.0, entry_price=100.0, stoploss=91.0, target=113.0,
                entry_time=None, option_type="CE", structural_stop_level=None, exit_reason=None) -> None:
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Quick Scalp - Nifty 50", signal=f"BUY_{option_type}",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=24000,
        expiry="28AUG2026", option_type=option_type, quantity=75,
        entry_price=entry_price, current_premium=current_premium, stoploss=stoploss, target=target,
        entry_time=entry_time or utc_now(), origin=origin, status=status,
        result=(TradeResult.OPEN if status == TradeStatus.OPEN else (TradeResult.WIN if exit_reason == "TARGET" else TradeResult.LOSS)),
        mode=TradingMode.PAPER, structural_stop_level=structural_stop_level, exit_reason=exit_reason,
    ))
    db.commit()


class FakeSmartAPI:
    def __init__(self, price: float | None = 100.0, spot: float = 24000.0) -> None:
        self.price = price
        self.spot = spot

    def get_ltp(self, *_args, **_kwargs) -> float | None:
        return self.price

    def get_index_spot(self, _index) -> float:
        return self.spot

    def get_candles(self, *_args, **_kwargs):
        return []

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("Quick Scalp must never place a real order")


class FakeOptionFinder:
    def __init__(self, contract: OptionContract | None, futures: dict | None = None) -> None:
        self.contract = contract
        self.futures = futures
        self.calls = 0

    def find_deep_itm_contract(self, signal: Signal, index: IndexConfig, offset_points: float,
                                min_dte: int = 0, now_ist=None) -> OptionContract:
        self.calls += 1
        if self.contract is None:
            raise ValueError("no contract available")
        return self.contract

    def find_current_futures_contract(self, index: IndexConfig):
        return self.futures


class FakeTelegram:
    def send(self, *_args, **_kwargs) -> None:
        raise AssertionError("Quick Scalp trades must never notify Telegram")


def _make_contract(dte_days: int = 3, lot_size: int = 75) -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=dte_days)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol=f"NIFTY{expiry}23900CE", symboltoken="123", strike=23900,
        expiry=expiry, option_type="CE", lot_size=lot_size,
    )


def _make_trade_manager(smartapi=None) -> MultiStrategyTradeManager:
    return MultiStrategyTradeManager(None, smartapi or FakeSmartAPI(), FakeOptionFinder(None), FakeTelegram())


def _make_signal(action: str = "BUY_CE", trigger_level: float = 24010.0, setup_low: float = 23990.0, setup_high: float = 24010.0) -> _ScalpSignal:
    return _ScalpSignal(action=action, trigger_level=trigger_level, setup_low=setup_low, setup_high=setup_high)


# ---------------------------------------------------------------------------
# _compute_vwap_bands
# ---------------------------------------------------------------------------

def test_vwap_bands_equal_weighted_matches_plain_stdev():
    ts = datetime(2026, 9, 4, 9, 15)
    # Typical prices 10, 20, 30 with equal weight -> mean 20, population
    # variance ((10-20)^2+(20-20)^2+(30-20)^2)/3 = 66.667, sigma ~= 8.165.
    bars = [
        _bar(ts, 10, 10, 10, 10),
        _bar(ts + timedelta(minutes=1), 20, 20, 20, 20),
        _bar(ts + timedelta(minutes=2), 30, 30, 30, 30),
    ]
    vwap_series, sigma_series = _compute_vwap_bands(bars, [0.0, 0.0, 0.0])
    assert vwap_series[-1] == 20.0
    assert round(sigma_series[-1], 3) == round((66.6667) ** 0.5, 3)


def test_vwap_bands_volume_weighted_pulls_toward_higher_volume_bar():
    ts = datetime(2026, 9, 4, 9, 15)
    bars = [_bar(ts, 100, 100, 100, 100), _bar(ts + timedelta(minutes=1), 200, 200, 200, 200)]
    vwap_series, _ = _compute_vwap_bands(bars, [10.0, 30.0])
    # (100*10 + 200*30) / 40 = 175
    assert vwap_series[-1] == 175.0


def test_vwap_bands_single_bar_has_zero_sigma():
    ts = datetime(2026, 9, 4, 9, 15)
    vwap_series, sigma_series = _compute_vwap_bands([_bar(ts, 50, 50, 50, 50)], [1.0])
    assert vwap_series[0] == 50.0
    assert sigma_series[0] == 0.0


# ---------------------------------------------------------------------------
# vwap_scalp_action
# ---------------------------------------------------------------------------

def _features_for(c0: Bar, c1: Bar, *, vwap0: float, sigma0: float, rsi0: float) -> _ScalpFeatures:
    bars = [c0, c1]
    return _ScalpFeatures(
        session_bars=bars,
        vwap_series=[vwap0, vwap0],
        sigma_series=[sigma0, sigma0],
        rsi_series=[rsi0, rsi0],
    )


def test_buy_ce_fires_on_full_setup_and_trigger():
    ts = datetime(2026, 9, 4, 10, 0)
    # VWAP 24000, sigma 5 -> lower band 23990. C0 pierces (low 23980), closes
    # back above (23995), wick = 23995-23980=15 of a 30-point range (50%),
    # RSI 25 (<30). C1 crosses above C0.high (24010).
    c0 = _bar(ts, o=23995, h=24010, l=23980, c=23995)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24015, l=23998, c=24012)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=25.0)
    signal = vwap_scalp_action(features)
    assert signal is not None
    assert signal.action == "BUY_CE"
    assert signal.trigger_level == c0.high
    assert signal.setup_low == c0.low
    assert signal.setup_high == c0.high


def test_buy_ce_declines_without_rsi_confirmation():
    ts = datetime(2026, 9, 4, 10, 0)
    c0 = _bar(ts, o=23995, h=24010, l=23980, c=23995)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24015, l=23998, c=24012)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=_RSI_OVERSOLD)  # exactly at floor, not below
    assert vwap_scalp_action(features) is None


def test_buy_ce_declines_without_wick_rejection():
    ts = datetime(2026, 9, 4, 10, 0)
    # Range 30, lower wick only 3 (10%) -- below the 30% floor.
    c0 = _bar(ts, o=23990, h=24010, l=23980, c=23983)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24015, l=23998, c=24012)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=25.0)
    assert vwap_scalp_action(features) is None


def test_buy_ce_declines_without_inside_close():
    ts = datetime(2026, 9, 4, 10, 0)
    # Closes below the lower band (23988 < 23990) instead of back inside it.
    c0 = _bar(ts, o=23995, h=24010, l=23980, c=23988)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24015, l=23998, c=24012)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=25.0)
    assert vwap_scalp_action(features) is None


def test_buy_ce_disarms_when_c1_does_not_cross():
    ts = datetime(2026, 9, 4, 10, 0)
    c0 = _bar(ts, o=23995, h=24010, l=23980, c=23995)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24008, l=23998, c=24005)  # high 24008 < c0.high 24010
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=25.0)
    assert vwap_scalp_action(features) is None


def test_buy_pe_fires_on_full_setup_and_trigger():
    ts = datetime(2026, 9, 4, 10, 0)
    # VWAP 24000, sigma 5 -> upper band 24010. C0 pierces (high 24020),
    # closes back below (24005), upper wick = 24020-24005=15 of a 30-point
    # range (50%), RSI 75 (>70). C1 crosses below C0.low (23990).
    c0 = _bar(ts, o=24005, h=24020, l=23990, c=24005)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24002, l=23985, c=23988)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=75.0)
    signal = vwap_scalp_action(features)
    assert signal is not None
    assert signal.action == "BUY_PE"
    assert signal.trigger_level == c0.low
    assert signal.setup_low == c0.low
    assert signal.setup_high == c0.high


def test_buy_pe_declines_without_rsi_confirmation():
    ts = datetime(2026, 9, 4, 10, 0)
    c0 = _bar(ts, o=24005, h=24020, l=23990, c=24005)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24002, l=23985, c=23988)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=_RSI_OVERBOUGHT)
    assert vwap_scalp_action(features) is None


def test_returns_none_with_fewer_than_two_bars():
    ts = datetime(2026, 9, 4, 10, 0)
    single = _ScalpFeatures(session_bars=[_bar(ts, 1, 1, 1, 1)], vwap_series=[1.0], sigma_series=[1.0], rsi_series=[50.0])
    empty = _ScalpFeatures(session_bars=[], vwap_series=[], sigma_series=[], rsi_series=[])
    assert vwap_scalp_action(single) is None
    assert vwap_scalp_action(empty) is None


def test_returns_none_when_sigma_is_zero_or_missing():
    ts = datetime(2026, 9, 4, 10, 0)
    c0 = _bar(ts, o=23995, h=24010, l=23980, c=23995)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24015, l=23998, c=24012)
    zero_sigma = _features_for(c0, c1, vwap0=24000.0, sigma0=0.0, rsi0=25.0)
    missing = _ScalpFeatures(session_bars=[c0, c1], vwap_series=[None, None], sigma_series=[None, None], rsi_series=[25.0, 25.0])
    assert vwap_scalp_action(zero_sigma) is None
    assert vwap_scalp_action(missing) is None


def test_returns_none_on_a_flat_zero_range_bar():
    ts = datetime(2026, 9, 4, 10, 0)
    c0 = _bar(ts, o=24000, h=24000, l=24000, c=24000)
    c1 = _bar(ts + timedelta(minutes=1), o=24001, h=24002, l=24000, c=24001)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=25.0)
    assert vwap_scalp_action(features) is None


# ---------------------------------------------------------------------------
# _structural_stop_level
# ---------------------------------------------------------------------------

def test_structural_stop_level_ce_uses_raw_when_within_cap():
    # trigger 24010, setup_low 23999 -> raw = 23998, cap = 24010-14 = 23996.
    # raw (23998) is CLOSER to entry (tighter) -> max(raw, cap) picks raw.
    signal = _make_signal("BUY_CE", trigger_level=24010.0, setup_low=23999.0, setup_high=24010.0)
    level = _structural_stop_level(signal)
    assert level == 23999.0 - _STRUCTURAL_BUFFER_POINTS


def test_structural_stop_level_ce_capped_when_raw_too_far():
    # setup_low far below -> raw would exceed the 14pt cap, so the capped
    # level (closer to entry) is used instead.
    signal = _make_signal("BUY_CE", trigger_level=24010.0, setup_low=23950.0, setup_high=24010.0)
    level = _structural_stop_level(signal)
    assert level == 24010.0 - _MAX_INDEX_STOP_POINTS


def test_structural_stop_level_pe_uses_raw_when_within_cap():
    signal = _make_signal("BUY_PE", trigger_level=23990.0, setup_low=23990.0, setup_high=24001.0)
    level = _structural_stop_level(signal)
    assert level == 24001.0 + _STRUCTURAL_BUFFER_POINTS


def test_structural_stop_level_pe_capped_when_raw_too_far():
    signal = _make_signal("BUY_PE", trigger_level=23990.0, setup_low=23990.0, setup_high=24050.0)
    level = _structural_stop_level(signal)
    assert level == 23990.0 + _MAX_INDEX_STOP_POINTS


# ---------------------------------------------------------------------------
# _sibling_trade_id
# ---------------------------------------------------------------------------

def test_sibling_trade_id_round_trips():
    assert _sibling_trade_id("abc123A") == "abc123B"
    assert _sibling_trade_id("abc123B") == "abc123A"


# ---------------------------------------------------------------------------
# _has_open_quick_scalp_trade
# ---------------------------------------------------------------------------

def test_no_open_trade_when_table_empty():
    db = _make_session()
    assert _has_open_quick_scalp_trade(db, "NIFTY") is False


def test_true_when_a_quick_scalp_trade_is_open():
    db = _make_session()
    _add_trade(db, trade_id="t1A")
    assert _has_open_quick_scalp_trade(db, "NIFTY") is True


def test_false_when_open_trade_belongs_to_a_different_origin():
    db = _make_session()
    _add_trade(db, trade_id="t1A", origin="AI_ORIGIN_OPENAI")
    assert _has_open_quick_scalp_trade(db, "NIFTY") is False


def test_false_when_the_trade_is_already_closed():
    db = _make_session()
    _add_trade(db, trade_id="t1A", status=TradeStatus.CLOSED)
    assert _has_open_quick_scalp_trade(db, "NIFTY") is False


# ---------------------------------------------------------------------------
# open_scalp_trade
# ---------------------------------------------------------------------------

def test_open_scalp_trade_splits_into_two_legs_with_correct_levels():
    db = _make_session()
    index = _make_index()
    # setup_low is only 10 points below the trigger -- well within the 14pt
    # cap, so the raw C0.low-1 level (23999) is the tighter, winning one.
    signal = _make_signal("BUY_CE", trigger_level=24010.0, setup_low=24000.0, setup_high=24010.0)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract(lot_size=75))

    trades = open_scalp_trade(db, index, signal, smartapi, option_finder, to_ist(utc_now()))

    assert len(trades) == 2
    leg_a, leg_b = trades
    assert leg_a.trade_id.endswith("A") and leg_b.trade_id.endswith("B")
    assert leg_a.quantity + leg_b.quantity == 75
    assert leg_a.quantity == 37 and leg_b.quantity == 38
    assert leg_a.origin == ORIGIN and leg_b.origin == ORIGIN
    assert leg_a.mode == TradingMode.PAPER and leg_b.mode == TradingMode.PAPER
    assert leg_a.stoploss == round(100.0 - _OPTION_SL_POINTS, 2)
    assert leg_b.stoploss == round(100.0 - _OPTION_SL_POINTS, 2)
    assert leg_a.target == round(100.0 + _TARGET1_OPTION_POINTS, 2)
    assert leg_b.target == round(100.0 * 5, 2)  # runner sentinel, never meant to fire
    assert leg_a.structural_stop_level == round(24000.0 - _STRUCTURAL_BUFFER_POINTS, 2)
    assert leg_b.structural_stop_level == leg_a.structural_stop_level


def test_open_scalp_trade_falls_back_to_a_single_leg_when_lot_too_small():
    db = _make_session()
    index = _make_index()
    signal = _make_signal()
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract(lot_size=1))

    trades = open_scalp_trade(db, index, signal, smartapi, option_finder, to_ist(utc_now()))

    assert len(trades) == 1
    assert trades[0].quantity == 1
    assert trades[0].trade_id.endswith("A")


def test_open_scalp_trade_handles_contract_resolution_failure():
    db = _make_session()
    index = _make_index()
    trades = open_scalp_trade(db, index, _make_signal(), FakeSmartAPI(price=100.0), FakeOptionFinder(None), to_ist(utc_now()))
    assert trades == []


def test_open_scalp_trade_handles_missing_ltp():
    db = _make_session()
    index = _make_index()
    trades = open_scalp_trade(db, index, _make_signal(), FakeSmartAPI(price=None), FakeOptionFinder(_make_contract()), to_ist(utc_now()))
    assert trades == []


def test_open_scalp_trade_declines_when_option_stop_would_be_non_positive():
    db = _make_session()
    index = _make_index()
    # entry 5.0 - 9.0 option points = negative stop -- must decline outright
    # rather than open a trade with an already-breached stop.
    trades = open_scalp_trade(db, index, _make_signal(), FakeSmartAPI(price=5.0), FakeOptionFinder(_make_contract()), to_ist(utc_now()))
    assert trades == []


def test_open_scalp_trade_never_places_a_real_order():
    db = _make_session()
    index = _make_index()
    trades = open_scalp_trade(db, index, _make_signal(), FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), to_ist(utc_now()))
    assert len(trades) == 2
    assert all(t.mode == TradingMode.PAPER for t in trades)


# ---------------------------------------------------------------------------
# check_quick_scalp_entry
# ---------------------------------------------------------------------------

def _dummy_features() -> _ScalpFeatures:
    ts = datetime(2026, 9, 4, 10, 0)
    return _ScalpFeatures(session_bars=[_bar(ts, 1, 1, 1, 1)] * 2, vwap_series=[1.0, 1.0], sigma_series=[1.0, 1.0], rsi_series=[50.0, 50.0])


def test_check_entry_skips_when_position_already_open(monkeypatch):
    import app.quick_scalp as module
    db = _make_session()
    index = _make_index()
    _add_trade(db, trade_id="t1A")
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: (_ for _ in ()).throw(AssertionError("must not check signal")))

    result = check_quick_scalp_entry(db, index, _dummy_features(), FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))
    assert result == []
    assert option_finder.calls == 0


def test_check_entry_skips_when_no_features():
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    result = check_quick_scalp_entry(db, index, None, FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))
    assert result == []
    assert option_finder.calls == 0


def test_check_entry_opens_a_trade_on_a_signal(monkeypatch):
    import app.quick_scalp as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: _make_signal("BUY_PE", trigger_level=23990.0, setup_low=23990.0, setup_high=24000.0))

    result = check_quick_scalp_entry(db, index, _dummy_features(), FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))

    assert len(result) == 2
    assert all(t.origin == ORIGIN for t in result)


def test_check_entry_no_signal_opens_nothing(monkeypatch):
    import app.quick_scalp as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: None)

    result = check_quick_scalp_entry(db, index, _dummy_features(), FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))
    assert result == []
    assert option_finder.calls == 0


# ---------------------------------------------------------------------------
# check_quick_scalp_exits -- breakeven move
# ---------------------------------------------------------------------------

def test_breakeven_move_tightens_runner_stop_after_target1():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", entry_price=100.0, stoploss=91.0, status=TradeStatus.CLOSED,
               exit_reason="TARGET", entry_time=now - timedelta(minutes=1))
    _add_trade(db, trade_id="grpB", entry_price=100.0, current_premium=105.0, stoploss=91.0,
               entry_time=now - timedelta(minutes=1))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index=None)

    leg_b = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpB").one()
    assert leg_b.stoploss == round(100.0 + _BREAKEVEN_BUFFER_POINTS, 2)


def test_breakeven_move_does_not_fire_when_sibling_target_not_hit():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", entry_price=100.0, stoploss=91.0, status=TradeStatus.OPEN,
               entry_time=now - timedelta(minutes=1))
    _add_trade(db, trade_id="grpB", entry_price=100.0, current_premium=105.0, stoploss=91.0,
               entry_time=now - timedelta(minutes=1))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index=None)

    leg_b = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpB").one()
    assert leg_b.stoploss == 91.0


# ---------------------------------------------------------------------------
# check_quick_scalp_exits -- structural stop
# ---------------------------------------------------------------------------

def test_structural_stop_closes_a_ce_when_spot_breaches_the_level():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", option_type="CE", structural_stop_level=23996.0,
               current_premium=95.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index={"NIFTY": (23995.0, None)})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_STRUCTURAL_STOP.value


def test_structural_stop_does_not_fire_when_spot_has_not_breached():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", option_type="CE", structural_stop_level=23990.0,
               current_premium=105.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index={"NIFTY": (23995.0, None)})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()
    assert trade.status == TradeStatus.OPEN


def test_structural_stop_closes_a_pe_when_spot_breaches_above():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", option_type="PE", structural_stop_level=24010.0,
               current_premium=95.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index={"NIFTY": (24011.0, None)})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_STRUCTURAL_STOP.value


# ---------------------------------------------------------------------------
# check_quick_scalp_exits -- VWAP-cross runner target (leg B only)
# ---------------------------------------------------------------------------

def test_vwap_cross_closes_the_runner_leg_when_spot_reaches_vwap():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpB", option_type="CE", current_premium=98.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index={"NIFTY": (24001.0, 24000.0)})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpB").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_VWAP_TARGET.value


def test_vwap_cross_never_fires_for_the_target1_leg():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", option_type="CE", current_premium=98.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index={"NIFTY": (24001.0, 24000.0)})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()
    assert trade.status == TradeStatus.OPEN


def test_vwap_cross_does_not_fire_before_spot_reaches_it():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpB", option_type="CE", current_premium=98.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index={"NIFTY": (23990.0, 24000.0)})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpB").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# check_quick_scalp_exits -- hard time stop
# ---------------------------------------------------------------------------

def test_hard_time_stop_fires_at_3_minutes():
    assert _HARD_TIME_STOP_MINUTES == 3
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", current_premium=100.5, entry_time=now - timedelta(minutes=3, seconds=1))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_TIME_STOP.value


def test_hard_time_stop_does_not_fire_before_3_minutes():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", current_premium=100.5, entry_time=now - timedelta(minutes=2, seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_isolated_from_other_origins():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t-signalA", origin="SIGNAL", current_premium=105.0, entry_time=now - timedelta(minutes=10))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t-signalA").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_never_touches_telegram_or_strategy_stats():
    db = _make_session()
    _add_trade(db, trade_id="grpA", current_premium=105.0)
    trade_manager = _make_trade_manager()
    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()

    trade_manager.close_trade(db, trade, 105.0, ExitReason.SCALP_TIME_STOP)

    assert trade.status == TradeStatus.CLOSED


# ---------------------------------------------------------------------------
# _square_off_all
# ---------------------------------------------------------------------------

def test_square_off_all_closes_every_open_leg_via_time_exit():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="grpA", current_premium=101.0, entry_time=now - timedelta(minutes=1))
    _add_trade(db, trade_id="grpB", current_premium=102.0, entry_time=now - timedelta(minutes=1))
    trade_manager = _make_trade_manager()

    _square_off_all(db, trade_manager)

    for trade_id in ("grpA", "grpB"):
        trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == trade_id).one()
        assert trade.status == TradeStatus.CLOSED
        assert trade.exit_reason == "TIME_EXIT"


# ---------------------------------------------------------------------------
# run_quick_scalp_checks (end-to-end wiring)
# ---------------------------------------------------------------------------

def test_run_quick_scalp_checks_skips_without_dependencies(caplog):
    with caplog.at_level("INFO"):
        run_quick_scalp_checks(None, None, None)
    assert "Skipped" in caplog.text


def test_run_quick_scalp_checks_skips_outside_market_hours(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 20, 0, tzinfo=IST))  # night
    db = _make_session()

    def _exploding(*a, **k):
        raise AssertionError("must not reach entry/exit checks outside market hours")

    monkeypatch.setattr(module, "check_quick_scalp_exits", _exploding)
    run_quick_scalp_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), db=db)


def test_run_quick_scalp_checks_opens_a_trade_end_to_end(monkeypatch):
    import app.quick_scalp as module

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 11, 0, tzinfo=IST))  # Friday, well inside window
    db = _make_session()
    db.add(_make_index())
    db.commit()

    monkeypatch.setattr(module, "_compute_scalp_features", lambda *a, **k: (_dummy_features(), False))
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: _make_signal("BUY_CE", trigger_level=24010.0, setup_low=23990.0, setup_high=24010.0))
    option_finder = FakeOptionFinder(_make_contract())

    run_quick_scalp_checks(FakeSmartAPI(price=100.0), option_finder, _make_trade_manager(), db=db)

    trades = db.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).all()
    assert len(trades) == 2


def test_run_quick_scalp_checks_blocks_entries_before_warmup_end(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 9, 20, tzinfo=IST))  # before 09:30
    db = _make_session()
    db.add(_make_index())
    db.commit()

    monkeypatch.setattr(module, "_compute_scalp_features", lambda *a, **k: (_dummy_features(), False))
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: (_ for _ in ()).throw(AssertionError("must not check signal before warmup")))

    run_quick_scalp_checks(FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), _make_trade_manager(), db=db)
    assert db.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).count() == 0


def test_run_quick_scalp_checks_blocks_entries_after_the_entry_cutoff(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 15, 11, tzinfo=IST))  # past 15:10
    db = _make_session()
    db.add(_make_index())
    db.commit()

    monkeypatch.setattr(module, "_compute_scalp_features", lambda *a, **k: (_dummy_features(), False))
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: (_ for _ in ()).throw(AssertionError("must not check signal past the entry cutoff")))

    run_quick_scalp_checks(FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), _make_trade_manager(), db=db)
    assert db.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).count() == 0


def test_run_quick_scalp_checks_still_enters_just_before_the_cutoff(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 15, 9, tzinfo=IST))
    db = _make_session()
    db.add(_make_index())
    db.commit()

    monkeypatch.setattr(module, "_compute_scalp_features", lambda *a, **k: (_dummy_features(), False))
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: _make_signal())
    run_quick_scalp_checks(FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), _make_trade_manager(), db=db)

    assert db.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).count() == 2


def test_run_quick_scalp_checks_squares_off_everything_past_15_15(monkeypatch):
    import app.quick_scalp as module
    now = datetime(2026, 9, 4, 15, 15, tzinfo=IST)
    monkeypatch.setattr(module, "utc_now", lambda: now)
    db = _make_session()
    db.add(_make_index())
    now_utc = now.astimezone(UTC).replace(tzinfo=UTC)
    _add_trade(db, trade_id="grpA", current_premium=101.0, entry_time=now_utc - timedelta(minutes=10))
    db.commit()

    monkeypatch.setattr(module, "_compute_scalp_features", lambda *a, **k: (None, False))

    run_quick_scalp_checks(FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), _make_trade_manager(), db=db)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "TIME_EXIT"


def test_run_quick_scalp_checks_still_runs_exits_outside_the_entry_window(monkeypatch):
    # A trade already open when the entry window closes still needs its own
    # hard-time-stop check kept running -- exits are not gated by the entry
    # window the way entries are.
    import app.quick_scalp as module
    now = datetime(2026, 9, 4, 12, 0, tzinfo=IST)  # inside the window, uneventful for entries
    monkeypatch.setattr(module, "utc_now", lambda: now)
    db = _make_session()
    db.add(_make_index())
    now_utc = now.astimezone(UTC).replace(tzinfo=UTC)
    _add_trade(db, trade_id="grpA", current_premium=101.0, entry_time=now_utc - timedelta(minutes=5))
    db.commit()

    monkeypatch.setattr(module, "_compute_scalp_features", lambda *a, **k: (None, False))
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: None)

    run_quick_scalp_checks(FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), _make_trade_manager(), db=db)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "grpA").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_TIME_STOP.value


def test_run_quick_scalp_checks_halts_new_entries_on_a_refresh_failure(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 11, 0, tzinfo=IST))
    db = _make_session()
    db.add(_make_index())
    db.commit()

    # refresh_failed=True even though features happen to be available --
    # entries must be skipped regardless.
    monkeypatch.setattr(module, "_compute_scalp_features", lambda *a, **k: (_dummy_features(), True))
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: (_ for _ in ()).throw(AssertionError("must not check signal after a refresh failure")))

    run_quick_scalp_checks(FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), _make_trade_manager(), db=db)
    assert db.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).count() == 0
