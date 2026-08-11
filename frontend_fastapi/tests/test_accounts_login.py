def test_login_form_renders_for_anonymous_visitor(client):
    resp = client.get("/accounts/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_login_form_redirects_already_authenticated_user(client, make_user, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    client.post("/accounts/login", data={
        "username": "alice", "password": "correct horse battery staple", "csrf_token": csrf_token(),
    })
    resp = client.get("/accounts/login", follow_redirects=False)
    assert resp.status_code == 303


def test_wrong_password_shows_generic_error(client, make_user, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    resp = client.post("/accounts/login", data={
        "username": "alice", "password": "wrong password", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    # Deliberately generic -- doesn't reveal whether the username exists.
    assert "Incorrect username or password" in resp.text


def test_unknown_username_shows_the_same_generic_error(client, csrf_token):
    resp = client.post("/accounts/login", data={
        "username": "nobody", "password": "whatever", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "Incorrect username or password" in resp.text


def test_blank_submission_shows_the_same_generic_error(client, csrf_token):
    """Regression test: an empty username/password fails WTForms'
    DataRequired() before the credential check ever runs -- must not
    render with no explanation at all."""
    resp = client.post("/accounts/login", data={"username": "", "password": "", "csrf_token": csrf_token()})
    assert resp.status_code == 400
    assert "Incorrect username or password" in resp.text
    assert "Incorrect username or password" in resp.text


def test_inactive_user_cannot_log_in(client, make_user, csrf_token):
    make_user(username="alice", password="correct horse battery staple", is_active=False)
    resp = client.post("/accounts/login", data={
        "username": "alice", "password": "correct horse battery staple", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "Incorrect username or password" in resp.text


def test_correct_credentials_log_the_user_in(client, make_user, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    resp = client.post("/accounts/login", data={
        "username": "alice", "password": "correct horse battery staple", "csrf_token": csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/test/whoami").json()["username"] == "alice"


def test_login_without_csrf_token_is_rejected(client, make_user):
    make_user(username="alice", password="correct horse battery staple")
    resp = client.post("/accounts/login", data={"username": "alice", "password": "correct horse battery staple"})
    assert resp.status_code == 403


def test_next_param_redirects_to_a_local_path_on_success(client, make_user, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    resp = client.post("/accounts/login", data={
        "username": "alice", "password": "correct horse battery staple",
        "csrf_token": csrf_token(), "next": "/accounts/users",
    }, follow_redirects=False)
    assert resp.headers["location"] == "/accounts/users"


def test_next_param_rejects_an_external_url(client, make_user, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    resp = client.post("/accounts/login", data={
        "username": "alice", "password": "correct horse battery staple",
        "csrf_token": csrf_token(), "next": "https://evil.example.com/phish",
    }, follow_redirects=False)
    assert resp.headers["location"] == "/"


def test_next_param_rejects_a_protocol_relative_url(client, make_user, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    resp = client.post("/accounts/login", data={
        "username": "alice", "password": "correct horse battery staple",
        "csrf_token": csrf_token(), "next": "//evil.example.com",
    }, follow_redirects=False)
    assert resp.headers["location"] == "/"


def test_logout_ends_the_session(client, make_user, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    client.post("/accounts/login", data={
        "username": "alice", "password": "correct horse battery staple", "csrf_token": csrf_token(),
    })
    assert client.get("/test/whoami").json()["username"] == "alice"

    client.post("/accounts/logout", data={"csrf_token": csrf_token()})
    assert client.get("/test/whoami").json()["username"] is None
