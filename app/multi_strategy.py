from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db_models import SLMode, StrategyConfig, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus, TradingMode
from app.models import ExitReason, Signal, WebhookResponse
from app.option_finder import OptionFinder
from app.platform import get_index_config, get_or_create_settings, log_event, update_strategy_stats_after_close
from app.signal_validation import check_premium_sanity, check_spot_price_deviation
from app.smartapi_client import SmartAPIClient
from app.premium_model import days_to_expiry, symmetric_premium_percent
from app.telegram_service import TelegramService
from app.trade_costs import estimate_round_trip_cost
from app.time_utils import IST, format_ist, parse_hhmm, to_ist, utc_now

logger = logging.getLogger(__name__)

# AI Origination only (see the origin check below) -- these trades get no
# further AI judgment after entry, unlike real signal trades which at least
# get a shadow exit review. Real intraday options traders commonly use a
# 30-60 min time stop specifically because theta decay makes a stalled,
# near-breakeven position a slow bleed regardless of direction; a momentum
# thesis that hasn't shown anything after an hour has effectively already
# been disproven. Deliberately separate from the existing 15:15 TIME_EXIT
# check below -- that's the end-of-day catch-all for every trade, this is an
# earlier, AI-Origination-specific "this isn't developing" exit.
_STALL_WINDOW_MINUTES = 60
_STALL_BAND_PERCENT = 5.0

# AI Origination only, and deliberately so. Every AI Origination trade runs in
# FIXED mode in practice (the TRAILING fallback only engages when the model
# returns unusable sl/target numbers, which it reliably doesn't), and the FIXED
# branch has no protection at all between the stop and the target. The measured
# consequence across 21-24 Jul: trades repeatedly travelled most of the way to
# target and gave it all back, including peaks of +17.26% and +13.65% that
# closed at -12.09% and -14.67%.
#
# These constants are scoped to AI_ORIGIN_* trades ONLY. The same FIXED branch
# is shared by BNV5.1/BNV6/BNV7/NV1, which are currently the profitable
# strategies -- changing their behaviour here would both alter what's working
# and confound the single-variable measurement this change exists to enable.
#
# The 5% trail is anchored to ENTRY price, not to the running high, matching
# the existing TRAILING branch's form (trailing_stop = high - entry*offset).
# That tightens proportionally as the trade runs and is the already-tested
# shape; against a running-high anchor it differs by ~1pp on a trade peaking
# at +20%.
_AI_ORIGIN_TRAIL_ACTIVATION_PERCENT = 8.0
_AI_ORIGIN_TRAIL_OFFSET_PERCENT = 5.0

# Temporary 2-week live trial (3 Sep 2026), admin-toggleable via
# PlatformSettings.giveback_ratio_stop_enabled, off by default. The real
# 2-month cross-strategy review (scripts/strategy_review.py) found 84% of
# losses across every strategy had a positive MFE before finishing negative
# -- scripts/giveback_ratio_backtest.py then tested a proportional trailing
# stop (protected room scales with the size of the move, unlike the already-
# falsified fixed-point trail) against 227 real closed AI Origination
# trades. Exactly one (floor, ratio) cell cleared the bootstrap CI: floor=12%
# of entry, ratio=30% of the peak gain protected once armed (n=57 armed,
# 90% CI on mean delta [+1.07%, +3.64%]).
#
# Scoped to QUICK_SCALP/AUTONOMOUS_AI only, NOT AI_ORIGIN_*, SIGNAL, or (as
# of 5 Sep 2026) VALIDATED_SIGNAL:
#   - AI_ORIGIN_* already has its own trailing mechanism just above (trail_
#     activate_percent/trail_width_percent, arms ~8%) -- the backtest never
#     modeled stacking a second, independent trail on top of that, so
#     applying this there would be deploying an untested interaction, not
#     the validated result.
#   - SIGNAL is off-limits per this file's own "shared-FIXED-branch hazard"
#     precedent: BNV5.1/BNV6/BNV7/NV1 are the currently profitable
#     strategies and changing their exit logic risks both breaking what
#     works and confounding the single-variable measurement this trial
#     needs. Every prior AI-Origination-only mechanism in this file (STALL_
#     EXIT, the trail above) drew the same line.
#   - VALIDATED_SIGNAL was REMOVED from this trial's scope when that origin
#     was rebuilt to an external Morning/Afternoon breakout spec (5 Sep
#     2026, app.validated_signal). This trial was scoped to origins with
#     "zero trailing/discretionary protection today" -- true of the
#     superseded fixed-12%/20% build, no longer true of the rebuild, which
#     now runs its own complete, spot-level stop/target/stagnation exit
#     engine on its own 5-second poll. Leaving it in scope would let this
#     premium-based mechanism silently override that engine's exits on a
#     subset of trades (the rebuild's own premium stoploss/target fields are
#     deliberately unreachable sentinels, but giveback_stop_level is
#     computed independently of them from trade.highest_price, so it would
#     still have been able to fire).
# QUICK_SCALP/AUTONOMOUS_AI currently have no trailing/discretionary exit of
# their own on their FIXED-mode trades, so this is the only protective
# mechanism active for them -- no interaction to reason about.
#
# Review after the 2-week trial: if the real outcome supports it, this
# becomes a permanent, unconditional mechanism for these origins; if not,
# delete this block and the admin toggle rather than leaving it dormant.
_GIVEBACK_STOP_FLOOR_PERCENT = 12.0
_GIVEBACK_STOP_RATIO = 0.30
_GIVEBACK_STOP_ORIGINS = frozenset({"QUICK_SCALP", "AUTONOMOUS_AI"})

# Fallback only -- PlatformSettings.trading_start_time/square_off_time (Settings
# > General) are the real, admin-editable values. These match what was
# hardcoded before 19 Aug 2026 (AI Origination's own _TRADING_START_HOUR/
# _MINUTE default and the TIME_EXIT check's literal 15:15) so a missing/
# malformed setting degrades to the exact behaviour this app already had.
_DEFAULT_TRADING_START = (9, 45)
_DEFAULT_TRADING_END = (15, 15)

# NV1 only. 18 Aug 2026: NV1 opened a same-day-expiry (0 DTE) Nifty PE that lost
# -25.52%, beyond even its own correctly-rescaled 23.9% stop (confirmed via
# nv1_stop_check.py against the real trade row -- the CE/PE symmetric-premium
# rescale computed and stored exactly 23.9%; the extra -1.62pp was execution
# slippage on a fast-moving 0 DTE premium between 30s monitor ticks). 0 DTE is
# more extreme than the worst bucket stop_survivability.py ever measured
# (36.5% noise-breach at 2-5 DTE on Bank Nifty calls, rising as DTE shrinks) --
# AI Origination has carried an equivalent floor (_MIN_DTE_TO_TRADE in
# app/ai/originator.py) since 3 Aug for the same reason; rule-based strategies
# never got one because handle_signal never passed min_dte to
# find_atm_contract at all.
#
# Scoped to NV1 alone, not all four rule-based strategies -- BNV5.1/BNV6/BNV7
# are the currently-profitable strategies under this file's own change-freeze
# (see CLAUDE.md's "shared-FIXED-branch hazard"), and NV1 is the one that
# actually hit this, fires under 3x/month, and is already flagged elsewhere as
# the least statistically tested of the four.
#
# Trade-off, deliberately accepted rather than hidden: option_finder.py's
# expiry_itm_strikes shift (NV1's "Expiry ITM" setting) only ever fires when
# find_atm_contract's selected expiry IS today (is_expiry_day). Rolling past
# 0 DTE here means that branch can never trigger for NV1 again -- this floor
# does not tighten the existing expiry-day mitigation, it removes the
# scenario it was built for. Accepted because today's real result shows the
# ITM shift alone did not keep the loss inside the intended stop distance;
# not trading 0 DTE at all is the more direct fix for what was actually
# measured.
_NV1_MIN_DTE = 1


class MultiStrategyTradeManager:
    def __init__(
        self,
        settings: Settings,
        smartapi: SmartAPIClient,
        option_finder: OptionFinder,
        telegram: TelegramService,
    ) -> None:
        self.settings = settings
        self.smartapi = smartapi
        self.option_finder = option_finder
        self.telegram = telegram

    def handle_signal(
        self,
        db: Session,
        strategy_name: str | None,
        signal: Signal,
        market_data: dict[str, object] | None = None,
    ) -> WebhookResponse:
        resolved_name = (strategy_name or self.settings.default_strategy_name).strip()
        strategy = self.get_strategy(db, resolved_name)
        if strategy is None:
            return WebhookResponse(accepted=False, message=f"Rejected: strategy '{resolved_name}' does not exist")
        if strategy.name.upper() == "V7" and signal in {Signal.SELL_CE, Signal.SELL_PE}:
            return self.handle_v7_tv_exit(db, strategy.name, signal)
        state = self.current_state(db, strategy.name)
        if signal in {Signal.BUY_CE, Signal.BUY_PE}:
            if state != "FLAT":
                event = "OPEN_CE" if signal == Signal.BUY_CE else "OPEN_PE"
                message = f"[STATE] {event} ignored"
                log_event(db, "STATE", message, "WARNING")
                return WebhookResponse(accepted=False, message=message)
            log_event(db, "STATE", "[STATE] OPEN_CE accepted" if signal == Signal.BUY_CE else "[STATE] OPEN_PE accepted")
        elif signal in {Signal.SELL_CE, Signal.SELL_PE}:
            # TradingView SELL_CE/SELL_PE signals are observational only -- the
            # real trade is never closed off this signal. Exits are decided
            # exclusively by monitor_open_trades' own SL/target/trailing logic.
            # Mirrors V7Manager.record_exit_suggestion, which already worked
            # this way; this path (used by BNV6/BNV7/etc, i.e. every non-V7
            # strategy) previously still closed on TV_EXIT -- that's the gap
            # being fixed here.
            option_type = "CE" if signal == Signal.SELL_CE else "PE"
            event = "CLOSE_CE" if option_type == "CE" else "CLOSE_PE"
            expected_state = "LONG_CE" if option_type == "CE" else "LONG_PE"
            observation_reason = ""
            if state != expected_state:
                if state == "FLAT":
                    observation_reason = f"Ignored {event} because no active {option_type} position exists."
                elif state == "LONG_CE":
                    observation_reason = f"Ignored {event} because active CE position exists."
                elif state == "LONG_PE":
                    observation_reason = f"Ignored {event} because active PE position exists."
                else:
                    observation_reason = f"Ignored {event} due to invalid state {state}."
                log_event(db, "STATE", f"[STATE] {event} ignored", "WARNING", {"reason": observation_reason})
            return self.record_exit_suggestion(db, strategy.name, signal, observation_reason)
        if not strategy.enabled:
            return WebhookResponse(accepted=False, message=f"Rejected: strategy '{strategy.name}' is disabled")
        if strategy.mode == TradingMode.PAPER and not strategy.paper_trade:
            return WebhookResponse(accepted=False, message=f"Rejected: paper trading is disabled for '{strategy.name}'")
        if strategy.mode == TradingMode.LIVE and not strategy.live_trade:
            return WebhookResponse(accepted=False, message=f"Rejected: live trading is disabled for '{strategy.name}'")

        # Trading-window gate (Settings > General), 19 Aug 2026. Previously
        # unenforced for every rule-based strategy -- check_market_hours() at
        # the webhook layer only ever logged a WARNING, never rejected, so a
        # BUY_CE/BUY_PE arriving at any hour the market technically permits
        # would open a trade regardless of the admin's intended trading
        # window. BUY_CE/BUY_PE is the only signal shape that reaches this
        # point (SELL_* returns above as an observation), so no extra signal
        # check is needed here.
        platform_settings = get_or_create_settings(db)
        start_hm = parse_hhmm(platform_settings.trading_start_time, _DEFAULT_TRADING_START)
        end_hm = parse_hhmm(platform_settings.square_off_time, _DEFAULT_TRADING_END)
        now_ist_for_window = datetime.now(IST)
        now_hm = (now_ist_for_window.hour, now_ist_for_window.minute)
        if now_hm < start_hm or now_hm >= end_hm:
            message = (
                f"Rejected: outside trading window ({start_hm[0]:02d}:{start_hm[1]:02d}-"
                f"{end_hm[0]:02d}:{end_hm[1]:02d} IST, Settings > General)"
            )
            log_event(db, "STATE", f"[STATE] FAILED_ENTRY {signal.value}", "WARNING", {"strategy": strategy.name, "reason": message})
            return WebhookResponse(accepted=False, message=message)

        active_count = self.active_trade_count(db, strategy.name)
        if active_count >= strategy.max_active_trades:
            return WebhookResponse(
                accepted=False,
                message=f"Rejected: strategy '{strategy.name}' active trade limit reached",
            )
        index = get_index_config(db, strategy.index_symbol)
        if index is None or not index.enabled:
            message = f"Rejected: index '{strategy.index_symbol}' is not configured/enabled. Configure it in Settings > Instruments."
            log_event(db, "STATE", f"[STATE] FAILED_ENTRY {signal.value}", "WARNING", {"strategy": strategy.name, "reason": message})
            return WebhookResponse(accepted=False, message=message)
        min_dte = _NV1_MIN_DTE if strategy.name == "NV1" else 0
        try:
            contract = self.option_finder.find_atm_contract(
                signal, index, strategy.expiry_itm_strikes, min_dte=min_dte
            )
            entry_price = self.smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
        except Exception as exc:
            log_event(
                db,
                "STATE",
                f"[STATE] FAILED_ENTRY {signal.value}",
                "WARNING",
                {"strategy": strategy.name, "error": str(exc)},
            )
            raise
        # Reuse the spot price OptionFinder already fetched while picking the
        # ATM strike -- no extra SmartAPI call, avoids tripping the 1 req/sec
        # /quote rate limit.
        claimed_price = (market_data or {}).get("banknifty_price") or (market_data or {}).get("index_price")
        for warning in (
            check_spot_price_deviation(claimed_price, contract.spot_price),
            check_premium_sanity(entry_price),
        ):
            if warning:
                log_event(db, "VALIDATION", warning, "WARNING", {"strategy": strategy.name, "signal": signal.value})
        mode = self.resolve_mode(strategy)
        quantity = self.calculate_quantity(strategy, entry_price, contract.lot_size)
        required_capital = round(entry_price * quantity, 2)
        reject_reason = "lots_per_trade is invalid" if quantity <= 0 else ""
        logger.info(
            "Mode: %s | Configured lots_per_trade: %s | Lot size: %s | Required capital: %.2f | Trade quantity: %s | Reject reason: %s",
            mode, strategy.lots_per_trade, contract.lot_size, required_capital, quantity, reject_reason,
        )
        if quantity <= 0:
            log_event(
                db,
                "STATE",
                f"[STATE] FAILED_ENTRY {signal.value}",
                "WARNING",
                {"strategy": strategy.name, "reason": "lots_per_trade is invalid"},
            )
            return WebhookResponse(
                accepted=False,
                message=f"Rejected: lots_per_trade is invalid for {contract.tradingsymbol}",
            )

        is_short = signal.value.startswith("SELL")
        order_id = None
        if mode == TradingMode.LIVE:
            order_id = self.smartapi.place_market_order(contract, "SELL" if is_short else "BUY", quantity)

        # Same CE/PE asymmetry as AI Origination: an identical percentage is a
        # tighter index distance on a put. Rescale so both sides are the same
        # bet. Falls through unchanged when no fitted coefficient covers the
        # contract, rather than borrowing an unrelated bucket's.
        dte = days_to_expiry(contract.expiry, to_ist(utc_now()).date())
        sl_percent, bucket_matched = symmetric_premium_percent(
            strategy.sl_percent, strategy.index_symbol, contract.option_type, dte
        )
        tp_percent, _ = symmetric_premium_percent(
            strategy.tp_percent, strategy.index_symbol, contract.option_type, dte
        )
        stoploss = round(entry_price * (1 + sl_percent / 100), 2) if is_short else round(entry_price * (1 - sl_percent / 100), 2)
        target = round(entry_price * (1 - tp_percent / 100), 2) if is_short else round(entry_price * (1 + tp_percent / 100), 2)
        now = utc_now()
        trade = StrategyTrade(
            trade_id=uuid4().hex,
            strategy_name=strategy.name,
            signal=signal.value,
            index_symbol=strategy.index_symbol,
            exchange=contract.exchange,
            tradingsymbol=contract.tradingsymbol,
            symboltoken=contract.symboltoken,
            strike=contract.strike,
            expiry=contract.expiry,
            option_type=contract.option_type,
            quantity=quantity,
            calibration_bucket_matched=bucket_matched,
            investment_amount=required_capital,
            entry_price=round(entry_price, 2),
            current_premium=round(entry_price, 2),
            stoploss=stoploss,
            target=target,
            entry_time=now,
            mode=mode,
            status=TradeStatus.OPEN,
            result=TradeResult.OPEN,
            entry_order_id=order_id,
            highest_price=round(entry_price, 2),
            lowest_price=round(entry_price, 2),
            trailing_active=False,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        log_event(
            db,
            "TRADE",
            f"[{strategy.name}] {signal.value} opened @ strike {trade.strike}",
            payload={"trade_id": trade.trade_id, "entry_time_ist": format_ist(trade.entry_time)},
        )
        self.telegram.send(db, f"Trade Opened\n[{strategy.name}] {signal.value}\nEntry: {trade.entry_price}")
        return WebhookResponse(accepted=True, message="Trade opened")

    def record_exit_suggestion(
        self, db: Session, strategy_name: str, signal: Signal, observation_reason: str = ""
    ) -> WebhookResponse:
        """Logs a TradingView SELL_CE/SELL_PE signal for visibility without acting
        on it -- the real trade is never closed here. Mirrors
        V7Manager.record_exit_suggestion (kept as a separate copy rather than a
        shared helper since the two managers' StrategyTrade lookups and default
        index fallback differ slightly)."""
        option_type = "CE" if signal == Signal.SELL_CE else "PE"
        trade = db.scalar(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == strategy_name,
                StrategyTrade.option_type == option_type,
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == "SIGNAL",
            )
            .order_by(StrategyTrade.entry_time.desc())
            .limit(1)
        )
        spot = None
        premium = None
        pnl_unrealized = None
        trailing_active = False
        trailing_sl = None
        trade_id = None
        if trade is not None:
            trade_id = trade.trade_id
            premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            pnl_unrealized = round((premium - trade.entry_price) * trade.quantity, 2)
            trailing_active = trade.trailing_active
            trailing_sl = trade.trailing_stop
        index_symbol = trade.index_symbol if trade is not None else None
        if index_symbol is None:
            strategy = self.get_strategy(db, strategy_name)
            index_symbol = strategy.index_symbol if strategy is not None else "BANKNIFTY"
        try:
            index = get_index_config(db, index_symbol)
            spot = round(self.smartapi.get_index_spot(index) if index is not None else self.smartapi.get_banknifty_spot(), 2)
        except Exception:
            spot = None
        payload = {
            "timestamp": utc_now().isoformat(),
            "trade_id": trade_id,
            "signal": signal.value,
            "index_symbol": index_symbol,
            "index_price": spot,
            "option_premium": round(premium, 2) if premium is not None else None,
            "unrealized_pnl": pnl_unrealized,
            "trailing_active": trailing_active,
            "current_trailing_sl": trailing_sl,
            "observation_reason": observation_reason,
        }
        log_event(db, "TRADE", f"[{strategy_name}] TradingView Exit Suggestion", payload=payload)
        message = "TradingView Exit Suggestion recorded"
        if observation_reason:
            message = f"{message}: {observation_reason}"
        return WebhookResponse(accepted=True, message=message)

    def handle_v7_tv_exit(self, db: Session, strategy_name: str, signal: Signal) -> WebhookResponse:
        option_type = "CE" if signal == Signal.SELL_CE else "PE"
        log_event(db, "WEBHOOK", f"[V7] TradingView exit signal received: {signal.value}")
        trade = db.scalar(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == strategy_name,
                StrategyTrade.option_type == option_type,
                StrategyTrade.status == TradeStatus.OPEN,
            )
            .order_by(StrategyTrade.entry_time.desc())
            .limit(1)
        )
        if trade is None:
            return WebhookResponse(accepted=True, message=f"No active {option_type} trade to close")
        premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
        self.close_trade(db, trade, premium, ExitReason.TV_EXIT)
        log_event(db, "TRADE", f"[V7] Closed active trade: {trade.trade_id}")
        return WebhookResponse(accepted=True, message=f"Closed active {option_type} trade")

    def monitor_open_trades(self, db: Session) -> list[StrategyTrade]:
        closed: list[StrategyTrade] = []
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    func.upper(StrategyTrade.strategy_name) != "V7",
                )
            )
        )
        if not trades:
            return closed
        strategy_names = {trade.strategy_name for trade in trades}
        strategies_by_name = {
            strategy.name: strategy
            for strategy in db.scalars(select(StrategyConfig).where(StrategyConfig.name.in_(strategy_names)))
        }
        now_ist = datetime.now(IST)
        # Fetched once per tick, not per trade -- Settings > General's
        # square_off_time (19 Aug 2026) replaces what was a hardcoded 15:15
        # literal below. Also carries giveback_ratio_stop_enabled (3 Sep 2026).
        platform_settings = get_or_create_settings(db)
        square_off_hm = parse_hhmm(platform_settings.square_off_time, _DEFAULT_TRADING_END)
        for trade in trades:
            try:
                strategy = strategies_by_name.get(trade.strategy_name)
                # Per-trade sl_mode wins when set (AI Origin trades use this --
                # see StrategyTrade.sl_mode's docstring); otherwise fall back to
                # the strategy-level setting as before.
                sl_mode = trade.sl_mode or (strategy.sl_mode if strategy is not None else SLMode.FIXED)
                activation_percent = strategy.trailing_activation_percent if strategy is not None else 10.0
                offset_percent = strategy.trailing_offset_percent if strategy is not None else 5.0

                premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
                trade.current_premium = round(premium, 2)
                trade.pnl_percent = round(((premium - trade.entry_price) / trade.entry_price) * 100, 2)
                db.add(StrategyTradeTick(trade_id=trade.trade_id, premium=trade.current_premium))
                is_short = trade.signal.startswith("SELL")
                activation_threshold = round(trade.entry_price * (activation_percent / 100), 2)
                trailing_offset = round(trade.entry_price * (offset_percent / 100), 2)
                reason: ExitReason | None = None

                if is_short:
                    trade.lowest_price = premium if trade.lowest_price is None else min(trade.lowest_price, premium)
                    if sl_mode == SLMode.TRAILING:
                        if not trade.trailing_active and premium <= trade.entry_price - activation_threshold:
                            trade.trailing_active = True
                        if trade.trailing_active:
                            trade.trailing_stop = round(trade.lowest_price + trailing_offset, 2)
                        if trade.trailing_active and trade.trailing_stop is not None and premium >= trade.trailing_stop:
                            reason = ExitReason.STOPLOSS
                        elif not trade.trailing_active and premium >= trade.stoploss:
                            reason = ExitReason.STOPLOSS
                    else:
                        if premium >= trade.stoploss:
                            reason = ExitReason.STOPLOSS
                        elif premium <= trade.target:
                            reason = ExitReason.TARGET
                else:
                    trade.highest_price = premium if trade.highest_price is None else max(trade.highest_price, premium)
                    # 24 Aug 2026: long trades (every non-V7 strategy only ever opens
                    # BUY_CE/BUY_PE -- SELL_* is observation-only) never touched
                    # lowest_price, since the only place that updated it was the
                    # is_short branch above, which is structurally unreachable here.
                    # highest_price alone is sufficient for the long-side trailing
                    # logic below, so this doesn't change any exit decision -- it's
                    # purely restoring a real running-low value to a column that was
                    # otherwise permanently pinned at entry_price. MAE% in exports is
                    # already computed from strategy_trade_ticks (see dashboard_
                    # routes.py's _excursion), not this column, so that figure is
                    # unaffected either way.
                    trade.lowest_price = premium if trade.lowest_price is None else min(trade.lowest_price, premium)
                    if sl_mode == SLMode.TRAILING:
                        if not trade.trailing_active and premium >= trade.entry_price + activation_threshold:
                            trade.trailing_active = True
                        if trade.trailing_active:
                            trade.trailing_stop = round(trade.highest_price - trailing_offset, 2)
                        if trade.trailing_active and trade.trailing_stop is not None and premium <= trade.trailing_stop:
                            reason = ExitReason.STOPLOSS
                        elif not trade.trailing_active and premium <= trade.stoploss:
                            reason = ExitReason.STOPLOSS
                    else:
                        # AI Origination only -- arms a trailing stop once the
                        # trade has proven itself, keeping the original stop in
                        # force until activation and the target in force
                        # throughout. For every other strategy trailing_active
                        # stays False here, so the two checks below run in
                        # their original order with their original meaning and
                        # behaviour is unchanged.
                        if trade.origin.startswith("AI_ORIGIN_"):
                            # Per-trade values when present: they were rescaled
                            # at entry so a CE and a PE get the same INDEX
                            # distance rather than the same premium percentage
                            # (puts are 1.28-1.53x more sensitive). Falls back
                            # to the shared nominals for trades opened before
                            # that rescale existed.
                            activate_pct = trade.trail_activate_percent or _AI_ORIGIN_TRAIL_ACTIVATION_PERCENT
                            width_pct = trade.trail_width_percent or _AI_ORIGIN_TRAIL_OFFSET_PERCENT
                            activation_price = trade.entry_price * (1 + activate_pct / 100)
                            if not trade.trailing_active and premium >= activation_price:
                                trade.trailing_active = True
                                logger.info(
                                    "[TRAIL] %s armed at %.2f (entry %.2f, +%.1f%%)",
                                    trade.trade_id, premium, trade.entry_price, activate_pct,
                                )
                            if trade.trailing_active:
                                trade.trailing_stop = round(
                                    trade.highest_price - (trade.entry_price * (width_pct / 100)), 2
                                )

                        # See _GIVEBACK_STOP_* constants' comment above for
                        # scope and the real backtest result behind this.
                        # giveback_stop_level stays None (operative_stop ==
                        # trade.stoploss, byte-identical to before this
                        # existed) for every trade not in the trial's three
                        # origins, or while the toggle is off, or before the
                        # trade's own MFE has cleared the floor.
                        giveback_stop_level: float | None = None
                        if (
                            platform_settings.giveback_ratio_stop_enabled
                            and trade.origin in _GIVEBACK_STOP_ORIGINS
                            and trade.entry_price > 0
                        ):
                            mfe_percent = (trade.highest_price - trade.entry_price) / trade.entry_price * 100.0
                            if mfe_percent >= _GIVEBACK_STOP_FLOOR_PERCENT:
                                giveback_stop_level = trade.highest_price - _GIVEBACK_STOP_RATIO * (
                                    trade.highest_price - trade.entry_price
                                )
                        operative_stop = trade.stoploss
                        if giveback_stop_level is not None:
                            operative_stop = max(operative_stop, giveback_stop_level)

                        if trade.trailing_active and trade.trailing_stop is not None and premium <= trade.trailing_stop:
                            reason = ExitReason.TRAIL_EXIT
                        elif premium <= operative_stop:
                            reason = ExitReason.GIVEBACK_STOP if operative_stop > trade.stoploss else ExitReason.STOPLOSS
                        elif premium >= trade.target:
                            reason = ExitReason.TARGET

                # Trailing activation exempts a trade from STALL_EXIT for the
                # rest of its life. STALL_EXIT exists to kill trades going
                # nowhere; a trade that reached +8% went somewhere, and the
                # trail owns it from that point. Written as an exemption rather
                # than a check-ordering so exit_reason stays unambiguous when
                # reading the results -- a trade that armed the trail can never
                # afterwards be recorded as a stall.
                if reason is None and trade.origin.startswith("AI_ORIGIN_") and not trade.trailing_active:
                    # SQLite doesn't reliably round-trip tzinfo even on a
                    # DateTime(timezone=True) column -- trade.entry_time can
                    # come back offset-naive, which breaks a raw subtraction
                    # against an offset-aware now(). to_ist() already exists
                    # specifically to normalize that (see its docstring-less
                    # but consistent use everywhere else in this codebase);
                    # reuse it here instead of subtracting the raw values.
                    entry_time_ist = to_ist(trade.entry_time)
                    elapsed_minutes = (now_ist - entry_time_ist).total_seconds() / 60 if entry_time_ist else 0
                    if elapsed_minutes >= _STALL_WINDOW_MINUTES and abs(trade.pnl_percent) <= _STALL_BAND_PERCENT:
                        reason = ExitReason.STALL_EXIT

                if reason is None and (now_ist.hour, now_ist.minute) >= square_off_hm:
                    reason = ExitReason.TIME_EXIT
                if reason is not None:
                    self.close_trade(db, trade, premium, reason)
                    closed.append(trade)
            except Exception as exc:
                logger.exception("Multi-strategy monitor failed for trade %s", trade.trade_id)
                log_event(db, "ERROR", f"[{trade.strategy_name}] monitor failed", "ERROR", {"error": str(exc)})
                self.telegram.send(db, f"System Error\n[{trade.strategy_name}] monitor failed: {exc}")
        db.commit()
        return closed

    def close_trade(self, db: Session, trade: StrategyTrade, exit_price: float, reason: ExitReason) -> StrategyTrade:
        if trade.status != TradeStatus.OPEN:
            return trade
        if trade.mode == TradingMode.LIVE:
            from app.models import OptionContract

            contract = OptionContract(
                exchange=trade.exchange,
                tradingsymbol=trade.tradingsymbol,
                symboltoken=trade.symboltoken,
                strike=trade.strike,
                expiry=trade.expiry,
                option_type=trade.option_type,
                lot_size=max(trade.quantity, 1),
            )
            trade.exit_order_id = self.smartapi.place_market_order(
                contract,
                "BUY" if trade.signal.startswith("SELL") else "SELL",
                trade.quantity,
            )
        trade.exit_price = round(exit_price, 2)
        trade.current_premium = round(exit_price, 2)
        trade.exit_time = utc_now()
        direction = -1 if trade.signal.startswith("SELL") else 1
        trade.profit_loss = round((exit_price - trade.entry_price) * trade.quantity * direction, 2)
        trade.pnl_percent = round(((exit_price - trade.entry_price) / trade.entry_price) * 100 * direction, 2)
        # Gross figures above are left exactly as they were. Cost is recorded
        # alongside rather than deducted, so historical rows stay comparable.
        trade.estimated_cost = estimate_round_trip_cost(
            trade.entry_price, exit_price, trade.quantity
        ).total
        trade.net_pnl = round(trade.profit_loss - trade.estimated_cost, 2)
        trade.result = self.result_for_pnl(trade.pnl_percent)
        trade.status = TradeStatus.CLOSED
        trade.exit_reason = reason.value
        # AI_ALT_* trades are evaluation-only side-by-side comparisons against the
        # real signal trade -- they must never affect the real strategy's risk
        # state (consecutive losses / risk lock), send Telegram notifications, or
        # trigger further AI review. Their own P&L stays on the trade row itself
        # for later comparison.
        is_ai_alternative = trade.origin != "SIGNAL"
        if not is_ai_alternative:
            stats = update_strategy_stats_after_close(db, trade.strategy_name, trade.result)
        db.commit()
        db.refresh(trade)
        log_event(
            db,
            "TRADE",
            f"[{trade.strategy_name}] trade closed: {reason.value} (strike {trade.strike})",
            payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent, "exit_time_ist": format_ist(trade.exit_time), "origin": trade.origin},
        )
        if is_ai_alternative:
            return trade
        if stats.risk_locked:
            log_event(db, "RISK", f"Strategy {trade.strategy_name} locked due to consecutive losses", "WARNING")
            self.telegram.send(db, f"Strategy Risk Lock\n[{trade.strategy_name}] consecutive losses: {stats.consecutive_losses}")
        self.telegram.send(db, f"Trade Closed\n[{trade.strategy_name}] {reason.value}\nP&L: {trade.pnl_percent:.2f}%")
        # Exit shadow review removed: it never had real market/indicator context
        # (TradingView only sends that on entry signals) and never affected
        # anything -- no alternative trade, no risk-state impact -- so it was
        # pure token spend for a note that just restated numbers already on
        # the trade row. Entry reviews (main.py) are unaffected.
        return trade

    def current_state(self, db: Session, strategy_name: str) -> str:
        # origin == "SIGNAL" only: AI_ALT_* paper trades are evaluation-only side
        # trades and must never influence the real signal's state machine.
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.strategy_name == strategy_name,
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.origin == "SIGNAL",
                )
            )
        )
        if not trades:
            return "FLAT"
        if any(trade.option_type == "CE" for trade in trades):
            return "LONG_CE"
        if any(trade.option_type == "PE" for trade in trades):
            return "LONG_PE"
        return "FLAT"

    def latest_open_trade_for_option(self, db: Session, strategy_name: str, option_type: str) -> StrategyTrade | None:
        # origin == "SIGNAL" only -- a TradingView exit signal must always act on
        # the real trade, never an AI_ALT_* evaluation side trade that happens to
        # share the same option type.
        return db.scalar(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == strategy_name,
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.option_type == option_type,
                StrategyTrade.origin == "SIGNAL",
            )
            .order_by(StrategyTrade.entry_time.desc())
            .limit(1)
        )

    def square_off_all(self, db: Session) -> list[StrategyTrade]:
        closed: list[StrategyTrade] = []
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    func.upper(StrategyTrade.strategy_name) != "V7",
                )
            )
        )
        for trade in trades:
            try:
                premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
                closed.append(self.close_trade(db, trade, premium, ExitReason.TIME_EXIT))
            except Exception as exc:
                # One bad/stale contract token must never stop every other open
                # trade from being squared off -- this is the last line of
                # defense before market close, so it needs to be as resilient
                # as monitor_open_trades already is per-trade.
                logger.exception("Square-off failed for trade %s", trade.trade_id)
                log_event(db, "ERROR", f"[{trade.strategy_name}] square-off failed", "ERROR", {"trade_id": trade.trade_id, "error": str(exc)})
        return closed

    def square_off_strategy(self, db: Session, strategy_name: str) -> list[StrategyTrade]:
        closed: list[StrategyTrade] = []
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.strategy_name == strategy_name,
                )
            )
        )
        for trade in trades:
            try:
                premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
                closed.append(self.close_trade(db, trade, premium, ExitReason.TIME_EXIT))
            except Exception as exc:
                # One bad/stale contract token must never stop every other open
                # trade from being squared off -- this is the last line of
                # defense before market close, so it needs to be as resilient
                # as monitor_open_trades already is per-trade.
                logger.exception("Square-off failed for trade %s", trade.trade_id)
                log_event(db, "ERROR", f"[{trade.strategy_name}] square-off failed", "ERROR", {"trade_id": trade.trade_id, "error": str(exc)})
        return closed

    def get_strategy(self, db: Session, name: str) -> StrategyConfig | None:
        return db.scalar(select(StrategyConfig).where(func.lower(StrategyConfig.name) == name.lower()))

    def active_trade_count(self, db: Session, strategy_name: str) -> int:
        # origin == "SIGNAL" only -- AI_ALT_* evaluation trades must not count
        # against the strategy's real max_active_trades limit.
        return int(
            db.scalar(
                select(func.count()).select_from(StrategyTrade).where(
                    StrategyTrade.strategy_name == strategy_name,
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.origin == "SIGNAL",
                )
            )
            or 0
        )

    def calculate_quantity(self, strategy: StrategyConfig, entry_price: float, lot_size: int) -> int:
        if lot_size <= 0 or strategy.lots_per_trade <= 0:
            return 0
        return strategy.lots_per_trade * lot_size

    def resolve_mode(self, strategy: StrategyConfig) -> str:
        if strategy.mode == TradingMode.LIVE and strategy.live_trade and self.settings.live_trading:
            return TradingMode.LIVE
        return TradingMode.PAPER

    def result_for_pnl(self, pnl_percent: float) -> str:
        if pnl_percent > 0:
            return TradeResult.WIN
        if pnl_percent < 0:
            return TradeResult.LOSS
        return TradeResult.BREAKEVEN
