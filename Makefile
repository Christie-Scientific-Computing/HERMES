.PHONY: dev-up dev-up-gateway dev-up-remote-proxy

# Run the whole HERMES dev stack (backend + worker(s) + frontend_fastapi,
# the production frontend post-Phase-5-cutover -- see
# HERMES_DEV_USE_DJANGO_FRONTEND in scripts/dev-up.sh to run the legacy
# Django frontend instead). See scripts/dev-up.sh for prerequisites
# (DATABASE_URL, etc.) and env-var overrides.
dev-up:
	./scripts/dev-up.sh

# Same as dev-up, but also starts proxy/ and routes the frontend's backend
# calls through it -- for exercising the DMZ-facing proxy hop locally,
# e.g. to test the anonymisation boundary. See scripts/dev-up-gateway.sh.
dev-up-gateway:
	./scripts/dev-up-gateway.sh

# Same as dev-up, but routes the frontend through a proxy/ already running
# on a separate (e.g. DMZ test) machine instead of starting one locally.
# Usage: make dev-up-remote-proxy PROXY_HOST=192.168.1.60
# See scripts/dev-up-remote-proxy.sh -- that machine's proxy/.env HERMES_URL
# must point back at this machine's IP.
dev-up-remote-proxy:
	./scripts/dev-up-remote-proxy.sh $(PROXY_HOST)
