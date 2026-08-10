"""
FastAPI dependencies: session loading, current-user resolution, the
login/staff gates, CSRF protection, and the shared template context. Together
these replace what Django's SessionMiddleware + AuthenticationMiddleware +
CsrfViewMiddleware + context processors gave for free.

NotAuthenticated/Forbidden are raised here and turned into an actual
redirect-to-login / 403 page by exception handlers registered in main.py --
see that module for why (a dependency can't itself return "a different
response" the way a route handler can).
"""
import secrets
from datetime import timedelta

import httpx
from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session as DBSession

from frontend_fastapi import backend_client, security
from frontend_fastapi.database import get_db
from frontend_fastapi.flash import pop_flashes
from frontend_fastapi.models import Session, User, as_aware_utc, utcnow
from frontend_fastapi.settings import DEBUG, SESSION_COOKIE_NAME, SESSION_LIFETIME_DAYS


class NotAuthenticated(Exception):
    """No active session user -- caught in main.py, redirects to login."""


class Forbidden(Exception):
    """Logged in, but not staff -- caught in main.py, renders a 403 page."""


def get_session(request: Request, response: Response, db: DBSession = Depends(get_db)) -> Session:
    """
    Loads the session row named by the request's cookie, or creates a new
    (anonymous) one. Only sets the cookie here when a session is newly
    created -- an existing session's cookie is already correct in the
    browser (see auth.login_user for the one place that re-issues it, once
    the "remember me" choice is known). A brand-new session's cookie is
    intentionally a true browser-session cookie (no Max-Age) until login
    decides otherwise.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session = db.get(Session, session_id) if session_id else None
    if session is not None and as_aware_utc(session.expires_at) < utcnow():
        session = None

    if session is None:
        session = Session(
            id=security.new_session_id(),
            csrf_token=security.new_csrf_token(),
            flash_messages=[],
            expires_at=utcnow() + timedelta(days=SESSION_LIFETIME_DAYS),
        )
        db.add(session)
        # Flush (not commit) immediately: auth.login_user may run later in
        # this same request and needs to db.delete() this row to rotate the
        # session id -- SQLAlchemy can't delete an object that's merely
        # pending (added but never flushed), so a same-request "brand new
        # anonymous session immediately logs in" sequence would otherwise
        # raise InvalidRequestError. get_db's teardown still owns the
        # actual commit.
        db.flush()
        response.set_cookie(
            SESSION_COOKIE_NAME, session.id,
            httponly=True, samesite="lax", secure=not DEBUG,
        )

    request.state.session = session
    return session


def get_current_user(session: Session = Depends(get_session), db: DBSession = Depends(get_db)) -> User | None:
    if session.user_id is None:
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_login(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_data_custodian(user: User = Depends(require_login)) -> User:
    if not user.is_staff:
        raise Forbidden()
    return user


async def csrf_protect(request: Request, session: Session = Depends(get_session)) -> None:
    """
    Double-submit check against the session's own csrf_token (set once, at
    session creation, never rotated mid-session). request.form() is safe to
    call here even though the route handler will also read form fields --
    Starlette caches the parsed body on first read, so this doesn't consume
    it. GET/HEAD/OPTIONS never mutate state in this app, so they're exempt,
    matching Django's CsrfViewMiddleware.
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return
    form = await request.form()
    submitted = form.get("csrf_token")
    if not submitted or not secrets.compare_digest(str(submitted), session.csrf_token):
        raise Forbidden()


def get_template_context(
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
            nav_active_projects = backend_client.list_user_active_projects(user.username)
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
