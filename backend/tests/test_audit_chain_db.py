"""
Tests for AuditChainDB (backend/src/status/audit_chain_db.py) -- persisting
the outcome of periodic hash-chain verification runs (Phase 4).
"""
import uuid

import pytest

from backend.scripts.verify_audit_chain import verify_chain
from backend.src.status.audit_chain_db import AuditChainDB
from backend.src.status.db_client import StatusDB


@pytest.fixture
def db():
    return AuditChainDB()


def test_record_and_latest_round_trip_a_healthy_check(db):
    db.record_check(ok=True, bad_event_id=None, reason=None)

    latest = db.latest_check()

    assert latest["ok"] is True
    assert latest["bad_event_id"] is None
    assert latest["reason"] is None
    assert latest["checked_at"] is not None


def test_record_and_latest_round_trip_a_tampered_check(db):
    status_db = StatusDB()
    job_id = f"audit-chain-test-{uuid.uuid4()}"
    status_db.create_job(job_id)
    status_db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success")
    from backend.scripts.verify_audit_chain import fetch_events_in_order
    bad_id = fetch_events_in_order()[-1]["id"]

    db.record_check(ok=False, bad_event_id=bad_id, reason="row_hash mismatch on event id=...")

    latest = db.latest_check()
    assert latest["ok"] is False
    assert latest["bad_event_id"] == bad_id
    assert "row_hash mismatch" in latest["reason"]


def test_latest_check_returns_the_most_recently_recorded_one(db):
    db.record_check(ok=True)
    db.record_check(ok=False, reason="second, more recent check")

    latest = db.latest_check()

    assert latest["ok"] is False
    assert latest["reason"] == "second, more recent check"


def test_verify_chain_against_an_empty_event_list_does_not_error():
    """The orchestration in backend/worker.py's _run_audit_chain_check calls
    verify_chain with whatever fetch_events_in_order() returns -- on a
    fresh deployment (or, here, isolated from the shared test DB's actual
    rows) that's an empty list, and must be reported as trivially intact,
    not raise."""
    ok, bad_row, reason = verify_chain([], chain_state_last_hash=None)

    assert ok is True
    assert bad_row is None
    assert reason is None
