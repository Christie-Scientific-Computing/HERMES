"""
Integration tests for the error report endpoints
(backend/src/error_reports/endpoints.py, item 06).
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.error_reports import endpoints as error_reports_endpoints
from backend.src.error_reports.db_client import ErrorReportsDB


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(error_reports_endpoints.router)
    return TestClient(app)


@pytest.fixture
def username():
    return f"user-{uuid.uuid4()}"


def test_create_error_report(client, username):
    resp = client.post("/error_reports", json={
        "username": username, "category": "error", "message": "Saw an MRN in an error banner.", "urgent": True,
    })

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    reports = [r for r in ErrorReportsDB().list_all() if r["username"] == username]
    assert len(reports) == 1
    assert reports[0]["category"] == "error"
    assert reports[0]["urgent"] is True
    assert reports[0]["job_id"] is None


def test_create_error_report_with_an_unknown_job_id_returns_422_not_500(client, username):
    """job_id is free-typed by the reporting user (ErrorReportForm has no
    validation against real jobs) -- a typo must surface as a clear 422,
    not an opaque 500 from the underlying FK violation."""
    resp = client.post("/error_reports", json={
        "username": username, "category": "feedback", "message": "hi", "job_id": f"no-such-job-{uuid.uuid4()}",
    })

    assert resp.status_code == 422


def test_create_error_report_rejects_an_unknown_category(client, username):
    resp = client.post("/error_reports", json={
        "username": username, "category": "not-a-real-category", "message": "hi",
    })

    assert resp.status_code == 422


def test_list_error_reports_returns_newest_first(client, username):
    client.post("/error_reports", json={"username": username, "category": "feedback", "message": "first"})
    client.post("/error_reports", json={"username": username, "category": "feedback", "message": "second"})

    resp = client.get("/error_reports")

    assert resp.status_code == 200
    own = [r for r in resp.json()["error_reports"] if r["username"] == username]
    assert [r["message"] for r in own] == ["second", "first"]


def test_resolve_error_report_marks_addressed(client, username):
    client.post("/error_reports", json={"username": username, "category": "feedback", "message": "please address"})
    report_id = next(r for r in ErrorReportsDB().list_all() if r["username"] == username)["id"]

    resp = client.post(f"/error_reports/{report_id}/resolve", params={"username": "alice"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    report = next(r for r in ErrorReportsDB().list_all() if r["id"] == report_id)
    assert report["resolved_by"] == "alice"
    assert report["resolved_at"] is not None


def test_resolve_error_report_already_addressed_returns_404(client, username):
    client.post("/error_reports", json={"username": username, "category": "feedback", "message": "please address"})
    report_id = next(r for r in ErrorReportsDB().list_all() if r["username"] == username)["id"]
    client.post(f"/error_reports/{report_id}/resolve", params={"username": "alice"})

    resp = client.post(f"/error_reports/{report_id}/resolve", params={"username": "bob"})

    assert resp.status_code == 404


def test_list_error_reports_unaddressed_only(client, username):
    client.post("/error_reports", json={"username": username, "category": "feedback", "message": "one"})
    client.post("/error_reports", json={"username": username, "category": "feedback", "message": "two"})
    # list_all orders newest-first, so own[0] is "two".
    own = [r for r in ErrorReportsDB().list_all() if r["username"] == username]
    client.post(f"/error_reports/{own[0]['id']}/resolve", params={"username": "alice"})

    resp = client.get("/error_reports", params={"unaddressed_only": True})

    own_unaddressed = [r for r in resp.json()["error_reports"] if r["username"] == username]
    assert [r["message"] for r in own_unaddressed] == ["one"]
