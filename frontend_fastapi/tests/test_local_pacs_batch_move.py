"""
Tests for round-2 F002 (routers/local_pacs.py's batch move + SSE progress).
conquest_client.move_study and backend_client.audit_local_pacs_move are
monkeypatched at their boundaries, same as test_local_pacs_move.py -- this
is the integration point, not F001/F005's own test suite.

Route-level tests (starting/gating a batch) go through the TestClient;
_run_batch/_stream_batch_events are also awaited directly (mirroring
backend/tests/test_observer_stream.py's approach to testing an async
generator) so the event sequence for a mixed-outcome batch can be asserted
precisely without racing a real background task through HTTP.
"""
import json
from unittest.mock import AsyncMock, Mock

import pytest

from frontend_fastapi import backend_client
from frontend_fastapi.local_pacs import conquest_client as cc
from frontend_fastapi.models import LocalPacsDestination
from frontend_fastapi.routers import local_pacs


@pytest.fixture()
def destination(db):
    d = LocalPacsDestination(ae_title="THIRDPARTY", display_name="Third Party PACS", created_by="system")
    db.add(d)
    db.commit()
    return d


@pytest.fixture()
def mock_audit(monkeypatch):
    m = AsyncMock(return_value={"ok": True, "job_id": "job-1"})
    monkeypatch.setattr(backend_client, "audit_local_pacs_move", m)
    return m


@pytest.fixture(autouse=True)
def _clean_batches():
    """_BATCHES is module-level, shared process state -- clear it around
    every test so one test's batch_id can't leak into another's."""
    local_pacs._BATCHES.clear()
    yield
    local_pacs._BATCHES.clear()


def _item(study_instance_uid, patient_id, study_description="Planning CT"):
    return json.dumps({
        "study_instance_uid": study_instance_uid, "patient_id": patient_id, "study_description": study_description,
    })


# ---- POST /move_batch: validation and gating ----

def test_non_custodian_cannot_start_a_batch(client, make_user, login, csrf_token, destination, mock_audit):
    make_user("alice", is_staff=False)
    login("alice")

    resp = client.post("/local_pacs/move_batch", data={
        "csrf_token": csrf_token(), "selected": [_item("1.2.3", "ANON1")], "destination_id": str(destination.id),
    }, follow_redirects=False)

    assert resp.status_code == 403
    assert local_pacs._BATCHES == {}


def test_empty_selection_is_rejected(client, make_user, login, csrf_token, destination):
    make_user("bob", is_staff=True)
    login("bob")

    resp = client.post("/local_pacs/move_batch", data={
        "csrf_token": csrf_token(), "destination_id": str(destination.id),
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Select at least one study" in resp.content
    assert local_pacs._BATCHES == {}


def test_missing_destination_is_rejected(client, make_user, login, csrf_token):
    make_user("bob", is_staff=True)
    login("bob")

    resp = client.post("/local_pacs/move_batch", data={
        "csrf_token": csrf_token(), "selected": [_item("1.2.3", "ANON1")],
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Choose a destination" in resp.content
    assert local_pacs._BATCHES == {}


def test_malformed_selected_rows_are_skipped_not_a_500(client, make_user, login, csrf_token, destination):
    make_user("bob", is_staff=True)
    login("bob")

    resp = client.post("/local_pacs/move_batch", data={
        "csrf_token": csrf_token(), "selected": ["not-json", "{}"], "destination_id": str(destination.id),
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Select at least one study" in resp.content


def test_starting_a_batch_redirects_to_the_watch_page(client, make_user, login, csrf_token, destination, mock_audit, monkeypatch):
    make_user("bob", is_staff=True)
    login("bob")
    monkeypatch.setattr(cc, "move_study", Mock(return_value=cc.MoveResult(completed=1, status_code=0x0000)))

    resp = client.post("/local_pacs/move_batch", data={
        "csrf_token": csrf_token(), "selected": [_item("1.2.3", "ANON1"), _item("1.2.4", "ANON2")],
        "destination_id": str(destination.id),
    }, follow_redirects=False)

    assert resp.status_code == 303
    assert "/local_pacs/move_batch/" in resp.headers["location"]
    assert "/watch" in resp.headers["location"]
    assert len(local_pacs._BATCHES) == 1
    batch = next(iter(local_pacs._BATCHES.values()))
    assert batch["started_by"] == "bob"
    assert len(batch["items"]) == 2


# ---- watch/stream ownership ----

def test_unknown_batch_id_is_404(client, make_user, login):
    make_user("bob", is_staff=True)
    login("bob")

    resp = client.get("/local_pacs/move_batch/does-not-exist/watch")

    assert resp.status_code == 404


def test_another_users_batch_is_not_visible(client, make_user, login):
    make_user("bob", is_staff=True)
    local_pacs._BATCHES["some-batch"] = {
        "items": [], "destination_ae_title": "X", "destination_display_name": "X", "started_by": "carol",
        "created_at": 0, "events": [{"type": "done"}], "finished": True, "back_url": "/local_pacs",
    }
    login("bob")

    resp = client.get("/local_pacs/move_batch/some-batch/watch")

    assert resp.status_code == 404


def test_non_custodian_cannot_watch_a_batch(client, make_user, login):
    make_user("alice", is_staff=False)
    local_pacs._BATCHES["some-batch"] = {
        "items": [], "destination_ae_title": "X", "destination_display_name": "X", "started_by": "alice",
        "created_at": 0, "events": [{"type": "done"}], "finished": True, "back_url": "/local_pacs",
    }
    login("alice")

    resp = client.get("/local_pacs/move_batch/some-batch/watch")

    assert resp.status_code == 403


# ---- _run_batch / _stream_batch_events: the actual event sequence ----

async def _drain(batch: dict) -> list[dict]:
    return [json.loads(chunk.decode().split("data: ", 1)[1]) async for chunk in local_pacs._stream_batch_events(batch)]


def _new_batch(items, destination_ae_title="THIRDPARTY", destination_display_name="Third Party PACS", started_by="bob"):
    return {
        "items": items, "destination_ae_title": destination_ae_title, "destination_display_name": destination_display_name,
        "started_by": started_by, "created_at": 0, "events": [], "finished": False, "back_url": "/local_pacs",
    }


async def test_mixed_outcomes_are_all_reported_with_a_final_done(mock_audit, monkeypatch):
    results = iter([
        cc.MoveResult(completed=1, status_code=0x0000),  # study 1: success
        cc.ConquestUnreachable("destination host down"),  # study 2: raises -> failure
    ])

    def fake_move(**kwargs):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(cc, "move_study", Mock(side_effect=fake_move))

    batch = _new_batch([
        {"study_instance_uid": "1.2.3", "patient_id": "ANON1", "label": "ANON1 — Planning CT"},
        {"study_instance_uid": "1.2.4", "patient_id": "ANON2", "label": "ANON2 — Planning CT"},
    ])
    local_pacs._BATCHES["batch-1"] = batch

    await local_pacs._run_batch("batch-1")
    events = await _drain(batch)

    types = [e["type"] for e in events]
    assert types == ["start", "progress", "success", "progress", "error", "done"]
    assert events[0]["total"] == 2
    assert events[2]["study"] == "ANON1 — Planning CT"
    assert events[4]["study"] == "ANON2 — Planning CT"
    assert "destination host down" in events[4]["error"]
    assert mock_audit.call_count == 2


async def test_audit_failure_is_a_warning_not_a_stopped_batch(monkeypatch):
    monkeypatch.setattr(cc, "move_study", Mock(return_value=cc.MoveResult(completed=1, status_code=0x0000)))
    audit_mock = AsyncMock(side_effect=backend_client.BackendError(503, "HermesDB unreachable"))
    monkeypatch.setattr(backend_client, "audit_local_pacs_move", audit_mock)

    batch = _new_batch([
        {"study_instance_uid": "1.2.3", "patient_id": "ANON1", "label": "ANON1 — Planning CT"},
        {"study_instance_uid": "1.2.4", "patient_id": "ANON2", "label": "ANON2 — Planning CT"},
    ])
    local_pacs._BATCHES["batch-2"] = batch

    await local_pacs._run_batch("batch-2")
    events = await _drain(batch)

    success_events = [e for e in events if e["type"] == "success"]
    assert len(success_events) == 2
    assert all("audit not recorded" in e["status"] for e in success_events)
    assert audit_mock.call_count == 2
    assert events[-1]["type"] == "done"


async def test_auditing_happens_once_per_study(mock_audit, monkeypatch):
    monkeypatch.setattr(cc, "move_study", Mock(return_value=cc.MoveResult(completed=1, status_code=0x0000)))

    items = [{"study_instance_uid": f"1.2.{i}", "patient_id": f"ANON{i}", "label": f"ANON{i}"} for i in range(3)]
    batch = _new_batch(items)
    local_pacs._BATCHES["batch-3"] = batch

    await local_pacs._run_batch("batch-3")

    assert mock_audit.call_count == 3
    called_uids = {c.kwargs["study_instance_uid"] for c in mock_audit.call_args_list}
    assert called_uids == {"1.2.0", "1.2.1", "1.2.2"}
