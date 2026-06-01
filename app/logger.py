from __future__ import annotations

import csv
import logging
from pathlib import Path
from threading import RLock
from typing import Iterable


TRADE_COLUMNS = [
    "date",
    "signal",
    "strike",
    "entry_price",
    "exit_price",
    "stoploss",
    "target",
    "entry_time",
    "exit_time",
    "exit_reason",
    "pnl_percent",
]


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


class TradeCSVLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=TRADE_COLUMNS).writeheader()

    def append(self, row: dict[str, object]) -> None:
        with self._lock:
            self._ensure_file()
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=TRADE_COLUMNS)
                writer.writerow({column: row.get(column, "") for column in TRADE_COLUMNS})

    def read_all(self) -> list[dict[str, str]]:
        with self._lock:
            self._ensure_file()
            with self.path.open("r", newline="", encoding="utf-8") as handle:
                return list(csv.DictReader(handle))

    def rows_for_date(self, date_value: str) -> Iterable[dict[str, str]]:
        return (row for row in self.read_all() if row.get("date") == date_value)
