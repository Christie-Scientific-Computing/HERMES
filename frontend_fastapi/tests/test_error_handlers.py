"""
Exercises the REAL exception handlers from main.py (conftest.py wires them
into the test app directly, rather than test-only stand-ins) -- regression
coverage for two bugs a multi-angle code review caught in Phase 0's first
draft: the login redirect silently dropping the original query string, and
the 403 page misrepresenting an authenticated-but-not-staff user as logged
out.
"""
from urllib.parse import unquote


def test_not_authenticated_redirect_preserves_query_string(client):
    resp = client.get("/test/protected?foo=bar", follow_redirects=False)
    assert resp.status_code == 303
    next_param = resp.headers["location"].split("next=", 1)[1]
    assert unquote(next_param) == "/test/protected?foo=bar"


def test_forbidden_page_shows_the_logged_in_non_staff_user(client, make_user):
    csrf_token = client.get("/test/context").json()["csrf_token"]
    make_user(username="alice", is_staff=False)
    client.post("/test/login", data={"username": "alice", "remember": "true", "csrf_token": csrf_token})

    resp = client.get("/test/staff-only")
    assert resp.status_code == 403
    # base.html renders the username (not a "Log in" link) whenever `user`
    # is truthy in the template context -- see templates/base.html.
    assert "alice" in resp.text
    assert "Log in" not in resp.text


def test_forbidden_page_carries_a_usable_csrf_token(client, make_user):
    csrf_token = client.get("/test/context").json()["csrf_token"]
    make_user(username="alice", is_staff=False)
    client.post("/test/login", data={"username": "alice", "remember": "true", "csrf_token": csrf_token})

    resp = client.get("/test/staff-only")
    assert 'name="csrf_token"' in resp.text
