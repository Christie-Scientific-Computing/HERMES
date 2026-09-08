"""
Integration test for the review_project endpoint's Phase 4 addition:
notifying every current member of a project once a review decision lands
(backend/src/projects/endpoints.py). Doesn't need the PinnacleExport
submodule.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.projects import endpoints as projects_endpoints
from backend.src.notifications.db_client import NotificationsDB


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(projects_endpoints.router)
    return TestClient(app)


def _create_and_submit(client, owner):
    resp = client.post("/projects", json={"title": "Test project", "created_by": owner})
    project_id = resp.json()["project_id"]
    client.post(f"/projects/{project_id}/submit", json={"username": owner})
    return project_id


def test_review_approve_notifies_every_current_member(client):
    owner = f"owner-{uuid.uuid4()}"
    colleague = f"colleague-{uuid.uuid4()}"
    project_id = _create_and_submit(client, owner)
    client.post(f"/projects/{project_id}/members", json={"username": colleague, "added_by": owner})

    resp = client.post(f"/projects/{project_id}/review", json={
        "reviewer": "admin", "approved": True, "expiry_date": "2027-01-01T00:00:00Z",
    })
    assert resp.status_code == 200

    notifications_db = NotificationsDB()
    for member in (owner, colleague):
        notifications = notifications_db.list_for_user(member)
        assert len(notifications) == 1
        assert notifications[0]["kind"] == "project_reviewed"
        assert "approved" in notifications[0]["message"]
        assert notifications[0]["project_id"] == project_id


def test_review_reject_notifies_members_with_rejected_wording(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_and_submit(client, owner)

    resp = client.post(f"/projects/{project_id}/review", json={"reviewer": "admin", "approved": False})
    assert resp.status_code == 200

    notifications = NotificationsDB().list_for_user(owner)
    assert len(notifications) == 1
    assert "rejected" in notifications[0]["message"]


def test_review_does_not_notify_a_former_member_who_was_removed_before_review(client):
    owner = f"owner-{uuid.uuid4()}"
    former_member = f"former-{uuid.uuid4()}"
    project_id = _create_and_submit(client, owner)
    client.post(f"/projects/{project_id}/members", json={"username": former_member, "added_by": owner})
    client.delete(f"/projects/{project_id}/members/{former_member}", params={"removed_by": owner})

    resp = client.post(f"/projects/{project_id}/review", json={
        "reviewer": "admin", "approved": True, "expiry_date": "2027-01-01T00:00:00Z",
    })
    assert resp.status_code == 200

    assert NotificationsDB().list_for_user(former_member) == []
    assert len(NotificationsDB().list_for_user(owner)) == 1
