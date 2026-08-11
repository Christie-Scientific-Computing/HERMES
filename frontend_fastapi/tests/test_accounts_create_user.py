from frontend_fastapi.models import User


def _login(client, csrf_token, username, password="correct horse battery staple"):
    client.post("/accounts/login", data={"username": username, "password": password, "csrf_token": csrf_token()})


def test_non_staff_user_cannot_create_accounts(client, make_user, csrf_token):
    make_user(username="alice", is_staff=False)
    _login(client, csrf_token, "alice")
    resp = client.get("/accounts/users/create")
    assert resp.status_code == 403


def test_staff_creates_an_immediately_usable_account(client, make_user, csrf_token, db):
    make_user(username="bob", is_staff=True)
    _login(client, csrf_token, "bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "a genuinely strong passphrase",
        "password2": "a genuinely strong passphrase", "csrf_token": csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303

    new_user = db.query(User).filter_by(username="carol").one()
    assert new_user.is_active is True

    # Immediately usable -- no activation step.
    logout_client_csrf = csrf_token()
    client.post("/accounts/logout", data={"csrf_token": logout_client_csrf})
    resp = client.post("/accounts/login", data={
        "username": "carol", "password": "a genuinely strong passphrase", "csrf_token": csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303


def test_mismatched_passwords_are_rejected(client, make_user, csrf_token):
    make_user(username="bob", is_staff=True)
    _login(client, csrf_token, "bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "a genuinely strong passphrase",
        "password2": "a totally different passphrase", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "didn&#39;t match" in resp.text or "didn't match" in resp.text


def test_password_too_short_is_rejected(client, make_user, csrf_token):
    make_user(username="bob", is_staff=True)
    _login(client, csrf_token, "bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "short1", "password2": "short1", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "too short" in resp.text


def test_password_too_similar_to_username_is_rejected(client, make_user, csrf_token):
    make_user(username="bob", is_staff=True)
    _login(client, csrf_token, "bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "carol carol carol", "password2": "carol carol carol",
        "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "too similar" in resp.text


def test_duplicate_username_is_rejected(client, make_user, csrf_token):
    make_user(username="bob", is_staff=True)
    make_user(username="carol")
    _login(client, csrf_token, "bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "a genuinely strong passphrase",
        "password2": "a genuinely strong passphrase", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "already exists" in resp.text
