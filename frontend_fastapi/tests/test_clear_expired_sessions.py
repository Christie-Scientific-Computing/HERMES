from datetime import timedelta

from frontend_fastapi import security
from frontend_fastapi.models import Session, utcnow
from frontend_fastapi.scripts.clear_expired_sessions import clear_expired_sessions


def _session(expires_at) -> Session:
    return Session(
        id=security.new_session_id(), csrf_token=security.new_csrf_token(),
        flash_messages=[], expires_at=expires_at,
    )


def test_deletes_only_expired_sessions(db, SessionFactory, monkeypatch):
    expired = _session(utcnow() - timedelta(days=1))
    still_valid = _session(utcnow() + timedelta(days=1))
    db.add_all([expired, still_valid])
    db.commit()

    # clear_expired_sessions opens its OWN SessionLocal() -- point it at the
    # same in-memory test engine the `db`/SessionFactory fixtures already use.
    monkeypatch.setattr("frontend_fastapi.scripts.clear_expired_sessions.SessionLocal", SessionFactory)

    deleted_count = clear_expired_sessions()

    assert deleted_count == 1
    remaining_ids = {row.id for row in db.query(Session).all()}
    assert remaining_ids == {still_valid.id}
