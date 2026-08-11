import os
import uuid

import pytest

TEST_DATABASE_URL = os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:test@localhost:55432/hermes_test"
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
