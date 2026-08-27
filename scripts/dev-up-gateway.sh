#!/usr/bin/env bash
# Same as ./scripts/dev-up.sh, but also starts proxy/ and routes the
# frontend's backend calls through it instead of straight to the backend --
# for exercising the DMZ-facing proxy hop locally (e.g. confirming the
# anonymisation boundary, backend/src/identity/anon.py, behaves as expected
# when requests actually cross it). That itself still needs ANON_DB_*
# configured in the backend's own .env; routing through the proxy alone
# doesn't turn passthrough mode on.
#
# Usage:
#   ./scripts/dev-up-gateway.sh
#
# See ./scripts/dev-up.sh for the full set of assumptions/env vars this
# shares (DATABASE_URL, HERMES_DEV_WORKERS, HERMES_DEV_BACKEND_PORT,
# HERMES_DEV_FRONTEND_PORT) plus HERMES_DEV_PROXY_PORT (default 8001,
# matching proxy/main.py's own docstring example).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HERMES_DEV_USE_PROXY=1
exec "$ROOT_DIR/scripts/dev-up.sh"
