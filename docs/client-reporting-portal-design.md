# Client Reporting Portal — Design Notes

Status: Locked concept, not yet built.

## Purpose

A separate, client-facing view of trading performance, entirely apart from
the internal admin dashboard (StrikeVault). The admin dashboard is an ops
console (settings, AI review internals, logs, health checks, the AI
alternative-call evaluation view) and none of that belongs in front of a
client. This page shows only outcomes, presented cleanly.

## Locked layout (see mockup)

- Header: portfolio/brand name, plain-language date range filter (7d / 30d /
  All), and a small note under the title stating the current data status
  (see Disclosure below).
- Four headline metric cards, equal visual weight: net return %, win rate,
  total trades, max drawdown. Drawdown sits at the same weight as return
  deliberately -- a client evaluating a strategy should see the downside as
  readily as the upside, not have it buried.
- Cumulative return (equity curve): the centerpiece chart, area-filled line.
- Two side-by-side charts: daily P&L bar chart (green/red by sign) and a
  win/loss donut.
- Simplified recent trades list: date, position (plain language -- "Long
  call" / "Long put", not CE/PE), result badge (Win/Loss), return %. No
  strike, expiry, exchange, symboltoken, order ID, exit-reason code, or any
  other operational field.

## Data source

Real signal trades only -- `StrategyTrade` rows with `origin == "SIGNAL"`.
`AI_ALT_*` evaluation trades must never appear here; they're internal
experimentation noise that would confuse or undermine a client's confidence,
not something to show them regardless of how they perform.

## Presentation choices (deliberate)

- Percentage returns, not absolute currency amounts -- reads as a track
  record regardless of position size, and sidesteps disclosing capital
  amounts.
- "Simulated results, paper trading" stated plainly under the title while
  the bot remains in evaluation mode. This must be updated (or removed) the
  moment real capital is actually live for this client -- showing paper
  results without that label once real money is involved would be
  materially misleading.

## Open items (not yet decided -- revisit before building)

- **Access model**: separate lightweight client login, or a shareable
  read-only link, or something else. Whatever it is, it must be read-only --
  no settings, no bot control, no ability to trigger any action.
- **Single client vs multi-tenant**: is this one fixed view, or should the
  design assume multiple clients later, each scoped to their own subset of
  trades? Changes whether auth/scoping needs to exist from day one.
- **Refresh mechanism**: reuse the existing meta-refresh polling pattern from
  the admin dashboard, or something smoother (periodic fetch/AJAX) given
  "live" is explicitly part of the ask.
- **Branding**: an actual client-facing name/identity distinct from
  "StrikeVault" (which is the internal/admin brand).
- Daily P&L is currently planned as a bar chart; a calendar heatmap was
  considered but deferred as a nice-to-have, not required for v1.
