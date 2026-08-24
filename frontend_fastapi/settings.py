"""
Settings for the HERMES production frontend (FastAPI + Jinja2 rewrite).

Replaces frontend/hermes_frontend/settings.py (Django). This project is the
sole caller of the HERMES FastAPI backend (see CLAUDE.md's "Architecture"
section) -- every backend call goes through backend_client.py, authenticated
by this project's own session (see deps.py's get_current_user), never by a
value the browser supplied.

This project's own local database (see database.py, models.py) holds only
auth/session/document-upload bookkeeping -- NOT job/event/project data,
which is backend-owned (HermesDB, via the API). Never conflate the two.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Load the repo root .env first (the same variables webui/, proxy/, and the
# Django frontend already use: BACKEND_URI/BACKEND_PORT), then an optional
# frontend_fastapi-local .env for anything specific to this project. Mirrors
# frontend/hermes_frontend/settings.py's own two-stage load order exactly,
# so existing .env files need minimal changes to serve this project too.
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR / ".env")


SECRET_KEY = os.getenv(
    "HERMES_FRONTEND_SECRET_KEY",
    "insecure-dev-only-set-HERMES_FRONTEND_SECRET_KEY-in-prod",
)

DEBUG = os.getenv("HERMES_FRONTEND_DEBUG", "true").lower() == "true"

ALLOWED_HOSTS = [
    h.strip() for h in os.getenv("HERMES_FRONTEND_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()
]

# This project's own local database (users/sessions/project_documents) --
# entirely separate from HermesDB (DATABASE_URL, backend-owned) and the
# anon-mapping DB (ANON_DB_*, externally-owned). Never point this at either.
DATABASE_URL = os.getenv("HERMES_FRONTEND_DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}")

STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
MEDIA_ROOT = Path(os.getenv("HERMES_FRONTEND_MEDIA_ROOT", str(BASE_DIR / "media")))

# --- Sessions ----------------------------------------------------------------
SESSION_COOKIE_NAME = "hermes_session"
# The session ROW's own server-side lifetime -- always this long regardless
# of the per-login "remember me" choice. What "remember me" actually
# controls is the browser COOKIE's Max-Age (see auth.py's login_user):
# present (this many days) when remembered, omitted entirely (a true
# browser-session cookie) when not. Mirrors Django's SessionMiddleware
# default (2 weeks) + HermesLoginView's set_expiry(0) split.
SESSION_LIFETIME_DAYS = 14

# --- HERMES backend connection ------------------------------------------------
# Same env-var convention webui/ and frontend/ already use, so this can point
# straight at backend/ (internal-only deployment) or at proxy/ (this
# frontend is external/DMZ-facing).
BACKEND_URI = os.getenv("BACKEND_URI", "localhost")
BACKEND_PORT = os.getenv("BACKEND_PORT", "8000")
BACKEND_URL = f"http://{BACKEND_URI}:{BACKEND_PORT}"

# Shared secret sent as X-Hermes-Internal-Key on every backend call -- see
# backend/src/projects/enforcement.py. Must match the backend's own
# HERMES_INTERNAL_KEY. Unset in both places -> no-op (dev/internal-only).
HERMES_INTERNAL_KEY = os.getenv("HERMES_INTERNAL_KEY")

LOGIN_URL = "/accounts/login"
LOGIN_REDIRECT_URL = "/"

# --- Email --------------------------------------------------------------------
# No SMTP server exists for most deployments -- unset HERMES_FRONTEND_SMTP_HOST
# (the default) means email_backend.send_mail logs the message instead of
# attempting delivery, mirroring Django's console EmailBackend default
# (frontend/hermes_frontend/settings.py) rather than hanging/failing against
# a nonexistent localhost:25. The in-app activation-link flash message
# (routers/accounts.py's invite_user) works regardless of whether this is
# ever pointed at a real relay -- see that view's own comment.
SMTP_HOST = os.getenv("HERMES_FRONTEND_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("HERMES_FRONTEND_SMTP_PORT", "25"))
SMTP_USE_TLS = os.getenv("HERMES_FRONTEND_SMTP_USE_TLS", "true").lower() == "true"
SMTP_USERNAME = os.getenv("HERMES_FRONTEND_SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("HERMES_FRONTEND_SMTP_PASSWORD", "")
SMTP_FROM_ADDRESS = os.getenv("HERMES_FRONTEND_SMTP_FROM_ADDRESS", "hermes@localhost")
