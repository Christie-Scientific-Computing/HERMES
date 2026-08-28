"""
Tests for routers/admin.py -- the staff-only compliance dashboard (Phase 4,
see docs/plans/frontend-rewrite-implementation-plan.md §6). The explicit
non-staff-gets-403 test below is the one the plan calls out by name: it's
what would catch a route that forgot require_data_custodian.
"""
from unittest.mock import AsyncMock

import pytest

from frontend_fastapi import backend_client


@pytest.fixture()
def mock_backend(monkeypatch):
    mocks = {}
    for name in ("admin_overview", "list_projects", "list_user_active_projects"):
        m = AsyncMock()
        monkeypatch.setattr(backend_client, name, m)
        mocks[name] = m
    mocks["list_user_active_projects"].return_value = []
    mocks["admin_overview"].return_value = {
        "expiring_projects": [], "recent_jobs": [], "audit_chain_check": None,
    }
    mocks["list_projects"].return_value = []
    return mocks


def test_admin_overview_requires_staff(client, make_user, login):
    make_user(username="alice", is_staff=False)
    login("alice")

    resp = client.get("/admin")

    assert resp.status_code == 403


def test_admin_overview_unauthenticated_redirects_to_login(client):
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/accounts/login")


def test_admin_overview_renders_for_staff(client, make_user, login, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["admin_overview"].return_value = {
        "expiring_projects": [{"project_id": "p1", "title": "Trial X", "created_by": "alice", "expiry_date": "2027-01-01"}],
        "recent_jobs": [{
            "job_id": "job-1", "created_by": "alice", "created_at": "2026-01-01T00:00:00Z",
            "imported_count": 2, "submitted_count": 3, "exported_count": 1, "export_attempted_count": 1,
        }],
        "audit_chain_check": {"ok": True, "checked_at": "2026-01-01T00:00:00Z", "bad_event_id": None, "reason": None},
    }
    mock_backend["list_projects"].return_value = [{"status": "approved"}, {"status": "approved"}, {"status": "draft"}]

    resp = client.get("/admin")

    assert resp.status_code == 200
    assert "Trial X" in resp.text
    assert "job-1" in resp.text
    assert "OK" in resp.text


def test_admin_overview_shows_tampered_audit_chain_state(client, make_user, login, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["admin_overview"].return_value = {
        "expiring_projects": [], "recent_jobs": [],
        "audit_chain_check": {"ok": False, "checked_at": "2026-01-01T00:00:00Z", "bad_event_id": 42, "reason": "row_hash mismatch"},
    }

    resp = client.get("/admin")

    assert resp.status_code == 200
    assert "TAMPERED" in resp.text
    assert "row_hash mismatch" in resp.text


def test_admin_overview_never_verified_state(client, make_user, login, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["admin_overview"].return_value = {
        "expiring_projects": [], "recent_jobs": [], "audit_chain_check": None,
    }

    resp = client.get("/admin")

    assert resp.status_code == 200
    assert "Never verified yet" in resp.text


def test_admin_overview_backend_error_shows_inline_message(client, make_user, login, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["admin_overview"].side_effect = backend_client.BackendError(503, "backend down")

    resp = client.get("/admin")

    assert resp.status_code == 200
    assert "backend down" in resp.text
