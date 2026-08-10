"""
Shared test fixtures for frontend_fastapi.

`app`/`client` build a throwaway FastAPI app that mounts frontend_fastapi's
real dependencies (deps.py, auth.py) behind a handful of test-only routes --
Phase 0 has no real routers yet (those land in Phase 1+), so this is how its
own primitives (sessions, CSRF, login/logout, flash, the auth gates) get
exercised through actual HTTP request/response cycles rather than only as
bare function calls.
"""
import pytest
from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from frontend_fastapi import auth, security
from frontend_fastapi.deps import (
    Forbidden,
    NotAuthenticated,
    csrf_protect,
    get_current_user,
    get_session,
    get_template_context,
    require_data_custodian,
    require_login,
)
from frontend_fastapi.flash import flash
from frontend_fastapi.models import Base, Session, User


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
    test_app = FastAPI()

    def override_get_db():
        db = SessionFactory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    from frontend_fastapi.database import get_db as real_get_db
    test_app.dependency_overrides[real_get_db] = override_get_db

    @test_app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        return RedirectResponse(f"/accounts/login?next={request.url.path}", status_code=303)

    @test_app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return Response(status_code=403, content="forbidden")

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

    @test_app.post("/test/csrf-protected", dependencies=[Depends(csrf_protect)])
    async def _csrf_protected():
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
    return TestClient(app)
