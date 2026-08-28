.PHONY: dev-up dev-up-gateway

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
