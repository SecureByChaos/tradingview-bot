"""Round-trip cost estimation for options trades (Angel One, NSE F&O).

Purpose: every P&L figure this app produces is gross. `close_trade` computes
(exit - entry) * quantity and nothing else, so the dashboard has always shown a
number that overstates what the account actually earns. At the margins AI
Origination currently operates on -- roughly +0.27% expectancy per trade -- cost
is not a rounding error, it is the difference between a profitable system and an
unprofitable one.

This module does NOT change gross P&L. `StrategyTrade.profit_loss` stays gross
and unchanged so every historical row remains comparable to analysis already
done against it. Cost is recorded alongside, in two new fields, and net is
derived from them.

Rates verified against Angel One's published charge list (July 2026). Anything
statutory here can change with a budget or a SEBI circular -- if these numbers
start disagreeing with contract notes, this module is the single place to fix.
"""

from __future__ import annotations

from dataclasses import dataclass

# Flat per executed order. A round trip is two orders (entry + exit).
BROKERAGE_PER_ORDER = 20.0

# Securities Transaction Tax: 0.1% of SELL-side premium turnover only.
STT_SELL_RATE = 0.001

# NSE equity-options exchange transaction charge, on both legs' turnover.
# Angel One publishes 0.0355299% for NSE options (note: this is slightly higher
# than the 0.03503% figure that circulated earlier -- the current published rate
# is used here).
EXCHANGE_TXN_RATE = 0.000355299

# SEBI turnover fee: Rs 10 per crore = 0.0001% of total turnover.
SEBI_TURNOVER_RATE = 0.000001

# Stamp duty: 0.003% of BUY-side turnover only.
STAMP_DUTY_BUY_RATE = 0.00003

# GST applies to brokerage + exchange transaction charge + SEBI fee.
# Not to STT and not to stamp duty.
GST_RATE = 0.18

# Slippage is deliberately NOT folded into the formula above. Everything above
# is a published, verifiable rate; slippage is an empirical property of how this
# specific bot's market orders actually fill, and there is no honest way to know
# it before real fills exist. Keeping it separate and explicit means the
# statutory estimate stays trustworthy and the uncertain part stays visible and
# tunable, rather than being buried inside a single blended number.
#
# 0.0 is the deliberate default: paper trading has no slippage at all, so
# assuming some would silently understate paper results against their own
# premise. Set this once live fills give something real to calibrate against.
DEFAULT_SLIPPAGE_PERCENT = 0.0


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_fee: float
    stamp_duty: float
    gst: float
    slippage: float
    total: float

    @property
    def as_dict(self) -> dict[str, float]:
        return {
            "brokerage": self.brokerage,
            "stt": self.stt,
            "exchange_txn": self.exchange_txn,
            "sebi_fee": self.sebi_fee,
            "stamp_duty": self.stamp_duty,
            "gst": self.gst,
            "slippage": self.slippage,
            "total": self.total,
        }


def estimate_round_trip_cost(
    entry_price: float,
    exit_price: float,
    quantity: int,
    slippage_percent: float = DEFAULT_SLIPPAGE_PERCENT,
) -> CostBreakdown:
    """Estimated total round-trip cost in rupees for one options position.

    quantity is the full position size (lots * lot_size), matching
    StrategyTrade.quantity. Both legs are assumed to be market orders.
    """
    if not entry_price or not quantity:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    buy_turnover = entry_price * quantity
    sell_turnover = (exit_price or 0.0) * quantity
    total_turnover = buy_turnover + sell_turnover

    brokerage = BROKERAGE_PER_ORDER * 2
    stt = sell_turnover * STT_SELL_RATE
    exchange_txn = total_turnover * EXCHANGE_TXN_RATE
    sebi_fee = total_turnover * SEBI_TURNOVER_RATE
    stamp_duty = buy_turnover * STAMP_DUTY_BUY_RATE
    gst = (brokerage + exchange_txn + sebi_fee) * GST_RATE
    slippage = total_turnover * (slippage_percent / 100.0)

    total = brokerage + stt + exchange_txn + sebi_fee + stamp_duty + gst + slippage
    return CostBreakdown(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_txn=round(exchange_txn, 2),
        sebi_fee=round(sebi_fee, 2),
        stamp_duty=round(stamp_duty, 2),
        gst=round(gst, 2),
        slippage=round(slippage, 2),
        total=round(total, 2),
    )
