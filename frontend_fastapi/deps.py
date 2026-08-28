"""
FastAPI dependencies: session/current-user resolution, the login/staff
gates, CSRF protection, and the shared template context. Together these
replace what Django's AuthenticationMiddleware + CsrfViewMiddleware +
context processors gave for free (session loading itself is
session_middleware.SessionMiddleware -- see that module's docstring for
why that one specifically has to be ASGI middleware, not a Depends()).

NotAuthenticated/Forbidden (exceptions.py) are raised here and turned into
an actual redirect-to-login / 403 page by exception handlers registered in
main.py -- see that module for why (a dependency can't itself return "a
different response" the way a route handler can).
"""
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import Depends, Request
from sqlalchemy.orm import Session as DBSession

from frontend_fastapi import backend_client
from frontend_fastapi.database import get_db
from frontend_fastapi.exceptions import Forbidden, NotAuthenticated
from frontend_fastapi.flash import pop_flashes
from frontend_fastapi.models import Session, User

EXPIRING_SOON_WITHIN_DAYS = 30


def expiring_soon(active_projects: list[dict], within_days: int = EXPIRING_SOON_WITHIN_DAYS) -> list[dict]:
    """Filters a list of projects down to ones that are currently approved,
    non-revoked, and whose expiry_date falls within the next `within_days`
    days. Explicitly re-checks `status == "approved"` itself rather than
    trusting the caller to have pre-filtered -- research_projects.py's
    list.html call site passes get_template_context's nav_active_projects
    (already approved+non-expired, via backend_client.list_user_active_projects),
    but detail.html's passes a single project of ANY status, which might
    have a future expiry_date left over from a since-revoked approval. An
    open-ended approval (expiry_date is None) never qualifies -- there's
    nothing to warn about. Each returned dict gains a `days_remaining` key.

    Lives here (not in routers/research_projects.py, where it originated)
    so get_template_context below can also use it for the notification
    dropdown's "live" expiring-soon section -- routers already import FROM
    deps, so the reverse would be circular."""
    now = datetime.now(timezone.utc)
    soon = []
    for project in active_projects:
        if project.get("status") != "approved":
            continue
        expiry = project.get("expiry_date")
        if not expiry:
            continue
        expiry_dt = datetime.fromisoformat(expiry) if isinstance(expiry, str) else expiry
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        days_remaining = (expiry_dt - now).days
        if 0 <= days_remaining <= within_days:
            soon.append({**project, "days_remaining": days_remaining})
    return soon

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")


def get_session(request: Request, db: DBSession = Depends(get_db)) -> Session:
    """
    Fetches this request's session row, by the id SessionMiddleware already
    resolved (loaded or newly created+committed) and stashed on
    request.state before any dependency runs -- guaranteed present for
    every route this dependency is used from (SessionMiddleware only skips
    /static and /health, neither of which use it).

    Cached on request.state rather than relying solely on FastAPI's own
    per-dependency cache: csrf_protect calls this as a plain function (not
    via Depends(), see its own docstring for why), which FastAPI's cache
    doesn't cover -- without this, a request that both goes through
    csrf_protect AND some other Depends(get_session) route (any mutating,
    authenticated route) would fetch the same row twice.
    """
    cached = getattr(request.state, "session", None)
    if cached is not None:
        return cached
    session = db.get(Session, request.state.session_id)
    request.state.session = session
    # Stashed separately so main.py's exception handlers (which run outside
    # normal Depends() resolution, for a NotAuthenticated/Forbidden a later
    # dependency in the same chain raises) can still render an accurate
    # CSRF-protected error page without a second DB round trip.
    request.state.csrf_token = session.csrf_token
    return session


def get_current_user(request: Request, session: Session = Depends(get_session), db: DBSession = Depends(get_db)) -> User | None:
    user = None
    if session.user_id is not None:
        candidate = db.get(User, session.user_id)
        if candidate is not None and candidate.is_active:
            user = candidate
    # Stashed on request.state so main.py's Forbidden exception handler
    # (which runs outside normal Depends() resolution) can still render an
    # accurate nav for an authenticated-but-not-staff user hitting a
    # require_data_custodian route, instead of showing them as logged out.
    request.state.user = user
    return user


def require_login(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_data_custodian(user: User = Depends(require_login)) -> User:
    if not user.is_staff:
        raise Forbidden()
    return user


async def csrf_protect(request: Request, db: DBSession = Depends(get_db)) -> None:
    """
    Applied globally (see main.py's `FastAPI(dependencies=[...])`) rather
    than opt-in per route -- matching Django's CsrfViewMiddleware default
    of protecting every request unless a view explicitly opts out, instead
    of the reverse. Deliberately does NOT declare `session: Session =
    Depends(get_session)` as a parameter: FastAPI resolves every declared
    dependency before this function's body runs regardless of the method
    check below, and get_session requires request.state.session_id, which
    SessionMiddleware never sets for its exempt paths (e.g. /health) --
    declaring that dependency here would break those paths even though
    this function would never actually need it for a GET. Calling
    get_session(request, db) directly, only when actually needed, avoids
    that.

    Double-submit check against the session's own csrf_token (set once, at
    session creation, never rotated mid-session). request.form() is safe to
    call here even though the route handler will also read form fields --
    Starlette caches the parsed body on first read, so this doesn't consume
    it.
    """
    if request.method in _SAFE_METHODS:
        return
    session = get_session(request, db)
    form = await request.form()
    submitted = form.get("csrf_token")
    if not submitted or not secrets.compare_digest(str(submitted), session.csrf_token):
        raise Forbidden()


async def get_template_context(
    request: Request,
    session: Session = Depends(get_session),
    user: User | None = Depends(get_current_user),
) -> dict:
    """
    The context every template render needs, assembled once per request --
    replaces Django's request/auth/messages/active_projects context
    processors. Routers call `templates.TemplateResponse(request, name,
    {**ctx, ...page-specific...})` with this as `ctx`.
    """
    nav_active_projects: list[dict] = []
    nav_notifications: list[dict] = []
    if user is not None:
        try:
            nav_active_projects = await backend_client.list_user_active_projects(user.username)
        except (backend_client.BackendError, httpx.HTTPError):
            # A down/unreachable backend must not take the whole page down --
            # this banner is a convenience, not load-bearing (the real
            # enforcement is server-side, per request, on the backend
            # itself). httpx.HTTPError also covers connection failures/
            # timeouts, which BackendError alone does not.
            nav_active_projects = []
        try:
            nav_notifications = await backend_client.list_notifications(user.username, unread_only=True, limit=10)
        except (backend_client.BackendError, httpx.HTTPError):
            # Same reasoning as nav_active_projects above -- the dropdown is
            # a convenience, not load-bearing.
            nav_notifications = []
    return {
        "request": request,
        "user": user,
        "csrf_token": session.csrf_token,
        "flashes": pop_flashes(session),
        "nav_active_projects": nav_active_projects,
        "nav_notifications": nav_notifications,
        # The notification dropdown's "live" section (Phase 4 §6.1 point 3)
        # -- computed from nav_active_projects, already fetched above, not a
        # second backend call. Distinct from nav_notifications (persisted
        # rows): this is live-computed on every render, same as
        # research_projects.py's own expiring-soon banner.
        "nav_expiring_soon": expiring_soon(nav_active_projects),
    }
