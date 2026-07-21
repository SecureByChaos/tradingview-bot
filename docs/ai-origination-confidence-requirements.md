# AI Origination — Requirements for Genuinely Higher Confidence

Status: Discussion / not implemented. Captured on 2026-07-21 for future reference —
nothing in this doc has been built yet. Do not implement without explicit request.

## Context

AI Origination (`app/ai/originator.py`) currently decides BUY_CE/BUY_PE/NONE from a
single, fairly sparse signal: recent index tick momentum (up/down move counts,
window high/low, change%) plus, as of 2026-07-21, the day's real exchange OHLC.
The open question: what would it actually take to trust higher confidence scores
from this feature, as opposed to just telling the model to report bigger numbers
(which is trivial and meaningless).

Distinction that matters: a higher *reported* confidence number is easy to get by
prompting for it. A higher *deserved* confidence — one that actually correlates
with win rate — requires richer corroborating signal, a way to check agreement,
and eventually empirical proof that confidence predicts outcomes in this system's
own data.

## Requirements, in rough order of how foundational they are

1. **Volume/liquidity data.** The index itself has no volume, so the AI currently
   can't distinguish a real move from noise. Needs a pull from the near-month
   futures contract or the specific option contract being considered.

2. **Denser, cleaner price history.** Current tick series is opportunistic
   (recorded whenever the scheduler or dashboard happens to poll), not a clean
   time series. Needs real 1-min or 5-min candle data (Angel One `getCandleData`)
   instead of the current gappy sampling.

3. **Multi-timeframe confirmation.** Run the momentum calculation at two or three
   lookback windows (e.g. 15-min, 45-min, 90-min) instead of one, so the AI can
   check whether short-term and medium-term direction actually agree.

4. **Cross-index divergence signal.** Compare Bank Nifty's and Nifty's momentum
   against each other in the same check cycle, rather than evaluating each index
   in total isolation.

5. **Ensemble/consensus requirement.** Change from "either provider can act
   independently" to "both primary and secondary must agree" before a trade
   opens. Cheapest lever on this list — no new data needed, just a different
   rule for using what both providers already return.

6. **Self-performance feedback.** Feed AI Origination's own closed-trade history
   (win rate by confidence band, by index, maybe by time of day) back into the
   prompt or the acting threshold, so the AI isn't operating with zero memory of
   its own track record.

7. **Regime/session awareness.** Tell the AI what part of the trading day it's in
   (opening volatility vs. midday chop vs. closing trend) and ideally a
   volatility-regime signal, since momentum strategies behave differently in each.

8. **Explicit confidence-calibration guidance in the prompt.** Define what
   evidence justifies what confidence band (e.g. "80%+ requires X, Y, and Z all
   agreeing") instead of leaving confidence entirely to the model's unguided
   judgment.

9. **Empirical validation of confidence itself.** Once there's enough closed
   AI Origination trade history, check whether higher-confidence calls actually
   win more often than lower-confidence ones. This is the item that tells you
   whether everything above is working — it's the check on the decision-making,
   not an input to it.

10. **(Further out) Options-chain OI / PCR data.** Full option-chain pull for a
    put-call-ratio sentiment signal. More SmartAPI work than items 1-9.

11. **(Further out) Expiry/max-pain awareness.** OI-by-strike near weekly expiry,
    so behavior can adapt when OI-pinning dynamics dominate over pure momentum.

## Known issue: primary/secondary ordering bias

Discovered 2026-07-21, not yet fixed. `run_origination_checks` gives the primary
provider (currently OpenAI) the first attempt on every enabled index, every
cycle. The secondary provider (currently Claude) only gets called for a given
index if the primary either returns NONE or its confidence falls below the
0.55 floor — `_has_open_origination` is rechecked right before the secondary
call, so once primary fills an index's single trade slot, secondary is skipped
entirely for that cycle. This is why OpenAI visibly dominated trade volume on
the AI Origination page (most rows "Openai", only a few "Claude") — it's a
structural artifact of call order, not evidence that OpenAI is actually the
better-performing provider. Any comparison of provider win rates from this
data is confounded by this ordering bias.

Possible fixes, not implemented:

- Alternate which provider goes first each cycle (or each day), so both get an
  equal number of first attempts over time.
- Split indices between providers (e.g. OpenAI evaluates Bank Nifty, Claude
  evaluates Nifty) instead of both racing for the same slot.
- Require both to evaluate independently and only act on agreement — this was
  already requirement #5 above (ensemble/consensus), and would also remove the
  ordering bias as a side effect, since neither provider "wins" a slot by going
  first.

## Read alongside

- Strategy alternatives discussed the same day (mean-reversion, opening-range
  breakout, PCR/sentiment, expiry pinning, session-aware behavior) — this doc
  covers the confidence question specifically, not which strategy to run.
- `app/ai/originator.py` — current implementation these requirements would extend.
