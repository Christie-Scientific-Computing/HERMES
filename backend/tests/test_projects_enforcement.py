import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.src.projects import enforcement
from backend.src.projects.db_client import ProjectsDB


@pytest.fixture
def db():
    return ProjectsDB()


def test_require_project_member_allows_active_member(db, active_project):
    project_id, username = active_project
    enforcement.require_project_member(project_id, username)  # must not raise


def test_require_project_member_denies_non_member(db, active_project):
    project_id, _ = active_project
    with pytest.raises(HTTPException) as exc:
        enforcement.require_project_member(project_id, f"stranger-{uuid.uuid4()}")
    assert exc.value.status_code == 403


def test_require_project_member_denies_draft_project(db):
    owner = f"owner-{uuid.uuid4()}"
    project_id = str(uuid.uuid4())
    db.create_project(project_id, "Still a draft", owner)
    with pytest.raises(HTTPException) as exc:
        enforcement.require_project_member(project_id, owner)
    assert exc.value.status_code == 403


def test_require_project_member_denies_expired_project(db):
    owner = f"owner-{uuid.uuid4()}"
    project_id = str(uuid.uuid4())
    db.create_project(project_id, "Expired one", owner)
    db.submit_project(project_id, owner)
    db.review_project(
        project_id, approved=True, reviewer="admin",
        expiry_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    with pytest.raises(HTTPException) as exc:
        enforcement.require_project_member(project_id, owner)
    assert exc.value.status_code == 403


def test_require_project_member_denies_revoked_project(db):
    owner = f"owner-{uuid.uuid4()}"
    project_id = str(uuid.uuid4())
    db.create_project(project_id, "Revoked one", owner)
    db.submit_project(project_id, owner)
    db.review_project(project_id, approved=True, reviewer="admin", expiry_date=None)
    db.revoke_project(project_id, revoked_by="admin")
    with pytest.raises(HTTPException) as exc:
        enforcement.require_project_member(project_id, owner)
    assert exc.value.status_code == 403


def test_require_any_active_project_allows_member_of_some_active_project(active_project):
    _, username = active_project
    enforcement.require_any_active_project(username)  # must not raise


def test_require_any_active_project_denies_user_with_no_projects():
    with pytest.raises(HTTPException) as exc:
        enforcement.require_any_active_project(f"nobody-{uuid.uuid4()}")
    assert exc.value.status_code == 403


def test_require_project_member_fails_closed_on_db_error(active_project, monkeypatch):
    """
    A DB error checking membership must deny (503), never silently allow --
    unlike StatusDB's best-effort bookkeeping elsewhere, this is an
    authorization gate and must not adopt a log-and-continue tone.
    """
    project_id, username = active_project

    def boom(self, *args, **kwargs):
        raise ConnectionError("db is down")

    monkeypatch.setattr(ProjectsDB, "is_active_member", boom)
    with pytest.raises(HTTPException) as exc:
        enforcement.require_project_member(project_id, username)
    assert exc.value.status_code == 503


def test_require_any_active_project_fails_closed_on_db_error(active_project, monkeypatch):
    _, username = active_project

    def boom(self, *args, **kwargs):
        raise ConnectionError("db is down")

    monkeypatch.setattr(ProjectsDB, "has_any_active_project", boom)
    with pytest.raises(HTTPException) as exc:
        enforcement.require_any_active_project(username)
    assert exc.value.status_code == 503


def test_verify_internal_key_noop_when_unset(monkeypatch):
    monkeypatch.setattr(enforcement, "_INTERNAL_KEY", None)
    enforcement.verify_internal_key(x_hermes_internal_key=None)  # must not raise


def test_verify_internal_key_rejects_missing_or_wrong_key(monkeypatch):
    monkeypatch.setattr(enforcement, "_INTERNAL_KEY", "s3cr3t")
    with pytest.raises(HTTPException) as exc:
        enforcement.verify_internal_key(x_hermes_internal_key=None)
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        enforcement.verify_internal_key(x_hermes_internal_key="wrong")
    assert exc.value.status_code == 401


def test_verify_internal_key_accepts_correct_key(monkeypatch):
    monkeypatch.setattr(enforcement, "_INTERNAL_KEY", "s3cr3t")
    enforcement.verify_internal_key(x_hermes_internal_key="s3cr3t")  # must not raise
