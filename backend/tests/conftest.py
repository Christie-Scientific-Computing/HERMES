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
