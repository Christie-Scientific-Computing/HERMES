"""
Owns the session-cookie lifecycle: loading an existing session or creating a
brand-new anonymous one, and guaranteeing the cookie for a newly-created one
actually reaches the browser -- on every response, including a redirect or
403 built by an exception handler for a dependency that runs and fails
*after* this middleware (deps.require_login raising NotAuthenticated, e.g.).

This has to be middleware, not a FastAPI Depends() (which is what an
earlier version of this file was): a FastAPI-dependency-injected Response
object's header mutations are only merged into the final response along
the success path of dependency resolution. If a route's OWN dependency
chain has get_session run first and require_login raise afterward, the
redirect response main.py's exception handler builds is a completely
different object that never sees what get_session staged on the original
one -- a first-time anonymous visit to any login-gated page would silently
never receive its session cookie. Because exception handlers are wired
into Starlette's ExceptionMiddleware, which sits *inside* (closer to the
router than) any middleware added via app.add_middleware, this class's
`call_next()` always gets back whatever the client will actually receive --
success or exception-handled -- so setting the cookie here is reliable
regardless of what happens downstream.

Written as a raw ASGI middleware rather than Starlette's BaseHTTPMiddleware
deliberately: BaseHTTPMiddleware is documented to buffer/interfere with
streaming responses, and this app's roadmap (Phase 3a, the job-progress SSE
relay) will add a live-streamed response that must not be buffered.

Uses its OWN short-lived DB session (SessionLocal(), not the per-request one
FastAPI hands routes via Depends(get_db)) so a newly-created session row is
committed immediately and independently of whatever the rest of the request
does -- it must survive even if a later permission check fails and rolls
its own work back. deps.get_session then re-fetches that same
already-committed row through the request's own DB session, for the rest
of the request to read/mutate (e.g. popping flash messages) as part of its
normal transaction.
"""
from datetime import timedelta

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response

from frontend_fastapi import security
from frontend_fastapi.database import SessionLocal
from frontend_fastapi.models import Session, as_aware_utc, utcnow
from frontend_fastapi.settings import DEBUG, SESSION_COOKIE_NAME, SESSION_LIFETIME_DAYS

# Paths that never need a session: static assets, and a liveness probe that
# a load balancer / process supervisor may hit every few seconds forever --
# without this, either would silently accumulate one orphan anonymous
# session row per hit, indefinitely.
_EXEMPT_PREFIXES = ("/static", "/health")


class SessionMiddleware:
    def __init__(self, app, session_factory=SessionLocal):
        # session_factory is a constructor argument, not a bare module-level
        # reference to SessionLocal, specifically so tests can point this at
        # an isolated in-memory-sqlite sessionmaker -- middleware sits
        # outside FastAPI's routing/dependency-injection machinery entirely,
        # so app.dependency_overrides (what every other DB-touching
        # dependency in this codebase is swapped via for tests) has no
        # effect on it.
        self.app = app
        self.session_factory = session_factory

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        db = self.session_factory()
        try:
            session_id = request.cookies.get(SESSION_COOKIE_NAME)
            session = db.get(Session, session_id) if session_id else None
            if session is not None and as_aware_utc(session.expires_at) < utcnow():
                db.delete(session)
                db.commit()
                session = None

            is_new = session is None
            if session is None:
                session = Session(
                    id=security.new_session_id(),
                    csrf_token=security.new_csrf_token(),
                    flash_messages=[],
                    expires_at=utcnow() + timedelta(days=SESSION_LIFETIME_DAYS),
                )
                db.add(session)
                db.commit()

            session_id = session.id
        finally:
            db.close()

        # Shares `scope` (a plain dict) with every Request(scope) built
        # downstream, including inside route handlers -- this is the
        # standard way ASGI middleware hands data to the rest of the stack.
        request.state.session_id = session_id

        if not is_new:
            await self.app(scope, receive, send)
            return

        # Build the Set-Cookie value through Starlette's own Response.set_cookie
        # rather than hand-formatting cookie-attribute syntax, then transplant
        # it onto the real outgoing message -- see send_with_cookie below.
        cookie_carrier = Response()
        cookie_carrier.set_cookie(SESSION_COOKIE_NAME, session_id, httponly=True, samesite="lax", secure=not DEBUG)
        cookie_header_value = cookie_carrier.headers["set-cookie"]

        async def send_with_cookie(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("set-cookie", cookie_header_value)
            await send(message)

        await self.app(scope, receive, send_with_cookie)
