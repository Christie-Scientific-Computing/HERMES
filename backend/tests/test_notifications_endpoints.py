"""
Integration tests for the notification endpoints
(backend/src/notifications/endpoints.py, Phase 4). Doesn't need the
PinnacleExport submodule.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.notifications import endpoints as notifications_endpoints
from backend.src.notifications.db_client import NotificationsDB


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(notifications_endpoints.router)
    return TestClient(app)


@pytest.fixture
def username():
    return f"user-{uuid.uuid4()}"


def test_list_notifications_returns_only_that_users_own(client, username):
    db = NotificationsDB()
    db.create(username, kind="job_complete", message="mine")
    db.create(f"other-{uuid.uuid4()}", kind="job_complete", message="not mine")

    resp = client.get("/notifications", params={"username": username})

    assert resp.status_code == 200
    notifications = resp.json()["notifications"]
    assert [n["message"] for n in notifications] == ["mine"]


def test_list_notifications_unread_only(client, username):
    db = NotificationsDB()
    db.create(username, kind="job_complete", message="unread")
    db.create(username, kind="job_complete", message="will be read")
    to_mark = db.list_for_user(username)[0]
    db.mark_read(to_mark["id"], username)

    resp = client.get("/notifications", params={"username": username, "unread_only": True})

    assert [n["message"] for n in resp.json()["notifications"]] == ["unread"]


def test_mark_notification_read_succeeds_for_owner(client, username):
    db = NotificationsDB()
    db.create(username, kind="job_complete", message="hello")
    notification_id = db.list_for_user(username)[0]["id"]

    resp = client.post(f"/notifications/{notification_id}/read", params={"username": username})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert db.list_for_user(username)[0]["read_at"] is not None


def test_mark_notification_read_404s_for_a_different_user(client, username):
    """The access-control boundary at the HTTP layer: a user must not be
    able to mark someone else's notification read, and the 404 (rather
    than 403) doesn't even confirm the id belongs to anyone."""
    db = NotificationsDB()
    db.create(username, kind="job_complete", message="hello")
    notification_id = db.list_for_user(username)[0]["id"]

    resp = client.post(f"/notifications/{notification_id}/read", params={"username": f"mallory-{uuid.uuid4()}"})

    assert resp.status_code == 404
    assert db.list_for_user(username)[0]["read_at"] is None


def test_mark_notification_read_404s_for_unknown_id(client, username):
    resp = client.post("/notifications/999999999/read", params={"username": username})
    assert resp.status_code == 404
