from frontend_fastapi import security
from frontend_fastapi.models import User
from frontend_fastapi.scripts.reset_password import reset_password
from frontend_fastapi.scripts.set_staff import set_staff


def _make_user(db, username="alice", password="correct horse battery staple", is_staff=False) -> User:
    user = User(username=username, password_hash=security.hash_password(password), is_staff=is_staff, is_active=True)
    db.add(user)
    db.commit()
    return user


def test_reset_password_updates_an_existing_user(db, SessionFactory, monkeypatch):
    _make_user(db, username="alice", password="old password here")
    monkeypatch.setattr("frontend_fastapi.scripts.reset_password.SessionLocal", SessionFactory)

    assert reset_password("alice", "a new strong password") is True

    db.expire_all()
    user = db.query(User).filter_by(username="alice").one()
    assert security.verify_password("a new strong password", user.password_hash)


def test_reset_password_returns_false_for_unknown_user(db, SessionFactory, monkeypatch):
    monkeypatch.setattr("frontend_fastapi.scripts.reset_password.SessionLocal", SessionFactory)
    assert reset_password("nobody", "whatever") is False


def test_set_staff_grants_by_default(db, SessionFactory, monkeypatch):
    _make_user(db, username="alice", is_staff=False)
    monkeypatch.setattr("frontend_fastapi.scripts.set_staff.SessionLocal", SessionFactory)

    assert set_staff("alice", is_staff=True) is True

    db.expire_all()
    user = db.query(User).filter_by(username="alice").one()
    assert user.is_staff is True


def test_set_staff_can_revoke(db, SessionFactory, monkeypatch):
    _make_user(db, username="alice", is_staff=True)
    monkeypatch.setattr("frontend_fastapi.scripts.set_staff.SessionLocal", SessionFactory)

    assert set_staff("alice", is_staff=False) is True

    db.expire_all()
    user = db.query(User).filter_by(username="alice").one()
    assert user.is_staff is False


def test_set_staff_returns_false_for_unknown_user(db, SessionFactory, monkeypatch):
    monkeypatch.setattr("frontend_fastapi.scripts.set_staff.SessionLocal", SessionFactory)
    assert set_staff("nobody", is_staff=True) is False
