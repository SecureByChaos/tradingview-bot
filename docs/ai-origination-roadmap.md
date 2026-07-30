# AI Origination — path to profitability

Status as of 30 Jul 2026.

> ## SUPERSEDED — read this first
>
> **The premise of this document was wrong.** Everything below Phase 0 was built on the
> assumption that AI Origination had a weak-but-real directional signal that was simply
> uncompensated for costs, and that better market context would sharpen it.
>
> A two-year backtest across ~37,000 five-minute bars per index says otherwise:
> **trading with 45-minute spot drift has no detectable directional edge at all**, and is
> mildly *inverted* in the drift range where most trades actually occur.
>
> The Phase 2/3 plan below (enriched prompts, setup detection, entry gating) is therefore
> answering the wrong question. Adding context cannot rescue a signal that does not
> predict. The Phase 0 and Phase 1 work still stands on its own merits — the trailing
> stop, cost fields and candle store are all independently useful — but the roadmap's
> destination no longer follows from its starting point.
>
> See "Two-year backtest results" below. Keep the rest of this document for the reasoning
> and the caveats, not for the plan.
>
> **Update, same day:** indicator-based setups — which are a different input from drift —
> DO show a replicated positive edge, but only in a specific time window. See "Indicator
> setup results" below. That is the live thread; the drift premise remains dead.

## Indicator setup results (30 Jul 2026) — the current live thread

Method: `scripts/setup_significance.py`. Same discipline as the drift test — edge against
the unconditional base rate, non-overlapping subsample, day-block bootstrap,
direction-aware verdict. 16 setups declared before any result was inspected.

### What replicated

**Momentum and breakout setups work between 11:00 and 14:00, and fail after 14:00.**

Two conceptually distinct families agree, which matters more than either alone:

| Setup | Regime | BN 30m | BN 60m | NIFTY 30m | NIFTY 60m |
|---|---|---|---|---|---|
| `EMA_STACK` | 11:00–14:00 | +1.62 | +1.88 | +2.53 | **+3.95** |
| `ST_ALIGNED` | 11:00–14:00 | +1.71 | +2.20 | +3.21 | +4.09 |
| `ORB_BREAK[hold=2]` | 11:00–14:00 | +1.65 | — | +3.23 | +4.91 |
| `PDH_PDL_BREAK[hold=3]` | 11:00–14:00 | +1.74 | — | +2.32 | +3.95 |

All CIs exclude zero. **`NIFTY 60min EMA_STACK 1100_1400` clears Bonferroni** at
p = 0.000035 against a 0.000103 threshold over 484 comparisons — the only cell in any
analysis so far to survive the harshest correction rather than relying on replication.

And the mirror, which is the same claim from the other side: `ST_ALIGNED` is reliably
**backwards** 14:00–15:15 on both indices (BN −2.31 / −3.11, Nifty −2.40). A sign flip in
adjacent, disjoint windows of the same sessions.

### The economics are marginal

Best cell is +3.95pp. At symmetric ±12% payoffs that is ~0.95% gross against ~0.56%
costs — **+0.39% net**. Bank Nifty's +1.88pp is ~0.45% gross, i.e. **net negative**.

So: Nifty midday trend-following is marginally net positive; Bank Nifty is not. Any
viable version has to come from the win/loss ratio, not the hit rate.

### Caveat that must be resolved before trusting the late-session result

The 60-minute forward window is **clipped at session end**. A 14:30 signal measures ~45
minutes and a 15:00 signal ~15, so the "60min" label is wrong inside the 14:00–15:15
bucket and the effective horizon shrinks systematically across it.

Truncation should dilute an edge toward zero rather than make it negative, so it probably
does not explain a reliable −2.3 to −3.1pp. But it is a distortion correlated with the
exact variable under test. **Re-run 14:00–15:15 with a 15-minute horizon that fits inside
the window before treating late-session reversal as established.**

### Also outstanding: the drift test was never run conditionally

`band_significance.py` pooled all times of day. If momentum works midday and reverses
late, pooling would produce a net negative — which is what it found. That does not
overturn the drift result, but it was never tested conditionally, and it should be before
"momentum is dead" is treated as settled.

### Holdout candidates (single-use, at most two)

1. **`EMA_STACK` @ 11:00–14:00** — Bonferroni-clearing, full four-partition replication
2. **`ORB_BREAK[hold=2]` @ 11:00–14:00** — different information family, so a genuine
   second test rather than a restatement

`ST_ALIGNED` is a near-duplicate of `EMA_STACK`; taking both would spend the holdout twice
on one idea. Do the truncation check and the conditional drift re-run first — either could
change which candidate deserves the single shot.

## Two-year backtest results (30 Jul 2026)

Method: `scripts/band_significance.py`, 2024-07-29 to 2026-07-28, both indices,
30- and 60-minute horizons, four drift bands.

Critically, the first pass **overstated significance badly**. Sampling every 5 minutes
with a 60-minute forward window means consecutive samples share 11 of 12 forward bars.
They are not independent, but the binomial standard error assumes they are — inflating z
by roughly √12. Corrected via a non-overlapping subsample plus a day-block bootstrap.

**No band shows a positive edge with a confidence interval excluding zero.**
**Zero of 16 bands clear a Bonferroni-corrected threshold.**

The one encouraging result from the earlier 30-day run — Bank Nifty drift ≥0.50% at
+2.79pp — collapsed to a CI of [−0.92, +6.31] once dependence was handled. It was an
artefact of overlapping samples and a 51-observation subsample.

Six bands are **reliably negative**, and they are where production trades. The
0.00–0.25% drift range holds ~25,000 of ~37,000 bars:

| Index | Horizon | Band | Edge | 90% CI |
|---|---|---|---|---|
| BANKNIFTY | 30 min | 0.00–0.10 | −1.36pp | [−2.22, −0.53] |
| BANKNIFTY | 30 min | 0.10–0.25 | −2.00pp | [−3.27, −0.84] |
| BANKNIFTY | 60 min | 0.10–0.25 | −2.19pp | [−3.53, −0.82] |
| NIFTY | 30 min | 0.00–0.10 | −1.40pp | [−2.20, −0.62] |
| NIFTY | 30 min | 0.10–0.25 | −1.32pp | [−2.54, −0.11] |
| NIFTY | 60 min | 0.00–0.10 | −0.98pp | [−1.84, −0.16] |

### Why inverting the rule is not the obvious fix

Two reasons, and the second is arithmetic rather than caution.

These bands were **selected on the same two years** a fade rule would be validated
against. An inverted rule inherits the full selection bias and means nothing until it
clears the untouched holdout.

More decisively: at symmetric ±12% payoffs, a 2pp hit-rate edge is worth about **0.48%
per trade against ~0.56% costs — still net negative.** A directional edge of this size
cannot carry the system alone. Any viable version lives in the win/loss *ratio*, not the
hit rate.

## Put/call sensitivity asymmetry (30 Jul 2026)

Independent of the signal question, and probably the most operationally significant
finding. From `scripts/calibrate_premium.py`, fitted against 64 archived contracts:

| Index | ATM CE λ | ATM PE λ | ratio |
|---|---|---|---|
| NIFTY | +63.6 | −97.1 | **1.53** |
| BANKNIFTY | +56.2 | −72.1 | **1.28** |

(λ = premium % move per index % move. Validated against first principles — Bank Nifty
fitted 65.4 vs theoretical 67.7, Nifty 68.0 vs 74.8.)

**An identical percentage stop is a materially different index distance on a put than on
a call.** A 12% stop on a Nifty PE is ~0.11% of index movement; on a CE, ~0.18%. Every PE
trade has been running a tighter effective stop than every CE trade, and nobody chose
that. Given the heavy PE skew on several sessions, this is a plausible contributor to the
loss clustering — and it would persist under *any* entry rule, including a purely
deterministic one.

## Stop survivability, corrected (30 Jul 2026)

Method: `scripts/stop_survivability.py`, two years, fitted per-bucket coefficients,
60-minute forward window from every eligible bar.

An earlier figure — "a 10% stop is breached by noise in 62.3% (BN) / 55.8% (NIFTY) of
bars" — used a **flat multiplier of 105 for everything** and was wrong. Percentage of
bars breaching a 12% stop within 60 minutes:

| Bucket | old (flat 105) | corrected |
|---|---|---|
| BANKNIFTY CE 2–5 DTE | 56.0% | 36.5% |
| BANKNIFTY CE 6–10 DTE | 56.0% | 23.4% |
| BANKNIFTY PE 2–5 DTE | 54.0% | 45.5% |
| NIFTY CE 2–5 DTE | 49.5% | 31.4% |
| NIFTY PE 2–5 DTE | 45.8% | 47.1% |

The correction runs in **opposite directions for calls and puts**: fitted call
coefficients (47–68) are roughly half the old flat value so call rates fall sharply,
while fitted put coefficients (−85 to −108) straddle it and barely move. The pooled
figure was approximately right for puts and badly wrong for calls. Never pool them.

### "Structurally broken" was too strong — retract it

At 15–18% on a 6–10 DTE call, breach rates are 11–19%. Survivable configurations plainly
exist. The problem was never that the stop is impossible to survive; it is that no stop
helps without an edge.

MFE and MAE come back near-symmetric from random entry (BANKNIFTY CE −8.72% / +8.28%;
NIFTY PE −11.15% / +12.22%). That symmetry is the martingale signature, and it is why
sweeping exit rules against unconditional entry can only find configurations that lose
*less*, never one that wins.

### Days-to-expiry is a larger lever than the stop percentage

The most actionable risk-side finding, and it is independent of whether any entry signal
works. Same 12% stop, breach rate by DTE bucket:

| | 2–5 DTE | 6–10 DTE | reduction |
|---|---|---|---|
| BANKNIFTY CE | 36.5% | 23.4% | −36% |
| BANKNIFTY PE | 45.5% | 31.6% | −31% |
| NIFTY CE | 31.4% | 26.1% | −17% |
| NIFTY PE | 47.1% | 37.4% | −21% |

Mechanism: longer-dated contracts carry higher premium, so λ falls and the same
percentage stop becomes a wider index distance.

**AI Origination currently uses `find_atm_contract(signal, index, 0)` — nearest available
expiry, no offset, always.** Moving to a later expiry would materially reduce
noise-stopping at no cost, and would apply to the rule-based strategies too. Test it
properly before acting on it, but it is the cheapest structural improvement identified so
far.

### Stops in comparable units

A percentage stop is not a comparable quantity across option types. Nifty, 2–5 DTE,
nominal 12% stop:

| | index points | ATR multiples |
|---|---|---|
| CE | 43 | 2.02 |
| PE | 27 | 1.27 |

Same label, 37% tighter stop on the put. Bank Nifty shows the same effect (101 vs 79
points). Any future risk-parameter work should be specified in index points or ATR
multiples, not premium percent.

## Original framing (retained for context — see SUPERSEDED above)

43 valid trades, 21–24 Jul. 20 Jul excluded — those trades accepted sub-1% stops, from
before the 5–50% validation band existed, so their exits aren't comparable.

| Metric | Value |
|---|---|
| Win rate | 46.5% (20W / 23L) |
| Average win | +13.85% |
| Average loss | −11.54% |
| Reward:risk | 1.20 |
| Breakeven win rate required | 45.5% |
| Profit factor (gross) | 1.04 |
| Profit factor after ~1.8% round-trip costs | 0.78 |
| Expectancy per trade | +0.27% |

This was read at the time as "one percentage point above breakeven gross, below breakeven
net — not broken, just uncompensated for costs." The two-year result says that reading was
too generous: 43 trades over four days could not distinguish a weak edge from no edge, and
the larger sample says no edge.

Caveat that should travel with every number above: 43 trades over four days with a heavy
directional skew is a small, regime-specific sample.

## Phase 0 — shipped (not deployed)

### Trailing stop on the FIXED branch, AI Origination only

The diagnosis: every AI Origination trade runs in `FIXED` mode, and that branch has a
stop and a target and nothing in between. No trailing, no partial booking, no breakeven
shift. The `TRAILING` fallback exists but only engages when the model returns unusable
sl/target numbers, which it reliably doesn't.

Measured consequence — trades travelling most of the way to target and giving it back:

| Date | Arm | Peak (MFE) | Actual exit |
|---|---|---|---|
| 23 Jul | Claude Nifty PE | +21.60% | +1.08% |
| 23 Jul | OpenAI BN PE | +17.26% | **−12.09%** |
| 22 Jul | OpenAI Nifty PE | +19.22% | +4.86% |
| 23 Jul | OpenAI BN PE | +17.75% | +3.86% |
| 24 Jul | Claude BN CE | +13.65% | **−14.67%** |
| 24 Jul | Claude BN PE | +15.60% | +3.84% |
| 23 Jul | OpenAI Nifty CE | +14.93% | +4.27% |
| 24 Jul | OpenAI Nifty PE | +14.91% | +3.68% |

Roughly ₹23,000 of unrealised profit reached and surrendered, against ₹10,465 gross
realised.

**Implemented:** +8% activation, 5% entry-anchored trail (`high − entry×0.05`, matching
the existing TRAILING branch's form), original stop in force until activation, target in
force throughout. Scoped to `AI_ORIGIN_*` — BNV/NV strategies are untouched.

Trailing activation **exempts** the trade from `STALL_EXIT` for the rest of its life,
written as an exemption rather than a check-ordering so `exit_reason` stays unambiguous.

New `TRAIL_EXIT` reason, distinct from `STOPLOSS`, so a rescued winner is
distinguishable from a plain loss.

Reconstructed effect on the same 43 trades (user's analysis, not independently verified):

| Metric | Now | With trailing |
|---|---|---|
| Win rate | 46.5% | 55.8% |
| Profit factor after costs | 0.78 | 1.30 |
| Expectancy/trade | +0.27% | +3.61% |

### Cost modelling

`estimated_cost` and `net_pnl` added; `profit_loss` left gross and unchanged so
historical rows stay comparable. Formula in `app/trade_costs.py`, Angel One published
rates. Slippage is a separate constant, default 0.0.

Sensitivity — both conclusions survive the full plausible cost range:

| Cost/trade | PF now | PF with trailing |
|---|---|---|
| 0.6% (statutory only) | 0.95 | 1.56 |
| 1.2% | 0.86 | 1.42 |
| 1.8% | 0.78 | 1.30 |

### Dropped from Phase 0: constraining the target band to 12–20%

Rejected because out-of-band values don't clamp — they set `use_trailing = True`, routing
the trade into the TRAILING branch **where the target is never checked at all**. Claude
proposes ~22% almost every trade, so narrowing the band would have ejected one arm's
entire flow into a no-target mode. A structural change to a single arm, inside the window
meant to measure one variable.

Also largely subsumed: a trail at high−5% captures the near-miss peaks (21.60%, 19.22%,
17.75%, 17.26%) regardless of where the target sits. Revisit after a clean week.

## Phase 1 — SmartAPI candle layer (BUILT, 29 Jul 2026)

Shipped: `app/market_data.py` (candle store, session-anchored resampling),
`app/indicators.py`, `app/market_context.py` (levels, CPR, setups), wired into
`originator.py` computing and persisting `market_context_json` per trade **without**
feeding the prompt. Backfill via `scripts/backfill_candles.py`; equivalence between
resampled-1-minute and exchange-served 5-minute verified at 0 mismatches over 1,500 bars.

This phase was worth building regardless of the Phase 2/3 outcome — it is what made the
two-year backtest possible, which is what revealed the premise was wrong. That is the
argument for building measurement infrastructure before acting on hypotheses.

Do **not** change prompts in this phase. Phase 0 must be measurable alone.

Rationale for candles over TradingView payloads: candles are historical, so indicators can
be recomputed for any past moment, which makes **offline replay** possible. Every future
prompt change can then be tested against 20 Jul onward for the cost of a script instead of
a live trading week. Webhook payloads only exist at the instants alerts fired.

Decisions:

- Fetch **1-minute candles only**; resample in-app to 5/15/60. One call per index per
  minute, timeframes guaranteed mutually consistent.
- Warm up with 5 trading days of 1-minute candles at session start — enough to stabilise
  ADX(14)/ATR(14) on resampled 5-min bars, and delivers previous-day levels free.
- **Fail closed.** On candle-fetch failure: retry twice, then skip origination for that
  cycle. Never fall back to a thin prompt. At ~1.8% round-trip cost a marginal trade is
  negative expectancy — fewer trades on better data is what the cost math demands.
- **Retire the tick recorder — as a migration, not a deletion.** `IndexPriceTick` also
  feeds `get_index_live_figures` (today's change and day range on the live dashboard,
  computed from the session's first tick because the SmartAPI wrapper exposes no reliable
  previous close). It stays until candles can supply that.
- Defer index-futures/VWAP to Phase 3 — index tokens always report volume 0, so VWAP
  needs the current-month FUTIDX contract. Not on the critical path; the diagnosed failure
  mode is exhaustion-blindness, which ADX, range-percentile and ATR-extension address
  with spot-only data.

Storage: `candles(index, interval, ts_ist, o, h, l, c, v)`, PK `(index, interval, ts_ist)`.
Store 1-minute only, retain ≥30 days so replay stays possible.

Indicators to compute: EMA9/21/50, Supertrend(10,3), 15-min HTF EMA agreement, **ADX(14)**
(the direct antidote to "at the session high, therefore bullish"), ATR(14) absolute and
as % of price, percentile within today's range, distance from session high/low, distance
from EMA in ATR units, previous-day H/L/C, opening range 9:15–9:30, 5-day H/L, RSI(14),
multi-window drift.

## Phase 2 — prove the prompt change offline first

> **Not started, and per the SUPERSEDED note at the top, should not start as written.**
> This phase assumes the model is making poor use of a real signal. The two-year result
> says there is no signal in 45-minute drift to make better use of. An enriched prompt
> reasoning over a non-predictive input produces better-argued coin flips.
>
> The observation below — that the model reads *being at an extreme* as directional
> evidence — is still a genuine defect in its reasoning. It just isn't the reason the
> system loses money.

Replay over 20 Jul onward: same moments, production prompt vs enriched prompt, scored
against actual outcomes.

The defect being tested — stored `ai_reasoning` shows the model reading *being at an
extreme* as directional evidence:

- 24 Jul 12:31, both providers: *"price is at session highs with a steady uptrend"* →
  BUY_CE → −15.56% and −15.65% within three minutes
- 24 Jul 12:56: *"pressing the session high"* → BUY_CE → −13.70%
- 24 Jul 14:07: *"trading near the session high"* → BUY_CE → −11.26%

Candidate system-prompt addition:

> Being at the top or bottom of a range is not by itself directional evidence. It is
> equally consistent with continuation and with exhaustion. Weigh it against ADX, the
> extension figures, and whether a named breakout setup is actually active. A price at the
> session high that is far extended from its short-term mean with weak ADX is a weaker
> continuation case, not a stronger one.

Read the result in this order:

1. **Net sum P&L** — not accuracy, not win rate. A prompt that trades less often at a
   better hit rate can still lose after costs.
2. **The control subset** — how many target-hitting trades the enriched prompt still
   takes. A "safer" prompt that rescues losers by also skipping the +20% winners is a
   downgrade in disguise.
3. **The MFE<1% subset** last — the only trades context could plausibly fix.

Two limits to state plainly:

- The enriched prompt was designed by reading these same days. Any gain measured on them
  is optimistic. Hold out a day, or treat it as a hypothesis for the following week.
- **Replay is only fully scoreable in one direction** unless option premium paths exist.
  Where the enriched prompt says NONE and production traded, the outcome is known. Where
  it would *take* a trade production skipped, there's no outcome — the candle store holds
  index candles, not option premiums. This is why the 20–24 Jul option-candle pull matters
  (`scripts/pull_option_candles.py`), and why it was deadline-bound by the 28-Jul expiry.

## Phase 3 — only what Phase 2 earns

Named setup detection (ORB, previous-day-H/L breakout, EMA cross with HTF confirmation,
Supertrend flip, Bollinger squeeze, RSI extreme gated on weak ADX, and most relevant to
the diagnosed defect, **failed breakout / reversal at extreme**). Pass active setups and
strengths into the prompt including conflicting ones — conflict is information.

Entry gating on named-setup-active + ADX threshold. Decide from replay numbers, not
intuition: gating is the change most likely to cut winners along with losers.

Index futures for VWAP.

## Sequencing

Ship Phase 0 **alone** and measure a full week before starting Phase 1's prompt effects.
If trailing and market context land together and results improve, there is no way to know
which did it — and Phase 0 is the change backed by arithmetic rather than hypothesis.

## Measurement caveats carried forward

- **MFE has 30-second granularity and misses the first cycle entirely.** A trade that
  popped +3% for fifteen seconds is indistinguishable from one that went straight down.
  The correct phrasing for the MFE<1% group is "no *recorded* excursion above 1%" —
  treat it as indicative, not established.
- `Tick Samples`, `Day OHLC Present` and `Spot At Entry` are forward-only; they cannot be
  backfilled and are blank for everything before Phase 0 deploys.
- The 95-row 7-day view vs the 43-trade valid set needs confirming with
  `python -m scripts.reconcile_origination` rather than assumed.
- **Overlapping samples inflate significance.** Any forward-window analysis sampled more
  frequently than the window length produces dependent observations. The binomial standard
  error assumes independence and will overstate z by roughly √(window/stride). Use
  `scripts/band_significance.py`'s day-block bootstrap, not a raw z, for anything that
  informs a decision. This error was made once already and produced three "significant"
  bands that all evaporated.
- **Bank Nifty premium coefficients are fitted on 0–10 DTE only.** Bank Nifty now trades
  a ~27 DTE monthly, outside the fitted range, where gamma differs substantially.
  `select_multiplier` returns an `extrapolated` flag for exactly this — surface it rather
  than applying the coefficient silently.

## Method notes worth keeping

Things that turned out to matter more than expected, recorded so they aren't relearned.

- **Test against the base rate, not 50%.** Over a rising sample an always-long rule beats
  a coin flip on market drift alone.
- **Direction of a confidence interval matters.** A CI excluding zero on the negative side
  is evidence a rule is backwards, not evidence of exploitable edge. Both are findings;
  they are not the same finding.
- **Premium elasticity is not delta.** λ = premium % per index %, delta = premium ₹ per
  index point. They differ by spot/premium — roughly 200× for Nifty. Conflating them
  produces plausible-looking output that is meaningless; the tell was 100% end-of-day
  exits, because nothing ever reached a band.
- **Hit rate alone cannot carry this system.** At symmetric ±12% payoffs a 2pp edge is
  ~0.48% per trade against ~0.56% costs. Any viable configuration lives in the win/loss
  *ratio*.
- **Expired contracts vanish from the broker API but survive in archived filenames.**
  `pull_option_candles.py` writes `<TRADINGSYMBOL>_<TOKEN>.csv` so metadata stays
  recoverable after the scrip master drops the instrument. That is the normal case for
  any historical calibration, not an edge case.
