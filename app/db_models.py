from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional

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


class SLMode:
    FIXED = "FIXED"
    TRAILING = "TRAILING"


class IndexSymbol:
    BANKNIFTY = "BANKNIFTY"
    NIFTY = "NIFTY"
    SENSEX = "SENSEX"


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
    square_off_time: Mapped[str] = mapped_column(String(8), default="15:15", nullable=False)
    telegram_bot_token: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AISettings(Base):
    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="SHADOW", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="dummy", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    api_key: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.2, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    confidence_threshold: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    secondary_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    secondary_provider: Mapped[str] = mapped_column(String(32), default="claude", nullable=False)
    secondary_model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    secondary_api_key: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    secondary_base_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AITradeReview(Base):
    __tablename__ = "ai_trade_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    strategy: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    signal: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False)
    context_version: Mapped[str] = mapped_column(String(16), nullable=False)
    framework_version: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    entry_quality: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    market_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    risk: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    reason_to_buy: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    reason_not_to_buy: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_result: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    actual_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AIExitCall(Base):
    """Independent, read-only AI exit-call tracking for the AI Exit Calls page.
    Never written to or read by the real trading logic -- app/ai/exit_shadow.py
    is the only writer, and it never touches StrategyTrade/risk/stats/telegram.
    Each row is one periodic check; a trade stops being re-checked once a row
    with decision == 'EXIT' exists for it (see exit_shadow.py)."""

    __tablename__ = "ai_exit_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, default="", nullable=False)
    premium_at_check: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_percent_at_check: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AIContextLog(Base):
    __tablename__ = "ai_context_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    signal: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    paper_live: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    trade_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    trade_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    session: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    context_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    request_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    payload_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    context_version: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    completeness_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    missing_fields: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_to_buy: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    reason_not_to_buy: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SystemHealthLog(Base):
    __tablename__ = "system_health_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    overall_status: Mapped[str] = mapped_column(String(16), nullable=False)
    health_score: Mapped[float] = mapped_column(Float, nullable=False)
    broker_status: Mapped[str] = mapped_column(String(16), nullable=False)
    database_status: Mapped[str] = mapped_column(String(16), nullable=False)
    webhook_status: Mapped[str] = mapped_column(String(16), nullable=False)
    trading_status: Mapped[str] = mapped_column(String(16), nullable=False)
    ai_status: Mapped[str] = mapped_column(String(16), nullable=False)
    server_status: Mapped[str] = mapped_column(String(16), nullable=False)
    ltp_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    ram_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class IndexConfig(Base):
    __tablename__ = "index_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    exchange_segment: Mapped[str] = mapped_column(String(8), default="NFO", nullable=False)
    instrument_name: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    spot_exchange: Mapped[str] = mapped_column(String(8), default="NSE", nullable=False)
    spot_symbol: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    spot_token: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    strike_interval: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default=TradingMode.PAPER, nullable=False)
    index_symbol: Mapped[str] = mapped_column(String(32), default=IndexSymbol.BANKNIFTY, nullable=False)
    expiry_itm_strikes: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    tp_percent: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    sl_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    sl_mode: Mapped[str] = mapped_column(String(16), default=SLMode.FIXED, nullable=False)
    trailing_activation_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    trailing_offset_percent: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    max_active_trades: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_trades_per_day: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    max_consecutive_losses: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    daily_max_loss_percent: Mapped[float] = mapped_column(Float, default=-20.0, nullable=False)
    lots_per_trade: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Deprecated/unused: retained only so inserts satisfy the legacy NOT NULL column still
    # present in older production databases from before position sizing switched to lots.
    capital_per_trade: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    paper_trade: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    live_trade: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StrategyDailyStats(Base):
    __tablename__ = "strategy_daily_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("strategy_name", "trade_date", name="uq_strategy_daily_stats_name_date"),)


class StrategyStats(Base):
    __tablename__ = "strategy_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    risk_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StrategyTrade(Base):
    __tablename__ = "strategy_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    signal: Mapped[str] = mapped_column(String(16), nullable=False)
    index_symbol: Mapped[str] = mapped_column(String(32), default=IndexSymbol.BANKNIFTY, nullable=False)
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
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    profit_loss: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    result: Mapped[str] = mapped_column(String(16), default=TradeResult.OPEN, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=TradeStatus.OPEN, index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default=TradingMode.PAPER, nullable=False)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entry_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    exit_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    lowest_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trailing_stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-trade SL-mode override. Null for almost every trade -- sl_mode is
    # normally a strategy-level setting (StrategyConfig.sl_mode), looked up by
    # strategy_name in monitor_open_trades. AI Origin trades have no matching
    # StrategyConfig row (synthetic strategy_name) and need this decided per
    # trade instead: when the AI's own sl_percent/target_percent proposal is
    # invalid or unreasonably wide, that specific trade falls back to trailing
    # instead of a number we picked -- see app/ai/originator.py.
    sl_mode: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # origin distinguishes the original TradingView signal ("SIGNAL") from a paper
    # trade opened because an AI reviewer proposed an alternative call after
    # rejecting the original signal ("AI_ALT_OPENAI", "AI_ALT_CLAUDE", etc). Lets
    # the evaluation phase compare outcomes side by side without touching the
    # normal signal-trading path. source_trade_id links an AI_ALT_* trade back to
    # the original trade it was proposed alongside.
    origin: Mapped[str] = mapped_column(String(32), default="SIGNAL", nullable=False)
    source_trade_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    # Structured record of the AI's rationale for an AI_ALT_* trade (null for
    # normal SIGNAL trades) so it can be displayed directly instead of only
    # existing inside a LogEvent JSON payload.
    ai_action: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ai_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StrategyTradeTick(Base):
    """Periodic premium samples for an open StrategyTrade, recorded on the
    existing 30s monitor tick. Powers the live per-trade sparkline on the
    owner/client live dashboards -- without this there is no real history to
    chart, only the single current_premium point."""

    __tablename__ = "strategy_trade_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    premium: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True, nullable=False)


class IndexPriceTick(Base):
    """Periodic spot-price samples per index, recorded (throttled) whenever
    the live dashboard polls for fresh figures. Used to compute today's
    change and day range without assuming the broker API exposes a reliable
    previous-close field."""

    __tablename__ = "index_price_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True, nullable=False)


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
    pnl_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
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


class ReportType:
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    PATTERN = "PATTERN"


class AIReport(Base):
    __tablename__ = "ai_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stats_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True, nullable=False)
