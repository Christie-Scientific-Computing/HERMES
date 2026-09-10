"""
Tests for routers/error_reports.py (item 06) -- the report-an-issue form.
"""
from unittest.mock import AsyncMock

import pytest

from frontend_fastapi import backend_client


@pytest.fixture()
def mock_backend(monkeypatch):
    mocks = {}
    for name in ("create_error_report", "list_user_active_projects"):
        m = AsyncMock()
        monkeypatch.setattr(backend_client, name, m)
        mocks[name] = m
    mocks["list_user_active_projects"].return_value = []
    return mocks


def test_report_form_requires_login(client):
    resp = client.get("/report", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/accounts/login")


def test_report_form_renders_for_any_logged_in_user(client, make_user, login):
    make_user(username="alice", is_staff=False)
    login("alice")

    resp = client.get("/report")

    assert resp.status_code == 200
    assert "Report an issue" in resp.text


def test_report_submit_sends_session_username_not_a_form_value(client, make_user, login, csrf_token, mock_backend):
    """username must always come from the session, never trusted from the
    form -- there is no username field on ErrorReportForm at all, so this
    also guards against one being added carelessly later."""
    make_user(username="alice", is_staff=False)
    login("alice")

    resp = client.post("/report", data={
        "category": "error", "message": "Saw an MRN in an error banner.", "csrf_token": csrf_token(),
    }, follow_redirects=False)

    assert resp.status_code == 303
    mock_backend["create_error_report"].assert_awaited_once_with(
        username="alice", category="error", message="Saw an MRN in an error banner.",
        urgent=False, job_id=None,
    )


def test_report_submit_requires_a_message(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice", is_staff=False)
    login("alice")

    resp = client.post("/report", data={"category": "feedback", "message": "", "csrf_token": csrf_token()})

    assert resp.status_code == 400
    mock_backend["create_error_report"].assert_not_awaited()


def test_report_submit_backend_error_shown_inline(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice", is_staff=False)
    login("alice")
    mock_backend["create_error_report"].side_effect = backend_client.BackendError(500, "boom")

    resp = client.post("/report", data={
        "category": "feedback", "message": "hello", "csrf_token": csrf_token(),
    })

    assert resp.status_code == 400
    assert "boom" in resp.text
