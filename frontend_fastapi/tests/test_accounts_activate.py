from frontend_fastapi import security
from frontend_fastapi.models import User


def _invite(db, username="carol", email="carol@example.com") -> User:
    user = User(username=username, email=email, is_active=True, password_hash=security.unusable_password())
    db.add(user)
    db.commit()
    return user


def test_invalid_token_shows_invalid_page(client):
    resp = client.get("/accounts/activate/not-a-real-token")
    assert resp.status_code == 400
    assert "invalid or has expired" in resp.text


def test_valid_token_renders_the_activate_form(client, db):
    user = _invite(db)
    token = security.make_account_token(user.id, user.password_hash)
    resp = client.get(f"/accounts/activate/{token}")
    assert resp.status_code == 200
    assert "Set your password" in resp.text


def test_activating_sets_the_password_and_logs_the_user_in(client, db, csrf_token):
    user = _invite(db)
    token = security.make_account_token(user.id, user.password_hash)
    resp = client.post(f"/accounts/activate/{token}", data={
        "password1": "a genuinely strong passphrase", "password2": "a genuinely strong passphrase",
        "csrf_token": csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/test/whoami").json()["username"] == "carol"

    db.refresh(user)
    assert security.is_usable_password(user.password_hash)
    assert security.verify_password("a genuinely strong passphrase", user.password_hash)


def test_token_cannot_be_reused_after_activation(client, db, csrf_token):
    user = _invite(db)
    token = security.make_account_token(user.id, user.password_hash)
    client.post(f"/accounts/activate/{token}", data={
        "password1": "a genuinely strong passphrase", "password2": "a genuinely strong passphrase",
        "csrf_token": csrf_token(),
    })
    client.post("/accounts/logout", data={"csrf_token": csrf_token()})

    resp = client.get(f"/accounts/activate/{token}")
    assert resp.status_code == 400
    assert "invalid or has expired" in resp.text


def test_password_too_similar_to_username_is_rejected(client, db, csrf_token):
    user = _invite(db, username="dave12345")
    token = security.make_account_token(user.id, user.password_hash)
    resp = client.post(f"/accounts/activate/{token}", data={
        "password1": "dave12345 is my password", "password2": "dave12345 is my password",
        "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "too similar" in resp.text


def test_mismatched_passwords_are_rejected(client, db, csrf_token):
    user = _invite(db)
    token = security.make_account_token(user.id, user.password_hash)
    resp = client.post(f"/accounts/activate/{token}", data={
        "password1": "a genuinely strong passphrase", "password2": "a different passphrase entirely",
        "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
