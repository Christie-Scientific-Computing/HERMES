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
    async def _fake_list(username):
        return [{"project_id": "p1", "title": "Study A"}]

    make_user(username="alice")
    monkeypatch.setattr(backend_client, "list_user_active_projects", _fake_list)
    csrf_token = client.get("/test/context").json()["csrf_token"]
    client.post("/test/login", data={"username": "alice", "remember": "true", "csrf_token": csrf_token})
    ctx = client.get("/test/context").json()
    assert ctx["has_user"] is True
    assert ctx["nav_active_projects"] == [{"project_id": "p1", "title": "Study A"}]


def test_template_context_survives_backend_being_unreachable(client, make_user, monkeypatch):
    async def _raise(username):
        raise backend_client.BackendError(503, "backend down")

    make_user(username="alice")
    monkeypatch.setattr(backend_client, "list_user_active_projects", _raise)
    csrf_token = client.get("/test/context").json()["csrf_token"]
    client.post("/test/login", data={"username": "alice", "remember": "true", "csrf_token": csrf_token})
    ctx = client.get("/test/context").json()
    assert ctx["nav_active_projects"] == []


def test_template_context_reports_backend_health(client, monkeypatch):
    async def _fake_health():
        return {"status": "ok", "anonymisation_active": True}

    monkeypatch.setattr(backend_client, "get_backend_health", _fake_health)
    ctx = client.get("/test/context").json()
    assert ctx["backend_health"] == {"status": "ok", "anonymisation_active": True}


def test_template_context_backend_health_is_none_when_unreachable(client, monkeypatch):
    async def _raise():
        raise backend_client.BackendError(503, "backend down")

    monkeypatch.setattr(backend_client, "get_backend_health", _raise)
    ctx = client.get("/test/context").json()
    assert ctx["backend_health"] is None


# ---- nav_review_queue_count (F002) / nav_urgent_reports_count (F004) ----

def test_review_queue_count_for_staff_sums_submitted_and_amendments(client, make_user, login, monkeypatch):
    make_user(username="admin", is_staff=True)
    login("admin")

    async def _list_projects(username=None, status=None):
        # list_user_active_projects (nav_active_projects) also calls this
        # with username= set -- only the review-badge's own username-less,
        # status="submitted" call should contribute to the count.
        if username is not None:
            return []
        return [{"project_id": "p1"}]

    async def _amendments():
        return [{"project_id": "p2"}, {"project_id": "p3"}]

    monkeypatch.setattr(backend_client, "list_projects", _list_projects)
    monkeypatch.setattr(backend_client, "list_pending_amendments", _amendments)

    ctx = client.get("/test/context").json()

    assert ctx["nav_review_queue_count"] == 3


def test_review_queue_count_absent_for_non_staff(client, make_user, login, monkeypatch):
    make_user(username="alice", is_staff=False)
    login("alice")

    async def _list_projects(username=None, status=None):
        return [{"project_id": "p1"}]

    monkeypatch.setattr(backend_client, "list_projects", _list_projects)

    ctx = client.get("/test/context").json()

    assert ctx["nav_review_queue_count"] == 0


def test_review_queue_count_degrades_to_zero_on_backend_error(client, make_user, login, monkeypatch):
    make_user(username="admin", is_staff=True)
    login("admin")

    async def _list_projects(username=None, status=None):
        if username is not None:
            return []
        raise backend_client.BackendError(503, "backend down")

    monkeypatch.setattr(backend_client, "list_projects", _list_projects)

    ctx = client.get("/test/context").json()

    assert ctx["nav_review_queue_count"] == 0


def test_urgent_reports_count_for_staff_counts_only_urgent(client, make_user, login, monkeypatch):
    make_user(username="admin", is_staff=True)
    login("admin")

    async def _reports(limit=100, unaddressed_only=False):
        return [
            {"id": 1, "urgent": True, "resolved_at": None},
            {"id": 2, "urgent": False, "resolved_at": None},
        ]

    monkeypatch.setattr(backend_client, "list_error_reports", _reports)

    ctx = client.get("/test/context").json()

    assert ctx["nav_urgent_reports_count"] == 1


def test_urgent_reports_count_absent_for_non_staff(client, make_user, login, monkeypatch):
    make_user(username="alice", is_staff=False)
    login("alice")

    async def _reports(limit=100, unaddressed_only=False):
        return [{"id": 1, "urgent": True, "resolved_at": None}]

    monkeypatch.setattr(backend_client, "list_error_reports", _reports)

    ctx = client.get("/test/context").json()

    assert ctx["nav_urgent_reports_count"] == 0


def test_urgent_reports_count_degrades_to_zero_on_backend_error(client, make_user, login, monkeypatch):
    make_user(username="admin", is_staff=True)
    login("admin")

    async def _raise(limit=100, unaddressed_only=False):
        raise backend_client.BackendError(503, "backend down")

    monkeypatch.setattr(backend_client, "list_error_reports", _raise)

    ctx = client.get("/test/context").json()

    assert ctx["nav_urgent_reports_count"] == 0
