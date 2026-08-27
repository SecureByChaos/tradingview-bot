from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint, func
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
    # 19 Aug 2026: square_off_time was editable here but not actually read by
    # either the rule-based TIME_EXIT check (app/multi_strategy.py hardcoded
    # 15:15) or AI Origination's end gate (app/ai/originator.py's own hardcoded
    # _TRADING_END_HOUR/_MINUTE) -- both now read this column instead. Default
    # "09:45" matches AI Origination's previous hardcoded _TRADING_START_HOUR/
    # _MINUTE, so deploying this column changes nothing until an admin edits
    # it. See handle_signal's and monitor_open_trades' trading-window checks.
    trading_start_time: Mapped[str] = mapped_column(String(8), default="09:45", nullable=False)
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
    # AI Origination-only manual risk knobs (17 Aug 2026) -- both default to the
    # values the code previously hardcoded (app/ai/originator.py's
    # _MAX_SL_TARGET_PERCENT / _DEFAULT_MAX_SAME_DIRECTION_LOSSES), so deploying
    # this column changes nothing until an admin actually edits it.
    #
    # ai_origination_max_sl_percent caps only the AI's proposed STOP -- the
    # target keeps its own separate hardcoded 50% ceiling. A trade whose
    # sl_percent or target_percent falls outside its respective band is not
    # substituted with a fixed number; it falls back to trailing-stop
    # methodology instead (see _open_trade's _stop_is_sane/_target_is_sane).
    ai_origination_max_sl_percent: Mapped[float] = mapped_column(Float, default=50.0, nullable=False)
    # Not an entry-count gate -- a CONSECUTIVE-LOSS gate. Blocks a new
    # same-direction (index+action) AI Origination entry only once the most
    # recent N same-direction trades today were losses IN A ROW; a single win
    # anywhere in that window resets the streak to 0. Replaces the original
    # 11 Aug pure-count gate (_MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK), which
    # blocked a 3rd same-direction entry regardless of whether the first two
    # had won -- see app/ai/originator.py's _same_direction_consecutive_losses.
    ai_origination_max_same_direction_losses: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # 25 Aug 2026: was a hardcoded nominal (_TRAIL_ACTIVATION_NOMINAL = 8.0) in
    # app/ai/originator.py -- made admin-configurable after a real trade's
    # trailing stop never armed (MFE 9.12%, needed 11.59% once the CE/PE
    # rescale widened this same 8.0 nominal for a put). Default 8.0 matches
    # the old hardcoded value, so deploying this column changes nothing until
    # an admin edits it. The CE/PE rescale (symmetric_premium_percent) still
    # applies on top of whatever nominal is configured here -- this changes
    # the INPUT to that rescale, not whether the rescale itself happens.
    ai_origination_trail_activate_percent: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    # 27 Aug 2026: an admin-opt-in risk control, NOT a validated finding --
    # scripts/chop_gate_backtest.py was built the same day chop_efficiency_
    # ratio started being logged, so there is no real closed-trade history
    # yet to backtest a floor against. Defaults to disabled so deploying
    # this column changes no live behavior; an admin who wants it enforced
    # before that backtest returns a result is opting into that risk
    # knowingly, not something this project is asserting is correct.
    # min_efficiency_ratio's default (0.3) matches the existing CHOPPY
    # threshold already shown on the dashboard and in the model's own
    # prompt (_efficiency_ratio_text), not a separately-chosen number.
    ai_origination_chop_gate_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_origination_chop_gate_min_efficiency_ratio: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)
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
    # Separate opt-in from StrategyConfig.live_trade -- AI Origination has no
    # StrategyConfig row of its own (synthetic strategy_name), so its live/paper
    # choice is made per index here instead. Defaults to False (paper) on every
    # index; even when True, app/smartapi_client.py's place_market_order() still
    # only fires a real order if the server-side SMARTAPI_LIVE_TRADING env var
    # (Settings.live_trading) is also on -- this is the per-index half of that
    # same two-key safety pattern every other live-capable strategy already uses.
    ai_origination_live_trade: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    # Actual capital committed at entry -- entry_price * quantity, i.e. total
    # premium paid for the whole position (quantity is already lots *
    # lot_size, not a lot count). Stored rather than computed on read so it's
    # available directly in exports/CSV and stays correct even if entry_price
    # display rounding ever changes; set once at trade-open time across every
    # strategy path (SIGNAL, AI_ALT_*, AI_ORIGIN_*).
    investment_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    stoploss: Mapped[float] = mapped_column(Float, nullable=False)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # GROSS profit/loss -- (exit - entry) * quantity, no costs deducted. Left
    # deliberately unchanged so every historical row stays comparable to the
    # analysis already run against it. Costs are recorded separately below.
    profit_loss: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Estimated round-trip cost (brokerage, STT, exchange txn, SEBI, stamp
    # duty, GST, plus configurable slippage) and profit_loss net of it. See
    # app/trade_costs.py. Set at close time; 0.0 on open trades and on rows
    # that closed before these columns existed.
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
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
    # Diagnostic snapshot of what the AI actually saw at entry. Null for every
    # non-AI_ORIGIN_* trade and for AI_ORIGIN_* trades opened before these
    # columns existed -- none of it is reconstructable after the fact, which is
    # exactly why it's stored now:
    #   spot_at_entry     -- index spot at the moment of the decision. Lets a
    #                        premium move be converted to index points, to test
    #                        whether stops fire inside normal intraday noise.
    #   day_ohlc_present  -- whether the "Today's session range" line made it
    #                        into the prompt. It's best-effort (Angel One often
    #                        returns zeroed OHLC for index instruments) and is
    #                        the only thing anchoring the 45-min window to the
    #                        broader session, so its absence is a real variable.
    #   tick_sample_count -- how many price samples the 45-min window actually
    #                        contained. Varies ~3 to 100+ depending on whether
    #                        a dashboard tab was open driving tick recording,
    #                        so it's a confound under every cross-trade and
    #                        cross-provider comparison.
    spot_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    day_ohlc_present: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tick_sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Full structural market context at entry (JSON: regime, CPR, ADX, levels,
    # active setups). Phase 1 computes and stores this WITHOUT feeding it to
    # the prompt -- so that when Phase 2 does start prompting with it, there is
    # already a body of paired "what the market looked like" / "what the model
    # decided blind" / "what happened" data to evaluate against. Null for every
    # trade before Phase 1 and for all non-AI_ORIGIN_* trades.
    market_context_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Stop and target expressed in units that are comparable across option
    # types. A "12% stop" is 2.02 ATR on a Nifty call and 1.27 ATR on a put,
    # because ATM puts are 1.28-1.53x more index-sensitive -- so the premium
    # percent is a label that hides what is actually being risked. Computed at
    # entry from the fitted per-bucket coefficients (app/premium_model.py) and
    # the contract's own spot/ATR. Null when no fitted coefficient covers the
    # contract, or when ATR was unavailable -- never guessed.
    stop_index_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_atr_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_index_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_atr_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True when the coefficient used came from outside the fitted DTE range
    # (e.g. Bank Nifty's ~27 DTE monthly against a 0-10 DTE archive). Callers
    # must surface this rather than treating the numbers as measured.
    risk_units_extrapolated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Whether a fitted premium coefficient covered this contract's
    # (index, option_type, dte, moneyness) bucket. False means the CE/PE
    # symmetry rescale could NOT be applied and the trade fell back to raw
    # premium-percent behaviour -- so its stop is not comparable to a matched
    # trade's. Must be surfaced, not silently absorbed.
    calibration_bucket_matched: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Trailing activation and width, per trade rather than the shared
    # StrategyConfig defaults, because they carry the same CE/PE asymmetry as
    # the stop and need the same rescale.
    trail_activate_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    trail_width_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True when this trade's market_context_json was built after the live
    # candle refresh call itself failed (rate limit, network, auth) this
    # cycle -- i.e. from whatever was already stored rather than a fresh
    # pull. Added after the Friday incident where a SmartAPI rate-limit
    # episode caused a silent fallback with no record of which trades, if
    # any, were affected. Null for every trade before this column existed;
    # False (not just absence of True) once it does, so "not stale" is an
    # observed fact rather than an assumed default. See
    # app/ai/originator.py's _load_market_context.
    data_stale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # True when the OTHER provider already opened the same strike and side
    # within a short window -- i.e. Claude and OpenAI independently reached the
    # same conclusion and the account now holds two full-size positions on one
    # idea. Observation only: nothing reads this to change sizing or block an
    # entry. It exists so the frequency and outcome of agreement can be
    # measured before anyone decides whether the doubled exposure is worth the
    # head-to-head comparison it buys. Null for trades predating the column.
    concurrent_correlated_entry: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # trade_id of the other provider's trade, when the above is True. Kept so a
    # correlated pair can be reconstructed and scored together rather than
    # only counted.
    correlated_with_trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    # 26 Aug 2026: four 0-100 sub-scores recorded alongside ai_confidence for
    # future calibration research (see CLAUDE.md). Pure instrumentation --
    # nothing reads these to gate, size, or shape a trade; null on any trade
    # opened before this column existed or whose provider omitted them.
    ai_setup_quality: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ai_entry_quality: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ai_risk_quality: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ai_market_alignment: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
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


class Candle(Base):
    """Exchange OHLCV candles per index, from SmartAPI getCandleData.

    Exists to replace IndexPriceTick as the AI's market-data input. Ticks were
    sampled by whatever happened to call the recorder, so their density varied
    with whether a browser tab was open -- a confound underneath every
    comparison. Candles come from the exchange and don't vary with anything.

    Storage policy: 1-minute rows are the source of truth for the live path,
    with 5/15/60-minute derived by resampling, so the timeframes cannot drift
    out of agreement with each other. 5-minute rows are ALSO stored directly
    for historical backfill, because SmartAPI only serves ~30 days of 1-minute
    but ~100 days per request of 5-minute -- a multi-year backtest is only
    possible at 5-minute. See scripts/backfill_candles.py, which checks the two
    agree on their overlapping window before any fitted parameter is trusted.

    volume is stored but is always 0 for index instruments -- the index itself
    isn't traded. Real volume/VWAP needs the FUTIDX contract, which
    scripts/backfill_futures.py stores in this same table under
    "<INDEX_SYMBOL>_FUT" as index_symbol (e.g. "BANKNIFTY_FUT") -- a distinct
    key, not a new table, so existing load_bars/resample tooling needs no
    changes to read futures candles instead of spot ones.
    """

    __tablename__ = "candles"

    index_symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(16), primary_key=True)
    # IST-naive minute timestamp. SmartAPI returns IST-offset strings; storing
    # the naive local minute keeps the primary key stable and comparisons cheap
    # without re-introducing the tzinfo round-trip problem SQLite has.
    ts_ist: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class AIOriginationLog(Base):
    """One row per AI Origination decision, including the ones that never trade.

    WHY THIS EXISTS
    ---------------
    AI Origination's most common output is NONE, and until now NONE left no
    queryable trace -- only a `logger.info` line that journalctl eventually
    rotates away. Every other strategy path writes to ai_context_logs; this one
    did not, so "why did it decline" was answerable only for as long as the
    journal happened to retain it.

    That gap became concrete on 6 Aug: Bank Nifty held a genuine TREND regime
    from 14:54 to 15:09 with four setups active, and Claude returned NONE every
    cycle. The plausible explanation is the trend-age caution shipped days
    earlier doing its job -- but the trend-age values at those moments were
    never stored, so it cannot be confirmed or refuted. That specific question
    is permanently unanswerable; this table exists so the next one is not.

    It matters most right now because the two-week observation window on the
    trend-age fix has just started, and the whole question is whether behaviour
    changed. Judging that from trades alone would sample only the decisions that
    said yes.

    PURE INSTRUMENTATION. Nothing reads this to make a decision.
    """

    __tablename__ = "ai_origination_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    index_name: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_role: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 26 Aug 2026: four 0-100 sub-scores, same instrumentation-only status and
    # null-on-omission convention as confidence itself -- see StrategyTrade's
    # matching ai_setup_quality/etc. columns and CLAUDE.md.
    setup_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_alignment: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Null unless the decision actually opened a trade -- which is correct, not
    # a gap: NONE and ERROR have no trade to point at.
    trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    regime: Mapped[str] = mapped_column(String(16), nullable=False)
    adx: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpr: Mapped[str | None] = mapped_column(String(16), nullable=True)
    setups: Mapped[str] = mapped_column(Text, nullable=False)

    trend_duration_bars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trend_duration_pct_of_session: Mapped[float | None] = mapped_column(Float, nullable=True)
    move_extent_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 27 Aug 2026: Kaufman's Efficiency Ratio over the last ~hour of 5-min
    # bars -- see app/market_context.py's compute_efficiency_ratio. Same
    # descriptive-only, not-backtested status as the trend-age fields above.
    chop_efficiency_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    # SPLIT BY SIDE rather than the single integer the spec asked for. At
    # decision time the direction is not yet known -- the decision is what
    # determines it -- so a single "same direction" count is undefined for
    # exactly the NONE rows this table was built to capture. Both are stored so
    # a declined cycle can still be read against how many entries the prevailing
    # direction had already taken.
    same_direction_entries_ce: Mapped[int | None] = mapped_column(Integer, nullable=True)
    same_direction_entries_pe: Mapped[int | None] = mapped_column(Integer, nullable=True)

    concurrent_correlated_entry: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    correlated_with_trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    # The PARSED decision, not the raw HTTP body. Retaining full bodies for
    # every cycle of every provider would grow without bound for data that is
    # only diagnostic on failure -- and on failure the raw payload is already
    # preserved in error_detail, which carries a bounded excerpt of it.
    model_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    data_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    risk_units_extrapolated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_ai_origination_logs_index_provider", "index_name", "provider", "timestamp"),
    )


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
    ORIGINATION = "ORIGINATION"


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
