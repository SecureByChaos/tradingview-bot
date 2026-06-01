from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from zoneinfo import ZoneInfo

from app.config import Settings
from app.logger import TradeCSVLogger
from app.models import ActiveTrade, OptionContract, Signal
from app.trade_manager import TradeManager


IST = ZoneInfo("Asia/Kolkata")


class FakeSmartAPI:
    def __init__(self, prices: list[float]) -> None:
        self.prices = prices
        self.orders: list[tuple[str, int]] = []

    def get_ltp(self, *_: object) -> float:
        return self.prices.pop(0)

    def place_market_order(self, contract: OptionContract, transaction_type: str, quantity: int) -> str:
        self.orders.append((transaction_type, quantity))
        return "OID"

    def close_position(self, contract: OptionContract, quantity: int) -> str:
        self.orders.append(("SELL", quantity))
        return "EXIT"


class FakeOptionFinder:
    def __init__(self, contract: OptionContract) -> None:
        self.contract = contract

    def find_atm_contract(self, signal: Signal) -> OptionContract:
        return self.contract


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        smartapi_api_key="x",
        smartapi_client_id="x",
        smartapi_pin="x",
        smartapi_totp_secret="x",
        data_dir=tmp_path,
        trades_csv_path=tmp_path / "trades.csv",
        active_trade_path=tmp_path / "active_trade.json",
        instrument_cache_path=tmp_path / "instruments.json",
    )


def make_test_dir() -> Path:
    path = Path(".test-runs") / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_opens_trade_and_persists_state() -> None:
    tmp_path = make_test_dir()
    settings = make_settings(tmp_path)
    contract = OptionContract(
        tradingsymbol="BANKNIFTY01JAN2650000CE",
        symboltoken="123",
        strike=50000,
        expiry="01JAN2026",
        option_type="CE",
        lot_size=35,
    )
    smartapi = FakeSmartAPI([100.0])
    manager = TradeManager(settings, smartapi, FakeOptionFinder(contract), TradeCSVLogger(settings.trades_csv_path))

    response = manager.handle_signal(Signal.BUY_CE)

    assert response.accepted is True
    assert response.active_trade is not None
    assert response.active_trade.stoploss == 90.0
    assert response.active_trade.target == 120.0
    assert settings.active_trade_path.exists()


def test_ignores_signal_when_trade_is_active() -> None:
    tmp_path = make_test_dir()
    settings = make_settings(tmp_path)
    contract = OptionContract(
        tradingsymbol="BANKNIFTY01JAN2650000PE",
        symboltoken="456",
        strike=50000,
        expiry="01JAN2026",
        option_type="PE",
        lot_size=35,
    )
    settings.active_trade_path.write_text(
        ActiveTrade(
            signal=Signal.BUY_PE,
            contract=contract,
            entry_price=100,
            stoploss=90,
            target=120,
            quantity=35,
            entry_time=datetime.now(IST),
        ).model_dump_json(),
        encoding="utf-8",
    )
    manager = TradeManager(settings, FakeSmartAPI([100.0]), FakeOptionFinder(contract), TradeCSVLogger(settings.trades_csv_path))

    response = manager.handle_signal(Signal.BUY_CE)

    assert response.accepted is False
    assert "active trade" in response.message


def test_closes_on_target_and_logs_trade() -> None:
    tmp_path = make_test_dir()
    settings = make_settings(tmp_path)
    contract = OptionContract(
        tradingsymbol="BANKNIFTY01JAN2650000CE",
        symboltoken="123",
        strike=50000,
        expiry="01JAN2026",
        option_type="CE",
        lot_size=35,
    )
    logger = TradeCSVLogger(settings.trades_csv_path)
    smartapi = FakeSmartAPI([100.0, 121.0])
    manager = TradeManager(settings, smartapi, FakeOptionFinder(contract), logger)
    manager.handle_signal(Signal.BUY_CE)

    manager.evaluate_exit()

    rows = logger.read_all()
    assert len(rows) == 1
    assert rows[0]["exit_reason"] == "TARGET"
    assert rows[0]["pnl_percent"] == "21.0"
    assert settings.active_trade_path.read_text(encoding="utf-8") == ""
