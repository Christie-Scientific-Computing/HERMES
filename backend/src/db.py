"""
Shared PostgreSQL connection pool for the backend.

Everything in `backend/` that needs its own persistent storage (job/event
tracking today, more to come later per CLAUDE.md) pulls a connection from
this pool rather than opening one itself. Built from a single DATABASE_URL
env var (a Postgres DSN).

This is NOT the anon-mapping database (see backend/src/identity/anon.py) —
that's a separate, externally-owned, read-only database on a different
Postgres server entirely.

Uses ThreadedConnectionPool, not SimpleConnectionPool. SimpleConnectionPool's
own docstring says it "can't be shared across different threads" —
getconn()/putconn() do no locking. That was safe only as long as every call
into the pool happened synchronously on the single asyncio event-loop thread.
StatusDB.add_event's hash-chain row lock (docs/safety-plan.md §D1) is now
awaited via asyncio.to_thread (backend/src/common/sse.py's run_batch_job),
which puts genuinely concurrent OS threads through getconn/putconn — so the
pool itself has to be thread-safe too. ThreadedConnectionPool has the same
constructor signature as SimpleConnectionPool; it just adds an internal
threading.Lock() around getconn/putconn on top of the same base class.
"""
import os
from contextlib import contextmanager

from psycopg2.pool import ThreadedConnectionPool

_pool: ThreadedConnectionPool | None = None


def init_pool(database_url: str, minconn: int = 1, maxconn: int = 10) -> None:
    """Create the process-wide pool. Call once at startup."""
    global _pool
    if _pool is not None:
        return
    _pool = ThreadedConnectionPool(minconn, maxconn, dsn=database_url)


def get_pool() -> ThreadedConnectionPool:
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
