from __future__ import annotations

import ctypes
import os
import shutil
import time
from datetime import datetime

from sqlalchemy import func, select

from app.db_models import BotState, BotStatus, DailyStats, StrategyTrade, TradeStatus
from app.health.models import CRITICAL, HEALTHY, WARNING, HealthResult
from app.platform import today_ist
from app.time_utils import IST


class TradingEngineHealth:
    def check(self, db: object) -> HealthResult:
        state = db.get(BotState, 1)
        if state is None:
            return HealthResult(CRITICAL, "Bot state unavailable")
        inconsistent = int(
            db.scalar(
                select(func.count()).select_from(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.exit_time.is_not(None),
                )
            )
            or 0
        )
        if inconsistent:
            return HealthResult(CRITICAL, f"{inconsistent} inconsistent active trade(s)")
        stats = db.scalar(select(DailyStats).where(DailyStats.trade_date == today_ist()))
        if stats and (stats.trade_count or stats.consecutive_losses) and datetime.now(IST).hour < 9:
            return HealthResult(CRITICAL, "Daily counters were not reset")
        if state.risk_locked:
            return HealthResult(CRITICAL, "Global risk lock active")
        if state.status != BotStatus.RUNNING or not state.trading_allowed:
            return HealthResult(WARNING, f"Bot status is {state.status}")
        return HealthResult(HEALTHY, "Trading engine ready")


class ServerHealth:
    def check(self) -> HealthResult:
        cpu = self._cpu_percent()
        ram = self._ram_percent()
        disk = round(shutil.disk_usage(".").used / shutil.disk_usage(".").total * 100, 2)
        highest = max(cpu, ram, disk)
        status = CRITICAL if highest >= 95 else WARNING if highest >= 85 else HEALTHY
        return HealthResult(
            status,
            "Server resources collected",
            details={"cpu_percent": cpu, "ram_percent": ram, "disk_percent": disk, "server_time": datetime.now(IST).isoformat()},
        )

    @staticmethod
    def _cpu_percent() -> float:
        if not hasattr(ctypes, "windll"):
            try:
                load1 = os.getloadavg()[0]
                cpus = os.cpu_count() or 1
                return round(min((load1 / cpus) * 100, 100.0), 2)
            except Exception:
                return 0.0
        idle1, kernel1, user1 = ServerHealth._system_times()
        time.sleep(0.1)
        idle2, kernel2, user2 = ServerHealth._system_times()
        total = (kernel2 - kernel1) + (user2 - user1)
        return round((1 - ((idle2 - idle1) / total)) * 100, 2) if total else 0.0

    @staticmethod
    def _system_times() -> tuple[int, int, int]:
        idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        return idle.value, kernel.value, user.value

    @staticmethod
    def _ram_percent() -> float:
        if not hasattr(ctypes, "windll"):
            try:
                with open("/proc/meminfo", "r", encoding="utf-8") as f:
                    mem = dict(
                        (line.split(":", 1)[0], float(line.split(":", 1)[1].strip().split()[0]))
                        for line in f
                        if ":" in line
                    )
                total = mem.get("MemTotal", 0.0)
                available = mem.get("MemAvailable", 0.0)
                if total <= 0:
                    return 0.0
                return round(((total - available) / total) * 100, 2)
            except Exception:
                return 0.0

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(f"value{i}", ctypes.c_ulonglong) for i in range(7)]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return float(status.load)
