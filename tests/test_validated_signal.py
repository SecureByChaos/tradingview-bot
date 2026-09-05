from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_data import Bar
from app.models import ExitReason, OptionContract, Signal
from app.time_utils import IST, to_ist, utc_now
from app.validated_signal import (
    ORIGIN,
    _SignalCandidate,
    _box_levels,
    _evaluate_session1,
    _evaluate_session2,
    _has_open_trade_anywhere,
    _orb_levels,
    _volume_sma,
    check_validated_signal_exits,
    evaluate_intraday_signal,
    open_validated_trade,
    run_validated_signal_entry_checks,
    run_validated_signal_exit_checks,
    select_itm_strike,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index(symbol: str = "BANKNIFTY") -> IndexConfig:
    names = {"BANKNIFTY": "Bank Nifty", "NIFTY": "Nifty 50"}
    return IndexConfig(symbol=symbol, display_name=names.get(symbol, symbol), spot_token="1", enabled=True)


def _bar(hh: int, mm: int, o: float, h: float, l: float, c: float, v: float = 100.0, day: int = 4) -> Bar:
    hh, mm = divmod(hh * 60 + mm, 60)
    return Bar(ts_ist=datetime(2026, 9, day, hh, mm), open=o, high=h, low=l, close=c, volume=v)


class FakeSmartAPI:
    def __init__(self, price: float | None = 100.0, spot: float = 57000.0) -> None:
        self.price = price
        self.spot = spot
        self.ltp_calls = 0
        self.spot_calls = 0

    def get_ltp(self, *_args, **_kwargs) -> float | None:
        self.ltp_calls += 1
        return self.price

    def get_index_spot(self, _index) -> float:
        self.spot_calls += 1
        return self.spot

    def get_candles(self, *_args, **_kwargs):
        raise AssertionError("must not fetch candles in this test")

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("Validated Signal must never place a real order")


class FakeOptionFinder:
    def __init__(self, contract: OptionContract | None) -> None:
        self.contract = contract
        self.calls = 0
        self.last_strike: int | None = None

    def find_contract_at_strike(self, signal: Signal, index: IndexConfig, target_strike: int, min_dte: int = 0) -> OptionContract:
        self.calls += 1
        self.last_strike = target_strike
        if self.contract is None:
            raise ValueError("no contract available")
        return self.contract


def _make_contract(option_type: str = "CE", strike: int = 57000) -> OptionContract:
    return OptionContract(
        tradingsymbol=f"BANKNIFTY28SEP2026{strike}{option_type}",
        symboltoken="123",
        strike=strike,
        expiry="28SEP2026",
        option_type=option_type,
        lot_size=35,
    )


def _add_trade(db, *, trade_id, index_symbol="BANKNIFTY", origin=ORIGIN, status=TradeStatus.OPEN, option_type="CE",
                structural_stop_level=None, structural_target_level=None, spot_at_entry=None, entry_time=None) -> StrategyTrade:
    trade = StrategyTrade(
        trade_id=trade_id, strategy_name="Validated Signal - Bank Nifty (Morning Impulse)", signal=f"BUY_{option_type}",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28SEP2026", option_type=option_type, quantity=35,
        entry_price=100.0, current_premium=100.0, stoploss=1.0, target=10000.0,
        entry_time=entry_time or utc_now(),
        origin=origin, status=status, result=TradeResult.OPEN if status == TradeStatus.OPEN else TradeResult.WIN,
        mode=TradingMode.PAPER,
        structural_stop_level=structural_stop_level, structural_target_level=structural_target_level,
        spot_at_entry=spot_at_entry,
    )
    db.add(trade)
    db.commit()
    return trade


class FakeTradeManager:
    def __init__(self) -> None:
        self.closed: list[tuple[StrategyTrade, float, ExitReason]] = []

    def close_trade(self, db, trade, exit_price, reason):
        trade.status = TradeStatus.CLOSED
        trade.exit_reason = reason.value
        self.closed.append((trade, exit_price, reason))


# ---------------------------------------------------------------------------
# select_itm_strike -- Section 6, preserved verbatim
# ---------------------------------------------------------------------------

def test_select_itm_strike_buy_ce_spot_at_or_above_atm():
    # atm_strike = round(24305/50)*50 = 24300; spot(24305) >= atm -> atm - step
    assert select_itm_strike(24305.0, "BUY_CE", "NIFTY") == 24250


def test_select_itm_strike_buy_ce_spot_below_atm():
    # atm_strike = round(24290/50)*50 = 24300; spot(24290) < atm -> atm - 2*step
    assert select_itm_strike(24290.0, "BUY_CE", "NIFTY") == 24200


def test_select_itm_strike_buy_pe_spot_at_or_below_atm():
    assert select_itm_strike(24290.0, "BUY_PE", "NIFTY") == 24350


def test_select_itm_strike_buy_pe_spot_above_atm():
    assert select_itm_strike(24305.0, "BUY_PE", "NIFTY") == 24400


def test_select_itm_strike_uses_100_step_for_banknifty():
    # atm_strike = round(57030/100)*100 = 57000; spot >= atm -> atm - 100
    assert select_itm_strike(57030.0, "BUY_CE", "BANKNIFTY") == 56900


def test_select_itm_strike_raises_on_unknown_action():
    with pytest.raises(ValueError):
        select_itm_strike(24300.0, "SELL_CE", "NIFTY")


# ---------------------------------------------------------------------------
# _volume_sma -- NAMED DEVIATIONS #2 (real average, not /20.0 fixed)
# ---------------------------------------------------------------------------

def test_volume_sma_none_below_minimum_bars():
    volumes = [100.0, 100.0, 100.0]
    assert _volume_sma(volumes, 3) is None


def test_volume_sma_averages_actual_preceding_count_not_fixed_20():
    volumes = [100.0, 200.0, 300.0, 400.0, 500.0]  # 5 preceding bars, index 5
    volumes.append(999.0)
    assert _volume_sma(volumes, 5) == pytest.approx(300.0)


def test_volume_sma_caps_at_20_preceding_bars():
    volumes = [10.0] * 25 + [1000.0]  # 25 preceding bars of 10.0, then trigger
    assert _volume_sma(volumes, 25) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# _orb_levels / _box_levels
# ---------------------------------------------------------------------------

def test_orb_levels_none_with_fewer_than_two_bars():
    bars = [_bar(9, 15, 100, 105, 99, 104)]
    assert _orb_levels(bars) is None


def test_orb_levels_from_exactly_the_two_range_formation_bars():
    bars = [
        _bar(9, 15, 100, 105, 99, 104),
        _bar(9, 20, 104, 108, 103, 107),
        _bar(9, 25, 107, 110, 106, 109),  # outside [09:15,09:25) -- excluded
    ]
    assert _orb_levels(bars) == (108, 99)


def test_box_levels_none_when_no_bars_in_window():
    bars = [_bar(9, 15, 100, 105, 99, 104)]
    assert _box_levels(bars) is None


def test_box_levels_from_1100_1315_window():
    bars = [
        _bar(10, 55, 100, 105, 99, 104),   # excluded
        _bar(11, 0, 104, 108, 100, 106),
        _bar(13, 10, 106, 112, 98, 107),
        _bar(13, 15, 107, 200, 1, 150),    # excluded (>= 13:15)
    ]
    assert _box_levels(bars) == (112, 98)


# ---------------------------------------------------------------------------
# _evaluate_session1 -- Morning Trend Impulse
# ---------------------------------------------------------------------------

def _warm_volumes(n: int, base: float = 100.0) -> list[float]:
    return [base] * n


def test_session1_none_without_pdh_pdl():
    bars = [_bar(9, 15, 100, 101, 99, 100), _bar(9, 20, 100, 101, 99, 100)]
    bars += [_bar(9, 20 + 5 * i, 100, 101, 99, 100) for i in range(1, 20)]
    volumes = _warm_volumes(len(bars))
    assert _evaluate_session1(bars, volumes, None, None, "NIFTY") is None


def test_session1_bullish_breakout_opens_buy_ce():
    # PDH=24300, ORB high=24290 (from the first two bars) -> upper=24300
    bars = [_bar(9, 15, 24280, 24290, 24270, 24285), _bar(9, 20, 24285, 24288, 24280, 24284)]
    bars += [_bar(9, 20 + 5 * i, 24284, 24286, 24282, 24284, v=100.0) for i in range(1, 20)]
    # Trigger candle: genuine cross above 24300
    trigger = _bar(9, 20 + 5 * 20, 24298, 24320, 24297, 24310, v=500.0)
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [500.0]

    candidate = _evaluate_session1(bars, volumes, pdh=24300.0, pdl=24200.0, index_symbol="NIFTY")

    assert candidate is not None
    assert candidate.action == "BUY_CE"
    assert candidate.session == "MORNING_IMPULSE"
    assert candidate.spot_entry == 24310
    assert candidate.spot_sl == pytest.approx(24297 - 2.0)  # trigger low - Nifty buffer
    risk = 24310 - (24297 - 2.0)
    assert candidate.spot_target == pytest.approx(24310 + 2.0 * risk)
    assert candidate.hard_exit_hm == (11, 15)
    assert candidate.volume_ratio == pytest.approx(5.0)


def test_session1_bearish_breakdown_opens_buy_pe():
    bars = [_bar(9, 15, 24280, 24290, 24270, 24275), _bar(9, 20, 24275, 24278, 24260, 24265)]
    bars += [_bar(9, 20 + 5 * i, 24265, 24267, 24263, 24265, v=100.0) for i in range(1, 20)]
    # high-close=9, risk=(high+2)-close=11, within Nifty's 16pt Session 1 cap
    trigger = _bar(9, 20 + 5 * 20, 24202, 24205, 24190, 24196, v=500.0)
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [500.0]

    candidate = _evaluate_session1(bars, volumes, pdh=24350.0, pdl=24200.0, index_symbol="NIFTY")

    assert candidate is not None
    assert candidate.action == "BUY_PE"
    assert candidate.spot_sl == pytest.approx(24205 + 2.0)
    risk = (24205 + 2.0) - 24196
    assert candidate.spot_target == pytest.approx(24196 - 2.0 * risk)


def test_session1_volume_gate_blocks_when_not_surging():
    bars = [_bar(9, 15, 24280, 24290, 24270, 24285), _bar(9, 20, 24285, 24288, 24280, 24284)]
    bars += [_bar(9, 20 + 5 * i, 24284, 24286, 24282, 24284, v=100.0) for i in range(1, 20)]
    trigger = _bar(9, 20 + 5 * 20, 24298, 24320, 24297, 24310, v=110.0)  # below 1.5x SMA
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [110.0]

    assert _evaluate_session1(bars, volumes, pdh=24300.0, pdl=24200.0, index_symbol="NIFTY") is None


def test_session1_blowout_candle_gate_cancels_when_risk_too_wide():
    bars = [_bar(9, 15, 24280, 24290, 24270, 24285), _bar(9, 20, 24285, 24288, 24280, 24284)]
    bars += [_bar(9, 20 + 5 * i, 24284, 24286, 24282, 24284, v=100.0) for i in range(1, 20)]
    # Trigger low far below close -> risk > 16pt Nifty cap
    trigger = _bar(9, 20 + 5 * 20, 24298, 24320, 24280, 24310, v=500.0)
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [500.0]

    assert _evaluate_session1(bars, volumes, pdh=24300.0, pdl=24200.0, index_symbol="NIFTY") is None


def test_session1_upper_is_always_at_least_lower_by_construction():
    # UpperBoundary = max(PDH, ORB_High) >= ORB_High >= ORB_Low >= min(PDL,
    # ORB_Low) = LowerBoundary always -- see module docstring's NAMED
    # DEVIATIONS #3. Documents this invariant directly against a case that
    # might look like it could invert the two (PDH far below the opening
    # range, PDL far above it).
    bars = [_bar(9, 15, 100, 101, 99, 100), _bar(9, 20, 100, 101, 99, 100)]
    orb_high, orb_low = _orb_levels(bars)
    pdh, pdl = 10.0, 500.0  # PDH far below ORB, PDL far above it
    upper = max(pdh, orb_high)
    lower = min(pdl, orb_low)
    assert upper >= lower



# ---------------------------------------------------------------------------
# _evaluate_session2 -- Afternoon Box Expansion
# ---------------------------------------------------------------------------

def _box_bars(day: int = 4) -> list[Bar]:
    bars = []
    t = (11, 0)
    minutes = 11 * 60
    while minutes < 13 * 60 + 15:
        hh, mm = divmod(minutes, 60)
        bars.append(_bar(hh, mm, 24280, 24300, 24270, 24285, v=100.0, day=day))
        minutes += 5
    return bars


def test_session2_compression_precondition_disables_when_box_too_wide():
    bars = _box_bars()
    # Widen the box beyond Nifty's 45pt cap
    bars[0] = _bar(11, 0, 24280, 24400, 24100, 24285, v=100.0)
    trigger = _bar(14, 0, 24298, 24320, 24297, 24310, v=500.0)
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [500.0]

    assert _evaluate_session2(bars, volumes, "NIFTY") is None


def test_session2_bullish_expansion_opens_buy_ce():
    bars = _box_bars()
    box_high = max(b.high for b in bars)
    trigger = _bar(14, 0, box_high - 1, box_high + 15, box_high - 3, box_high + 10, v=500.0)
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [500.0]

    candidate = _evaluate_session2(bars, volumes, "NIFTY")

    assert candidate is not None
    assert candidate.action == "BUY_CE"
    assert candidate.session == "AFTERNOON_EXPANSION"
    assert candidate.hard_exit_hm == (15, 10)
    risk = trigger.close - (trigger.low - 2.0)
    assert candidate.spot_target == pytest.approx(trigger.close + 2.0 * risk)


def test_session2_bearish_expansion_opens_buy_pe():
    bars = _box_bars()
    box_low = min(b.low for b in bars)
    trigger = _bar(14, 0, box_low + 1, box_low - 2, box_low - 15, box_low - 10, v=500.0)
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [500.0]

    candidate = _evaluate_session2(bars, volumes, "NIFTY")

    assert candidate is not None
    assert candidate.action == "BUY_PE"


def test_session2_risk_cap_cancels_when_too_wide():
    bars = _box_bars()
    box_high = max(b.high for b in bars)
    # Nifty Session 2 cap is 18pts -- make the wick far below the close
    trigger = _bar(14, 0, box_high - 1, box_high + 30, box_high - 25, box_high + 20, v=500.0)
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [500.0]

    assert _evaluate_session2(bars, volumes, "NIFTY") is None


# ---------------------------------------------------------------------------
# evaluate_intraday_signal -- window gating
# ---------------------------------------------------------------------------

def test_evaluate_intraday_signal_none_outside_both_windows():
    bars = [_bar(9, 15, 100, 101, 99, 100)]
    now_ist = datetime(2026, 9, 4, 12, 0, tzinfo=IST)  # the Dead Zone
    assert evaluate_intraday_signal(bars, [100.0], 100.0, 90.0, now_ist, "NIFTY") is None


def test_evaluate_intraday_signal_none_on_length_mismatch():
    bars = [_bar(9, 15, 100, 101, 99, 100)]
    now_ist = datetime(2026, 9, 4, 9, 30, tzinfo=IST)
    assert evaluate_intraday_signal(bars, [100.0, 200.0], 100.0, 90.0, now_ist, "NIFTY") is None


def test_evaluate_intraday_signal_none_for_unsupported_index():
    bars = [_bar(9, 15, 100, 101, 99, 100)]
    now_ist = datetime(2026, 9, 4, 9, 30, tzinfo=IST)
    assert evaluate_intraday_signal(bars, [100.0], 100.0, 90.0, now_ist, "SENSEX") is None


# ---------------------------------------------------------------------------
# _has_open_trade_anywhere -- Single Active Position Rule across BOTH indices
# ---------------------------------------------------------------------------

def test_has_open_trade_anywhere_false_when_empty():
    db = _make_session()
    assert _has_open_trade_anywhere(db) is False


def test_has_open_trade_anywhere_true_regardless_of_which_index():
    db = _make_session()
    _add_trade(db, trade_id="t1", index_symbol="NIFTY")
    assert _has_open_trade_anywhere(db) is True


def test_has_open_trade_anywhere_false_for_other_origin():
    db = _make_session()
    _add_trade(db, trade_id="t1", origin="AI_ORIGIN_OPENAI")
    assert _has_open_trade_anywhere(db) is False


def test_has_open_trade_anywhere_false_when_closed():
    db = _make_session()
    _add_trade(db, trade_id="t1", status=TradeStatus.CLOSED)
    assert _has_open_trade_anywhere(db) is False


# ---------------------------------------------------------------------------
# open_validated_trade
# ---------------------------------------------------------------------------

def _candidate(action="BUY_CE", session="MORNING_IMPULSE", spot_entry=24310.0, spot_sl=24295.0, spot_target=24340.0,
               hard_exit_hm=(11, 15), volume_ratio=5.0) -> _SignalCandidate:
    return _SignalCandidate(action, session, spot_entry, spot_sl, spot_target, hard_exit_hm, volume_ratio)


def test_open_validated_trade_uses_select_itm_strike_and_stores_spot_levels():
    db = _make_session()
    index = _make_index("NIFTY")
    smartapi = FakeSmartAPI(price=55.0)
    option_finder = FakeOptionFinder(_make_contract(option_type="CE", strike=24250))
    candidate = _candidate()

    trade = open_validated_trade(db, index, candidate, smartapi, option_finder)

    assert trade is not None
    assert trade.origin == ORIGIN
    assert trade.mode == TradingMode.PAPER
    assert trade.sl_mode == "FIXED"
    assert trade.status == TradeStatus.OPEN
    assert option_finder.last_strike == select_itm_strike(24310.0, "BUY_CE", "NIFTY")
    assert trade.structural_stop_level == 24295.0
    assert trade.structural_target_level == 24340.0
    assert trade.spot_at_entry == 24310.0
    # Sentinel premium levels -- deliberately unreachable in either direction.
    assert trade.stoploss == round(55.0 * 0.01, 2)
    assert trade.target == round(55.0 * 100.0, 2)
    assert "Morning Impulse" in trade.strategy_name
    assert "spot" in trade.ai_reasoning.lower()


def test_open_validated_trade_declines_on_contract_resolution_failure():
    db = _make_session()
    index = _make_index("NIFTY")
    smartapi = FakeSmartAPI(price=55.0)
    option_finder = FakeOptionFinder(None)

    trade = open_validated_trade(db, index, _candidate(), smartapi, option_finder)

    assert trade is None


def test_open_validated_trade_declines_on_missing_ltp():
    db = _make_session()
    index = _make_index("NIFTY")
    smartapi = FakeSmartAPI(price=None)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_validated_trade(db, index, _candidate(), smartapi, option_finder)

    assert trade is None


def test_open_validated_trade_never_places_a_real_order():
    db = _make_session()
    index = _make_index("NIFTY")
    smartapi = FakeSmartAPI(price=55.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_validated_trade(db, index, _candidate(), smartapi, option_finder)

    assert trade is not None
    assert trade.mode == TradingMode.PAPER


# ---------------------------------------------------------------------------
# check_validated_signal_exits -- Section 5's 4-condition exit engine
# ---------------------------------------------------------------------------

def test_exits_no_open_trades_makes_zero_smartapi_calls():
    db = _make_session()
    smartapi = FakeSmartAPI()
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, to_ist(utc_now()))

    assert smartapi.spot_calls == 0
    assert trade_manager.closed == []


def test_exits_spot_stop_trigger_ce():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    trade = _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=utc_now() - timedelta(minutes=5),
    )
    smartapi = FakeSmartAPI(price=95.0, spot=56999.0)  # breached the stop
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, to_ist(utc_now()))

    assert len(trade_manager.closed) == 1
    closed_trade, exit_price, reason = trade_manager.closed[0]
    assert closed_trade.trade_id == "t1"
    assert reason == ExitReason.VS_SPOT_STOP


def test_exits_spot_stop_trigger_pe():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    _add_trade(
        db, trade_id="t1", option_type="PE", structural_stop_level=57100.0, structural_target_level=56900.0,
        spot_at_entry=57050.0, entry_time=utc_now() - timedelta(minutes=5),
    )
    smartapi = FakeSmartAPI(price=95.0, spot=57101.0)  # breached the PE stop (moved up)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, to_ist(utc_now()))

    assert trade_manager.closed[0][2] == ExitReason.VS_SPOT_STOP


def test_exits_spot_target_trigger_ce():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=utc_now() - timedelta(minutes=5),
    )
    smartapi = FakeSmartAPI(price=150.0, spot=57201.0)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, to_ist(utc_now()))

    assert trade_manager.closed[0][2] == ExitReason.VS_SPOT_TARGET


def test_exits_stagnation_after_20_minutes_with_no_favorable_move():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=utc_now() - timedelta(minutes=21),
    )
    # risk = 50; favorable_move = 57055-57050 = 5 < 50 -> stagnation
    smartapi = FakeSmartAPI(price=99.0, spot=57055.0)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, to_ist(utc_now()))

    assert trade_manager.closed[0][2] == ExitReason.VS_STAGNATION_EXIT


def test_exits_no_stagnation_before_20_minutes():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    now_ist = datetime(2026, 9, 4, 9, 40, tzinfo=IST)
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=now_ist - timedelta(minutes=10),
    )
    smartapi = FakeSmartAPI(price=99.0, spot=57055.0)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, now_ist)

    assert trade_manager.closed == []


def test_exits_no_stagnation_when_move_is_genuinely_favorable():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=utc_now() - timedelta(minutes=25),
    )
    # favorable_move = 57120-57050 = 70 >= risk(50) -- real move, not stagnant
    smartapi = FakeSmartAPI(price=120.0, spot=57120.0)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, to_ist(utc_now()))

    assert trade_manager.closed == []


def test_exits_morning_hard_exit_at_1115():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    # Entered less than 20 minutes before the hard exit, so the stagnation
    # check (elapsed >= 20) is deliberately NOT what would fire here --
    # this isolates the hard-exit condition on its own.
    entry_ist = datetime(2026, 9, 4, 11, 0, tzinfo=IST)
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=entry_ist.astimezone(UTC),
    )
    now_ist = datetime(2026, 9, 4, 11, 15, tzinfo=IST)
    # No move at all, but past the hard exit boundary
    smartapi = FakeSmartAPI(price=100.0, spot=57050.0)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, now_ist)

    assert trade_manager.closed[0][2] == ExitReason.TIME_EXIT


def test_exits_afternoon_hard_exit_at_1510():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    entry_ist = datetime(2026, 9, 4, 14, 55, tzinfo=IST)  # <20 min before hard exit
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=entry_ist.astimezone(UTC),
    )
    now_ist = datetime(2026, 9, 4, 15, 10, tzinfo=IST)
    smartapi = FakeSmartAPI(price=100.0, spot=57050.0)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, now_ist)

    assert trade_manager.closed[0][2] == ExitReason.TIME_EXIT


def test_exits_stop_check_wins_over_hard_exit_when_both_true():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    entry_ist = datetime(2026, 9, 4, 9, 40, tzinfo=IST)
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=entry_ist.astimezone(UTC),
    )
    now_ist = datetime(2026, 9, 4, 11, 20, tzinfo=IST)  # past the 11:15 hard exit too
    smartapi = FakeSmartAPI(price=80.0, spot=56999.0)  # AND the stop is breached
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, now_ist)

    assert trade_manager.closed[0][2] == ExitReason.VS_SPOT_STOP


def test_exits_skips_trade_missing_structural_levels():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    _add_trade(db, trade_id="t1", option_type="CE")  # no structural levels set
    smartapi = FakeSmartAPI(price=100.0, spot=57000.0)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, to_ist(utc_now()))

    assert trade_manager.closed == []
    assert smartapi.spot_calls == 0


def test_exits_falls_back_to_current_premium_when_fresh_ltp_fails():
    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=utc_now() - timedelta(minutes=5),
    )

    class _FailingLtpSmartAPI(FakeSmartAPI):
        def get_ltp(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    smartapi = _FailingLtpSmartAPI(spot=56999.0)
    trade_manager = FakeTradeManager()

    check_validated_signal_exits(db, trade_manager, smartapi, to_ist(utc_now()))

    assert len(trade_manager.closed) == 1
    assert trade_manager.closed[0][1] == 100.0  # fell back to trade.current_premium


# ---------------------------------------------------------------------------
# run_validated_signal_entry_checks -- scheduler entry point
# ---------------------------------------------------------------------------

def test_run_entry_checks_noop_without_dependencies():
    run_validated_signal_entry_checks(None, None)  # must not raise


def test_run_entry_checks_noop_on_a_weekend(monkeypatch):
    import app.validated_signal as module

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 6, 10, 0, tzinfo=IST))  # a Sunday

    class _ExplodingSmartAPI(FakeSmartAPI):
        def get_candles(self, *_a, **_k):
            raise AssertionError("must not fetch candles when market is closed")

    run_validated_signal_entry_checks(_ExplodingSmartAPI(), FakeOptionFinder(None))


def test_run_entry_checks_skips_outside_both_entry_windows(monkeypatch):
    import app.validated_signal as module

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 12, 0, tzinfo=IST))  # the Dead Zone

    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()

    def _exploding(*_a, **_k):
        raise AssertionError("must not load features outside both entry windows")

    monkeypatch.setattr(module, "_load_index_features", _exploding)

    run_validated_signal_entry_checks(FakeSmartAPI(), FakeOptionFinder(None), db=db)


def test_run_entry_checks_skips_when_a_position_is_already_open(monkeypatch):
    import app.validated_signal as module

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 4, 9, 40, tzinfo=IST))

    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    _add_trade(db, trade_id="t1", index_symbol="BANKNIFTY")

    def _exploding(*_a, **_k):
        raise AssertionError("must not evaluate a new entry -- single active position rule")

    monkeypatch.setattr(module, "_load_index_features", _exploding)

    run_validated_signal_entry_checks(FakeSmartAPI(), FakeOptionFinder(None), db=db)


def test_run_entry_checks_opens_trade_end_to_end(monkeypatch):
    import app.validated_signal as module

    now_ist = datetime(2026, 9, 4, 10, 0, tzinfo=IST)
    monkeypatch.setattr(module, "utc_now", lambda: now_ist)

    db = _make_session()
    index = _make_index("NIFTY")
    db.add(index)
    db.commit()

    bars = [_bar(9, 15, 24280, 24290, 24270, 24285), _bar(9, 20, 24285, 24288, 24280, 24284)]
    bars += [_bar(9, 20 + 5 * i, 24284, 24286, 24282, 24284, v=100.0) for i in range(1, 20)]
    trigger = _bar(9, 20 + 5 * 20, 24298, 24320, 24297, 24310, v=500.0)
    bars.append(trigger)
    volumes = _warm_volumes(len(bars) - 1) + [500.0]

    def _fake_features(_db, idx, *_a, **_k):
        if idx.symbol == "NIFTY":
            return bars, volumes, 24300.0, 24200.0, False
        return [], [], None, None, False

    monkeypatch.setattr(module, "_load_index_features", _fake_features)

    option_finder = FakeOptionFinder(_make_contract(option_type="CE", strike=24250))
    run_validated_signal_entry_checks(FakeSmartAPI(price=55.0), option_finder, db=db)

    trades = db.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).all()
    assert len(trades) == 1
    assert trades[0].signal == "BUY_CE"
    assert trades[0].index_symbol == "NIFTY"


def test_run_entry_checks_cross_index_tie_break_picks_higher_volume_ratio(monkeypatch):
    import app.validated_signal as module

    now_ist = datetime(2026, 9, 4, 10, 0, tzinfo=IST)
    monkeypatch.setattr(module, "utc_now", lambda: now_ist)

    db = _make_session()
    db.add(_make_index("NIFTY"))
    db.add(_make_index("BANKNIFTY"))
    db.commit()

    def _build(index_symbol: str, volume_ratio_multiplier: float):
        base = 24280.0 if index_symbol == "NIFTY" else 57200.0
        bars = [_bar(9, 15, base, base + 10, base - 10, base + 5), _bar(9, 20, base + 5, base + 8, base, base + 4)]
        bars += [_bar(9, 20 + 5 * i, base + 4, base + 6, base + 2, base + 4, v=100.0) for i in range(1, 20)]
        trigger_v = 100.0 * 1.5 * volume_ratio_multiplier
        trigger = _bar(9, 20 + 5 * 20, base + 18, base + 40, base + 17, base + 30, v=trigger_v)
        bars.append(trigger)
        volumes = _warm_volumes(len(bars) - 1) + [trigger_v]
        pdh = base + 20
        pdl = base - 30
        return bars, volumes, pdh, pdl

    nifty_bars, nifty_vols, nifty_pdh, nifty_pdl = _build("NIFTY", 2.0)
    bn_bars, bn_vols, bn_pdh, bn_pdl = _build("BANKNIFTY", 4.0)  # stronger volume ratio

    def _fake_features(_db, idx, *_a, **_k):
        if idx.symbol == "NIFTY":
            return nifty_bars, nifty_vols, nifty_pdh, nifty_pdl, False
        return bn_bars, bn_vols, bn_pdh, bn_pdl, False

    monkeypatch.setattr(module, "_load_index_features", _fake_features)

    option_finder = FakeOptionFinder(_make_contract(option_type="CE", strike=57100))
    run_validated_signal_entry_checks(FakeSmartAPI(price=55.0), option_finder, db=db)

    trades = db.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).all()
    assert len(trades) == 1
    assert trades[0].index_symbol == "BANKNIFTY"  # higher volume ratio wins


def test_run_entry_checks_halts_on_refresh_failure(monkeypatch):
    import app.validated_signal as module

    now_ist = datetime(2026, 9, 4, 10, 0, tzinfo=IST)
    monkeypatch.setattr(module, "utc_now", lambda: now_ist)

    db = _make_session()
    db.add(_make_index("NIFTY"))
    db.commit()

    monkeypatch.setattr(module, "_load_index_features", lambda *a, **k: ([], [], None, None, True))

    option_finder = FakeOptionFinder(None)
    run_validated_signal_entry_checks(FakeSmartAPI(), option_finder, db=db)

    assert option_finder.calls == 0


# ---------------------------------------------------------------------------
# run_validated_signal_exit_checks -- scheduler entry point
# ---------------------------------------------------------------------------

def test_run_exit_checks_noop_without_dependencies():
    run_validated_signal_exit_checks(None, None)


def test_run_exit_checks_noop_on_a_weekend(monkeypatch):
    import app.validated_signal as module

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 9, 6, 10, 0, tzinfo=IST))  # a Sunday

    class _ExplodingSmartAPI(FakeSmartAPI):
        def get_index_spot(self, _index):
            raise AssertionError("must not fetch spot when market is closed")

    run_validated_signal_exit_checks(_ExplodingSmartAPI(), FakeTradeManager())


def test_run_exit_checks_delegates_end_to_end(monkeypatch):
    import app.validated_signal as module

    now_ist = datetime(2026, 9, 4, 11, 15, tzinfo=IST)
    monkeypatch.setattr(module, "utc_now", lambda: now_ist)

    db = _make_session()
    db.add(_make_index("BANKNIFTY"))
    db.commit()
    entry_ist = datetime(2026, 9, 4, 11, 0, tzinfo=IST)  # <20 min before hard exit
    _add_trade(
        db, trade_id="t1", option_type="CE", structural_stop_level=57000.0, structural_target_level=57200.0,
        spot_at_entry=57050.0, entry_time=entry_ist.astimezone(UTC),
    )

    trade_manager = FakeTradeManager()
    run_validated_signal_exit_checks(FakeSmartAPI(price=100.0, spot=57050.0), trade_manager, db=db)

    assert trade_manager.closed[0][2] == ExitReason.TIME_EXIT
