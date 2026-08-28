#!/usr/bin/env bash
# Run the whole HERMES dev stack (backend + one worker + the Django
# frontend) with a single command, instead of three terminals.
#
# Usage:
#   ./scripts/dev-up.sh
#
# Also supports routing the frontend's backend calls through proxy/ instead
# of straight to the backend -- set HERMES_DEV_USE_PROXY=1, or just run
# ./scripts/dev-up-gateway.sh (a thin wrapper around this same script that
# sets it). Useful for exercising the DMZ-facing proxy hop locally, e.g. to
# confirm the anonymisation boundary (backend/src/identity/anon.py) behaves
# as expected when requests actually cross it -- that itself still needs
# ANON_DB_* configured in the backend's own .env; routing through the proxy
# alone doesn't turn passthrough mode on.
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
# Ctrl-C stops everything (backend, worker(s), frontend, and the proxy when
# HERMES_DEV_USE_PROXY=1) together.
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

# When set, also starts proxy/ (default :8001, matching its own docstring's
# example) and points the frontend's backend calls at it instead of
# straight at the backend -- see ./scripts/dev-up-gateway.sh.
USE_PROXY="${HERMES_DEV_USE_PROXY:-0}"
PROXY_PORT="${HERMES_DEV_PROXY_PORT:-8001}"

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

if [ "$USE_PROXY" = "1" ]; then
  echo "Starting HERMES dev stack: backend (:$BACKEND_PORT), $WORKER_COUNT worker(s), proxy (:$PROXY_PORT), frontend (:$FRONTEND_PORT, routed via proxy)"
else
  echo "Starting HERMES dev stack: backend (:$BACKEND_PORT), $WORKER_COUNT worker(s), frontend (:$FRONTEND_PORT)"
fi
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

# Frontend's backend calls (BACKEND_URI/BACKEND_PORT, read at Django
# settings-load time -- see frontend/hermes_frontend/settings.py) point
# straight at the backend by default; USE_PROXY re-targets them at the
# proxy instead, once it's up. HERMES_URL/proxy's own .env, if present, are
# overridden here rather than relied on, so this works the same regardless
# of what's (or isn't) configured in proxy/.env.
#
# FRONTEND_BACKEND_PORT is explicitly $BACKEND_PORT (this script's own
# resolved value, from HERMES_DEV_BACKEND_PORT or its 8000 default -- see
# above) rather than leaving the frontend to inherit .env's own BACKEND_PORT
# unmodified the way it did before this variable existed. Those two can
# already differ today (this script's backend always binds to $BACKEND_PORT
# regardless of what .env's own BACKEND_PORT says), so this incidentally
# fixes a pre-existing case where HERMES_DEV_BACKEND_PORT moved the backend
# but the frontend kept pointing at .env's stale value -- not a behavior
# change for the common case (.env's BACKEND_PORT matching the 8000 default).
FRONTEND_BACKEND_URI="localhost"
FRONTEND_BACKEND_PORT="$BACKEND_PORT"

if [ "$USE_PROXY" = "1" ]; then
  (cd proxy && HERMES_URL="http://localhost:$BACKEND_PORT" python -m uvicorn main:app --reload --port "$PROXY_PORT" 2>&1 | sed -u "s/^/[proxy]     /") &

  # Deliberately NOT /docs here: proxy/main.py's own FastAPI() app
  # auto-registers /docs, /redoc and /openapi.json at construction time,
  # *before* the catch-all @app.api_route("/{path:path}") route is added --
  # Starlette matches in registration order, so those three specific paths
  # are served by the proxy's OWN docs page, never forwarded (confirmed:
  # they still return 200 even with the backend down). Every other path
  # does hit the catch-all and gets forwarded, so /results/job/{job_id}
  # (a real, side-effect-free, fast backend route -- an empty summary for
  # an id with no rows, no external Mosaiq/Pinnacle/ProKnow/Orthanc call)
  # is what actually proves "the proxy is up AND can reach the backend,"
  # not just "the proxy process is listening."
  echo -n "Waiting for proxy to come up"
  proxy_ready=false
  for _ in $(seq 1 30); do
    if curl -sf "http://localhost:$PROXY_PORT/results/job/__hermes_dev_up_gateway_readiness_check__" -o /dev/null 2>/dev/null; then
      proxy_ready=true
      break
    fi
    echo -n "."
    sleep 1
  done
  echo ""
  if [ "$proxy_ready" != true ]; then
    echo "Proxy did not come up within 30s -- see the [proxy] output above." >&2
    exit 1
  fi

  FRONTEND_BACKEND_URI="localhost"
  FRONTEND_BACKEND_PORT="$PROXY_PORT"
fi

# Django's own migrate is idempotent -- safe to run every time, and means a
# fresh checkout works without a separate manual step first.
(cd frontend && python manage.py migrate --noinput) 2>&1 | sed -u "s/^/[frontend]  /"
(cd frontend && BACKEND_URI="$FRONTEND_BACKEND_URI" BACKEND_PORT="$FRONTEND_BACKEND_PORT" \
  python -m uvicorn hermes_frontend.asgi:application --reload --port "$FRONTEND_PORT" 2>&1 | sed -u "s/^/[frontend]  /") &

echo "All processes started. Frontend: http://localhost:$FRONTEND_PORT/  Backend: http://localhost:$BACKEND_PORT/$( [ "$USE_PROXY" = "1" ] && echo "  Proxy: http://localhost:$PROXY_PORT/ (frontend routed through here)" )  (Ctrl-C to stop everything)"
echo ""

wait
