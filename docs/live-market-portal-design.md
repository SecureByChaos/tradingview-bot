# Client Live Market Page — Design Notes

Status: Locked concept, not yet built.

## Purpose

The client portal's main/landing page while the market is open. Distinct
from the reporting page (docs/client-reporting-portal-design.md), which
summarizes historical performance -- this page exists purely so the client
can see, in real time, that the system is actually watching the market and
acting on it. Nothing here is retrospective.

## Locked layout (see mockups)

- Header: brand name, "last updated" timestamp, and a market status pill
  (Market open / Market closed).
- Index row: Nifty, Sensex, and Bank Nifty as **figures only** -- price,
  absolute change, percent change. No per-index chart. These are context,
  not the point of the page.
- Active trades: a **scalable card grid**, not a single fixed block. Each
  open trade gets its own card (index + direction in plain language, e.g.
  "Bank Nifty - Long call"; entry/current/live P&L% figures; a small chart
  of premium since entry). The grid goes from zero cards to many without a
  redesign:
  - Zero open trades: a quiet "monitoring markets, no active trade" state,
    not a blank gap.
  - One or a few: cards at comfortable size.
  - Confirmed comfortable range: **4-6 simultaneous cards**. Beyond that,
    revisit -- group by index, or switch to a compact list view instead of
    full cards. Not a problem to solve now, just a known ceiling.
- Shared activity feed below the trade cards: a single plain-language,
  timestamped log across all trades combined (not one feed per trade),
  since with multiple simultaneous trades a per-trade feed stops being
  readable. Translates real internal events ("[STATE] OPEN_CE accepted")
  into plain language ("Entered Bank Nifty long call").

## Data source

Real signal trades only -- `origin == "SIGNAL"`, same rule as the reporting
page. `AI_ALT_*` evaluation trades and their reasoning never appear here.

## Presentation choices (deliberate)

- Plain-language position naming throughout ("Long call" / "Long put"),
  never raw signal codes (BUY_CE/BUY_PE) or internal event strings.
- Chart budget is spent entirely on the trades themselves, not the indices
  -- the index row is glanceable figures so it doesn't compete visually
  with what the client actually cares about (how the trade is doing).

## Open items (not yet decided -- revisit before building)

- **Refresh mechanism**: needs to feel genuinely live (ticking numbers,
  updating chart) without a jarring full-page reload. The admin
  dashboard's meta-refresh-every-N-seconds pattern isn't a good fit here;
  this likely needs periodic fetch/AJAX or a push mechanism.
- **Market-closed behavior**: what this page shows/does outside market
  hours -- a resting state, or a handoff to the reporting page as the
  effective landing page when the market isn't live.
- **Access model / multi-tenant / branding**: same open questions as the
  reporting page (docs/client-reporting-portal-design.md) -- not resolved
  independently per page, should be decided once for the whole client
  portal.
