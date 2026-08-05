"""NSE session boundaries, including the Closing Auction Session.

WHY THIS EXISTS AS ITS OWN MODULE
---------------------------------
From 3 Aug 2026 the session has a third phase that did not exist before, and
the boundary times now appear in several places that must agree: the candle
audit, indicator computation, the option-chain collector's spot field, and any
future decision about the square-off time. Hardcoding 15:15 in each of them is
how they drift apart.

WHAT CAS CHANGED, AND WHAT IT DID NOT
-------------------------------------
For F&O-eligible STOCKS, continuous trading now ends at 15:15 and a Closing
Auction Session runs 15:15-15:35, replacing the old VWAP closing-price method.

Equity DERIVATIVES -- including Nifty and Bank Nifty index options and futures
-- are not auctioned and trade continuously through to 15:40.

The consequence for this system is entirely about the INDEX VALUE, not about
order execution. Nifty and Bank Nifty are computed from constituent stocks, and
every constituent is F&O-eligible, so during the auction there is no continuous
order matching underneath the index. NSE's 4 Aug clarification is specific
about what that looks like:

    "as there is no continuous order matching between 3:15 pm and 3:30 pm, the
     index value is constant as it is based on traded values"

CONSTANT, not volatile. That is the important detail and it inverts the naive
expectation. The risk is not a wild print to filter out; it is fifteen minutes
of a FLAT series that every indicator will happily consume as though it were
real quiet trading:

  * ATR collapses toward zero across the window
  * ADX decays -- no directional movement to measure
  * Supertrend bands narrow and cannot flip
  * any drift or return computed over a window containing it is diluted
  * the session's stored close becomes the frozen 15:15 value, not the CAS
    closing price the exchange actually publishes

None of that raises an error, and none of it looks wrong in a chart. This is
the same class of problem as the forward-window truncation artefact: a real
mechanism in the data-generating process that produces plausible numbers.

The published spot/futures divergence on 3 Aug is the observable signature --
index futures keep trading continuously while the spot index is frozen, so the
two legitimately disagree for fifteen minutes.
"""

from __future__ import annotations

from datetime import datetime, time

# Continuous trading in the underlying constituents ends here.
CONTINUOUS_TRADING_END = time(15, 15)
# Index value is frozen between these two: the auction's order-collection and
# matching phases, with no continuous matching underneath the index.
AUCTION_WINDOW_START = time(15, 15)
AUCTION_WINDOW_END = time(15, 30)
# Auction concludes and closing prices are finalised.
AUCTION_SESSION_END = time(15, 35)
# Index options and futures keep trading normally until here. This is why the
# 15:15 square-off still executes against a real, continuously-quoted premium.
DERIVATIVES_TRADING_END = time(15, 40)

# Pre-CAS sessions are unaffected and must not be flagged retroactively -- the
# old mechanism ran continuous trading through to 15:30, so a flat tail before
# this date means something else and should be investigated, not excused.
CAS_EFFECTIVE_DATE = datetime(2026, 8, 3).date()


def is_auction_window(ts: datetime) -> bool:
    """Whether a naive-IST timestamp falls where the index value is frozen.

    Half-open on the right: the 15:30 bar is the first one after continuous
    matching resumes, so it is real data and must not be excluded.
    """
    if ts.date() < CAS_EFFECTIVE_DATE:
        return False
    return AUCTION_WINDOW_START <= ts.time() < AUCTION_WINDOW_END


def is_index_value_reliable(ts: datetime) -> bool:
    """Inverse of is_auction_window, named for the question callers actually ask.

    Applies to the INDEX value only. An option or futures premium at the same
    moment is continuously traded and perfectly reliable -- which is why exits
    and square-off, which price off premium, are unaffected by any of this.
    """
    return not is_auction_window(ts)
