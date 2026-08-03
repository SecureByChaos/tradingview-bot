from __future__ import annotations

import os
from dataclasses import dataclass, replace
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
    default_strategy_name: str = "V5.1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Option-chain archival (collection only, no live trading effect) ---
    option_chain_collection_enabled: bool = True
    # Strikes either side of ATM. 10 gives a 21-strike chain per expiry per
    # side, which is the minimum useful width for an IV skew or OI-wall study.
    # Raising this raises storage roughly linearly -- see the volume note in
    # app/option_chain_store.py before increasing it.
    option_chain_strike_band: int = 10
    option_chain_expiry_count: int = 2
    option_chain_interval_minutes: int = 5

    # Optional SECOND set of SmartAPI credentials, used only by the option-chain
    # collector. Angel One rate-limits per API key, so this is the only way to
    # give analysis work a budget that genuinely cannot starve live trading --
    # a second session on the same key shares the same quota and merely drops
    # the process-wide throttle. Leave blank to run the collector subordinate
    # to the live client instead.
    smartapi_analytics_api_key: str = ""
    smartapi_analytics_client_id: str = ""
    smartapi_analytics_pin: str = ""
    smartapi_analytics_totp_secret: str = ""

    @property
    def order_quantity(self) -> int:
        return self.quantity_lots * self.banknifty_lot_size

    def as_analytics_credentials(self) -> "Settings":
        """A copy whose primary SmartAPI credentials are the analytics ones.

        SmartAPIClient reads settings.smartapi_* directly, so handing it a
        swapped copy is what lets one client class serve both budgets without
        threading a credential set through every call.

        live_trading is forced off: this client exists to read market data, and
        nothing should be able to place an order through it even if the env var
        is on for the live client.
        """
        return replace(
            self,
            smartapi_api_key=self.smartapi_analytics_api_key,
            smartapi_client_id=self.smartapi_analytics_client_id,
            smartapi_pin=self.smartapi_analytics_pin,
            smartapi_totp_secret=self.smartapi_analytics_totp_secret,
            live_trading=False,
        )


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
        default_strategy_name=os.getenv("DEFAULT_STRATEGY_NAME", "V5.1"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),  # type: ignore[arg-type]
        option_chain_collection_enabled=_get_bool("OPTION_CHAIN_COLLECTION_ENABLED", True),
        option_chain_strike_band=_get_int("OPTION_CHAIN_STRIKE_BAND", 10),
        option_chain_expiry_count=_get_int("OPTION_CHAIN_EXPIRY_COUNT", 2),
        option_chain_interval_minutes=_get_int("OPTION_CHAIN_INTERVAL_MINUTES", 5),
        smartapi_analytics_api_key=os.getenv("SMARTAPI_ANALYTICS_API_KEY", ""),
        smartapi_analytics_client_id=os.getenv("SMARTAPI_ANALYTICS_CLIENT_ID", ""),
        smartapi_analytics_pin=os.getenv("SMARTAPI_ANALYTICS_PIN", ""),
        smartapi_analytics_totp_secret=os.getenv("SMARTAPI_ANALYTICS_TOTP_SECRET", ""),
    )
