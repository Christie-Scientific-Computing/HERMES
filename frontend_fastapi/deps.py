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

import httpx
from fastapi import Depends, Request
from sqlalchemy.orm import Session as DBSession

from frontend_fastapi import backend_client
from frontend_fastapi.database import get_db
from frontend_fastapi.exceptions import Forbidden, NotAuthenticated
from frontend_fastapi.flash import pop_flashes
from frontend_fastapi.models import Session, User

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")


def get_session(request: Request, db: DBSession = Depends(get_db)) -> Session:
    """
    Fetches this request's session row, by the id SessionMiddleware already
    resolved (loaded or newly created+committed) and stashed on
    request.state before any dependency runs -- guaranteed present for
    every route this dependency is used from (SessionMiddleware only skips
    /static and /health, neither of which use it).
    """
    session = db.get(Session, request.state.session_id)
    # Stashed on request.state so main.py's exception handlers (which run
    # outside normal Depends() resolution, for a NotAuthenticated/Forbidden
    # a later dependency in the same chain raises) can still render an
    # accurate CSRF-protected error page without a second DB round trip.
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
    return {
        "request": request,
        "user": user,
        "csrf_token": session.csrf_token,
        "flashes": pop_flashes(session),
        "nav_active_projects": nav_active_projects,
    }
