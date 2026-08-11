"""
Unit tests for backend_client.py's HTTP plumbing, using httpx.MockTransport
rather than a real backend -- no network, no fixtures needed beyond what's
declared here.
"""
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
