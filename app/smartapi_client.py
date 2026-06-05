from __future__ import annotations

import logging
from typing import Any, Optional

import pyotp

from app.config import Settings
from app.models import OptionContract

logger = logging.getLogger(__name__)


class SmartAPIError(RuntimeError):
    pass


class SmartAPIClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None
        self._jwt_token: Optional[str] = None

    def authenticate(self) -> None:
        missing = [
            key
            for key, value in {
                "SMARTAPI_API_KEY": self.settings.smartapi_api_key,
                "SMARTAPI_CLIENT_ID": self.settings.smartapi_client_id,
                "SMARTAPI_PIN": self.settings.smartapi_pin,
                "SMARTAPI_TOTP_SECRET": self.settings.smartapi_totp_secret,
            }.items()
            if not value
        ]
        if missing:
            if self.settings.live_trading:
                raise SmartAPIError(f"Missing SmartAPI environment variables: {', '.join(missing)}")
            logger.warning(
                "SmartAPI credentials missing; market data will be unavailable until .env is configured"
            )
            return

        try:
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise SmartAPIError("SmartAPI SDK is not installed. Run pip install -r requirements.txt") from exc

        self._client = SmartConnect(self.settings.smartapi_api_key)
        totp = pyotp.TOTP(self.settings.smartapi_totp_secret).now()
        session = self._client.generateSession(
            self.settings.smartapi_client_id,
            self.settings.smartapi_pin,
            totp,
        )
        if not session or session.get("status") is False:
            raise SmartAPIError(f"SmartAPI login failed: {session}")
        self._jwt_token = session.get("data", {}).get("jwtToken")
        logger.info(
            "SmartAPI authenticated for client %s; live orders enabled=%s",
            self.settings.smartapi_client_id,
            self.settings.live_trading,
        )

        def _refresh_session(self) -> None:
        logger.warning("SmartAPI session expired. Re-authenticating...")
        self._client = None
        self._jwt_token = None
        self.authenticate()
        logger.info("SmartAPI re-authentication successful")

    	def _call_with_reauth(self, func, *args, **kwargs):
        response = func(*args, **kwargs)

        if (
            isinstance(response, dict)
            and response.get("errorCode") == "AG8001"
        ):
            self._refresh_session()
            response = func(*args, **kwargs)

        return response

    @property
    def client(self) -> Any:
        if self._client is None:
            self.authenticate()
        return self._client

    def get_ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> float:
        if self._client is None:
            self.authenticate()
        if self._client is None:
            raise SmartAPIError("SmartAPI is not authenticated; configure credentials for market data")
        response = self._call_with_reauth(
    		self.client.ltpData,
    		exchange,
    		tradingsymbol,
    		symboltoken,
	)
        if not response or response.get("status") is False:
            raise SmartAPIError(f"LTP request failed for {tradingsymbol}: {response}")
        try:
            return float(response["data"]["ltp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SmartAPIError(f"Unexpected LTP response for {tradingsymbol}: {response}") from exc

    def get_banknifty_spot(self) -> float:
        return self.get_ltp(
            self.settings.banknifty_spot_exchange,
            self.settings.banknifty_spot_symbol,
            self.settings.banknifty_spot_token,
        )

    def place_market_order(
        self,
        contract: OptionContract,
        transaction_type: str,
        quantity: int,
    ) -> Optional[str]:
        if not self.settings.live_trading:
            logger.info(
                "Paper %s order: %s qty=%s",
                transaction_type,
                contract.tradingsymbol,
                quantity,
            )
            return "PAPER_ORDER"

        orderparams = {
            "variety": self.settings.order_variety,
            "tradingsymbol": contract.tradingsymbol,
            "symboltoken": contract.symboltoken,
            "transactiontype": transaction_type,
            "exchange": contract.exchange,
            "ordertype": "MARKET",
            "producttype": self.settings.product_type,
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity),
        }
        response = self._call_with_reauth(
    		self.client.placeOrder,
    		orderparams,
	)
        if isinstance(response, str):
            return response
        if response and response.get("status") is not False:
            return str(response.get("data", {}).get("orderid") or response.get("orderid"))
        raise SmartAPIError(f"Order placement failed: {response}")

    def close_position(self, contract: OptionContract, quantity: int) -> Optional[str]:
        return self.place_market_order(contract, "SELL", quantity)
