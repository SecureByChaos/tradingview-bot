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
    _COST_BUFFER_POINTS,
    _HARD_TIME_STOP_MINUTES,
    _MAX_INDEX_STOP_POINTS,
    _MIN_TARGET_POINTS,
    _RSI_OVERBOUGHT,
    _RSI_OVERSOLD,
    _STOP_PERCENT,
    _STRUCTURAL_BUFFER_POINTS,
    _TARGET_PERCENT,
    _VWAP_SIGMA_MULTIPLIER,
    _WICK_REJECTION_RATIO,
    ORIGIN,
    _ScalpFeatures,
    _ScalpSignal,
    _compute_vwap_bands,
    _has_open_quick_scalp_trade,
    _on_scalp_bar_closed,
    _square_off_all,
    _structural_stop_level,
    check_quick_scalp_entry,
    check_quick_scalp_exits,
    make_bar_closed_callback,
    open_scalp_trade,
    run_quick_scalp_exit_checks,
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
                current_premium=100.0, entry_price=100.0, stoploss=97.5, target=112.0,
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
# _compute_vwap_bands (unchanged math from the 4 Sep build)
# ---------------------------------------------------------------------------

def test_vwap_bands_equal_weighted_matches_plain_stdev():
    ts = datetime(2026, 9, 4, 9, 15)
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
    assert vwap_series[-1] == 175.0


def test_vwap_bands_single_bar_has_zero_sigma():
    ts = datetime(2026, 9, 4, 9, 15)
    vwap_series, sigma_series = _compute_vwap_bands([_bar(ts, 50, 50, 50, 50)], [1.0])
    assert vwap_series[0] == 50.0
    assert sigma_series[0] == 0.0


# ---------------------------------------------------------------------------
# vwap_scalp_action (unchanged from the 4 Sep build)
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
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=_RSI_OVERSOLD)
    assert vwap_scalp_action(features) is None


def test_buy_ce_declines_without_wick_rejection():
    ts = datetime(2026, 9, 4, 10, 0)
    c0 = _bar(ts, o=23990, h=24010, l=23980, c=23983)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24015, l=23998, c=24012)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=25.0)
    assert vwap_scalp_action(features) is None


def test_buy_ce_declines_without_inside_close():
    ts = datetime(2026, 9, 4, 10, 0)
    c0 = _bar(ts, o=23995, h=24010, l=23980, c=23988)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24015, l=23998, c=24012)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=25.0)
    assert vwap_scalp_action(features) is None


def test_buy_ce_disarms_when_c1_does_not_cross():
    ts = datetime(2026, 9, 4, 10, 0)
    c0 = _bar(ts, o=23995, h=24010, l=23980, c=23995)
    c1 = _bar(ts + timedelta(minutes=1), o=24000, h=24008, l=23998, c=24005)
    features = _features_for(c0, c1, vwap0=24000.0, sigma0=5.0, rsi0=25.0)
    assert vwap_scalp_action(features) is None


def test_buy_pe_fires_on_full_setup_and_trigger():
    ts = datetime(2026, 9, 4, 10, 0)
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
# _structural_stop_level (unchanged construction, carried over from 4 Sep)
# ---------------------------------------------------------------------------

def test_structural_stop_level_ce_uses_raw_when_within_cap():
    signal = _make_signal("BUY_CE", trigger_level=24010.0, setup_low=23999.0, setup_high=24010.0)
    level = _structural_stop_level(signal)
    assert level == 23999.0 - _STRUCTURAL_BUFFER_POINTS


def test_structural_stop_level_ce_capped_when_raw_too_far():
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
# _has_open_quick_scalp_trade
# ---------------------------------------------------------------------------

def test_no_open_trade_when_table_empty():
    db = _make_session()
    assert _has_open_quick_scalp_trade(db, "NIFTY") is False


def test_true_when_a_quick_scalp_trade_is_open():
    db = _make_session()
    _add_trade(db, trade_id="t1")
    assert _has_open_quick_scalp_trade(db, "NIFTY") is True


def test_false_when_open_trade_belongs_to_a_different_origin():
    db = _make_session()
    _add_trade(db, trade_id="t1", origin="AI_ORIGIN_OPENAI")
    assert _has_open_quick_scalp_trade(db, "NIFTY") is False


def test_false_when_the_trade_is_already_closed():
    db = _make_session()
    _add_trade(db, trade_id="t1", status=TradeStatus.CLOSED)
    assert _has_open_quick_scalp_trade(db, "NIFTY") is False


# ---------------------------------------------------------------------------
# open_scalp_trade -- single-clip, percentage-based (8 Sep rebuild)
# ---------------------------------------------------------------------------

def test_open_scalp_trade_opens_exactly_one_position_at_percentage_levels():
    db = _make_session()
    index = _make_index()
    signal = _make_signal("BUY_CE", trigger_level=24010.0, setup_low=24000.0, setup_high=24010.0)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract(lot_size=75))

    trade = open_scalp_trade(db, index, signal, smartapi, option_finder, to_ist(utc_now()))

    assert trade is not None
    assert trade.origin == ORIGIN
    assert trade.mode == TradingMode.PAPER
    # 8 Sep 2026: Quick Scalp trades 2 lots by default, scoped to this
    # strategy only -- a real contract lot_size of 75 becomes quantity 150.
    assert trade.quantity == 150
    assert trade.investment_amount == round(100.0 * 150, 2)
    assert trade.stoploss == round(100.0 * (1 - _STOP_PERCENT), 2)
    # target%(3.75) of 100 = 103.75, floor entry+12=112 -- floor wins.
    assert trade.target == round(max(100.0 * (1 + _TARGET_PERCENT), 112.0), 2)
    assert trade.structural_stop_level == round(24000.0 - _STRUCTURAL_BUFFER_POINTS, 2)


def test_open_scalp_trade_uses_the_lot_multiplier_regardless_of_lot_size():
    db = _make_session()
    index = _make_index()
    signal = _make_signal()
    option_finder = FakeOptionFinder(_make_contract(lot_size=35))  # a different real lot size (Bank Nifty-shaped)

    trade = open_scalp_trade(db, index, signal, FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))

    assert trade.quantity == 70


def test_open_scalp_trade_target_uses_percentage_when_above_the_points_floor():
    db = _make_session()
    index = _make_index()
    signal = _make_signal()
    # entry 1000 -> 3.75% = 37.5 pts, well above the flat 12pt floor.
    smartapi = FakeSmartAPI(price=1000.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_scalp_trade(db, index, signal, smartapi, option_finder, to_ist(utc_now()))

    assert trade.target == round(1000.0 * (1 + _TARGET_PERCENT), 2)


def test_open_scalp_trade_handles_contract_resolution_failure():
    db = _make_session()
    index = _make_index()
    trade = open_scalp_trade(db, index, _make_signal(), FakeSmartAPI(price=100.0), FakeOptionFinder(None), to_ist(utc_now()))
    assert trade is None


def test_open_scalp_trade_handles_missing_ltp():
    db = _make_session()
    index = _make_index()
    trade = open_scalp_trade(db, index, _make_signal(), FakeSmartAPI(price=None), FakeOptionFinder(_make_contract()), to_ist(utc_now()))
    assert trade is None


def test_open_scalp_trade_declines_when_stop_would_be_non_positive():
    db = _make_session()
    index = _make_index()
    # A 2.5% stop off a near-zero entry can round to zero.
    trade = open_scalp_trade(db, index, _make_signal(), FakeSmartAPI(price=0.0), FakeOptionFinder(_make_contract()), to_ist(utc_now()))
    assert trade is None


def test_open_scalp_trade_never_places_a_real_order():
    db = _make_session()
    index = _make_index()
    trade = open_scalp_trade(db, index, _make_signal(), FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), to_ist(utc_now()))
    assert trade is not None
    assert trade.mode == TradingMode.PAPER


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
    _add_trade(db, trade_id="t1")
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: (_ for _ in ()).throw(AssertionError("must not check signal")))

    result = check_quick_scalp_entry(db, index, _dummy_features(), FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))
    assert result is None
    assert option_finder.calls == 0


def test_check_entry_skips_when_no_features():
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    result = check_quick_scalp_entry(db, index, None, FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))
    assert result is None
    assert option_finder.calls == 0


def test_check_entry_opens_a_trade_on_a_signal(monkeypatch):
    import app.quick_scalp as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: _make_signal("BUY_PE", trigger_level=23990.0, setup_low=23990.0, setup_high=24000.0))

    result = check_quick_scalp_entry(db, index, _dummy_features(), FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))

    assert result is not None
    assert result.origin == ORIGIN


def test_check_entry_no_signal_opens_nothing(monkeypatch):
    import app.quick_scalp as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: None)

    result = check_quick_scalp_entry(db, index, _dummy_features(), FakeSmartAPI(price=100.0), option_finder, to_ist(utc_now()))
    assert result is None
    assert option_finder.calls == 0


# ---------------------------------------------------------------------------
# _on_scalp_bar_closed / make_bar_closed_callback -- the WS feed's own entry point
# ---------------------------------------------------------------------------

def test_bar_closed_callback_opens_a_trade_end_to_end(monkeypatch):
    # _on_scalp_bar_closed opens its OWN session via SessionLocal (matching
    # every other entry point in this codebase) -- a plain "sqlite:///:memory:"
    # engine gives each new connection its own empty database, so this test
    # needs one shared in-memory DB across every Session(engine) call the
    # callback makes. StaticPool keeps a single underlying connection alive
    # for the whole engine regardless of how many Session()s are opened.
    import app.quick_scalp as module
    from sqlalchemy.pool import StaticPool

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 8, 11, 0, tzinfo=IST))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as seed:
        seed.add(_make_index())
        seed.commit()

    monkeypatch.setattr(module, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(module, "_load_scalp_features", lambda db, symbol, now_ist: _dummy_features())
    monkeypatch.setattr(module, "vwap_scalp_action", lambda f: _make_signal("BUY_CE", trigger_level=24010.0, setup_low=23990.0, setup_high=24010.0))

    callback = make_bar_closed_callback(FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()))
    callback("NIFTY")

    with Session(engine) as check:
        trades = check.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).all()
        assert len(trades) == 1


def test_bar_closed_callback_blocks_before_warmup_end(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 8, 9, 20, tzinfo=IST))  # before 09:30
    monkeypatch.setattr(
        module, "_load_scalp_features",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not load features before warmup")),
    )
    _on_scalp_bar_closed("NIFTY", FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()))


def test_bar_closed_callback_blocks_after_entry_cutoff(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 8, 15, 11, tzinfo=IST))  # past 15:10
    monkeypatch.setattr(
        module, "_load_scalp_features",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not load features past the entry cutoff")),
    )
    _on_scalp_bar_closed("NIFTY", FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()))


def test_bar_closed_callback_swallows_its_own_exceptions(monkeypatch, caplog):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 8, 11, 0, tzinfo=IST))

    def _exploding_session():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(module, "SessionLocal", _exploding_session)
    with caplog.at_level("ERROR"):
        _on_scalp_bar_closed("NIFTY", FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()))  # must not raise


# ---------------------------------------------------------------------------
# check_quick_scalp_exits -- structural stop
# ---------------------------------------------------------------------------

def test_structural_stop_closes_a_ce_when_spot_breaches_the_level():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t1", option_type="CE", structural_stop_level=23996.0,
               current_premium=95.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index={"NIFTY": 23995.0})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_STRUCTURAL_STOP.value


def test_structural_stop_does_not_fire_when_spot_has_not_breached():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t1", option_type="CE", structural_stop_level=23990.0,
               current_premium=105.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index={"NIFTY": 23995.0})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


def test_structural_stop_closes_a_pe_when_spot_breaches_above():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t1", option_type="PE", structural_stop_level=24010.0,
               current_premium=95.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index={"NIFTY": 24011.0})

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_STRUCTURAL_STOP.value


def test_structural_stop_skipped_without_a_current_spot_reading():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t1", option_type="CE", structural_stop_level=23996.0,
               current_premium=95.0, entry_time=now - timedelta(seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# check_quick_scalp_exits -- hard time stop: breakeven-trail vs. scratch (8 Sep rebuild)
# ---------------------------------------------------------------------------

def test_hard_time_stop_trails_to_breakeven_when_profitable_enough():
    assert _HARD_TIME_STOP_MINUTES == 3
    db = _make_session()
    now = utc_now()
    # current_premium (103) clears entry (100) + cost buffer (2) -> trail, not close.
    _add_trade(db, trade_id="t1", entry_price=100.0, stoploss=97.5, current_premium=103.0,
               entry_time=now - timedelta(minutes=3, seconds=1))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN
    assert trade.stoploss == round(100.0 + _COST_BUFFER_POINTS, 2)


def test_hard_time_stop_scratches_when_not_yet_profitable():
    db = _make_session()
    now = utc_now()
    # current_premium (100.5) does NOT clear entry+cost-buffer (102) -> scratch.
    _add_trade(db, trade_id="t1", entry_price=100.0, stoploss=97.5, current_premium=100.5,
               entry_time=now - timedelta(minutes=3, seconds=1))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_TIME_STOP.value


def test_hard_time_stop_scratches_a_losing_trade():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t1", entry_price=100.0, stoploss=97.5, current_premium=98.0,
               entry_time=now - timedelta(minutes=3, seconds=1))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_TIME_STOP.value


def test_hard_time_stop_does_not_fire_before_3_minutes():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t1", current_premium=103.0, entry_time=now - timedelta(minutes=2, seconds=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN
    assert trade.stoploss == 97.5  # untouched


def test_breakeven_trail_is_idempotent_on_a_later_cycle():
    db = _make_session()
    now = utc_now()
    # Already trailed (stoploss already at/above breakeven) -- must not
    # re-log or otherwise misbehave on a second pass.
    already_trailed_stop = round(100.0 + _COST_BUFFER_POINTS, 2)
    _add_trade(db, trade_id="t1", entry_price=100.0, stoploss=already_trailed_stop, current_premium=105.0,
               entry_time=now - timedelta(minutes=5))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN
    assert trade.stoploss == already_trailed_stop


def test_check_exits_isolated_from_other_origins():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t-signal", origin="SIGNAL", current_premium=105.0, entry_time=now - timedelta(minutes=10))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager, to_ist(now), current_spot_by_index=None)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t-signal").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_never_touches_telegram_or_strategy_stats():
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()
    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()

    trade_manager.close_trade(db, trade, 105.0, ExitReason.SCALP_TIME_STOP)

    assert trade.status == TradeStatus.CLOSED


# ---------------------------------------------------------------------------
# _square_off_all
# ---------------------------------------------------------------------------

def test_square_off_all_closes_every_open_trade_via_time_exit():
    db = _make_session()
    now = utc_now()
    _add_trade(db, trade_id="t1", current_premium=101.0, entry_time=now - timedelta(minutes=1))
    _add_trade(db, trade_id="t2", current_premium=102.0, entry_time=now - timedelta(minutes=1))
    trade_manager = _make_trade_manager()

    _square_off_all(db, trade_manager)

    for trade_id in ("t1", "t2"):
        trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == trade_id).one()
        assert trade.status == TradeStatus.CLOSED
        assert trade.exit_reason == "TIME_EXIT"


# ---------------------------------------------------------------------------
# run_quick_scalp_exit_checks (end-to-end wiring -- exits + square-off only)
# ---------------------------------------------------------------------------

def test_run_quick_scalp_exit_checks_skips_without_dependencies(caplog):
    with caplog.at_level("INFO"):
        run_quick_scalp_exit_checks(None, None)
    assert "Skipped" in caplog.text


def test_run_quick_scalp_exit_checks_skips_on_a_weekend(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 6, 11, 0, tzinfo=IST))  # Sunday
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=101.0)

    def _exploding(*a, **k):
        raise AssertionError("must not reach exit checks on a non-trading day")

    monkeypatch.setattr(module, "check_quick_scalp_exits", _exploding)
    run_quick_scalp_exit_checks(FakeSmartAPI(), _make_trade_manager(), db=db)


def test_run_quick_scalp_exit_checks_makes_zero_smartapi_calls_when_nothing_is_open():
    class _ExplodingSmartAPI(FakeSmartAPI):
        def get_index_spot(self, _index):
            raise AssertionError("must not fetch spot with nothing open")

    db = _make_session()
    db.add(_make_index())
    db.commit()

    run_quick_scalp_exit_checks(_ExplodingSmartAPI(), _make_trade_manager(), db=db)  # must not raise


def test_run_quick_scalp_exit_checks_squares_off_everything_past_15_15(monkeypatch):
    import app.quick_scalp as module
    now = datetime(2026, 9, 4, 15, 15, tzinfo=IST)
    monkeypatch.setattr(module, "utc_now", lambda: now)
    db = _make_session()
    db.add(_make_index())
    now_utc = now.astimezone(UTC).replace(tzinfo=UTC)
    _add_trade(db, trade_id="t1", current_premium=101.0, entry_time=now_utc - timedelta(minutes=10))
    db.commit()

    run_quick_scalp_exit_checks(FakeSmartAPI(price=100.0), _make_trade_manager(), db=db)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "TIME_EXIT"


def test_run_quick_scalp_exit_checks_runs_the_hard_time_stop_before_square_off_cutoff(monkeypatch):
    import app.quick_scalp as module
    now = datetime(2026, 9, 4, 12, 0, tzinfo=IST)
    monkeypatch.setattr(module, "utc_now", lambda: now)
    db = _make_session()
    db.add(_make_index())
    now_utc = now.astimezone(UTC).replace(tzinfo=UTC)
    _add_trade(db, trade_id="t1", entry_price=100.0, current_premium=98.0, entry_time=now_utc - timedelta(minutes=5))
    db.commit()

    run_quick_scalp_exit_checks(FakeSmartAPI(price=100.0), _make_trade_manager(), db=db)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SCALP_TIME_STOP.value


def test_run_quick_scalp_exit_checks_only_fetches_spot_for_indexes_with_open_trades(monkeypatch):
    import app.quick_scalp as module

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 12, 0, tzinfo=IST))  # inside the trading day, before square-off

    calls: list[str] = []

    class _TrackingSmartAPI(FakeSmartAPI):
        def get_index_spot(self, index):
            calls.append(index.symbol)
            return self.spot

    db = _make_session()
    db.add(_make_index())
    db.add(IndexConfig(symbol="BANKNIFTY", display_name="Bank Nifty", enabled=True, spot_token="99926009", spot_exchange="NSE"))
    db.commit()
    now_utc = datetime(2026, 9, 4, 6, 30, tzinfo=UTC)  # 12:00 IST
    _add_trade(db, trade_id="t1", index_symbol="NIFTY", current_premium=101.0, entry_time=now_utc - timedelta(seconds=30))

    run_quick_scalp_exit_checks(_TrackingSmartAPI(), _make_trade_manager(), db=db)

    assert calls == ["NIFTY"]
