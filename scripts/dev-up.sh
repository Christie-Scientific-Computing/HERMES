#!/usr/bin/env bash
# Run the whole HERMES dev stack (backend + one worker + the Django
# frontend) with a single command, instead of three terminals.
#
# Usage:
#   ./scripts/dev-up.sh
#
# Assumes:
#   - You're in whatever Python environment has the project's dependencies
#     installed (activate your venv/conda env first, if you use one).
#   - Postgres (DATABASE_URL) is already reachable -- this script doesn't
#     start one. See README.md for a throwaway `docker run postgres:16-alpine`
#     one-liner, or docker-compose.dev.yml for the full containerised stack
#     (which already does all of this via Docker Compose instead).
#   - frontend/ is a checkout with its own migrations applied at least once
#     (this script runs `manage.py migrate` itself, so a fresh checkout is fine).
#
# Ctrl-C stops everything (backend, worker(s), frontend) together.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is not set (checked the environment and .env at the repo root). Aborting." >&2
  exit 1
fi

# How many worker processes to run -- one by default. Override with
# HERMES_DEV_WORKERS=3 ./scripts/dev-up.sh to test queue parallelism locally.
WORKER_COUNT="${HERMES_DEV_WORKERS:-1}"

# Backend defaults to :8000 (BACKEND_URI/BACKEND_PORT in .env should point
# here). The frontend runs on :8010, not :8000, to avoid colliding with it --
# the same port split docker-compose.dev.yml already uses.
BACKEND_PORT="${HERMES_DEV_BACKEND_PORT:-8000}"
FRONTEND_PORT="${HERMES_DEV_FRONTEND_PORT:-8010}"

# kill 0 sends the signal to this script's whole process group -- every
# background job started below, in one shot -- rather than tracking PIDs by
# hand. Runs on Ctrl-C, on `kill`, and on normal exit alike, so a crash in
# one process (e.g. the frontend failing to boot) still tears the rest down
# instead of leaving orphaned backend/worker processes behind.
cleanup() {
  # Clear the trap first: kill 0 below signals this script's own process
  # too (it's part of its own process group), which would otherwise
  # re-enter cleanup on that self-delivered signal before the process
  # actually exits.
  trap - EXIT INT TERM
  echo ""
  echo "Stopping HERMES dev stack..."
  kill 0 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting HERMES dev stack: backend (:$BACKEND_PORT), $WORKER_COUNT worker(s), frontend (:$FRONTEND_PORT)"
echo ""

# Each service's output is prefixed and interleaved in this one terminal,
# rather than needing a separate terminal per process.
(python -m uvicorn backend.main:app --reload --port "$BACKEND_PORT" 2>&1 | sed -u "s/^/[backend]   /") &

# backend/main.py runs its Alembic migration at import time, before uvicorn
# ever starts accepting connections -- so waiting for the port to respond is
# also waiting for migrations to finish. Without this, a worker started
# against a fresh/empty database can hit a bare "relation tasks does not
# exist" and exit (backend/worker.py's claim loop has no retry for that) a
# few seconds before the backend it depends on has actually finished
# creating the table.
echo -n "Waiting for backend to come up"
backend_ready=false
for _ in $(seq 1 60); do
  if curl -sf "http://localhost:$BACKEND_PORT/docs" -o /dev/null 2>/dev/null; then
    backend_ready=true
    break
  fi
  echo -n "."
  sleep 1
done
echo ""
if [ "$backend_ready" != true ]; then
  echo "Backend did not come up within 60s -- see the [backend] output above." >&2
  exit 1
fi

for i in $(seq 1 "$WORKER_COUNT"); do
  (python -m backend.worker 2>&1 | sed -u "s/^/[worker-$i]  /") &
done

# Django's own migrate is idempotent -- safe to run every time, and means a
# fresh checkout works without a separate manual step first.
(cd frontend && python manage.py migrate --noinput) 2>&1 | sed -u "s/^/[frontend]  /"
(cd frontend && python -m uvicorn hermes_frontend.asgi:application --reload --port "$FRONTEND_PORT" 2>&1 | sed -u "s/^/[frontend]  /") &

echo "All processes started. Frontend: http://localhost:$FRONTEND_PORT/  Backend: http://localhost:$BACKEND_PORT/  (Ctrl-C to stop everything)"
echo ""

wait
