from frontend_fastapi import backend_client


def test_csrf_protect_rejects_missing_token(client):
    resp = client.post("/test/csrf-protected", data={})
    assert resp.status_code == 403


def test_csrf_protect_rejects_wrong_token(client):
    client.get("/test/context")  # establishes a session
    resp = client.post("/test/csrf-protected", data={"csrf_token": "not-the-real-token"})
    assert resp.status_code == 403


def test_csrf_protect_accepts_matching_token(client):
    ctx = client.get("/test/context").json()
    resp = client.post("/test/csrf-protected", data={"csrf_token": ctx["csrf_token"]})
    assert resp.status_code == 200


def test_csrf_token_is_stable_across_requests_in_the_same_session(client):
    first = client.get("/test/context").json()["csrf_token"]
    second = client.get("/test/context").json()["csrf_token"]
    assert first == second


def test_template_context_for_anonymous_visitor(client):
    ctx = client.get("/test/context").json()
    assert ctx["has_user"] is False
    assert ctx["nav_active_projects"] == []


def test_template_context_pops_flash_messages(client):
    client.get("/test/flash-and-render")
    ctx = client.get("/test/context").json()
    assert ctx["flashes"] == [{"tag": "success", "text": "hello"}]
    # Popped -- a second render doesn't see it again.
    ctx_again = client.get("/test/context").json()
    assert ctx_again["flashes"] == []


def test_template_context_fetches_active_projects_for_logged_in_user(client, make_user, monkeypatch):
    make_user(username="alice")
    monkeypatch.setattr(
        backend_client, "list_user_active_projects",
        lambda username: [{"project_id": "p1", "title": "Study A"}],
    )
    client.post("/test/login", data={"username": "alice", "remember": "true"})
    ctx = client.get("/test/context").json()
    assert ctx["has_user"] is True
    assert ctx["nav_active_projects"] == [{"project_id": "p1", "title": "Study A"}]


def test_template_context_survives_backend_being_unreachable(client, make_user, monkeypatch):
    def _raise(username):
        raise backend_client.BackendError(503, "backend down")

    make_user(username="alice")
    monkeypatch.setattr(backend_client, "list_user_active_projects", _raise)
    client.post("/test/login", data={"username": "alice", "remember": "true"})
    ctx = client.get("/test/context").json()
    assert ctx["nav_active_projects"] == []
