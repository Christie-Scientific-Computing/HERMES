from datetime import timedelta

from frontend_fastapi import security
from frontend_fastapi.flash import flash, pop_flashes
from frontend_fastapi.models import Session, utcnow


def _new_session() -> Session:
    return Session(
        id=security.new_session_id(), csrf_token=security.new_csrf_token(),
        flash_messages=[], expires_at=utcnow() + timedelta(days=1),
    )


def test_flash_then_pop_returns_and_clears():
    session = _new_session()
    flash(session, "success", "Project submitted for review")
    assert pop_flashes(session) == [{"tag": "success", "text": "Project submitted for review"}]
    assert pop_flashes(session) == []  # read-then-clear: a second pop is empty


def test_flash_preserves_order_of_multiple_messages():
    session = _new_session()
    flash(session, "info", "first")
    flash(session, "error", "second")
    assert [m["text"] for m in pop_flashes(session)] == ["first", "second"]


def test_flash_mutation_is_persisted_across_a_db_round_trip(SessionFactory, db_engine):
    """Regression test for the exact bug docs/frontend-rewrite-implementation-plan.md
    Phase 0 calls out by name: a plain (non-Mutable) JSON column doesn't
    notify SQLAlchemy's unit-of-work of an in-place .append(), so a flashed
    message would silently never make it to the database. Round-trips
    through two SEPARATE sessions (mirroring two separate requests) to
    prove the write actually landed, not just that the in-memory object
    looks right."""
    write_db = SessionFactory()
    session_row = _new_session()
    write_db.add(session_row)
    write_db.commit()

    flash(session_row, "warning", "flashed in request 1")
    write_db.commit()
    write_db.close()

    read_db = SessionFactory()
    reloaded = read_db.get(Session, session_row.id)
    assert reloaded.flash_messages == [{"tag": "warning", "text": "flashed in request 1"}]
    read_db.close()
