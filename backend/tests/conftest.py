import os
import uuid
from urllib.parse import urlparse

import pytest

TEST_DATABASE_URL = os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:test@localhost:55432/hermes_test"
)

# This suite runs destructive DDL (DROP SCHEMA ... CASCADE, DELETE FROM ...)
# against whatever DATABASE_URL points at. setdefault() above only supplies
# the safe throwaway default when DATABASE_URL is UNSET -- if it's already
# set (e.g. exported from this repo's own .env, which points at a real
# shared dev instance), that value wins untouched, and pytest happily runs
# schema-dropping fixtures against it. That's exactly what happened on
# 2026-09-07, twice within one session: pinnacle_index and then
# pinnacle_export (a separate, externally-owned schema in the same
# database) were both destroyed this way, recoverable only because a
# same-day pg_dump backup happened to exist. This check makes that
# structurally impossible to repeat by accident, rather than relying on
# whoever runs pytest to remember to check first -- see the "never run
# tests against .env DATABASE_URL" memory for the incident this closes.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def pytest_configure(config):
    host = urlparse(TEST_DATABASE_URL).hostname
    if host not in _LOOPBACK_HOSTS and not os.environ.get("HERMES_TESTS_ALLOW_REMOTE_DB"):
        raise pytest.UsageError(
            f"DATABASE_URL resolves to a non-local host ({host!r}) -- refusing to run "
            "backend/tests/, which runs destructive DDL (DROP SCHEMA ... CASCADE, etc.) "
            "against it. Point DATABASE_URL at a throwaway container instead, e.g.:\n\n"
            "  docker run --rm -d -e POSTGRES_PASSWORD=test -e POSTGRES_DB=hermes_test "
            "-p 55432:5432 postgres:16-alpine\n\n"
            "or, if you are certain this host is safe to run destructive tests against, "
            "set HERMES_TESTS_ALLOW_REMOTE_DB=1 to override."
        )


@pytest.fixture(scope="session", autouse=True)
def _database_url():
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    yield TEST_DATABASE_URL


@pytest.fixture(autouse=True)
def _clean_tasks_table():
    """
    TasksDB.claim() (backend/src/status/tasks_db.py) is deliberately
    global -- a real worker claims the next queued task across every job,
    not just one. Against this suite's shared, persistent test Postgres
    (tests don't run inside a transaction that rolls back), a leftover
    'queued' row from an earlier test -- in this file or any other --
    would otherwise be claimable by an unrelated test, making
    claim-ordering assertions flaky or silently wrong (e.g. a test
    asserting on a freshly-enqueued task's `kind` instead getting some
    other test's leftover row). Autouse + session-wide so every test file
    that touches TasksDB gets this for free rather than each needing its
    own copy. events.task_id is ON DELETE SET NULL (see the tasks
    migration), so this never fails on FK references from events written
    by other tests.
    """
    from backend.src.db import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM tasks")
    yield


@pytest.fixture
def active_project():
    """
    An approved, non-expired project with a single member -- the
    (project_id, username) pair the ethics-gate enforcement dependencies
    (backend/src/projects/enforcement.py) require before any import/export
    endpoint will do real work. Tests that exercise those endpoints need
    this instead of a bare made-up project_id/username, which would 403.
    """
    from backend.src.projects.db_client import ProjectsDB

    db = ProjectsDB()
    project_id = str(uuid.uuid4())
    username = f"tester-{uuid.uuid4()}"
    db.create_project(project_id, "Test project", username)
    db.submit_project(project_id, username)
    db.review_project(project_id, approved=True, reviewer="admin", expiry_date=None)
    return project_id, username
