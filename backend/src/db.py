"""
Shared PostgreSQL connection pool for the backend.

Everything in `backend/` that needs its own persistent storage (job/event
tracking today, more to come later per CLAUDE.md) pulls a connection from
this pool rather than opening one itself. Built from a single DATABASE_URL
env var (a Postgres DSN).

This is NOT the anon-mapping database (see backend/src/identity/anon.py) —
that's a separate, externally-owned, read-only database on a different
Postgres server entirely.
"""
import os
from contextlib import contextmanager

from psycopg2.pool import SimpleConnectionPool

_pool: SimpleConnectionPool | None = None


def init_pool(database_url: str, minconn: int = 1, maxconn: int = 10) -> None:
    """Create the process-wide pool. Call once at startup."""
    global _pool
    if _pool is not None:
        return
    _pool = SimpleConnectionPool(minconn, maxconn, dsn=database_url)


def get_pool() -> SimpleConnectionPool:
    if _pool is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL not set; cannot initialize the connection pool")
        init_pool(database_url)
    return _pool


@contextmanager
def get_conn():
    """Borrow a pooled connection; commits on success, rolls back on error."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
