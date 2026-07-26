# AI Origination — path to profitability

Status as of 26 Jul 2026. Phase 0 built locally, not yet deployed.

## Where the system stands

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

**One percentage point above breakeven gross, below breakeven net.** The system is not
broken — it is uncompensated for costs.

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

## Phase 1 — SmartAPI candle layer (not started)

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
