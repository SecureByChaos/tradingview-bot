#!/usr/bin/env bash
# Run this ON THE LIVE BOX, during a hang, from anywhere (it doesn't need the
# repo's Python venv -- it's a standalone shell script, not one of the
# `python -m scripts.*` tools). Captures a single snapshot: request timing at
# three layers, OS memory/swap pressure, a stack dump of every thread in the
# running process, and a lock/throttle-contention count from today's log.
#
# Usage:
#   ADMIN_USERNAME=admin ADMIN_PASSWORD=... ./scripts/portal_probe.sh
#   (ADMIN_USERNAME/ADMIN_PASSWORD default to the same env vars the app
#   itself reads -- see app/config.py -- so if this runs on the same box as
#   the service, `sudo systemctl show -p Environment tradingview-bot` may
#   already have them, or just pass them inline as above.)
#
# Nothing here places an order, changes settings, or writes to the app's own
# database -- read-only against the running process and the OS.

set -uo pipefail

PORT="${PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
SERVICE="${SERVICE:-tradingview-bot}"
COOKIEJAR="$(mktemp)"
trap 'rm -f "$COOKIEJAR"' EXIT

echo "=== portal_probe.sh -- $(date -Is) ==="
echo

echo "--- 1. Request timing: /health (no auth, no DB) vs authenticated /api/status and / ---"
echo "curl /health:"
curl -sS -o /dev/null -w "  status=%{http_code} time_total=%{time_total}s\n" \
  --max-time 15 "${BASE}/health"

if [ -n "$ADMIN_PASSWORD" ]; then
  curl -sS -c "$COOKIEJAR" -o /dev/null -w "login: status=%{http_code} time_total=%{time_total}s\n" \
    --max-time 15 -X POST "${BASE}/login" \
    -d "username=${ADMIN_USERNAME}&password=${ADMIN_PASSWORD}"

  echo "curl authenticated /api/status:"
  curl -sS -b "$COOKIEJAR" -o /dev/null -w "  status=%{http_code} time_total=%{time_total}s\n" \
    --max-time 15 "${BASE}/api/status"

  echo "curl authenticated / (Jinja2 dashboard):"
  curl -sS -b "$COOKIEJAR" -o /dev/null -w "  status=%{http_code} time_total=%{time_total}s\n" \
    --max-time 15 "${BASE}/"

  echo "curl authenticated /api/live-dashboard (the 10s-polled endpoint):"
  curl -sS -b "$COOKIEJAR" -o /dev/null -w "  status=%{http_code} time_total=%{time_total}s\n" \
    --max-time 15 "${BASE}/api/live-dashboard"
else
  echo "  ADMIN_PASSWORD not set -- skipping login and the three authenticated checks."
  echo "  Re-run as: ADMIN_PASSWORD=... $0"
fi
echo

echo "--- 2. Memory / swap (free -h) ---"
free -h
echo

echo "--- 3. vmstat 1 5 (watch si/so columns -- nonzero means active swap thrashing) ---"
vmstat 1 5
echo

echo "--- 4. Thread-level stack dump (py-spy) ---"
MAIN_PID="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)"
if [ -z "$MAIN_PID" ] || [ "$MAIN_PID" = "0" ]; then
  echo "  Could not resolve MainPID for systemd unit '${SERVICE}' -- is it running under that name?"
  echo "  (override with SERVICE=<unit-name> $0)"
elif ! command -v py-spy >/dev/null 2>&1; then
  echo "  py-spy not installed. Install with:"
  echo "    sudo pip install py-spy      # or: sudo apt install python3-pip && sudo pip install py-spy"
  echo "  Then re-run this script -- this step needs it to see which thread is actually stuck."
else
  echo "  MainPID=${MAIN_PID}"
  sudo py-spy dump --pid "$MAIN_PID" || echo "  py-spy dump failed -- likely needs sudo/ptrace permission (CAP_SYS_PTRACE)."
fi
echo

echo "--- 5. journalctl since 09:15 today: lock contention + throttle waits ---"
LOCK_COUNT="$(journalctl -u "$SERVICE" --since "09:15" 2>/dev/null | grep -cE "database is locked")"
THROTTLE_SLEEP_COUNT="$(journalctl -u "$SERVICE" --since "09:15" 2>/dev/null | grep -cE "\[THROTTLE\].*sleeping")"
echo "  'database is locked' occurrences: ${LOCK_COUNT}"
echo "  '[THROTTLE] ... sleeping' occurrences: ${THROTTLE_SLEEP_COUNT}"
echo

echo "=== Interpretation guide ==="
cat <<'EOF'
  /health fast, authenticated endpoints slow
      -> app-level blocking inside those specific handlers/dependencies
         (a DB call waiting on a lock, a SmartAPI call on the request path),
         not a systemic resource problem. Read the py-spy dump for which
         handler's thread is parked in sqlite3/requests and how many
         threads are stuck there at once.

  Both /health AND authenticated endpoints slow
      -> either every worker thread is blocked (thread-pool exhaustion --
         py-spy will show many threads all waiting on the same lock/socket),
         or the OS itself is starved (check vmstat's si/so columns first:
         nonzero means active swap thrashing, which slows every syscall
         including the ones /health makes just to accept the connection).

  High 'database is locked' count
      -> real writer-vs-writer contention on the SQLite file despite the
         WAL+busy_timeout fix -- look at which jobs/threads are writing
         around the same moments (scheduler jobs, the live feed thread,
         and request-driven writes like record_index_tick_if_stale all
         share one file).

  High '[THROTTLE] ... sleeping' count with slow authenticated pages
      -> a request-path SmartAPI call is queued behind the shared
         process-wide quote throttle other scheduler jobs are also using --
         confirms request handlers are reaching SmartAPI directly, which
         they should never do (see CLAUDE.md's own standing rule on this).

  Everything fast in this probe but the portal was slow minutes ago
      -> the hang was transient (a burst of concurrent polls near the tick-
         write throttle boundary, or one scheduler job's slow cycle) --
         re-run this script live DURING the next hang, not after it clears.
EOF
