from sqlalchemy import text

from frontend_fastapi import security
from frontend_fastapi.models import Session, User


def test_anonymous_visitor_is_redirected_to_login(client):
    resp = client.get("/accounts/me", follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_without_csrf_token_is_rejected(client, db, make_user, login):
    make_user(username="alice", password="correct horse battery staple")
    login("alice")
    resp = client.post("/accounts/me", data={
        "old_password": "correct horse battery staple",
        "password1": "a genuinely strong passphrase", "password2": "a genuinely strong passphrase",
    })
    assert resp.status_code == 403

    user = db.query(User).filter_by(username="alice").one()
    assert security.verify_password("correct horse battery staple", user.password_hash)


def test_correct_old_password_changes_it(client, db, make_user, login, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    login("alice")
    resp = client.post("/accounts/me", data={
        "old_password": "correct horse battery staple",
        "password1": "a genuinely strong passphrase", "password2": "a genuinely strong passphrase",
        "csrf_token": csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303

    user = db.query(User).filter_by(username="alice").one()
    assert security.verify_password("a genuinely strong passphrase", user.password_hash)


def test_wrong_old_password_is_rejected(client, db, make_user, login, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    login("alice")
    resp = client.post("/accounts/me", data={
        "old_password": "not the right password",
        "password1": "a genuinely strong passphrase", "password2": "a genuinely strong passphrase",
        "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "incorrect" in resp.text.lower()

    user = db.query(User).filter_by(username="alice").one()
    assert security.verify_password("correct horse battery staple", user.password_hash)


def test_new_password_too_similar_to_username_is_rejected(client, make_user, login, csrf_token):
    make_user(username="dave12345", password="correct horse battery staple")
    login("dave12345")
    resp = client.post("/accounts/me", data={
        "old_password": "correct horse battery staple",
        "password1": "dave12345 is my password", "password2": "dave12345 is my password",
        "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "too similar" in resp.text


def test_mismatched_new_passwords_are_rejected(client, make_user, login, csrf_token):
    make_user(username="alice", password="correct horse battery staple")
    login("alice")
    resp = client.post("/accounts/me", data={
        "old_password": "correct horse battery staple",
        "password1": "a genuinely strong passphrase", "password2": "a different passphrase entirely",
        "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400


def test_changing_password_signs_out_other_sessions(client, db, make_user, login, csrf_token):
    user = make_user(username="alice", password="correct horse battery staple")
    login("alice")

    other_session = Session(
        id="other-session-id-0123456789012345678901234", user_id=user.id,
        csrf_token="other-csrf-token-01234567890123456789012",
        expires_at=db.query(Session).filter_by(user_id=user.id).first().expires_at,
    )
    db.add(other_session)
    db.commit()

    client.post("/accounts/me", data={
        "old_password": "correct horse battery staple",
        "password1": "a genuinely strong passphrase", "password2": "a genuinely strong passphrase",
        "csrf_token": csrf_token(),
    })

    # Raw SQL, not db.get() -- the delete happened via a separate
    # SQLAlchemy session (the app's own), and re-loading an
    # identity-mapped-but-now-gone row through the ORM raises
    # ObjectDeletedError rather than just returning None.
    remaining = db.execute(text("SELECT COUNT(*) FROM sessions WHERE id = :id"), {"id": other_session.id}).scalar()
    assert remaining == 0
    # The session that made the change itself must survive.
    assert client.get("/test/whoami").json()["username"] == "alice"
