"""
Django settings for the HERMES production frontend.

This project is the sole caller of the HERMES FastAPI backend (see
CLAUDE.md's "Architecture" section) -- every backend call, including SSE
job-progress streams, goes through jobs/backend_client.py, authenticated by
this project's own session auth (request.user). The backend itself has no
auth of its own; HERMES_INTERNAL_KEY (below) is the shared secret that
makes "only this frontend calls the backend" an enforced invariant rather
than just a network-topology assumption.

Django's own DB (sqlite by default, see DATABASES) holds only this
project's own auth/session/admin machinery and the one HERMES-specific
local model that has to live here (research_projects.ProjectDocument, for
ethics-certificate file uploads) -- NOT job/event/project data, which is
backend-owned (HermesDB, via the API).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load the repo root .env first (same variables webui/ and proxy/ already
# use: BACKEND_URI/BACKEND_PORT), then an optional frontend-local .env for
# anything specific to this project (DJANGO_SECRET_KEY, HERMES_INTERNAL_KEY).
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR / ".env")


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-dev-only-set-DJANGO_SECRET_KEY-in-prod")

DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"

ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_cotton',
    'accounts',
    'research_projects',
    'jobs',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'hermes_frontend.urls'

TEMPLATES = [
    {
        # django_cotton's app config (see django_cotton.apps.LoaderAppConfig)
        # rewrites this at startup: swaps APP_DIRS for an explicit loaders
        # list (cotton loader + filesystem + app_directories, cached) and
        # adds its templatetags as a builtin. Nothing to configure here.
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'hermes_frontend.context_processors.active_projects',
            ],
        },
    },
]

WSGI_APPLICATION = 'hermes_frontend.wsgi.application'
ASGI_APPLICATION = 'hermes_frontend.asgi.application'


# Django's own auth/session/admin storage -- NOT HERMES job/project data
# (that's backend-owned, see jobs/backend_client.py). Sqlite is fine here,
# same as webui/'s equivalent use, since nothing here needs to scale beyond
# "however many Django accounts this deployment has".
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'jobs:dashboard'
LOGOUT_REDIRECT_URL = 'accounts:login'


# --- HERMES backend connection ---------------------------------------------
# Same env-var convention webui/ already uses (BACKEND_URI/BACKEND_PORT),
# so this can point straight at backend/ (internal-only deployment) or at
# proxy/ (this frontend is external/DMZ-facing) -- see jobs/backend_client.py
# and CLAUDE.md's "Architecture" section for why that distinction doesn't
# change anything about how this frontend calls the backend.
BACKEND_URI = os.getenv("BACKEND_URI", "localhost")
BACKEND_PORT = os.getenv("BACKEND_PORT", "8000")
BACKEND_URL = f"http://{BACKEND_URI}:{BACKEND_PORT}"

# Shared secret sent as X-Hermes-Internal-Key on every call to the backend's
# project-gated routers (import/export/projects) -- see
# backend/src/projects/enforcement.py. Must match the backend's own
# HERMES_INTERNAL_KEY. Unset in both places -> no-op (dev/internal-only).
HERMES_INTERNAL_KEY = os.getenv("HERMES_INTERNAL_KEY")

# Must match the backend's own HERMES_USE_QUEUE (docs/worker-queue-design.md).
# Only the import flow (jobs/views.py's collect_data) reads this today --
# export stays on the synchronous path until a later step. When True,
# collect_data posts directly to /import/batch_import_file (which, with the
# backend's own flag also set, enqueues onto the tasks table and returns
# {"job_id", "total"} immediately) instead of staging the upload via the
# pending_job session dance; job_watch/job_stream fall back to relaying the
# backend's observer stream (GET /results/job/{job_id}/stream) for any job_id
# that was never staged that way. Mismatched flags (one side set, the other
# not) will misbehave -- e.g. this True but the backend's False means
# collect_data expects a JSON receipt but gets an SSE stream instead -- so
# set both together, the same discipline HERMES_INTERNAL_KEY above requires.
HERMES_USE_QUEUE = os.getenv("HERMES_USE_QUEUE", "false").lower() == "true"


# --- Email ------------------------------------------------------------------
# No SMTP server exists for this deployment today. Left unconfigured, Django
# defaults to SMTP against localhost:25, which just fails/hangs -- the
# console backend is an explicit, safe default (invite emails print to the
# server log) rather than a silent trap. The in-app activation-link message
# on the Invite User page works regardless of whether this is ever pointed
# at a real relay.
EMAIL_BACKEND = os.getenv("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("DJANGO_EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("DJANGO_EMAIL_PORT", "25"))
EMAIL_HOST_USER = os.getenv("DJANGO_EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("DJANGO_EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("DJANGO_EMAIL_USE_TLS", "true").lower() == "true"
