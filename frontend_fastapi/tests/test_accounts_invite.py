from frontend_fastapi import email_backend
from frontend_fastapi.models import User


def _login(client, csrf_token, username, password="correct horse battery staple"):
    client.post("/accounts/login", data={"username": username, "password": password, "csrf_token": csrf_token()})


def test_non_staff_user_cannot_view_invite_form(client, make_user, csrf_token):
    make_user(username="alice", is_staff=False)
    _login(client, csrf_token, "alice")
    resp = client.get("/accounts/invite")
    assert resp.status_code == 403


def test_anonymous_visitor_is_redirected_to_login(client):
    resp = client.get("/accounts/invite", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/accounts/login")


def test_staff_user_can_view_invite_form(client, make_user, csrf_token):
    make_user(username="bob", is_staff=True)
    _login(client, csrf_token, "bob")
    resp = client.get("/accounts/invite")
    assert resp.status_code == 200
    assert "Invite a new user" in resp.text


def test_invite_creates_an_inactive_password_account_and_flashes_the_link(client, make_user, csrf_token, db, monkeypatch):
    sent = []
    monkeypatch.setattr(email_backend, "send_mail", lambda **kwargs: sent.append(kwargs))

    make_user(username="bob", is_staff=True)
    _login(client, csrf_token, "bob")
    resp = client.post("/accounts/invite", data={
        "username": "carol", "email": "carol@example.com", "csrf_token": csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303

    new_user = db.query(User).filter_by(username="carol").one()
    assert new_user.is_active is True
    assert new_user.email == "carol@example.com"
    from frontend_fastapi import security
    assert security.is_usable_password(new_user.password_hash) is False

    # The email attempt still happens (best-effort)...
    assert sent[0]["to"] == "carol@example.com"
    assert "/accounts/activate/" in sent[0]["body"]

    # ...but the flash message is what's actually load-bearing (no SMTP
    # configured in this test environment, matching most real deployments).
    flashed = client.get("/test/context").json()["flashes"]
    assert any("Activation link" in f["text"] for f in flashed)


def test_invite_rejects_a_duplicate_username(client, make_user, csrf_token):
    make_user(username="bob", is_staff=True)
    make_user(username="carol")
    _login(client, csrf_token, "bob")
    resp = client.post("/accounts/invite", data={
        "username": "carol", "email": "carol2@example.com", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "already exists" in resp.text


def test_invite_requires_an_email_address(client, make_user, csrf_token):
    make_user(username="bob", is_staff=True)
    _login(client, csrf_token, "bob")
    resp = client.post("/accounts/invite", data={"username": "dave", "csrf_token": csrf_token()})
    assert resp.status_code == 400
