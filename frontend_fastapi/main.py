"""
FastAPI + Jinja2 frontend for HERMES -- replaces frontend/ (Django). See
docs/frontend-rewrite-implementation-plan.md for the phased rewrite this
belongs to. Phase 0 built the scaffolding (sessions, CSRF, auth gates,
flash messages, static files, migrations); Phase 1 added accounts/; Phase 2
adds research_projects/ (the ethics-project workflow). jobs/ and the rest
follow phase by phase.

Run with:
    python -m uvicorn frontend_fastapi.main:app --reload
"""
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from frontend_fastapi import backend_client
from frontend_fastapi.deps import csrf_protect
from frontend_fastapi.exceptions import Forbidden, NotAuthenticated
from frontend_fastapi.migrations import run_migrations
from frontend_fastapi.routers import accounts, jobs, research_projects
from frontend_fastapi.session_middleware import SessionMiddleware
from frontend_fastapi.settings import ALLOWED_HOSTS, DATABASE_URL, LOGIN_URL, STATIC_DIR
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
    await backend_client.client.aclose()


# csrf_protect applied globally (every route, not opt-in per route) --
# matches Django's CsrfViewMiddleware default of protecting every request
# unless a view explicitly opts out, rather than the reverse. It no-ops for
# GET/HEAD/OPTIONS/TRACE, so this doesn't affect read-only routes.
app = FastAPI(lifespan=lifespan, dependencies=[Depends(csrf_protect)])
app.add_middleware(SessionMiddleware)
# Validates the request's Host header against ALLOWED_HOSTS -- settings.py
# already defined this setting, but nothing previously enforced it (Host
# header poisoning otherwise goes unguarded).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(jobs.router)
app.include_router(accounts.router)
app.include_router(research_projects.router)


def _best_effort_error_context(request: Request) -> dict:
    """
    Exception handlers run outside FastAPI's normal Depends() resolution,
    so this can't just call get_template_context() -- reads request.state
    instead, populated earlier in the SAME request by deps.get_session
    (csrf_token) and deps.get_current_user (user) before either exception
    this backs could have been raised (require_login/require_data_custodian/
    csrf_protect all resolve those first). Deliberately doesn't try to pop
    flash messages here (that would need its own DB round trip, the same
    class of test-swappability problem session_middleware.SessionMiddleware
    solves via a constructor-injected session_factory) -- an unshown flash
    on an error page is an acceptable, much smaller gap than the one this
    replaces (misrepresenting a logged-in user as anonymous).
    """
    return {
        "request": request,
        "user": getattr(request.state, "user", None),
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "flashes": [],
        "nav_active_projects": [],
    }


@app.exception_handler(NotAuthenticated)
async def _not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return RedirectResponse(f"{LOGIN_URL}?next={quote(next_path, safe='')}", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    return templates.TemplateResponse(request, "403.html", _best_effort_error_context(request), status_code=403)


@app.get("/health")
async def health() -> dict:
    """Unauthenticated liveness check for a process supervisor / load balancer."""
    return {"status": "ok"}
