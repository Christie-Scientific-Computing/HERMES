"""
FastAPI + Jinja2 frontend for HERMES -- replaces frontend/ (Django). See
docs/frontend-rewrite-implementation-plan.md for the phased rewrite this
belongs to; this module currently wires up only Phase 0's scaffolding
(sessions, CSRF, auth gates, flash messages, static files, migrations).
Routers for accounts/research_projects/jobs/etc. are added phase by phase.

Run with:
    python -m uvicorn frontend_fastapi.main:app --reload
"""
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from frontend_fastapi.deps import Forbidden, NotAuthenticated
from frontend_fastapi.migrations import run_migrations
from frontend_fastapi.settings import DATABASE_URL, LOGIN_URL, STATIC_DIR
from frontend_fastapi.templating import templates

logging.basicConfig(
    filename=None,
    level="INFO",
    format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations(DATABASE_URL)
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(NotAuthenticated)
async def _not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    next_url = quote(request.url.path)
    return RedirectResponse(f"{LOGIN_URL}?next={next_url}", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    return templates.TemplateResponse(request, "403.html", status_code=403)


@app.get("/health")
async def health() -> dict:
    """Unauthenticated liveness check for a process supervisor / load balancer."""
    return {"status": "ok"}
