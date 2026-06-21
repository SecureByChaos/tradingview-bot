from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BotStatus:
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    RISK_LOCKED = "RISK_LOCKED"


class TradingMode:
    PAPER = "PAPER"
    LIVE = "LIVE"


class TradeStatus:
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class TradeResult:
    OPEN = "OPEN"
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"


class BotState(Base):
    __tablename__ = "bot_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    status: Mapped[str] = mapped_column(String(32), default=BotStatus.STOPPED, nullable=False)
    trading_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    risk_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PlatformSettings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    trading_mode: Mapped[str] = mapped_column(String(16), default=TradingMode.PAPER, nullable=False)
    stop_loss_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    target_percent: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    max_trades_per_day: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    max_consecutive_losses: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    daily_max_loss_percent: Mapped[float] = mapped_column(Float, default=-20.0, nullable=False)
    square_off_time: Mapped[str] = mapped_column(String(8), default="15:15", nullable=False)
    telegram_bot_token: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default=TradingMode.PAPER, nullable=False)
    tp_percent: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    sl_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    max_active_trades: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    capital_per_trade: Mapped[float] = mapped_column(Float, default=20000.0, nullable=False)
    paper_trade: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    live_trade: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StrategyTrade(Base):
    __tablename__ = "strategy_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    signal: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), default="NFO", nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(128), nullable=False)
    symboltoken: Mapped[str] = mapped_column(String(64), nullable=False)
    strike: Mapped[int] = mapped_column(Integer, nullable=False)
    expiry: Mapped[str] = mapped_column(String(32), nullable=False)
    option_type: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    stoploss: Mapped[float] = mapped_column(Float, nullable=False)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    profit_loss: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    result: Mapped[str] = mapped_column(String(16), default=TradeResult.OPEN, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=TradeStatus.OPEN, index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default=TradingMode.PAPER, nullable=False)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    lowest_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trailing_stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TradeRecord(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    signal: Mapped[str] = mapped_column(String(16), nullable=False)
    strike: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    stoploss: Mapped[float] = mapped_column(Float, nullable=False)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(16), default=TradingMode.PAPER, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyStats(Base):
    __tablename__ = "daily_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, unique=True, index=True, nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    risk_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LogEvent(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="INFO", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
