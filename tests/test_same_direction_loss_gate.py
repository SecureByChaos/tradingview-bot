from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.originator import (
    _DEFAULT_MAX_SAME_DIRECTION_LOSSES,
    _MAX_SL_TARGET_PERCENT,
    _TRAIL_ACTIVATION_NOMINAL,
    _Decision,
    _max_same_direction_losses,
    _max_sl_percent,
    _open_trade,
    _same_direction_consecutive_losses,
    _trail_activate_nominal,
)
from app.ai.repository import create_settings
from app.db_models import Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import OptionContract, Signal
from app.time_utils import to_ist, utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(symbol="BANKNIFTY", display_name="Bank Nifty", ai_origination_live_trade=False)


def _trade(**overrides) -> StrategyTrade:
    fields = dict(
        trade_id="t-1",
        strategy_name="AI_ORIGIN", signal="BUY_CE", index_symbol="BANKNIFTY",
        tradingsymbol="X", symboltoken="1", strike=57000, expiry="28AUG2026",
        option_type="CE", quantity=35, entry_price=100.0, stoploss=90.0, target=120.0,
        entry_time=utc_now(), origin="AI_ORIGIN_CLAUDE", status=TradeStatus.CLOSED,
        result=TradeResult.LOSS, mode=TradingMode.PAPER,
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def _make_decision(action: str = "BUY_PE") -> _Decision:
    return _Decision(action=action, confidence=0.7, sl_percent=10.0, target_percent=20.0, reasoning="test")


class _FakeSettings:
    live_trading = False


class FakeSmartAPI:
    def __init__(self, price: float = 100.0) -> None:
        self.price = price
        self.settings = _FakeSettings()
        self.ltp_calls = 0

    def get_ltp(self, *_args, **_kwargs) -> float:
        self.ltp_calls += 1
        return self.price

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("should never place a real order in these tests")


class FakeOptionFinder:
    def __init__(self, contract: OptionContract) -> None:
        self.contract = contract
        self.calls = 0

    def find_atm_contract(self, signal: Signal, index: IndexConfig, offset: int, min_dte: int | None = None) -> OptionContract:
        self.calls += 1
        return self.contract


def _make_contract() -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=20)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol="BANKNIFTY25AUG2650000PE",
        symboltoken="123",
        strike=50000,
        expiry=expiry,
        option_type="PE",
        lot_size=35,
    )


# ---------------------------------------------------------------------------
# _same_direction_consecutive_losses
# ---------------------------------------------------------------------------

def test_no_trades_today_is_zero():
    db = _make_session()
    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 0


def test_two_losses_in_a_row_counts_two():
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), result=TradeResult.LOSS))
    db.commit()

    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 2


def test_a_win_resets_the_streak_to_zero():
    # The exact scenario from the request: 2 trades today, one won and one
    # lost -- the threshold must NOT apply (streak counts from the most
    # recent trade backward, and the most recent one here is a win).
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), result=TradeResult.WIN))
    db.commit()

    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 0


def test_win_then_loss_only_counts_the_trailing_loss():
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=90), result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=60), result=TradeResult.WIN))
    db.add(_trade(trade_id="t3", entry_time=now - timedelta(minutes=30), result=TradeResult.LOSS))
    db.commit()

    # Most recent (t3) is a loss, but the win at t2 breaks the chain before t1.
    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 1


def test_breakeven_also_resets_the_streak():
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), result=TradeResult.BREAKEVEN))
    db.commit()

    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 0


def test_counts_across_providers():
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), result=TradeResult.LOSS, origin="AI_ORIGIN_CLAUDE"))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), result=TradeResult.LOSS, origin="AI_ORIGIN_OPENAI"))
    db.commit()

    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 2


def test_scoped_per_direction_not_per_index():
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), signal="BUY_CE", result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), signal="BUY_CE", result=TradeResult.LOSS))
    db.commit()

    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_PE") == 0


def test_open_trades_are_excluded_entirely():
    # An unresolved trade sitting between two losses must not break the
    # streak -- it has no outcome yet, so it's skipped, not counted as a win.
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=90), result=TradeResult.LOSS))
    db.add(_trade(
        trade_id="t2", entry_time=now - timedelta(minutes=60), status=TradeStatus.OPEN,
        result=TradeResult.OPEN, exit_time=None,
    ))
    db.add(_trade(trade_id="t3", entry_time=now - timedelta(minutes=30), result=TradeResult.LOSS))
    db.commit()

    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 2


def test_trades_from_a_previous_day_do_not_count():
    db = _make_session()
    yesterday = utc_now() - timedelta(days=1)
    db.add(_trade(trade_id="t1", entry_time=yesterday, result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=yesterday - timedelta(hours=1), result=TradeResult.LOSS))
    db.commit()

    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 0


def test_non_ai_origination_trades_are_excluded():
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now, result=TradeResult.LOSS, origin="SIGNAL"))
    db.add(_trade(trade_id="t2", entry_time=now, result=TradeResult.LOSS, origin="AI_ALT_CLAUDE"))
    db.commit()

    assert _same_direction_consecutive_losses(db, "BANKNIFTY", "BUY_CE") == 0


# ---------------------------------------------------------------------------
# _max_same_direction_losses / _max_sl_percent -- admin-configurable knobs
# ---------------------------------------------------------------------------

def test_max_same_direction_losses_falls_back_without_settings_row():
    db = _make_session()
    assert _max_same_direction_losses(db) == _DEFAULT_MAX_SAME_DIRECTION_LOSSES


def test_max_same_direction_losses_reads_admin_configured_value():
    db = _make_session()
    create_settings(db, id=1, ai_origination_max_same_direction_losses=1)
    assert _max_same_direction_losses(db) == 1


def test_max_sl_percent_falls_back_without_settings_row():
    db = _make_session()
    assert _max_sl_percent(db) == _MAX_SL_TARGET_PERCENT


def test_max_sl_percent_reads_admin_configured_value():
    db = _make_session()
    create_settings(db, id=1, ai_origination_max_sl_percent=20.0)
    assert _max_sl_percent(db) == 20.0


# ---------------------------------------------------------------------------
# _open_trade integration -- the gate as actually wired in
# ---------------------------------------------------------------------------

def test_open_trade_blocks_after_two_consecutive_losses():
    db = _make_session()
    index = _make_index()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), signal="BUY_PE", result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), signal="BUY_PE", result=TradeResult.LOSS))
    db.commit()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert result is None
    assert option_finder.calls == 0  # gate short-circuits before any contract resolution
    assert smartapi.ltp_calls == 0


def test_open_trade_allows_one_win_one_loss_today():
    # The exact worked example from the request.
    db = _make_session()
    index = _make_index()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), signal="BUY_PE", result=TradeResult.WIN))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), signal="BUY_PE", result=TradeResult.LOSS))
    db.commit()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert result is not None
    assert option_finder.calls == 1


def test_open_trade_allows_first_entry_of_the_day():
    db = _make_session()
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert result is not None


def test_open_trade_gate_is_per_direction():
    db = _make_session()
    index = _make_index()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), signal="BUY_CE", result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), signal="BUY_CE", result=TradeResult.LOSS))
    db.commit()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert result is not None


def test_open_trade_honors_admin_configured_threshold_of_one():
    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_max_same_direction_losses=1)
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=30), signal="BUY_PE", result=TradeResult.LOSS))
    db.commit()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert result is None  # a single loss is already enough at threshold=1


def test_open_trade_no_market_context_still_gates_correctly():
    # Unlike the old market_context-driven gate, this one queries the DB
    # directly and must keep working even when market_context is None.
    db = _make_session()
    index = _make_index()
    now = utc_now()
    db.add(_trade(trade_id="t1", entry_time=now - timedelta(minutes=60), signal="BUY_PE", result=TradeResult.LOSS))
    db.add(_trade(trade_id="t2", entry_time=now - timedelta(minutes=30), signal="BUY_PE", result=TradeResult.LOSS))
    db.commit()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=None)

    assert result is None


# ---------------------------------------------------------------------------
# Stop/target sanity split -- admin max applies to the stop only
# ---------------------------------------------------------------------------

def test_open_trade_falls_back_to_trailing_when_stop_exceeds_admin_max():
    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_max_sl_percent=20.0)
    decision = _Decision(action="BUY_PE", confidence=0.7, sl_percent=45.0, target_percent=48.0, reasoning="test")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.sl_mode == "TRAILING"


def test_open_trade_uses_ai_stop_when_within_admin_max():
    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_max_sl_percent=20.0)
    decision = _Decision(action="BUY_PE", confidence=0.7, sl_percent=15.0, target_percent=25.0, reasoning="test")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.sl_mode == "FIXED"


def test_open_trade_target_ceiling_stays_fixed_at_fifty_even_with_lower_admin_stop_max():
    # Tightening the stop max must not also cap the target -- a target of 48%
    # is still within the original hardcoded 5-50% band and should be honored
    # even though the admin's own stop max is much lower.
    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_max_sl_percent=20.0)
    decision = _Decision(action="BUY_PE", confidence=0.7, sl_percent=15.0, target_percent=48.0, reasoning="test")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.sl_mode == "FIXED"


# ---------------------------------------------------------------------------
# 25 Aug 2026: admin max_sl_percent must cap the REALIZED stop, not just the
# AI's nominal pre-rescale input -- confirmed on a real trade that a nominal
# stop clearing the sanity check can still rescale wider than the admin's
# ceiling for a put. See CLAUDE.md.
# ---------------------------------------------------------------------------

def _widen_by(factor: float):
    """Fake symmetric_premium_percent -- multiplies every input by a fixed
    factor, simulating the real PE-widening rescale without needing a real
    fitted coefficients file (this sandbox has none)."""
    def _fake(proposed_percent, index_symbol, option_type, dte, moneyness="ATM"):
        return round(proposed_percent * factor, 2), True
    return _fake


def test_open_trade_clamps_a_rescaled_put_stop_to_the_admin_ceiling(monkeypatch):
    import app.ai.originator as originator_module
    monkeypatch.setattr(originator_module, "symmetric_premium_percent", _widen_by(1.45))

    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_max_sl_percent=12.0)
    # Nominal 12.0 clears _stop_is_sane (<= 12.0 ceiling); the fake rescale
    # then widens it to 17.4, mirroring the real 12% -> 17.39% production case.
    decision = _Decision(action="BUY_PE", confidence=0.7, sl_percent=12.0, target_percent=20.0, reasoning="test")
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.sl_mode == "FIXED"
    # Clamped back to the 12% ceiling, not the rescaled 17.4%.
    assert trade.stoploss == round(100.0 * (1 - 0.12), 2)


def test_open_trade_clamp_does_not_touch_the_target(monkeypatch):
    import app.ai.originator as originator_module
    monkeypatch.setattr(originator_module, "symmetric_premium_percent", _widen_by(1.45))

    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_max_sl_percent=12.0)
    decision = _Decision(action="BUY_PE", confidence=0.7, sl_percent=12.0, target_percent=20.0, reasoning="test")
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    # Target keeps the full rescaled 29% -- only the stop is capped by
    # max_sl_percent.
    assert trade.target == round(100.0 * (1 + 0.29), 2)


def test_open_trade_clamp_never_widens_a_stop_already_under_the_ceiling(monkeypatch):
    import app.ai.originator as originator_module
    monkeypatch.setattr(originator_module, "symmetric_premium_percent", _widen_by(1.2))

    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_max_sl_percent=20.0)
    decision = _Decision(action="BUY_PE", confidence=0.7, sl_percent=12.0, target_percent=20.0, reasoning="test")
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    # 12.0 * 1.2 = 14.4, comfortably under the 20.0 ceiling -- unclamped.
    assert trade.stoploss == round(100.0 * (1 - 0.144), 2)


def test_open_trade_clamp_applies_to_trailing_fallbacks_initial_stop_too(monkeypatch):
    import app.ai.originator as originator_module
    monkeypatch.setattr(originator_module, "symmetric_premium_percent", _widen_by(1.45))

    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_max_sl_percent=12.0)
    # sl_percent=45.0 fails _stop_is_sane -> falls back to TRAILING mode's
    # own _TRAILING_INITIAL_SL_PERCENT (10.0), which the fake rescale then
    # widens to 14.5 -- still above the 12.0 ceiling and still clamped.
    decision = _Decision(action="BUY_PE", confidence=0.7, sl_percent=45.0, target_percent=48.0, reasoning="test")
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.sl_mode == "TRAILING"
    assert trade.stoploss == round(100.0 * (1 - 0.12), 2)


# ---------------------------------------------------------------------------
# 25 Aug 2026: trail activation percent made admin-configurable -- a real
# trade's trailing stop never armed (MFE 9.12%, needed 11.59% once the CE/PE
# rescale widened the old hardcoded 8.0 nominal for a put).
# ---------------------------------------------------------------------------

def test_trail_activate_nominal_falls_back_without_settings_row():
    db = _make_session()
    assert _trail_activate_nominal(db) == _TRAIL_ACTIVATION_NOMINAL


def test_trail_activate_nominal_reads_admin_configured_value():
    db = _make_session()
    create_settings(db, id=1, ai_origination_trail_activate_percent=5.0)
    assert _trail_activate_nominal(db) == 5.0


def _identity_rescale(proposed_percent, index_symbol, option_type, dte, moneyness="ATM"):
    return proposed_percent, True


def test_open_trade_uses_admin_configured_trail_activate_nominal(monkeypatch):
    import app.ai.originator as originator_module
    monkeypatch.setattr(originator_module, "symmetric_premium_percent", _identity_rescale)

    db = _make_session()
    index = _make_index()
    create_settings(db, id=1, ai_origination_trail_activate_percent=5.0)
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.trail_activate_percent == 5.0


def test_open_trade_falls_back_to_default_trail_activate_without_admin_setting(monkeypatch):
    import app.ai.originator as originator_module
    monkeypatch.setattr(originator_module, "symmetric_premium_percent", _identity_rescale)

    db = _make_session()
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "claude", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.trail_activate_percent == _TRAIL_ACTIVATION_NOMINAL
