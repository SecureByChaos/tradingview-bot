from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db_models import (
    Base,
    PlatformSettings,
    StrategyTrade,
    TradeResult,
    TradeStatus,
    TradingMode,
)
from app.models import ExitReason, Signal
from app.multi_strategy import (
    _GIVEBACK_STOP_FLOOR_PERCENT,
    _GIVEBACK_STOP_ORIGINS,
    _GIVEBACK_STOP_RATIO,
    MultiStrategyTradeManager,
)
from app.time_utils import IST


class _NullTelegram:
    def send(self, *args, **kwargs) -> None:
        pass


class _SequenceSmartAPI:
    def __init__(self, prices: list[float]) -> None:
        self._prices = list(prices)

    def get_ltp(self, *args, **kwargs) -> float:
        return self._prices.pop(0)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _make_settings() -> Settings:
    return Settings(
        smartapi_api_key="x", smartapi_client_id="x", smartapi_pin="x", smartapi_totp_secret="x",
    )


def _enable_giveback_stop(db: Session) -> None:
    db.add(PlatformSettings(id=1, giveback_ratio_stop_enabled=True))
    db.commit()


def _open_trade(
    db: Session, origin: str, trade_id: str = "t1",
    entry_price: float = 100.0, stoploss: float = 50.0, target: float = 300.0,
) -> StrategyTrade:
    trade = StrategyTrade(
        trade_id=trade_id, strategy_name="Validated Signal - Bank Nifty", signal=Signal.BUY_CE.value,
        index_symbol="BANKNIFTY", tradingsymbol="BANKNIFTY19AUG26C57000", symboltoken="123",
        strike=57000, expiry="19AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, stoploss=stoploss, target=target,
        entry_time=datetime(2026, 9, 3, 4, 0, tzinfo=IST),  # ~09:30 IST
        mode=TradingMode.PAPER, status=TradeStatus.OPEN, result=TradeResult.OPEN,
        origin=origin, sl_mode="FIXED", highest_price=entry_price, lowest_price=entry_price,
    )
    db.add(trade)
    db.commit()
    return trade


def _ist_now(monkeypatch, when: datetime) -> None:
    import app.multi_strategy as multi_strategy_module

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(multi_strategy_module, "datetime", _FixedDateTime)


def _run(monkeypatch, db, prices: list[float]) -> StrategyTrade:
    _ist_now(monkeypatch, datetime(2026, 9, 3, 10, 0, tzinfo=IST))
    manager = MultiStrategyTradeManager(
        _make_settings(), _SequenceSmartAPI(prices), option_finder=None, telegram=_NullTelegram(),
    )
    manager.monitor_open_trades(db)
    return db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").first()


def test_constants_match_the_validated_backtest_cell():
    assert _GIVEBACK_STOP_FLOOR_PERCENT == 12.0
    assert _GIVEBACK_STOP_RATIO == 0.30


def test_included_origins_are_exactly_the_two_no_trailing_strategies():
    # VALIDATED_SIGNAL was REMOVED 5 Sep 2026 when that origin was rebuilt to
    # its own complete, spot-level stop/target/stagnation exit engine (see
    # app.validated_signal) -- this trial's "zero existing protection" scope
    # is no longer true for it.
    assert _GIVEBACK_STOP_ORIGINS == frozenset({"QUICK_SCALP", "AUTONOMOUS_AI"})


def test_toggle_off_by_default_behaves_exactly_as_before(monkeypatch, db_session: Session) -> None:
    # No PlatformSettings row seeded -- get_or_create_settings creates one
    # with the column's own default (False). A trade that peaks at +20%
    # (well past the 12% floor) then reverses all the way through the
    # ORIGINAL stop must still exit STOPLOSS, because the toggle is off --
    # a giveback level, if it were armed, would have caught this well
    # before it reached 45.
    _open_trade(db_session, origin="VALIDATED_SIGNAL", stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [120.0])
    trade = _run(monkeypatch, db_session, [45.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.STOPLOSS.value
    assert trade.exit_price == 45.0


def test_toggle_on_arms_and_catches_a_reversal_above_the_original_stop(monkeypatch, db_session: Session) -> None:
    _enable_giveback_stop(db_session)
    _open_trade(db_session, origin="AUTONOMOUS_AI", entry_price=100.0, stoploss=50.0, target=300.0)
    # Peak reaches 115 (15% MFE, clears the 12% floor). Giveback level =
    # 115 - 0.30*(115-100) = 110.5. A drop to 108 must trigger there, not
    # at the original 50 stop.
    _run(monkeypatch, db_session, [115.0])
    trade = _run(monkeypatch, db_session, [108.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.GIVEBACK_STOP.value
    assert trade.exit_price == 108.0  # closes at the real tick, not the theoretical 110.5 level


def test_toggle_on_never_arms_below_the_floor_falls_through_to_original_stop(monkeypatch, db_session: Session) -> None:
    _enable_giveback_stop(db_session)
    # Peak only reaches 105 (5% MFE, below the 12% floor).
    _open_trade(db_session, origin="QUICK_SCALP", entry_price=100.0, stoploss=90.0, target=300.0)
    _run(monkeypatch, db_session, [105.0])
    trade = _run(monkeypatch, db_session, [89.0])
    assert trade.exit_reason == ExitReason.STOPLOSS.value
    assert trade.exit_price == 89.0


def test_toggle_on_still_exits_at_target_when_reached(monkeypatch, db_session: Session) -> None:
    _enable_giveback_stop(db_session)
    _open_trade(db_session, origin="AUTONOMOUS_AI", entry_price=100.0, stoploss=50.0, target=130.0)
    trade = _run(monkeypatch, db_session, [131.0])
    assert trade.exit_reason == ExitReason.TARGET.value
    assert trade.exit_price == 131.0


def test_toggle_on_does_not_apply_to_ai_origination_trades(monkeypatch, db_session: Session) -> None:
    # AI Origination already has its own, MORE sensitive trail (arms at
    # +8%, wider than this mechanism's +12% floor) -- it fires first and
    # the trade must close via the EXISTING TRAIL_EXIT mechanism, never
    # GIVEBACK_STOP, confirming the new toggle adds nothing here.
    _enable_giveback_stop(db_session)
    _open_trade(db_session, origin="AI_ORIGIN_OPENAI", entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [115.0])
    trade = _run(monkeypatch, db_session, [108.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.TRAIL_EXIT.value


def test_toggle_on_does_not_apply_to_signal_origin_trades(monkeypatch, db_session: Session) -> None:
    # SIGNAL is the shared-FIXED-branch hazard population (BNV5.1/BNV6/BNV7/
    # NV1) -- must never be touched by this mechanism.
    _enable_giveback_stop(db_session)
    _open_trade(db_session, origin="SIGNAL", entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [115.0])
    trade = _run(monkeypatch, db_session, [108.0])
    assert trade.status == TradeStatus.OPEN


def test_toggle_on_does_not_apply_to_validated_signal_trades(monkeypatch, db_session: Session) -> None:
    # Removed from scope 5 Sep 2026 -- see the module-level comment on
    # _GIVEBACK_STOP_ORIGINS. This origin now runs its own complete,
    # spot-level exit engine (app.validated_signal); its premium stoploss
    # field is a deliberately unreachable sentinel, so confirm the giveback
    # mechanism can't fire on a real premium reversal that would otherwise
    # have armed it.
    _enable_giveback_stop(db_session)
    _open_trade(db_session, origin="VALIDATED_SIGNAL", entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [115.0])
    trade = _run(monkeypatch, db_session, [108.0])
    assert trade.status == TradeStatus.OPEN


def test_toggle_on_applies_to_quick_scalp(monkeypatch, db_session: Session) -> None:
    _enable_giveback_stop(db_session)
    _open_trade(db_session, origin="QUICK_SCALP", entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [115.0])
    trade = _run(monkeypatch, db_session, [108.0])
    assert trade.exit_reason == ExitReason.GIVEBACK_STOP.value


def test_toggle_on_applies_to_autonomous_ai(monkeypatch, db_session: Session) -> None:
    _enable_giveback_stop(db_session)
    _open_trade(db_session, origin="AUTONOMOUS_AI", entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [115.0])
    trade = _run(monkeypatch, db_session, [108.0])
    assert trade.exit_reason == ExitReason.GIVEBACK_STOP.value


def test_giveback_level_never_goes_below_the_original_stop(monkeypatch, db_session: Session) -> None:
    # A trade barely past the floor (peak 112, 12% MFE) still computes a
    # giveback level (112 - 0.3*12 = 108.4) comfortably above a 50 stop --
    # confirms the level is real room above the original stop, not
    # accidentally tighter than intended.
    _enable_giveback_stop(db_session)
    _open_trade(db_session, origin="QUICK_SCALP", entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [112.0])
    trade = _run(monkeypatch, db_session, [108.4])
    assert trade.exit_reason == ExitReason.GIVEBACK_STOP.value
    assert trade.exit_price == pytest.approx(108.4, abs=0.01)
