"""
Tests for routers/notifications.py (mark-read) and the notification
dropdown assembled by deps.get_template_context (Phase 4).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from frontend_fastapi import backend_client


@pytest.fixture()
def mock_backend(monkeypatch):
    mocks = {}
    for name in ("list_notifications", "mark_notification_read", "list_user_active_projects"):
        m = AsyncMock()
        monkeypatch.setattr(backend_client, name, m)
        mocks[name] = m
    mocks["list_user_active_projects"].return_value = []
    mocks["list_notifications"].return_value = []
    return mocks


def test_dashboard_shows_notification_count_when_present(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_notifications"].return_value = [
        {"id": 1, "message": "Job finished.", "created_at": "2026-01-01T00:00:00Z"},
        {"id": 2, "message": "Project approved.", "created_at": "2026-01-02T00:00:00Z"},
    ]

    resp = client.get("/")

    assert resp.status_code == 200
    assert "Job finished." in resp.text
    assert "Project approved." in resp.text
    # Not assert_awaited_once_with: login()'s own POST follows a redirect
    # to a page that also renders get_template_context, so this is called
    # more than once across the two requests -- what matters here is the
    # arguments, not the exact count.
    mock_backend["list_notifications"].assert_any_await("alice", unread_only=True, limit=10)


def test_dashboard_shows_no_notifications_when_empty(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")

    resp = client.get("/")

    assert resp.status_code == 200
    assert "No new notifications." in resp.text


def test_dropdown_shows_the_live_expiring_soon_section(client, make_user, login, mock_backend):
    """The "live" section plan §6.1 point 3 requires: the current user's own
    expiring-soon projects, computed fresh from nav_active_projects (deps.
    expiring_soon), distinct from persisted nav_notifications rows."""
    make_user(username="alice")
    login("alice")
    # +1 hour of margin: days_remaining is a floor (timedelta.days truncates
    # toward zero), so a bare "+5 days" computed here can read as 4 by the
    # time expiring_soon() recomputes "now" a moment later -- same reasoning
    # test_research_projects.py's own days-remaining test already applies.
    soon = (datetime.now(timezone.utc) + timedelta(days=5, hours=1)).isoformat()
    mock_backend["list_user_active_projects"].return_value = [
        {"project_id": "p1", "title": "Trial X", "status": "approved", "expiry_date": soon},
    ]

    resp = client.get("/")

    assert resp.status_code == 200
    assert "Trial X" in resp.text
    assert "expires in 5 days" in resp.text
    assert "No new notifications." not in resp.text


def test_dropdown_omits_the_live_section_for_a_project_outside_the_window(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    far = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
    mock_backend["list_user_active_projects"].return_value = [
        {"project_id": "p1", "title": "Trial Y", "status": "approved", "expiry_date": far},
    ]

    resp = client.get("/")

    assert resp.status_code == 200
    assert "Trial Y" not in resp.text
    assert "No new notifications." in resp.text


def test_dashboard_survives_a_backend_error_fetching_notifications(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_notifications"].side_effect = backend_client.BackendError(503, "down")

    resp = client.get("/")

    assert resp.status_code == 200  # the page still renders -- best-effort, not load-bearing


def test_mark_notification_read_calls_backend_with_current_user_and_redirects(
    client, make_user, login, csrf_token, mock_backend,
):
    make_user(username="alice")
    login("alice")

    resp = client.post("/notifications/1/read", data={"csrf_token": csrf_token()}, follow_redirects=False)

    assert resp.status_code == 303
    mock_backend["mark_notification_read"].assert_awaited_once_with(1, "alice")


def test_mark_notification_read_unauthenticated_redirects_to_login(client, csrf_token):
    # A valid CSRF token is required regardless of auth state (global
    # csrf_protect runs before require_login) -- an anonymous visitor's
    # session still has a real token, same as any other unauthenticated
    # POST test elsewhere in this suite.
    resp = client.post("/notifications/1/read", data={"csrf_token": csrf_token()}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/accounts/login")


def test_mark_notification_read_propagates_backend_404(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["mark_notification_read"].side_effect = backend_client.BackendError(404, "no such notification")

    resp = client.post("/notifications/999/read", data={"csrf_token": csrf_token()})

    assert resp.status_code == 404
