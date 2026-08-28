"""
Tests for NotificationsDB (backend/src/notifications/db_client.py) -- Phase 4.
"""
import uuid

import pytest

from backend.src.notifications.db_client import NotificationsDB
from backend.src.status.db_client import StatusDB


@pytest.fixture
def db():
    return NotificationsDB()


@pytest.fixture
def username():
    return f"user-{uuid.uuid4()}"


@pytest.fixture
def job_id():
    """notifications.job_id is a real FK to jobs.job_id -- unlike username
    (no user table exists), a job row genuinely must exist first."""
    job_id = f"notif-test-{uuid.uuid4()}"
    StatusDB().create_job(job_id)
    return job_id


def test_create_and_list_for_user(db, username, job_id):
    db.create(username, kind="job_complete", message="Job x finished.", job_id=job_id)

    notifications = db.list_for_user(username)

    assert len(notifications) == 1
    assert notifications[0]["kind"] == "job_complete"
    assert notifications[0]["message"] == "Job x finished."
    assert notifications[0]["job_id"] == job_id
    assert notifications[0]["read_at"] is None


def test_list_for_user_only_returns_that_users_own_notifications(db, username):
    other = f"user-{uuid.uuid4()}"
    db.create(username, kind="job_complete", message="mine")
    db.create(other, kind="job_complete", message="not mine")

    notifications = db.list_for_user(username)

    assert [n["message"] for n in notifications] == ["mine"]


def test_list_for_user_orders_newest_first(db, username):
    db.create(username, kind="job_complete", message="first")
    db.create(username, kind="job_complete", message="second")

    notifications = db.list_for_user(username)

    assert [n["message"] for n in notifications] == ["second", "first"]


def test_list_for_user_unread_only_filters_out_read_notifications(db, username):
    db.create(username, kind="job_complete", message="unread")
    db.create(username, kind="job_complete", message="will be read")
    to_mark = db.list_for_user(username)[0]  # "will be read" (newest)
    db.mark_read(to_mark["id"], username)

    unread = db.list_for_user(username, unread_only=True)

    assert [n["message"] for n in unread] == ["unread"]


def test_list_for_user_respects_limit(db, username):
    for i in range(5):
        db.create(username, kind="job_complete", message=f"n{i}")

    assert len(db.list_for_user(username, limit=2)) == 2


def test_mark_read_succeeds_for_the_owning_user(db, username):
    db.create(username, kind="job_complete", message="hello")
    notification_id = db.list_for_user(username)[0]["id"]

    assert db.mark_read(notification_id, username) is True
    assert db.list_for_user(username)[0]["read_at"] is not None


def test_mark_read_fails_for_a_different_user(db, username):
    """A user must never be able to mark someone ELSE's notification read --
    this is the actual access-control boundary NotificationsDB enforces."""
    db.create(username, kind="job_complete", message="hello")
    notification_id = db.list_for_user(username)[0]["id"]

    mallory = f"user-{uuid.uuid4()}"
    assert db.mark_read(notification_id, mallory) is False
    assert db.list_for_user(username)[0]["read_at"] is None  # untouched


def test_mark_read_is_idempotent_returns_false_on_second_call(db, username):
    db.create(username, kind="job_complete", message="hello")
    notification_id = db.list_for_user(username)[0]["id"]

    assert db.mark_read(notification_id, username) is True
    assert db.mark_read(notification_id, username) is False


def test_mark_read_unknown_id_returns_false(db, username):
    assert db.mark_read(999999999, username) is False
