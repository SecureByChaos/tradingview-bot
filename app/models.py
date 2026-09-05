from __future__ import annotations

from datetime import datetime
from enum import Enum
import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Signal(str, Enum):
    BUY_CE = "BUY_CE"
    SELL_CE = "SELL_CE"
    BUY_PE = "BUY_PE"
    SELL_PE = "SELL_PE"


class ExitReason(str, Enum):
    TARGET = "TARGET"
    STOPLOSS = "STOPLOSS"
    TIME_EXIT = "TIME_EXIT"
    TV_EXIT = "TV_EXIT"
    # AI Origination only -- distinct from TIME_EXIT (end-of-day square-off) so
    # reporting can tell "this thesis never developed" apart from "market
    # closed while it was still open." See monitor_open_trades in
    # multi_strategy.py for the stall-window/stall-band check that uses this.
    STALL_EXIT = "STALL_EXIT"
    # Distinct from STOPLOSS on purpose. Both are "premium fell to a level",
    # but they mean opposite things about the trade: STOPLOSS is the original
    # stop firing on a trade that never worked, TRAIL_EXIT is the trailing stop
    # banking a trade that reached at least +8% and then gave some back. Folding
    # them together would make the trailing-stop change unmeasurable -- you
    # could not tell a rescued winner from a plain loss in exit_reason.
    TRAIL_EXIT = "TRAIL_EXIT"
    # Autonomous AI only (app.ai.autonomous) -- the model's own voluntary
    # HOLD/EXIT decision, re-asked every cycle, actually closing the trade.
    # Distinct from STOPLOSS/TARGET (the wide safety-net backstop that
    # mechanically closes a trade the model never got around to exiting) so
    # reporting can tell "the model chose to leave" from "the backstop had to
    # catch it".
    AI_DISCRETION_EXIT = "AI_DISCRETION_EXIT"
    # Quick Scalp only (app.quick_scalp), original EMA/RSI-crossover build
    # (superseded 4 Sep 2026 by the VWAP 2-sigma mean-reversion rebuild
    # below) -- neither the 3% stop nor the 5% target was hit within the
    # strategy's own configured max-hold window. No longer produced by any
    # live code path; kept for historical rows.
    MAX_HOLD_EXIT = "MAX_HOLD_EXIT"
    # Quick Scalp only, 4 Sep 2026 VWAP 2-sigma mean-reversion rebuild --
    # the spec's own "Hard Time Stop": neither leg's target/stop/runner
    # condition fired within 3 completed 1-minute candles (180s) of entry.
    # Deliberately NOT reusing MAX_HOLD_EXIT (a different mechanism, 15-
    # minute window, from the superseded build) or STALL_EXIT (AI
    # Origination's own 60-min/+-5% mechanism) -- distinct parameters,
    # distinct reason, same as every other exit mechanism in this project.
    SCALP_TIME_STOP = "SCALP_TIME_STOP"
    # Quick Scalp only -- the underlying INDEX spot breached
    # StrategyTrade.structural_stop_level (C0's rejection-bar extreme +-1pt,
    # capped at 14 points from the trigger price). Distinct from STOPLOSS,
    # which is the OPTION PREMIUM stop (~8-10 points) checked independently
    # by the shared 30s monitor -- this is a second, index-level invalidation
    # layer the spec asks for explicitly, not a duplicate of the premium one.
    SCALP_STRUCTURAL_STOP = "SCALP_STRUCTURAL_STOP"
    # Quick Scalp only -- the spec's "Target 2 (Runner)": the runner leg
    # (the half of the position NOT closed at Target 1) exits when the
    # underlying spot reaches back to the current session VWAP. Distinct
    # from TARGET (the flat option-point Target 1 on the other leg).
    SCALP_VWAP_TARGET = "SCALP_VWAP_TARGET"
    # Temporary 2-week live trial (3 Sep 2026), admin-toggleable, off by
    # default -- see PlatformSettings.giveback_ratio_stop_enabled and
    # monitor_open_trades in app/multi_strategy.py. Scoped to VALIDATED_SIGNAL/
    # QUICK_SCALP/AUTONOMOUS_AI only. Distinct from STOPLOSS and TRAIL_EXIT so
    # reporting can isolate exactly which trades this specific mechanism
    # closed, matching the real (floor=12%, ratio=30%) cell validated in
    # scripts/giveback_ratio_backtest.py against 227 real AI Origination
    # trades (n=57 armed, 90% CI on mean delta [+1.07%, +3.64%]).
    GIVEBACK_STOP = "GIVEBACK_STOP"
    # Autonomous AI only (app.ai.autonomous), 3 Sep 2026 -- a real trade sat
    # open 4h44m at only +2.27% MFE before the model's own exit call finally
    # fired at -11.24%. Deliberately NOT reusing AI Origination's own
    # STALL_EXIT value (that one is tied to trade.origin.startswith(
    # "AI_ORIGIN_") in monitor_open_trades' shared branch, a different
    # mechanism) -- checked inside check_autonomous_exits itself, before the
    # model is asked, same window/band AI Origination's own STALL_EXIT
    # already uses (60 min / +-5%) rather than an invented new number.
    AUTONOMOUS_STALL_EXIT = "AUTONOMOUS_STALL_EXIT"
    # Autonomous AI only, 3 Sep 2026 -- the four new deterministic exit rules
    # from an external redesign document, implemented as specified rather
    # than adapted (see app.ai.autonomous's module docstring for why this
    # supersedes the earlier adapted-version decision). Each is a distinct
    # value, matching this project's own established convention (see
    # AUTONOMOUS_STALL_EXIT above) of never folding mechanically different
    # exit paths into one shared reason -- doing so would make each mechanism
    # unmeasurable on its own.
    #
    # Fixed-width peak-giveback: peak_pnl_pct >= 20% and a drop of >= 8% from
    # that peak. Deliberately NOT reusing GIVEBACK_STOP -- that mechanism
    # (app/multi_strategy.py) is a different, PROPORTIONAL shape (floor=12%,
    # width=30% of the peak-to-entry distance, validated against 227 real AI
    # Origination trades). This one is the document's own fixed-width rule,
    # a shape this project had previously tested and moved away from (the 31
    # Jul holdout) -- kept distinct so the two can never be confused in
    # reporting even though both can fire on the same trade population.
    AUTONOMOUS_TRAIL_EXIT = "AUTONOMOUS_TRAIL_EXIT"
    # Break-even violation: once peak_pnl_pct has reached >= 15%, a pullback
    # to <= 1% exits immediately rather than letting a real move round-trip
    # all the way back to breakeven or a loss.
    AUTONOMOUS_BREAKEVEN_EXIT = "AUTONOMOUS_BREAKEVEN_EXIT"
    # Structural invalidation: the underlying's spot-vs-VWAP relationship
    # contradicts the held position's side (e.g. holding CE but spot is now
    # below VWAP). Requires the new futures-based VWAP feature -- see
    # OptionFinder.find_current_futures_contract.
    AUTONOMOUS_STRUCTURAL_EXIT = "AUTONOMOUS_STRUCTURAL_EXIT"
    # Session-close warning: minutes_to_square_off <= 15 (i.e. from 14:45 IST
    # onward against this module's 15:00 cutoff). Distinct from the existing
    # unconditional TIME_EXIT this module already fires at 15:00 sharp --
    # this is an earlier, doc-specified warning-turned-exit, not a
    # replacement for that later hard backstop.
    AUTONOMOUS_SESSION_CLOSE = "AUTONOMOUS_SESSION_CLOSE"
    # Validated Signal only (app.validated_signal), 5 Sep 2026 full rebuild to
    # an external Morning/Afternoon breakout specification -- every stop/
    # target here is a SPOT INDEX price level (StrategyTrade.structural_
    # stop_level / structural_target_level), checked by this module's own
    # 5-second exit poll against live spot LTP, never the option premium the
    # shared 30s monitor_open_trades checks. Distinct from STOPLOSS/TARGET
    # (which mean a PREMIUM level was hit) on purpose, same reasoning as
    # Quick Scalp's SCALP_STRUCTURAL_STOP/SCALP_VWAP_TARGET above.
    VS_SPOT_STOP = "VS_SPOT_STOP"
    VS_SPOT_TARGET = "VS_SPOT_TARGET"
    # The spec's own 20-minute stagnation rule: neither the spot stop nor the
    # spot target has been touched within 20 minutes of entry. Distinct from
    # STALL_EXIT (AI Origination's own 60-min/+-5%-of-PREMIUM mechanism) --
    # different window, different measured quantity (elapsed time only, no
    # P&L band), different population.
    VS_STAGNATION_EXIT = "VS_STAGNATION_EXIT"


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    context_version: str | None = None
    strategy: str | None = None
    signal: Signal
    market_data: "TradingViewMarketData | None" = None
    indicators: "TradingViewIndicators | None" = None
    trend: "TradingViewTrend | None" = None
    strategy_filters: "TradingViewStrategyFilters | None" = None
    trade_state: "TradingViewTradeState | None" = None

    @model_validator(mode="before")
    @classmethod
    def _unwrap_json_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            text = data.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    return json.loads(text)
                except Exception:
                    return data
        return data


class TradingViewIndicators(BaseModel):
    model_config = ConfigDict(extra="allow")
    ema9: float | None = None
    ema20: float | None = None
    ema21: float | None = None
    ema_gap: float | None = None
    vwap: float | None = None
    rsi: float | None = None
    atr: float | None = None
    adx: float | None = None
    di_plus: float | None = None
    di_minus: float | None = None
    supertrend: float | None = None
    volume_ratio: float | None = None
    orb_high: float | None = None
    orb_low: float | None = None
    filters: dict[str, Any] | None = None
    rr_ratio: float | None = None


class TradingViewMarketData(BaseModel):
    model_config = ConfigDict(extra="allow")
    banknifty_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    timeframe: str | None = None
    volume: float | None = None
    timestamp: str | int | float | None = None


class TradingViewTrend(BaseModel):
    model_config = ConfigDict(extra="allow")
    trend_direction: str | None = None
    breakout: bool | None = None
    strong_candle: bool | None = None
    sideways_filter: bool | None = None
    htf_confirmation: bool | None = None


class TradingViewStrategyFilters(BaseModel):
    model_config = ConfigDict(extra="allow")
    ema_filter: bool | None = None
    supertrend_filter: bool | None = None
    adx_filter: bool | None = None
    session_filter: bool | None = None
    trade_limit_filter: bool | None = None


class TradingViewTradeState(BaseModel):
    model_config = ConfigDict(extra="allow")
    trade_number: int | None = None
    daily_trade_count: int | None = None
    position: int | str | None = None
    session: str | None = None
    market_condition: str | None = None
    trailing_active: bool | None = None


class OptionContract(BaseModel):
    exchange: str = "NFO"
    tradingsymbol: str
    symboltoken: str
    strike: int
    expiry: str
    option_type: str
    lot_size: int
    # Spot price OptionFinder already fetched from SmartAPI while picking the
    # ATM strike -- carried here so callers can reuse it (e.g. for the
    # spot-price cross-check) instead of firing a second, redundant ltpData
    # call against Angel One's 1 req/sec /quote rate limit.
    spot_price: float | None = None


class ActiveTrade(BaseModel):
    signal: Signal
    contract: OptionContract
    entry_price: float = Field(gt=0)
    stoploss: float = Field(gt=0)
    target: float = Field(gt=0)
    quantity: int = Field(gt=0)
    entry_time: datetime
    order_id: Optional[str] = None


class WebhookResponse(BaseModel):
    accepted: bool
    message: str
    active_trade: Optional[ActiveTrade] = None
