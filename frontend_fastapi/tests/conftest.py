"""
Shared test fixtures for frontend_fastapi.

`app`/`client` build a throwaway FastAPI app that mounts frontend_fastapi's
real dependencies (deps.py, auth.py) behind a handful of test-only routes --
Phase 0 has no real routers yet (those land in Phase 1+), so this is how its
own primitives (sessions, CSRF, login/logout, flash, the auth gates) get
exercised through actual HTTP request/response cycles rather than only as
bare function calls.
"""
import httpx
import pytest
from fastapi import Depends, FastAPI, Form, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from frontend_fastapi import auth, backend_client, security
from frontend_fastapi.deps import (
    csrf_protect,
    get_current_user,
    get_session,
    get_template_context,
    require_data_custodian,
    require_login,
)
from frontend_fastapi.exceptions import Forbidden, NotAuthenticated
from frontend_fastapi.flash import flash

# Reuses the REAL exception handlers (not test-only stand-ins) so fixes to
# them are actually exercised by this suite -- importing frontend_fastapi.main
# has no side effects at import time (DB access only happens inside its
# lifespan, which this fixture never runs; see main.py).
from frontend_fastapi.main import _forbidden, _not_authenticated
from frontend_fastapi.models import Base, Session, User
from frontend_fastapi.routers import accounts, jobs, research_projects
from frontend_fastapi.session_middleware import SessionMiddleware


@pytest.fixture(autouse=True)
def _isolated_backend_client(monkeypatch):
    """
    backend_client.client is a module-level httpx.AsyncClient, deliberately
    long-lived in production (see that module's docstring: one pooled
    client, closed once in main.py's lifespan). Under pytest-asyncio's
    function-scoped event loop, reusing that same object across tests binds
    its internal connection pool to a loop that's already closed by the
    time the next test runs, raising "RuntimeError: Event loop is closed"
    the moment any route calls it (every authenticated page render does,
    via get_template_context's nav_active_projects lookup) -- a pure
    test-harness artifact, not a production bug (a real uvicorn process has
    exactly one event loop for its entire lifetime).

    Autouse so every test gets a fresh client bound to the current test's
    event loop, defaulting to an empty-but-successful /projects response so
    that lookup resolves without every test file needing to think about it.
    Tests that care about a specific backend response (e.g. this module's
    own research_projects tests) monkeypatch backend_client.client again
    themselves, same as test_backend_client.py's existing per-test pattern.
    """
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"projects": []})

    monkeypatch.setattr(
        backend_client, "client",
        httpx.AsyncClient(base_url="http://backend.invalid", transport=httpx.MockTransport(default_handler)),
    )


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def SessionFactory(db_engine):
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(SessionFactory) -> DBSession:
    """A DB session for test setup/assertions -- separate from the one the
    app's own get_db dependency hands out per-request (below), matching how
    a real client and a real server never share a DB connection either."""
    session = SessionFactory()
    yield session
    session.close()


@pytest.fixture()
def make_user(db):
    def _make_user(username="alice", password="correct horse battery staple", is_staff=False, is_active=True):
        user = User(
            username=username, email=f"{username}@example.com",
            password_hash=security.hash_password(password), is_staff=is_staff, is_active=is_active,
        )
        db.add(user)
        db.commit()
        return user
    return _make_user


@pytest.fixture()
def app(SessionFactory):
    from frontend_fastapi import database
    from frontend_fastapi.database import get_db as real_get_db

    def override_get_db():
        # Reuses the real commit/rollback/NotAuthenticated+Forbidden-handling
        # logic against the test engine instead of duplicating it -- see
        # database._db_session's docstring for why that logic exists at all.
        yield from database._db_session(SessionFactory)

    # csrf_protect applied globally, same as main.py -- so tests exercise
    # the actual production wiring (every route protected by default)
    # rather than a looser test-only approximation.
    test_app = FastAPI(dependencies=[Depends(csrf_protect)])
    test_app.dependency_overrides[real_get_db] = override_get_db
    # SessionMiddleware talks to the DB directly (see its own docstring for
    # why), bypassing dependency_overrides entirely -- it needs the test
    # engine handed to it explicitly instead.
    test_app.add_middleware(SessionMiddleware, session_factory=SessionFactory)
    # Mirrors main.py's own TrustedHostMiddleware wiring -- deliberately
    # does NOT include "testserver" (httpx TestClient's default Host), so
    # the `client` fixture below uses base_url="http://localhost" and a
    # dedicated test can prove a mismatched Host is actually rejected.
    test_app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    test_app.exception_handler(NotAuthenticated)(_not_authenticated)
    test_app.exception_handler(Forbidden)(_forbidden)
    # The real accounts router, exercised through the same isolated test
    # engine as everything else here -- Phase 1's tests hit /accounts/*
    # directly rather than the /test/* stand-ins below, which stay in
    # place for the lower-level session/CSRF/auth-gate primitive tests
    # that predate any real router existing.
    test_app.include_router(jobs.router)
    test_app.include_router(accounts.router)
    test_app.include_router(research_projects.router)

    @test_app.post("/test/login")
    async def _login(
        response: Response, username: str = Form(...), remember: bool = Form(False),
        session: Session = Depends(get_session), db: DBSession = Depends(real_get_db),
    ):
        user = db.query(User).filter_by(username=username).one()
        auth.login_user(db, response, session, user, remember)
        return {"ok": True}

    @test_app.post("/test/logout")
    async def _logout(response: Response, session: Session = Depends(get_session), db: DBSession = Depends(real_get_db)):
        auth.logout_user(db, response, session)
        return {"ok": True}

    @test_app.get("/test/whoami")
    async def _whoami(user: User | None = Depends(get_current_user)):
        return {"username": user.username if user else None}

    @test_app.get("/test/protected")
    async def _protected(user: User = Depends(require_login)):
        return {"username": user.username}

    @test_app.get("/test/staff-only")
    async def _staff_only(user: User = Depends(require_data_custodian)):
        return {"username": user.username}

    @test_app.post("/test/csrf-protected")
    async def _csrf_protected():
        # No explicit Depends(csrf_protect) here on purpose -- proves the
        # APP-LEVEL dependency (registered above) protects a route that
        # never opted in itself, not just ones that remember to.
        return {"ok": True}

    @test_app.get("/test/flash-and-render")
    async def _flash_and_render(session: Session = Depends(get_session)):
        flash(session, "success", "hello")
        return {"ok": True}

    @test_app.get("/test/context")
    async def _context(ctx: dict = Depends(get_template_context)):
        return {
            "flashes": ctx["flashes"],
            "has_user": ctx["user"] is not None,
            "csrf_token": ctx["csrf_token"],
            "nav_active_projects": ctx["nav_active_projects"],
        }

    return test_app


@pytest.fixture()
def client(app):
    return TestClient(app, base_url="http://localhost")


@pytest.fixture()
def csrf_token(client):
    """Fetches a real CSRF token for the client's current session --
    every mutating request needs one now that csrf_protect is global."""
    def _get() -> str:
        return client.get("/test/context").json()["csrf_token"]
    return _get


@pytest.fixture()
def login(client, csrf_token):
    """Logs `client` in as an already-created user, via the real
    /accounts/login route -- shared by every test that needs an
    authenticated session but isn't itself testing login."""
    def _login(username: str, password: str = "correct horse battery staple") -> None:
        client.post("/accounts/login", data={"username": username, "password": password, "csrf_token": csrf_token()})
    return _login
