"""
SQLAlchemy engine/session for this project's OWN local database (users,
sessions, project_documents) -- see settings.DATABASE_URL and models.py's
module docstring for why this is never the same database as HermesDB or the
anon-mapping DB.
"""
from typing import Callable, Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from frontend_fastapi.exceptions import Forbidden, NotAuthenticated
from frontend_fastapi.settings import DATABASE_URL

# check_same_thread=False: FastAPI runs sync path-operation functions (and
# sync dependencies) in a worker thread, not always the thread that created
# the engine -- sqlite's default same-thread check would otherwise reject
# those connections. Not needed (and not passed) for Postgres.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _db_session(session_factory: Callable[[], Session]) -> Iterator[Session]:
    """
    Request-scoped DB session generator, parameterized by session_factory
    so tests can point it at an isolated engine via dependency_overrides
    (get_db, below, is what production wires to SessionLocal) without
    duplicating this commit/rollback logic.

    Commits once, automatically, if the request completes without raising --
    so an ordinary route (flash a message, log a user in, save an upload)
    doesn't need its own explicit db.commit() call, and a mid-request
    exception can't leave a half-applied write behind.

    NotAuthenticated/Forbidden get a plain commit too, not a rollback: they
    are expected control flow (deps.require_login/require_data_custodian/
    csrf_protect raising them, not a real error), and nothing on these
    paths ever mutates data -- but a rollback would still EXPIRE every
    object already loaded this request regardless (e.g. request.state.user,
    read by deps.get_current_user for main.py's exception handlers to
    render), and this generator's own `finally: db.close()` then detaches
    them, leaving a DetachedInstanceError the moment a template touches
    one. A plain commit leaves already-loaded attributes intact (this
    session is expire_on_commit=False) even after close().
    """
    db = session_factory()
    try:
        yield db
        db.commit()
    except (NotAuthenticated, Forbidden):
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    yield from _db_session(SessionLocal)
