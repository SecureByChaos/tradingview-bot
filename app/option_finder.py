from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import requests
from zoneinfo import ZoneInfo

from app.config import Settings
from app.models import OptionContract, Signal
from app.smartapi_client import SmartAPIClient

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def get_atm_option_token(spot_price: float, signal_type: str) -> dict:
    if signal_type not in {"BUY_CE", "BUY_PE"}:
        raise ValueError("signal_type must be BUY_CE or BUY_PE")

    option_type = "CE" if signal_type == "BUY_CE" else "PE"
    atm_strike = int(round(float(spot_price) / 100) * 100)
    cache_path = Path(os.getenv("INSTRUMENT_CACHE_PATH", "data/instruments.json"))
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists() and cache_path.stat().st_size > 0:
        instruments = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        url = os.getenv(
            "INSTRUMENT_MASTER_URL",
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        )
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        instruments = response.json()
        cache_path.write_text(json.dumps(instruments), encoding="utf-8")

    today = datetime.now(IST).date()
    matches: list[tuple[object, dict, int]] = []
    for item in instruments:
        if item.get("exch_seg") != "NFO" or item.get("instrumenttype") != "OPTIDX":
            continue
        if str(item.get("name", "")).upper() != "BANKNIFTY":
            continue
        if not str(item.get("symbol", "")).upper().endswith(option_type):
            continue
        try:
            expiry_dt = datetime.strptime(str(item["expiry"]), "%d%b%Y").date()
        except (KeyError, ValueError):
            continue
        if expiry_dt < today:
            continue
        strike_raw = float(item.get("strike", 0))
        strike = int(strike_raw / 100 if strike_raw > 100000 else strike_raw)
        if strike == atm_strike:
            matches.append((expiry_dt, item, strike))

    if not matches:
        raise LookupError(f"No BANKNIFTY {option_type} ATM contract found for strike {atm_strike}")

    expiry_dt, item, strike = sorted(matches, key=lambda row: row[0])[0]
    return {
        "symboltoken": str(item["token"]),
        "tradingsymbol": str(item["symbol"]),
        "strike": strike,
        "expiry": expiry_dt.isoformat(),
    }


class OptionFinder:
    def __init__(self, settings: Settings, smartapi: SmartAPIClient) -> None:
        self.settings = settings
        self.smartapi = smartapi

    def find_atm_contract(self, signal: Signal, index: Any = None, expiry_itm_strikes: int = 0) -> OptionContract:
        index = index or self._default_index()
        spot_price = self.smartapi.get_index_spot(index)
        strike_interval = index.strike_interval or 100
        atm_strike = int(round(spot_price / strike_interval) * strike_interval)
        option_type = "CE" if signal.value.endswith("CE") else "PE"
        instruments = self._load_instruments()
        matches = self._filter_index_options(instruments, index, option_type)
        if matches.empty:
            raise ValueError(f"No {index.symbol} {option_type} contracts found in instrument master")

        nearest_expiry = matches["expiry_dt"].min()
        is_expiry_day = nearest_expiry == datetime.now(IST).date()

        target_strike = atm_strike
        if is_expiry_day and expiry_itm_strikes > 0:
            itm_shift = strike_interval * expiry_itm_strikes
            # ITM for a call is a lower strike; ITM for a put is a higher strike. Never OTM.
            target_strike = atm_strike - itm_shift if option_type == "CE" else atm_strike + itm_shift

        expiry_contracts = matches[matches["expiry_dt"] == nearest_expiry].copy()
        expiry_contracts = expiry_contracts.assign(strike_diff=(expiry_contracts["strike_normalized"] - target_strike).abs())
        selected = expiry_contracts.sort_values(["strike_diff", "strike_normalized"]).iloc[0]
        logger.info(
            "Selected %s (%s) at spot %.2f: %s strike=%s expiry=%s%s",
            signal,
            index.symbol,
            spot_price,
            selected["symbol"],
            int(selected["strike_normalized"]),
            selected["expiry"],
            f" [expiry-day, {expiry_itm_strikes} strike(s) ITM targeted]" if is_expiry_day and expiry_itm_strikes > 0 else "",
        )
        return OptionContract(
            exchange=selected.get("exch_seg", index.exchange_segment),
            tradingsymbol=selected["symbol"],
            symboltoken=str(selected["token"]),
            strike=int(selected["strike_normalized"]),
            expiry=str(selected["expiry"]),
            option_type=option_type,
            lot_size=int(float(selected.get("lotsize") or index.lot_size)),
        )

    def _default_index(self) -> Any:
        """Fallback used only by legacy/unreachable call sites that predate multi-index
        support (find_atm_contract without an explicit index argument)."""
        return SimpleNamespace(
            symbol="BANKNIFTY",
            exchange_segment="NFO",
            instrument_name="BANKNIFTY",
            spot_exchange=self.settings.banknifty_spot_exchange,
            spot_symbol=self.settings.banknifty_spot_symbol,
            spot_token=self.settings.banknifty_spot_token,
            lot_size=self.settings.banknifty_lot_size,
            strike_interval=100,
        )

    def _load_instruments(self) -> pd.DataFrame:
        cache_path = self.settings.instrument_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload: list[dict[str, Any]]
        if self._cache_is_fresh(cache_path):
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            response = requests.get(self.settings.instrument_master_url, timeout=20)
            response.raise_for_status()
            payload = response.json()
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
        return pd.DataFrame(payload)

    def _cache_is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=IST)
        # Refresh once per calendar day (IST) rather than a rolling window, so the
        # first request each trading day always pulls the latest instrument master.
        # This matters specifically for expiry-day detection: if NSE shifts an
        # expiry date for a holiday, we want that reflected before market open,
        # not up to ~12h stale from a rolling window.
        return modified.date() == datetime.now(IST).date()

    def _filter_index_options(self, instruments: pd.DataFrame, index: Any, option_type: str) -> pd.DataFrame:
        required = {"exch_seg", "instrumenttype", "name", "symbol", "expiry", "strike", "token"}
        missing = required - set(instruments.columns)
        if missing:
            raise ValueError(f"Instrument master missing columns: {', '.join(sorted(missing))}")

        frame = instruments[
            (instruments["exch_seg"] == index.exchange_segment)
            & (instruments["instrumenttype"].isin(["OPTIDX", "OPTSTK"]))
            & (instruments["name"].astype(str).str.upper() == index.instrument_name.upper())
            & (instruments["symbol"].astype(str).str.upper().str.endswith(option_type))
        ].copy()
        frame["expiry_dt"] = pd.to_datetime(frame["expiry"], format="%d%b%Y", errors="coerce").dt.date
        today = datetime.now(IST).date()
        frame = frame[frame["expiry_dt"].notna() & (frame["expiry_dt"] >= today)]
        frame["strike_normalized"] = pd.to_numeric(frame["strike"], errors="coerce")
        frame = frame[frame["strike_normalized"].notna()]
        frame["strike_normalized"] = frame["strike_normalized"].apply(
            lambda value: value / 100 if value > 100000 else value
        )
        return frame
