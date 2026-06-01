from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from zoneinfo import ZoneInfo

from app.config import Settings
from app.models import OptionContract, Signal
from app.smartapi_client import SmartAPIClient

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class OptionFinder:
    def __init__(self, settings: Settings, smartapi: SmartAPIClient) -> None:
        self.settings = settings
        self.smartapi = smartapi

    def find_atm_contract(self, signal: Signal) -> OptionContract:
        spot_price = self.smartapi.get_banknifty_spot()
        atm_strike = int(round(spot_price / 100) * 100)
        option_type = "CE" if signal == Signal.BUY_CE else "PE"
        instruments = self._load_instruments()
        matches = self._filter_banknifty_options(instruments, option_type)
        if matches.empty:
            raise ValueError(f"No BankNifty {option_type} contracts found in instrument master")

        matches = matches.assign(strike_diff=(matches["strike_normalized"] - atm_strike).abs())
        nearest_expiry = matches["expiry_dt"].min()
        expiry_contracts = matches[matches["expiry_dt"] == nearest_expiry]
        selected = expiry_contracts.sort_values(["strike_diff", "strike_normalized"]).iloc[0]
        logger.info(
            "Selected %s at spot %.2f: %s strike=%s expiry=%s",
            signal,
            spot_price,
            selected["symbol"],
            int(selected["strike_normalized"]),
            selected["expiry"],
        )
        return OptionContract(
            exchange=selected.get("exch_seg", "NFO"),
            tradingsymbol=selected["symbol"],
            symboltoken=str(selected["token"]),
            strike=int(selected["strike_normalized"]),
            expiry=str(selected["expiry"]),
            option_type=option_type,
            lot_size=int(float(selected.get("lotsize") or self.settings.banknifty_lot_size)),
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
        return datetime.now(IST) - modified < timedelta(hours=12)

    def _filter_banknifty_options(self, instruments: pd.DataFrame, option_type: str) -> pd.DataFrame:
        required = {"exch_seg", "instrumenttype", "name", "symbol", "expiry", "strike", "token"}
        missing = required - set(instruments.columns)
        if missing:
            raise ValueError(f"Instrument master missing columns: {', '.join(sorted(missing))}")

        frame = instruments[
            (instruments["exch_seg"] == "NFO")
            & (instruments["instrumenttype"].isin(["OPTIDX", "OPTSTK"]))
            & (instruments["name"].astype(str).str.upper() == "BANKNIFTY")
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
