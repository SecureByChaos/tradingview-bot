# Owner's Personal Live Dashboard — Design Notes

Status: Locked concept, not yet built.

## Purpose

The operator's (Shivang's) own clean landing page -- the same live market
page built for clients (docs/live-market-portal-design.md), reused for
personal day-to-day monitoring, so checking "what's happening right now"
doesn't require wading through settings, AI internals, logs, and bot
control first. Those stay reachable from the sidebar; they just stop being
the first thing shown.

## Identical to the client live market page, with one difference

Everything from docs/live-market-portal-design.md carries over unchanged:
index figures row (Nifty/Sensex/Bank Nifty, figures only), the scalable
active-trade card grid (0 to a comfortable 4-6 cards, each with its own
mini premium chart), the shared plain-language activity feed, and the same
`origin == "SIGNAL"` data rule (no AI_ALT_* noise here either -- this is
still not the AI-alternative evaluation view, that stays on its own page).

The one addition: **strategy attribution is visible.** Each trade card
shows which strategy took it (e.g. "Strategy: V7"), and each activity feed
line is prefixed with the strategy name (e.g. "[V7] Entered Bank Nifty long
call"). The client-facing version stays strategy-blind; this owner version
is not, since knowing which strategy is doing what across Nifty and Bank
Nifty is exactly the point of it being the owner's own view.

## Placement

Becomes the new landing page ("/") for the admin session, replacing the
current cluttered dashboard.html in that role. The existing dashboard
content (health checks, recent logs, strategy metrics table) doesn't
disappear -- it stays reachable from the sidebar, just no longer the first
screen.

## Open items

- Whether the reporting page (docs/client-reporting-portal-design.md) gets
  the same owner-variant treatment (strategy attribution added) -- not yet
  asked for, worth confirming before assuming it's wanted too.
- Exact URL/route for the current dashboard.html once this becomes "/" --
  a straight rename, or a distinct "/ops" landing kept alongside it.

## Implementation notes (v1, as built)

- Built at "/" in app/dashboard_routes.py (`live_dashboard`), with the old
  cluttered dashboard moved to "/ops" (same `dashboard.html` template,
  unchanged, just relocated). Sidebar link "Dashboard" now points to "/";
  a new "Ops Summary" link points to "/ops".
- Two new tables power the "live" feel honestly rather than faking it:
  `StrategyTradeTick` (premium sample per open trade) populated on the
  existing 30s monitor tick in both `MultiStrategyTradeManager` and
  `V7Manager`, and `IndexPriceTick` (spot-price sample per index),
  throttled to at most one write per ~25s, recorded whenever the live
  dashboard is polled. Both are brand-new tables (no migration needed
  beyond `Base.metadata.create_all`).
- Index change/day-range figures are computed from our own recorded ticks
  (today's first tick as the reference point) rather than a broker
  "previous close" field, since the SmartAPI wrapper only exposes LTP.
- The activity feed is derived directly from `StrategyTrade` entry/exit
  timestamps (origin == SIGNAL only), not by parsing internal LogEvent
  message strings -- more robust, and naturally strategy-attributed.
- Refresh mechanism: the page renders server-side once, then polls
  `GET /api/live-dashboard` every 10s via `fetch`, re-rendering index
  cards, trade cards (destroying/recreating Chart.js instances), and the
  activity feed in place -- no full page reload.
- `app/platform.py`: `get_index_live_figures`, `get_open_trades_with_ticks`,
  `get_today_activity`. Template: `app/templates/live_dashboard.html`.
- Not verified by running the app -- the sandboxed shell was unavailable
  this session, so this was checked by careful manual trace only (and one
  real bug was caught and fixed this way: `get_today_activity` originally
  returned a raw `datetime` in each event, which Jinja's `tojson` filter
  can't serialize -- would have broken the page on first load). Recommend
  starting the app and loading "/" for real before trusting this.
