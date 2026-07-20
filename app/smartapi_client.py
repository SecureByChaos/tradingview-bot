from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pyotp

from app.config import Settings
from app.models import OptionContract

logger = logging.getLogger(__name__)


class SmartAPIError(RuntimeError):
    pass


BROKER_CONNECTED = "CONNECTED"
BROKER_RECONNECTING = "RECONNECTING"
BROKER_FAILED = "FAILED"

# Angel One's /quote (ltpData) endpoint is rate-limited to 1 request/second.
# This app now has several independent callers that can all want a quote in
# the same moment -- the 30s trade monitor (once per open trade, including
# AI_ALT_*/AI_ORIGIN_* shadow trades), the 5-min AI origination check (once
# per enabled index), webhook-triggered entries/exits, and manual dashboard
# actions. A small margin above 1.0s avoids edge-of-window rejections.
_MIN_QUOTE_INTERVAL_SECONDS = 1.05


class SmartAPIClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None
        self._jwt_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._auth_lock = threading.RLock()
        self._quote_rate_lock = threading.Lock()
        self._last_quote_call_monotonic: float = 0.0
        self._status = BROKER_RECONNECTING
        self._last_login: Optional[datetime] = None
        self._last_refresh: Optional[datetime] = None
        self._last_login_monotonic: float = 0.0
        self._last_refresh_monotonic: float = 0.0
        self._last_error: str = ""
        self._ltp_status: str = "UNKNOWN"
        self._auth_status: str = "DISCONNECTED"
        self._jwt_status: str = "MISSING"
        self._feed_status: str = "MISSING"
        self._recovery_count_total = 0
        self._recovery_count = 0
        self._recovery_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    @staticmethod
    def _now_ist() -> datetime:
        return datetime.now(ZoneInfo("Asia/Kolkata"))

    @staticmethod
    def _strip_bearer(token: str | None) -> str | None:
        if not token:
            return token
        if token.lower().startswith("bearer "):
            return token[7:].strip()
        return token

    def _mark_failed(self, reason: str) -> None:
        self._status = BROKER_FAILED
        self._auth_status = "FAILED"
        self._last_error = reason
        logger.error("[AUTH] Broker FAILED")

    def _apply_tokens(self, jwt: str | None, refresh: str | None, feed: str | None) -> None:
        self._jwt_token = self._strip_bearer(jwt)
        self._refresh_token = refresh
        self._feed_token = feed
        if self._client is not None:
            if self._jwt_token:
                self._client.setAccessToken(self._jwt_token)
            if self._refresh_token:
                self._client.setRefreshToken(self._refresh_token)
            if self._feed_token:
                self._client.setFeedToken(self._feed_token)
        self._jwt_status = "VALID" if self._jwt_token else "MISSING"
        self._feed_status = "VALID" if self._feed_token else "MISSING"
        self._auth_status = "OK" if self._jwt_token and self._feed_token else "FAILED"

    def get_broker_health(self) -> dict[str, object]:
        return {
            "status": self._status,
            "authentication_status": self._auth_status,
            "last_login": self._last_login,
            "last_refresh": self._last_refresh,
            "last_error": self._last_error,
            "recovery_count": self._recovery_count,
            "recovery_count_total": self._recovery_count_total,
            "jwt_status": self._jwt_status,
            "feed_token_status": self._feed_status,
            "ltp_status": self._ltp_status,
        }

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

        with self._auth_lock:
            if self._status == BROKER_FAILED:
                raise SmartAPIError(f"Broker in FAILED state: {self._last_error}")
            if self._client is not None and self._jwt_token and self._feed_token and self._status == BROKER_CONNECTED:
                return
            if (
                self._client is not None
                and self._jwt_token
                and self._feed_token
                and time.monotonic() - self._last_login_monotonic < 2.0
            ):
                return
            self._status = BROKER_RECONNECTING
            self._auth_status = "RECONNECTING"
            last_error = ""
            for attempt in range(3):
                try:
                    logger.info("[AUTH] Login started")
                    self._client = SmartConnect(self.settings.smartapi_api_key)
                    totp = pyotp.TOTP(self.settings.smartapi_totp_secret).now()
                    logger.info("[AUTH] TOTP generated")
                    session = self._client.generateSession(
                        self.settings.smartapi_client_id,
                        self.settings.smartapi_pin,
                        totp,
                    )
                    if not session or session.get("status") is False:
                        raise SmartAPIError(f"SmartAPI login failed: {session}")
                    data = session.get("data", {})
                    jwt = self._strip_bearer(data.get("jwtToken"))
                    refresh = data.get("refreshToken")
                    feed = data.get("feedToken") or self._client.getfeedToken()
                    if not jwt or not feed:
                        raise SmartAPIError("SmartAPI login failed: missing session tokens")
                    self._apply_tokens(jwt, refresh, feed)
                    self._last_login = self._now_ist()
                    self._last_login_monotonic = time.monotonic()
                    self._last_error = ""
                    self._status = BROKER_CONNECTED
                    logger.info("[AUTH] Login successful")
                    logger.info("[AUTH] JWT updated")
                    logger.info("[AUTH] Feed token updated")
                    return
                except Exception as exc:
                    last_error = str(exc)
                    logger.exception("[AUTH] Recovery failed")
                    if attempt < 2:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    self._mark_failed(last_error)
                    raise SmartAPIError(last_error) from exc

    def _token_expired(self, response: object) -> bool:
        if not isinstance(response, dict):
            return False
        message = str(response.get("message", "")).lower()
        return response.get("errorCode") in {"AG8001", "AG8003", "AB8050"} or ("token" in message and ("expired" in message or "missing" in message))

    def _rate_limited(self, response: object) -> bool:
        if not isinstance(response, dict):
            return False
        message = str(response.get("message", "")).lower()
        return "access rate" in message or "rate limit" in message

    def _refresh_session(self) -> None:
        with self._auth_lock:
            if self._status == BROKER_FAILED:
                raise SmartAPIError(f"Broker in FAILED state: {self._last_error}")
            if not self._client or not self._refresh_token:
                self.authenticate()
                return
            if self._jwt_token and self._feed_token and time.monotonic() - self._last_refresh_monotonic < 2.0:
                return
            self._status = BROKER_RECONNECTING
            self._auth_status = "RECONNECTING"
            last_error = ""
            for attempt in range(3):
                try:
                    logger.info("[AUTH] Refresh started")
                    response = self._client.generateToken(self._refresh_token)
                    if not response or response.get("status") is False:
                        raise SmartAPIError(f"SmartAPI refresh failed: {response}")
                    data = response.get("data", {})
                    jwt = self._strip_bearer(data.get("jwtToken") or self._jwt_token)
                    refresh = data.get("refreshToken") or self._refresh_token
                    feed = data.get("feedToken") or self._client.getfeedToken() or self._feed_token
                    if not jwt or not feed:
                        raise SmartAPIError("SmartAPI refresh failed: missing refreshed tokens")
                    self._apply_tokens(jwt, refresh, feed)
                    self._last_refresh = self._now_ist()
                    self._last_refresh_monotonic = time.monotonic()
                    self._last_error = ""
                    self._status = BROKER_CONNECTED
                    logger.info("[AUTH] Refresh successful")
                    logger.info("[AUTH] JWT updated")
                    logger.info("[AUTH] Feed token updated")
                    return
                except Exception as exc:
                    last_error = str(exc)
                    logger.exception("[AUTH] Recovery failed")
                    if attempt < 2:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    logger.exception("[AUTH] Refresh failed; performing full TOTP login")
                    self._client = None
                    self._jwt_token = None
                    self._refresh_token = None
                    self._feed_token = None
                    self._jwt_status = "MISSING"
                    self._feed_status = "MISSING"
                    self.authenticate()
                    return
            self._mark_failed(last_error)
            raise SmartAPIError(last_error)

    def _call_with_reauth(self, func, *args, **kwargs):
        if self._status == BROKER_FAILED:
            raise SmartAPIError(f"Broker in FAILED state: {self._last_error}")
        response = func(*args, **kwargs)
        auth_error = self._token_expired(response)
        rate_limited = self._rate_limited(response)
        if not auth_error and not rate_limited:
            return response

        code = ""
        if isinstance(response, dict):
            code = str(response.get("errorCode") or "")
            if code == "AG8001":
                logger.info("[AUTH] AG8001 detected")
            elif code == "AG8003":
                logger.info("[AUTH] AG8003 detected")
            elif code == "AB8050":
                logger.info("[AUTH] AB8050 detected")
            elif rate_limited:
                logger.warning("[AUTH] Access rate limit detected")

        method_name = getattr(func, "__name__", None)
        try:
            if auth_error:
                self._refresh_session()
            elif rate_limited:
                for attempt in range(3):
                    delay = 0.5 * (2 ** attempt)
                    time.sleep(delay)
                    retry_func = getattr(self._client, method_name, func) if method_name and self._client is not None else func
                    logger.info("[AUTH] Retrying original request")
                    retry_response = retry_func(*args, **kwargs)
                    if not self._rate_limited(retry_response):
                        logger.info("[AUTH] Recovery successful")
                        return retry_response
                    if attempt == 2:
                        self._mark_failed(f"Rate limit persists: {retry_response}")
                        logger.error("[AUTH] Recovery failed; API response: %s", retry_response)
                        raise SmartAPIError(f"SmartAPI rate limit recovery failed: {retry_response}")
                raise SmartAPIError("SmartAPI rate limit recovery failed")
        except Exception:
            logger.exception("[AUTH] Recovery failed")
            raise

        retry_func = getattr(self._client, method_name, func) if method_name and self._client is not None else func
        logger.info("[AUTH] Retrying original request")
        retry_response = retry_func(*args, **kwargs)
        if self._token_expired(retry_response) or self._rate_limited(retry_response):
            self._mark_failed(f"Recovery failed: {retry_response}")
            logger.error("[AUTH] Recovery failed; API response: %s", retry_response)
            raise SmartAPIError(f"SmartAPI recovery failed: {retry_response}")
        logger.info("[AUTH] Recovery successful")
        today = self._now_ist().date()
        if self._recovery_date != today:
            self._recovery_count = 0
            self._recovery_date = today
        self._recovery_count += 1
        self._recovery_count_total += 1
        return retry_response

    @property
    def client(self) -> Any:
        if self._client is None:
            self.authenticate()
        if self._status == BROKER_FAILED:
            raise SmartAPIError(f"Broker in FAILED state: {self._last_error}")
        return self._client

    def _throttle_quote_call(self) -> None:
        """Serializes every ltpData call (from any thread) to at least
        _MIN_QUOTE_INTERVAL_SECONDS apart, process-wide -- see the constant's
        comment for why this matters now that several independent loops share
        this one rate-limited endpoint."""
        with self._quote_rate_lock:
            wait = _MIN_QUOTE_INTERVAL_SECONDS - (time.monotonic() - self._last_quote_call_monotonic)
            if wait > 0:
                time.sleep(wait)
            self._last_quote_call_monotonic = time.monotonic()

    def get_ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> float:
        self._throttle_quote_call()
        started = time.perf_counter()
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
            self._ltp_status = "FAILED"
            raise SmartAPIError(f"LTP request failed for {tradingsymbol}: {response}")
        try:
            value = float(response["data"]["ltp"])
            self._ltp_status = "OK"
            logger.info("SmartAPI ltpData latency_ms=%.2f", (time.perf_counter() - started) * 1000)
            return value
        except (KeyError, TypeError, ValueError) as exc:
            self._ltp_status = "FAILED"
            raise SmartAPIError(f"Unexpected LTP response for {tradingsymbol}: {response}") from exc

    def get_banknifty_spot(self) -> float:
        return self.get_ltp(
            self.settings.banknifty_spot_exchange,
            self.settings.banknifty_spot_symbol,
            self.settings.banknifty_spot_token,
        )

    def get_index_spot(self, index: Any) -> float:
        """Generalized spot-price fetch for any configured index (BankNifty/Nifty/Sensex/...)."""
        if not index.spot_token:
            raise SmartAPIError(
                f"{index.symbol} spot token is not configured. Set it in Settings > Instruments before trading this index."
            )
        return self.get_ltp(index.spot_exchange, index.spot_symbol, index.spot_token)

    def get_index_ohlc(self, index: Any) -> dict[str, float] | None:
        """Real exchange-reported day open/high/low/close for an index, via
        getMarketData's OHLC mode -- same /quote rate-limit family as get_ltp,
        so it shares the same throttle. Angel One's index feed has been
        reported (SmartAPI forum, NIFTY OHLC request) to sometimes return 0 for
        open/high/low on pure index instruments even when close/ltp are
        populated -- so this returns None rather than zeros when that happens,
        letting the caller fall back to its own data instead of trusting a
        broken reading. Best-effort only: any failure here should never block
        a caller that has other data to fall back on."""
        if not index.spot_token:
            return None
        self._throttle_quote_call()
        try:
            response = self._call_with_reauth(
                self.client.getMarketData,
                "OHLC",
                {index.spot_exchange: [index.spot_token]},
            )
        except Exception as exc:
            logger.info("SmartAPI getMarketData(OHLC) failed for %s: %s", index.symbol, exc)
            return None
        if not response or response.get("status") is False:
            return None
        try:
            fetched = response["data"]["fetched"]
            if not fetched:
                return None
            row = fetched[0]
            values = {
                "open": float(row.get("open") or 0),
                "high": float(row.get("high") or 0),
                "low": float(row.get("low") or 0),
                "close": float(row.get("close") or 0),
            }
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            logger.info("Unexpected getMarketData(OHLC) response for %s: %s (%s)", index.symbol, response, exc)
            return None
        # Angel's index feed sometimes reports open/high/low as 0 while close
        # is populated -- treat that as "not really available" rather than
        # real zeros a trading decision could act on.
        if values["open"] <= 0 or values["high"] <= 0 or values["low"] <= 0:
            logger.info("SmartAPI getMarketData(OHLC) returned incomplete data for %s: %s", index.symbol, values)
            return None
        return values

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
