"""
Exercises session_middleware.SessionMiddleware / deps.get_session /
auth.login_user / auth.logout_user through real HTTP request/response
cycles against the throwaway app conftest.py builds -- see that module's
docstring for why Phase 0 needs its own test-only routes.
"""


def _csrf_token(client) -> str:
    """Every POST route is CSRF-protected globally (main.py, conftest.py's
    app fixture) -- fetch a real token for the client's current session
    before submitting one, the same way a real form page would embed it."""
    return client.get("/test/context").json()["csrf_token"]


def _post(client, path, **data):
    return client.post(path, data={**data, "csrf_token": _csrf_token(client)})


def test_anonymous_visit_gets_a_session_cookie_with_no_max_age(client):
    resp = client.get("/test/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"username": None}
    set_cookie = resp.headers.get("set-cookie")
    assert set_cookie is not None
    assert "hermes_session=" in set_cookie
    # No Max-Age/Expires -> a true browser-session cookie, the correct
    # default before any "remember me" choice has been made.
    assert "max-age" not in set_cookie.lower()
    assert "expires" not in set_cookie.lower()


def test_same_client_reuses_the_same_session_across_requests(client):
    first = client.get("/test/whoami")
    session_cookie = client.cookies.get("hermes_session")
    assert session_cookie is not None

    second = client.get("/test/whoami")
    assert client.cookies.get("hermes_session") == session_cookie
    assert first.status_code == second.status_code == 200


def test_login_without_remember_omits_cookie_max_age(client, make_user):
    make_user(username="alice")
    resp = _post(client, "/test/login", username="alice", remember="false")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie")
    assert "hermes_session=" in set_cookie
    assert "max-age" not in set_cookie.lower()


def test_login_with_remember_sets_cookie_max_age(client, make_user):
    make_user(username="alice")
    resp = _post(client, "/test/login", username="alice", remember="true")
    set_cookie = resp.headers.get("set-cookie")
    assert "hermes_session=" in set_cookie
    assert "max-age" in set_cookie.lower()


def test_login_actually_authenticates_the_session(client, make_user):
    make_user(username="alice")
    _post(client, "/test/login", username="alice", remember="true")
    resp = client.get("/test/whoami")
    assert resp.json() == {"username": "alice"}


def test_login_rotates_the_session_id(client, make_user):
    """Session fixation prevention: the pre-login (anonymous) session id
    must never become a privileged one -- login always issues a fresh id."""
    make_user(username="alice")
    client.get("/test/whoami")
    pre_login_session_id = client.cookies.get("hermes_session")

    _post(client, "/test/login", username="alice", remember="true")
    post_login_session_id = client.cookies.get("hermes_session")

    assert post_login_session_id != pre_login_session_id


def test_logout_clears_authentication(client, make_user):
    make_user(username="alice")
    _post(client, "/test/login", username="alice", remember="true")
    assert client.get("/test/whoami").json()["username"] == "alice"

    _post(client, "/test/logout")
    assert client.get("/test/whoami").json()["username"] is None


def test_require_login_redirects_anonymous_visitor(client):
    resp = client.get("/test/protected", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/accounts/login")


def test_require_login_redirect_carries_a_working_session_cookie(client):
    """Regression test: an anonymous visit to a login-gated page must not
    silently lose its brand-new session cookie just because a LATER
    dependency in the chain (require_login) raises after get_session
    already ran -- see session_middleware.SessionMiddleware's docstring."""
    resp = client.get("/test/protected", follow_redirects=False)
    set_cookie = resp.headers.get("set-cookie")
    assert set_cookie is not None and "hermes_session=" in set_cookie


def test_require_login_allows_authenticated_user(client, make_user):
    make_user(username="alice")
    _post(client, "/test/login", username="alice", remember="true")
    resp = client.get("/test/protected")
    assert resp.status_code == 200
    assert resp.json() == {"username": "alice"}


def test_require_data_custodian_rejects_non_staff_user(client, make_user):
    make_user(username="alice", is_staff=False)
    _post(client, "/test/login", username="alice", remember="true")
    resp = client.get("/test/staff-only")
    assert resp.status_code == 403


def test_require_data_custodian_allows_staff_user(client, make_user):
    make_user(username="bob", is_staff=True)
    _post(client, "/test/login", username="bob", remember="true")
    resp = client.get("/test/staff-only")
    assert resp.status_code == 200


def test_inactive_user_is_treated_as_logged_out(client, make_user, db):
    from frontend_fastapi.models import User

    make_user(username="alice", is_active=True)
    _post(client, "/test/login", username="alice", remember="true")
    assert client.get("/test/whoami").json()["username"] == "alice"

    user = db.query(User).filter_by(username="alice").one()
    user.is_active = False
    db.commit()

    assert client.get("/test/whoami").json()["username"] is None


def test_untrusted_host_header_is_rejected(client):
    resp = client.get("/test/whoami", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400
