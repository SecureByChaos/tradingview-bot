from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import Signal
from app.multi_strategy import MultiStrategyTradeManager, _QUOTE_BATCH_SIZE
from app.time_utils import utc_now


class _RecordingTelegram:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, db, message: str) -> None:
        self.messages.append(message)


def _make_settings() -> Settings:
    return Settings(smartapi_api_key="x", smartapi_client_id="x", smartapi_pin="x", smartapi_totp_secret="x")


def _row(trade_id: str, exchange: str = "NFO", tradingsymbol: str = "X", symboltoken: str = "1"):
    return SimpleNamespace(trade_id=trade_id, exchange=exchange, tradingsymbol=tradingsymbol, symboltoken=symboltoken)


def _pool_backed_session_factory():
    """A real file-backed SQLite engine -- QueuePool, matching production
    (app/database.py's own empirically-confirmed default), unlike the plain
    ':memory:' engines most of this test suite uses for convenience, whose
    pool behaviour under repeated open/close isn't the thing being tested
    here. Needed so pool.checkedout() reflects a REAL connection checkout/
    release cycle for the item-3 pool-discipline assertion below.

    Uses mkdtemp (atomic, race-free directory creation) rather than the
    deprecated tempfile.mktemp for the db file's path -- mktemp only
    generates a name, leaving a window where another process could create
    something at that path first (CodeQL: insecure temporary file, flagged
    on this exact line before this fix)."""
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "test.sqlite3")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _open_trade(db: Session, trade_id: str, symboltoken: str, strategy_name: str = "BNV7") -> StrategyTrade:
    trade = StrategyTrade(
        trade_id=trade_id, strategy_name=strategy_name, signal=Signal.BUY_CE.value, index_symbol="NIFTY",
        tradingsymbol=f"NIFTY19AUG26C{symboltoken}", symboltoken=symboltoken, strike=24000, expiry="19AUG2026",
        option_type="CE", quantity=75, entry_price=100.0, current_premium=100.0, stoploss=90.0, target=120.0,
        mode=TradingMode.PAPER, status=TradeStatus.OPEN, result=TradeResult.OPEN, exchange="NFO",
        entry_time=utc_now(),
    )
    db.add(trade)
    db.commit()
    return trade


class _BatchSmartAPI:
    def __init__(self, rows_by_call=None, raise_on_batch: bool = False) -> None:
        self._rows_by_call = list(rows_by_call or [])
        self._raise_on_batch = raise_on_batch
        self.market_data_calls: list[dict] = []
        self.ltp_calls: list[tuple] = []

    def get_market_data(self, mode, exchange_tokens):
        self.market_data_calls.append(exchange_tokens)
        if self._raise_on_batch:
            raise RuntimeError("broker error")
        return self._rows_by_call.pop(0) if self._rows_by_call else []

    def get_ltp(self, exchange, tradingsymbol, symboltoken):
        self.ltp_calls.append((exchange, tradingsymbol, symboltoken))
        return 105.0


class _NoBatchSmartAPI:
    """No get_market_data method at all -- the shape of every pre-9-Sep
    SmartAPI test stand-in in this codebase's OWN test suite."""

    def __init__(self) -> None:
        self.ltp_calls: list[tuple] = []

    def get_ltp(self, exchange, tradingsymbol, symboltoken):
        self.ltp_calls.append((exchange, tradingsymbol, symboltoken))
        return 99.0


# ---------------------------------------------------------------------------
# _fetch_premiums_batched
# ---------------------------------------------------------------------------

def test_fetch_premiums_uses_the_batch_for_a_single_chunk():
    smartapi = _BatchSmartAPI(rows_by_call=[[{"symbolToken": "1", "ltp": "101.5"}]])
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    result = manager._fetch_premiums_batched([_row("t1", symboltoken="1")])

    assert result == {"1": 101.5}
    assert len(smartapi.market_data_calls) == 1
    assert smartapi.ltp_calls == []


def test_fetch_premiums_chunks_at_the_50_token_cap():
    rows = [_row(f"t{i}", symboltoken=str(i)) for i in range(60)]
    smartapi = _BatchSmartAPI(rows_by_call=[[], []])  # neither chunk returns anything -- irrelevant to this test
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    manager._fetch_premiums_batched(rows)

    assert len(smartapi.market_data_calls) == 2
    assert sum(len(tokens) for tokens in smartapi.market_data_calls[0].values()) == _QUOTE_BATCH_SIZE
    assert sum(len(tokens) for tokens in smartapi.market_data_calls[1].values()) == 10


def test_fetch_premiums_falls_back_to_get_ltp_for_a_token_missing_from_the_batch():
    smartapi = _BatchSmartAPI(rows_by_call=[[{"symbolToken": "1", "ltp": "101.5"}]])  # only token "1", not "2"
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    result = manager._fetch_premiums_batched([_row("t1", symboltoken="1"), _row("t2", symboltoken="2")])

    assert result == {"1": 101.5, "2": 105.0}
    assert len(smartapi.ltp_calls) == 1  # only for the missing token


def test_fetch_premiums_falls_back_entirely_when_get_market_data_is_unavailable():
    smartapi = _NoBatchSmartAPI()
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    result = manager._fetch_premiums_batched([_row("t1", symboltoken="1"), _row("t2", symboltoken="2")])

    assert result == {"1": 99.0, "2": 99.0}
    assert len(smartapi.ltp_calls) == 2


def test_fetch_premiums_falls_back_entirely_when_the_batch_call_raises():
    smartapi = _BatchSmartAPI(raise_on_batch=True)
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    result = manager._fetch_premiums_batched([_row("t1", symboltoken="1")])

    assert result == {"1": 105.0}
    assert len(smartapi.ltp_calls) == 1


def test_fetch_premiums_a_failed_individual_fallback_is_simply_absent():
    class _AllFailSmartAPI(_BatchSmartAPI):
        def get_ltp(self, *args, **kwargs):
            raise RuntimeError("still broken")

    smartapi = _AllFailSmartAPI(rows_by_call=[[]])
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    result = manager._fetch_premiums_batched([_row("t1", symboltoken="1")])

    assert result == {}


# ---------------------------------------------------------------------------
# monitor_open_trades -- batching wired through, session discipline
# ---------------------------------------------------------------------------

def test_monitor_open_trades_uses_the_batch_not_per_trade_get_ltp():
    engine, SessionLocal = _pool_backed_session_factory()
    with SessionLocal() as db:
        _open_trade(db, "t1", "111")
        _open_trade(db, "t2", "222")

    smartapi = _BatchSmartAPI(rows_by_call=[[
        {"symbolToken": "111", "ltp": "108.0"}, {"symbolToken": "222", "ltp": "112.0"},
    ]])
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    with SessionLocal() as db:
        manager.monitor_open_trades(db)

    assert len(smartapi.market_data_calls) == 1
    assert smartapi.ltp_calls == []
    with SessionLocal() as db:
        t1 = db.scalar(select(StrategyTrade).where(StrategyTrade.trade_id == "t1"))
        t2 = db.scalar(select(StrategyTrade).where(StrategyTrade.trade_id == "t2"))
        assert t1.current_premium == 108.0
        assert t2.current_premium == 112.0


def test_monitor_open_trades_skips_a_trade_with_no_premium_and_does_not_alert():
    engine, SessionLocal = _pool_backed_session_factory()
    with SessionLocal() as db:
        _open_trade(db, "t1", "111")

    class _AllFailSmartAPI(_BatchSmartAPI):
        def get_ltp(self, *args, **kwargs):
            raise RuntimeError("still broken")

    smartapi = _AllFailSmartAPI(rows_by_call=[[]])
    telegram = _RecordingTelegram()
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, telegram)

    with SessionLocal() as db:
        closed = manager.monitor_open_trades(db)  # must not raise

    assert closed == []
    assert telegram.messages == []  # a routine missing quote is not a "System Error"
    with SessionLocal() as db:
        t1 = db.scalar(select(StrategyTrade).where(StrategyTrade.trade_id == "t1"))
        assert t1.current_premium == 100.0  # untouched -- never updated with a guessed value
        assert t1.status == TradeStatus.OPEN


def test_monitor_open_trades_recheck_skips_a_trade_closed_between_phase_a_and_c():
    # Simulates the real race this Phase C re-check guards against: another
    # job (a kill switch, the shared square-off) closes the trade in the gap
    # between Phase A's read and Phase C's write -- here, as a side effect
    # of the batch quote call itself, via a completely separate session.
    engine, SessionLocal = _pool_backed_session_factory()
    with SessionLocal() as db:
        _open_trade(db, "t1", "111")

    class _ClosesTradeMidflightSmartAPI(_BatchSmartAPI):
        def get_market_data(self, mode, exchange_tokens):
            with SessionLocal() as other_session:
                trade = other_session.scalar(select(StrategyTrade).where(StrategyTrade.trade_id == "t1"))
                trade.status = TradeStatus.CLOSED
                other_session.commit()
            return super().get_market_data(mode, exchange_tokens)

    smartapi = _ClosesTradeMidflightSmartAPI(rows_by_call=[[{"symbolToken": "111", "ltp": "108.0"}]])
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    with SessionLocal() as db:
        closed = manager.monitor_open_trades(db)  # must not raise, must not double-process t1

    assert closed == []


def test_monitor_open_trades_releases_the_pool_connection_before_phase_b():
    # The item-3 requirement, made concrete: a DB session must never be
    # checked out during the network fetch. Asserted directly against the
    # real connection pool's own checkedout() counter at the moment the
    # (fake) SmartAPI call is made -- if Phase A's commit didn't actually
    # release the connection, this would read >=1 instead of 0.
    engine, SessionLocal = _pool_backed_session_factory()
    with SessionLocal() as db:
        _open_trade(db, "t1", "111")

    checked_out_during_fetch: list[int] = []

    class _PoolCheckingSmartAPI(_BatchSmartAPI):
        def get_market_data(self, mode, exchange_tokens):
            checked_out_during_fetch.append(engine.pool.checkedout())
            return super().get_market_data(mode, exchange_tokens)

    smartapi = _PoolCheckingSmartAPI(rows_by_call=[[{"symbolToken": "111", "ltp": "108.0"}]])
    manager = MultiStrategyTradeManager(_make_settings(), smartapi, None, _RecordingTelegram())

    with SessionLocal() as db:
        manager.monitor_open_trades(db)

    assert checked_out_during_fetch == [0]
