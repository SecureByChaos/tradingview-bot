# Client Login & Data Model — Design Notes

Status: Locked concept, not yet built. See caveat at the bottom before
treating this as final.

## Business model this is built around

Clients do not execute through this system. Each client trades on their own
broker/data provider connection; this system only generates and distributes
signals. This system never places an order in a client's account and never
holds client capital. That single fact is what shapes everything below --
it's why there's no per-client execution, no capital ledger, and why the
portal's numbers must be framed as the signal's own track record, not "your
account results" (see docs/client-reporting-portal-design.md and
docs/live-market-portal-design.md).

## Data model: no per-client scoping

Confirmed: every active client sees the identical feed. There is no concept
of a client subscribing to a specific strategy, and no per-client data
variation at all. Login is purely an access gate, not a personalization
mechanism.

- `ClientAccount`: username/email, bcrypt password hash, `active` flag,
  optional subscription-expiry date, created_at.
- No entitlement/subscription-to-strategy mapping table -- not needed, since
  clients don't see strategy identity at all, only plain-language signal
  output ("Nifty trade taken, live P&L X%").
- Every portal query (live market page, reporting page) filters
  `StrategyTrade.origin == "SIGNAL"` only, same rule as before, with no
  additional per-client filter.

## Login: fully separate from admin auth

- Separate table, separate session key (e.g. `client_authenticated` +
  `client_id`, distinct from the admin's `admin_authenticated`), separate
  login page (`/client-login` vs `/login`).
- Route guards for client pages must be their own function (mirroring
  `require_admin_page`/`require_admin_api` in `app/auth.py` but keyed to the
  client session), so a client session can never reach `/settings`,
  `/ai-settings`, `/control`, `/logs`, or any operational route -- not just
  "the client doesn't know the URL," but structurally blocked.
- Login checks: valid credentials AND `active == true`. An expired or
  deactivated account fails login even with a correct password.

## Admin-side management

A "Manage Clients" page is worth building given "dozens or more" clients is
the expected scale (manual DB edits per client won't hold up):
- Create a client account, set a temporary password.
- Toggle active/inactive.
- View/set subscription expiry.

## Open items (not yet decided)

- Whether expiry auto-deactivates the account or an admin flips it manually
  when payment lapses.
- Password reset flow: "email the admin to reset" is fine at this scale;
  self-service reset needs email-sending infrastructure that doesn't exist
  yet.
- Signal delivery timeliness: a portal alone may not be fast enough for
  options; whether a push channel (Telegram, SMS) per client is needed so a
  client doesn't miss a fast-moving signal by not having the page open.

## Caveat -- flagged explicitly, not resolved

The person building this said, in their own words, they're "not sure if
this is a correct workflow." The technical design above is internally
consistent, but two non-technical questions sit underneath it and are the
actual source of that uncertainty, not the schema:

1. **Regulatory**: distributing paid trade signals/recommendations to
   dozens of clients in India may fall under SEBI's Research Analyst or
   Investment Adviser registration requirements, depending on how the
   service is structured and compensated. This has not been verified with
   a qualified professional and should be, before this goes out to paying
   clients at scale.
2. **Honesty of framing**: because the system never sees whether/how a
   client actually acted on a signal, every number shown must be labeled as
   the signal's own track record, never phrased as the client's personal
   results. Getting this labeling wrong is a real risk if it ever reads as
   a promise about individual account performance.

This document should be revisited once those two points are actually
resolved, not treated as a green light on its own.
