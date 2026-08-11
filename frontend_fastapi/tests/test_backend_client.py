"""
Unit tests for backend_client.py's HTTP plumbing, using httpx.MockTransport
rather than a real backend -- no network, no fixtures needed beyond what's
declared here.
"""
import json
from datetime import date, datetime, timezone

import httpx
import pytest

from frontend_fastapi import backend_client


@pytest.fixture(autouse=True)
def _isolated_client(monkeypatch):
    """Every test gets its own client bound to a mock transport, so tests
    can't leak state through the real module-level backend_client.client
    (or accidentally hit a real network address)."""
    def _make_client(transport: httpx.MockTransport) -> None:
        monkeypatch.setattr(backend_client, "client", httpx.AsyncClient(base_url="http://backend", transport=transport))
    return _make_client


async def test_list_user_active_projects_requests_approved_status(_isolated_client):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"projects": [{"project_id": "p1"}]})

    _isolated_client(httpx.MockTransport(handler))
    result = await backend_client.list_user_active_projects("alice")

    assert captured["params"] == {"username": "alice", "status": "approved"}
    assert result == [{"project_id": "p1"}]


async def test_error_response_raises_backend_error_with_detail(_isolated_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "project service unavailable"})

    _isolated_client(httpx.MockTransport(handler))
    with pytest.raises(backend_client.BackendError) as exc_info:
        await backend_client.list_user_active_projects("alice")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "project service unavailable"


async def test_error_response_with_non_json_body_falls_back_to_raw_text(_isolated_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    _isolated_client(httpx.MockTransport(handler))
    with pytest.raises(backend_client.BackendError) as exc_info:
        await backend_client.list_user_active_projects("alice")

    assert exc_info.value.status_code == 500
    assert "internal server error" in exc_info.value.detail


# ---- Projects (research_projects, ported in Phase 2) ----
#
# These exercise the actual HTTP request each function builds -- path,
# method, and JSON body shape -- against backend/src/projects/endpoints.py's
# Pydantic request models, which routers/research_projects.py's own tests
# never do (that module monkeypatches these functions directly, so their
# bodies never execute there). Point of failure this catches: a field
# rename or shape change on either side that every other test would miss
# since both sides would still agree with each other, just not with the
# real backend.

async def test_create_project_posts_expected_body(_isolated_client):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"project_id": "p1"})

    _isolated_client(httpx.MockTransport(handler))
    result = await backend_client.create_project("Title", "alice", description="desc", ethics_reference="ETH-1")

    assert captured["method"] == "POST"
    assert captured["path"] == "/projects"
    assert captured["json"] == {"title": "Title", "created_by": "alice", "description": "desc", "ethics_reference": "ETH-1"}
    assert result == {"project_id": "p1"}


async def test_submit_project_posts_username(_isolated_client):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"project_id": "p1", "status": "submitted"})

    _isolated_client(httpx.MockTransport(handler))
    await backend_client.submit_project("p1", "alice")

    assert captured["path"] == "/projects/p1/submit"
    assert captured["json"] == {"username": "alice"}


@pytest.mark.parametrize("expiry_date, expected", [
    (date(2027, 1, 1), "2027-01-01"),
    (datetime(2027, 1, 1, 12, 30, tzinfo=timezone.utc), "2027-01-01T12:30:00+00:00"),
    ("2027-01-01", "2027-01-01"),  # already a string -- passed through unchanged
    (None, None),
])
async def test_review_project_normalizes_expiry_date(_isolated_client, expiry_date, expected):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"project_id": "p1", "status": "approved"})

    _isolated_client(httpx.MockTransport(handler))
    await backend_client.review_project("p1", reviewer="admin", approved=True, comment="ok", expiry_date=expiry_date)

    assert captured["json"] == {"reviewer": "admin", "approved": True, "comment": "ok", "expiry_date": expected}


async def test_revoke_project_posts_expected_body(_isolated_client):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"project_id": "p1", "status": "revoked"})

    _isolated_client(httpx.MockTransport(handler))
    await backend_client.revoke_project("p1", revoked_by="admin", comment="superseded")

    assert captured["path"] == "/projects/p1/revoke"
    assert captured["json"] == {"revoked_by": "admin", "comment": "superseded"}


async def test_add_member_posts_expected_body(_isolated_client):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"members": []})

    _isolated_client(httpx.MockTransport(handler))
    await backend_client.add_member("p1", "carol", added_by="alice", role="member")

    assert captured["path"] == "/projects/p1/members"
    assert captured["json"] == {"username": "carol", "role": "member", "added_by": "alice"}


async def test_remove_member_sends_delete_with_removed_by_param(_isolated_client):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"members": []})

    _isolated_client(httpx.MockTransport(handler))
    await backend_client.remove_member("p1", "carol", removed_by="alice")

    assert captured["method"] == "DELETE"
    assert captured["path"] == "/projects/p1/members/carol"
    assert captured["params"] == {"removed_by": "alice"}


async def test_get_project_and_list_project_jobs_use_expected_paths(_isolated_client):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"project_id": "p1", "jobs": [{"job_id": "j1"}]})
        return httpx.Response(200, json={"project_id": "p1", "title": "Test"})

    _isolated_client(httpx.MockTransport(handler))
    project = await backend_client.get_project("p1")
    jobs = await backend_client.list_project_jobs("p1")

    assert paths == ["/projects/p1", "/projects/p1/jobs"]
    assert project == {"project_id": "p1", "title": "Test"}
    assert jobs == [{"job_id": "j1"}]
