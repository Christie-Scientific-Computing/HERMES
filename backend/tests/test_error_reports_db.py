"""
Tests for ErrorReportsDB (backend/src/error_reports/db_client.py) -- item 06.
"""
import uuid

import pytest

from backend.src.error_reports.db_client import ErrorReportsDB
from backend.src.status.db_client import StatusDB


@pytest.fixture
def db():
    return ErrorReportsDB()


@pytest.fixture
def username():
    return f"user-{uuid.uuid4()}"


@pytest.fixture
def job_id():
    """error_reports.job_id is a real FK to jobs.job_id, same as
    notifications.job_id -- a job row genuinely must exist first."""
    job_id = f"report-test-{uuid.uuid4()}"
    StatusDB().create_job(job_id)
    return job_id


def test_create_and_list_all(db, username, job_id):
    db.create(username, category="error", message="Saw MRN 12345 in an error banner.", urgent=True, job_id=job_id)

    reports = db.list_all()

    assert len(reports) >= 1
    report = next(r for r in reports if r["username"] == username)
    assert report["category"] == "error"
    assert report["message"] == "Saw MRN 12345 in an error banner."
    assert report["urgent"] is True
    assert report["job_id"] == job_id


def test_create_defaults_urgent_false_and_job_id_none(db, username):
    db.create(username, category="feedback", message="Nice UI.")

    report = next(r for r in db.list_all() if r["username"] == username)

    assert report["urgent"] is False
    assert report["job_id"] is None


def test_list_all_orders_newest_first(db, username):
    db.create(username, category="feedback", message="first")
    db.create(username, category="feedback", message="second")

    reports = [r for r in db.list_all() if r["username"] == username]

    assert [r["message"] for r in reports] == ["second", "first"]


def test_list_all_respects_limit(db, username):
    for i in range(5):
        db.create(username, category="feedback", message=f"n{i}")

    assert len(db.list_all(limit=2)) == 2
