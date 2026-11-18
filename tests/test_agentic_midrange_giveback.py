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
from app.multi_strategy import MultiStrategyTradeManager
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


def _enable_midrange(db: Session, floor_percent: float = 2.0, ratio: float = 0.60, giveback_too: bool = False) -> None:
    db.add(
        PlatformSettings(
            id=1,
            agentic_midrange_giveback_enabled=True,
            agentic_midrange_giveback_floor_percent=floor_percent,
            agentic_midrange_giveback_ratio=ratio,
            giveback_ratio_stop_enabled=giveback_too,
        )
    )
    db.commit()


def _open_trade(
    db: Session, origin: str = "AGENTIC_AI", trade_id: str = "t1",
    entry_price: float = 100.0, stoploss: float = 50.0, target: float = 300.0,
) -> StrategyTrade:
    trade = StrategyTrade(
        trade_id=trade_id, strategy_name="Agentic AI - Bank Nifty", signal=Signal.BUY_CE.value,
        index_symbol="BANKNIFTY", tradingsymbol="BANKNIFTY19AUG26C57000", symboltoken="123",
        strike=57000, expiry="19AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, stoploss=stoploss, target=target,
        entry_time=datetime(2026, 11, 18, 4, 0, tzinfo=IST),  # ~09:30 IST
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
    _ist_now(monkeypatch, datetime(2026, 11, 18, 10, 0, tzinfo=IST))
    manager = MultiStrategyTradeManager(
        _make_settings(), _SequenceSmartAPI(prices), option_finder=None, telegram=_NullTelegram(),
    )
    manager.monitor_open_trades(db)
    return db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").first()


def test_toggle_off_by_default_behaves_exactly_as_before(monkeypatch, db_session: Session) -> None:
    # No PlatformSettings row seeded -- the column's own default is False.
    # A trade that peaks at +4.55% (well past the real 2.0% default floor,
    # but the mechanism must be a no-op with the toggle off) then reverses
    # straight through the original stop must exit STOPLOSS, reproducing
    # the real 18 Nov Nifty BUY_PE shape (MFE 4.55% -> closed -6.87%) that
    # motivated building this at all.
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [104.55])
    trade = _run(monkeypatch, db_session, [49.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.STOPLOSS.value
    assert trade.exit_price == 49.0


def test_toggle_on_arms_at_the_default_floor_and_catches_a_reversal(monkeypatch, db_session: Session) -> None:
    _enable_midrange(db_session)  # floor=2.0, ratio=0.60
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=300.0)
    # Peak 104.55 (4.55% MFE, the real 18 Nov Nifty trigger shape) clears
    # the 2% floor. Protected level = 104.55 - 0.60*(104.55-100) = 101.83.
    # A drop to 101.0 must trigger there, not run through to the plain
    # stop the way the real, unprotected trade did.
    _run(monkeypatch, db_session, [104.55])
    trade = _run(monkeypatch, db_session, [101.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AGENTIC_MIDRANGE_GIVEBACK_STOP.value
    assert trade.exit_price == 101.0  # closes at the real tick, not the theoretical level


def test_toggle_on_never_arms_below_the_floor_falls_through_to_original_stop(monkeypatch, db_session: Session) -> None:
    _enable_midrange(db_session, floor_percent=2.0)
    _open_trade(db_session, entry_price=100.0, stoploss=90.0, target=300.0)
    # Peak only reaches 101 (1% MFE, below the 2% floor) -- never arms.
    _run(monkeypatch, db_session, [101.0])
    trade = _run(monkeypatch, db_session, [89.0])
    assert trade.exit_reason == ExitReason.STOPLOSS.value
    assert trade.exit_price == 89.0


def test_toggle_on_still_exits_at_target_when_reached(monkeypatch, db_session: Session) -> None:
    _enable_midrange(db_session)
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=130.0)
    trade = _run(monkeypatch, db_session, [131.0])
    assert trade.exit_reason == ExitReason.TARGET.value
    assert trade.exit_price == 131.0


def test_never_fires_while_the_trade_keeps_making_new_highs(monkeypatch, db_session: Session) -> None:
    # The whole point of the ratchet shape: a trade that keeps running past
    # the floor must never be closed by this mechanism, only a real
    # reversal should. Peaks 105, then 112, then 120 with no pullback --
    # should stay open the entire way, letting the win grow rather than
    # cutting it off the moment it clears the floor.
    _enable_midrange(db_session, floor_percent=2.0, ratio=0.60)
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [105.0])
    trade = _run(monkeypatch, db_session, [112.0])
    assert trade.status == TradeStatus.OPEN
    trade = _run(monkeypatch, db_session, [120.0])
    assert trade.status == TradeStatus.OPEN


def test_protected_level_ratchets_up_with_a_new_peak(monkeypatch, db_session: Session) -> None:
    # Peak 1: 105 -> level = 105 - 0.6*5 = 102.0. Peak 2: 112 -> level =
    # 112 - 0.6*12 = 104.8, strictly higher than the first level. A price
    # of 103 would have been safe against the FIRST level but must now
    # trigger against the SECOND, proving the floor only ever rises.
    _enable_midrange(db_session, floor_percent=2.0, ratio=0.60)
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [105.0])
    _run(monkeypatch, db_session, [112.0])
    trade = _run(monkeypatch, db_session, [103.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AGENTIC_MIDRANGE_GIVEBACK_STOP.value
    assert trade.exit_price == 103.0


def test_admin_configured_floor_and_ratio_are_actually_used(monkeypatch, db_session: Session) -> None:
    # A custom floor (5%) and ratio (0.3, i.e. the same tightness as
    # GIVEBACK_STOP) must change behaviour, not just the defaults.
    _enable_midrange(db_session, floor_percent=5.0, ratio=0.30)
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=300.0)
    # Peak 104 (4% MFE) is below the custom 5% floor -- must not arm.
    _run(monkeypatch, db_session, [104.0])
    trade = _run(monkeypatch, db_session, [95.0])
    assert trade.status == TradeStatus.OPEN
    # Now push past the custom floor: peak 110 (10% MFE). Level =
    # 110 - 0.3*10 = 107.0.
    trade = _run(monkeypatch, db_session, [110.0])
    assert trade.status == TradeStatus.OPEN
    trade = _run(monkeypatch, db_session, [106.5])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AGENTIC_MIDRANGE_GIVEBACK_STOP.value


@pytest.mark.parametrize("origin", ["AUTONOMOUS_AI", "QUICK_SCALP", "SIGNAL", "AI_ORIGIN_OPENAI", "VALIDATED_SIGNAL"])
def test_does_not_apply_to_any_other_origin(monkeypatch, db_session: Session, origin: str) -> None:
    _enable_midrange(db_session)
    _open_trade(db_session, origin=origin, entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [104.55])
    trade = _run(monkeypatch, db_session, [101.0])
    # None of these origins' own mechanisms happen to fire on this exact
    # price path (AI_ORIGIN_* needs its own 8%+ trail to arm first, SIGNAL/
    # AUTONOMOUS_AI/QUICK_SCALP/VALIDATED_SIGNAL have no sub-12% giveback
    # protection at all) -- the trade must stay open, proving the new
    # mechanism itself never touched it.
    assert trade.status == TradeStatus.OPEN


def test_giveback_stop_wins_when_both_mechanisms_are_enabled_and_mfe_clears_12_percent(
    monkeypatch, db_session: Session,
) -> None:
    # With both toggles on, GIVEBACK_STOP's own tighter (ratio=0.30) level
    # must win over this mechanism's looser (ratio=0.60) one once MFE
    # clears 12% -- the two coexist without the mid-range cap shadowing
    # the already-validated mechanism's own reporting.
    _enable_midrange(db_session, floor_percent=2.0, ratio=0.60, giveback_too=True)
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=300.0)
    # Peak 115 (15% MFE). GIVEBACK_STOP level = 115 - 0.3*15 = 110.5.
    # Midrange level = 115 - 0.6*15 = 106.0. GIVEBACK_STOP's is tighter.
    _run(monkeypatch, db_session, [115.0])
    trade = _run(monkeypatch, db_session, [108.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.GIVEBACK_STOP.value


def test_midrange_cap_fires_below_12_percent_even_with_giveback_stop_also_enabled(
    monkeypatch, db_session: Session,
) -> None:
    # The actual gap this was built for: MFE never reaches GIVEBACK_STOP's
    # 12% floor, so only the mid-range mechanism can protect this trade.
    _enable_midrange(db_session, floor_percent=2.0, ratio=0.60, giveback_too=True)
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=300.0)
    _run(monkeypatch, db_session, [104.55])
    trade = _run(monkeypatch, db_session, [101.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AGENTIC_MIDRANGE_GIVEBACK_STOP.value


def test_reproduces_the_real_18_nov_bank_nifty_trigger_trade(monkeypatch, db_session: Session) -> None:
    # Real shape: entry 466.9, MFE 2.57% (peak ~478.9), closed at -20.71%
    # (370.2) via plain STOPLOSS with the cap off. With the default floor/
    # ratio enabled, the cap must catch this long before -20.71%.
    _enable_midrange(db_session)  # floor=2.0, ratio=0.60
    _open_trade(db_session, entry_price=466.9, stoploss=370.2, target=700.0)
    peak = 466.9 * 1.0257  # 2.57% MFE, matching the real trade
    _run(monkeypatch, db_session, [peak])
    protected_level = peak - 0.60 * (peak - 466.9)
    trade = _run(monkeypatch, db_session, [protected_level - 0.5])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AGENTIC_MIDRANGE_GIVEBACK_STOP.value
    # The real unprotected loss was -20.71% (exit 370.2) -- confirm this
    # closes well above that, i.e. it actually did its job.
    assert trade.exit_price > 370.2
