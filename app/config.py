from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


@dataclass(frozen=True)
class Settings:
    smartapi_api_key: str
    smartapi_client_id: str
    smartapi_pin: str
    smartapi_totp_secret: str
    live_trading: bool = False
    quantity_lots: int = 1
    banknifty_lot_size: int = 35
    banknifty_spot_exchange: str = "NSE"
    banknifty_spot_symbol: str = "Nifty Bank"
    banknifty_spot_token: str = "99926009"
    product_type: str = "INTRADAY"
    order_variety: str = "NORMAL"
    instrument_master_url: str = DEFAULT_INSTRUMENT_MASTER_URL
    data_dir: Path = DATA_DIR
    trades_csv_path: Path = DATA_DIR / "trades.csv"
    active_trade_path: Path = DATA_DIR / "active_trade.json"
    instrument_cache_path: Path = DATA_DIR / "instruments.json"
    database_url: str = f"sqlite:///{DATA_DIR / 'platform.sqlite3'}"
    admin_username: str = "admin"
    admin_password: str = ""
    session_secret_key: str = "change-me-in-production"
    secure_cookies: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @property
    def order_quantity(self) -> int:
        return self.quantity_lots * self.banknifty_lot_size


def _get_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return default
    return int(value)


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        smartapi_api_key=os.getenv("SMARTAPI_API_KEY", ""),
        smartapi_client_id=os.getenv("SMARTAPI_CLIENT_ID", ""),
        smartapi_pin=os.getenv("SMARTAPI_PIN", ""),
        smartapi_totp_secret=os.getenv("SMARTAPI_TOTP_SECRET", ""),
        live_trading=_get_bool("SMARTAPI_LIVE_TRADING", False),
        quantity_lots=_get_int("QUANTITY_LOTS", 1),
        banknifty_lot_size=_get_int("BANKNIFTY_LOT_SIZE", 35),
        banknifty_spot_exchange=os.getenv("BANKNIFTY_SPOT_EXCHANGE", "NSE"),
        banknifty_spot_symbol=os.getenv("BANKNIFTY_SPOT_SYMBOL", "Nifty Bank"),
        banknifty_spot_token=os.getenv("BANKNIFTY_SPOT_TOKEN", "99926009"),
        product_type=os.getenv("SMARTAPI_PRODUCT_TYPE", "INTRADAY"),
        order_variety=os.getenv("SMARTAPI_ORDER_VARIETY", "NORMAL"),
        instrument_master_url=os.getenv(
            "INSTRUMENT_MASTER_URL",
            DEFAULT_INSTRUMENT_MASTER_URL,
        ),
        database_url=os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'platform.sqlite3'}"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        session_secret_key=os.getenv("SESSION_SECRET_KEY", "change-me-in-production"),
        secure_cookies=_get_bool("SECURE_COOKIES", False),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),  # type: ignore[arg-type]
    )
