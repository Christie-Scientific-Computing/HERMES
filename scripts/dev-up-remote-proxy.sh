#!/usr/bin/env bash
# Same as ./scripts/dev-up.sh, but routes the frontend's backend calls
# through a proxy/ already running on a SEPARATE machine, instead of
# starting one locally -- for testing that remote proxy (e.g. before
# deploying the frontend itself externally) without the frontend and
# backend also having to live on the DMZ box.
#
# On the remote machine: proxy/.env's HERMES_URL must point back at THIS
# machine's IP on port $HERMES_DEV_BACKEND_PORT (default 8000), e.g.
# HERMES_URL=http://192.168.1.50:8000 -- then `fastapi run main.py --port
# 8001` (or whatever HERMES_DEV_PROXY_PORT below matches).
#
# Usage:
#   ./scripts/dev-up-remote-proxy.sh 192.168.1.60
#   # or:
#   HERMES_DEV_PROXY_HOST=192.168.1.60 ./scripts/dev-up-remote-proxy.sh
#
# This machine's backend is bound to 0.0.0.0 (not localhost-only) so the
# remote proxy can actually reach it -- see dev-up.sh's own BACKEND_HOST.
#
# See ./scripts/dev-up.sh for the full set of shared env vars
# (HERMES_DEV_WORKERS, HERMES_DEV_BACKEND_PORT, HERMES_DEV_FRONTEND_PORT,
# HERMES_DEV_PROXY_PORT -- must match the remote proxy's own --port).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PROXY_HOST="${1:-${HERMES_DEV_PROXY_HOST:-}}"
if [ -z "$PROXY_HOST" ]; then
  echo "Usage: $0 <remote-proxy-host-or-ip>  (or set HERMES_DEV_PROXY_HOST)" >&2
  exit 1
fi

export HERMES_DEV_PROXY_HOST="$PROXY_HOST"
exec "$ROOT_DIR/scripts/dev-up.sh"
