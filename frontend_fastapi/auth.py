"""
Login/logout session mutation. Split out from deps.py because these aren't
FastAPI dependencies (they're called from inside a route body, once a
username/password or logout action has actually been validated), and because
login specifically needs the Response object to re-issue the session cookie
with the "remember me" choice baked into its Max-Age (see login_user's
docstring) -- unlike session_middleware.SessionMiddleware's own cookie
issue for a brand-new anonymous session, this one is safe to set directly
on the injected Response: it only ever runs once a route's entire
dependency chain has already succeeded, so there's no risk of a sibling
dependency raising afterward and discarding it (see SessionMiddleware's
docstring for why that risk is real for session bootstrap specifically).
"""
from datetime import timedelta

from fastapi import Response
from sqlalchemy.orm import Session as DBSession

from frontend_fastapi import security
from frontend_fastapi.models import Session, User, utcnow
from frontend_fastapi.settings import DEBUG, SESSION_COOKIE_NAME, SESSION_LIFETIME_DAYS


def login_user(db: DBSession, response: Response, old_session: Session, user: User, remember: bool) -> Session:
    """
    Rotates the session id on login (a fresh row, not a mutation of the
    pre-login one) -- prevents session fixation, since an anonymous
    session id an attacker already knows must never become a privileged
    one just because that browser later logs in. Carries over any flash
    messages already queued on the old (anonymous) session so a message
    set just before login isn't lost.

    Sets the cookie's Max-Age according to `remember`: present
    (SESSION_LIFETIME_DAYS) when the user asked to be remembered, omitted
    entirely when they didn't -- an omitted Max-Age is what makes a cookie
    a true browser-session cookie that the browser drops on its own. The
    session ROW's own lifetime (expires_at) is always the same either way;
    only the cookie's lifetime in the browser depends on `remember`.
    """
    new_session = Session(
        id=security.new_session_id(),
        user_id=user.id,
        csrf_token=security.new_csrf_token(),
        flash_messages=list(old_session.flash_messages),
        expires_at=utcnow() + timedelta(days=SESSION_LIFETIME_DAYS),
    )
    db.delete(old_session)
    db.add(new_session)

    response.set_cookie(
        SESSION_COOKIE_NAME, new_session.id,
        httponly=True, samesite="lax", secure=not DEBUG,
        max_age=SESSION_LIFETIME_DAYS * 86400 if remember else None,
    )
    return new_session


def logout_user(db: DBSession, response: Response, session: Session) -> None:
    db.delete(session)
    response.delete_cookie(SESSION_COOKIE_NAME)
