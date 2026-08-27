"""
Tests for the queue-driven observer stream (backend/src/results/endpoints.py's
_observe_job / GET /results/job/{job_id}/stream) -- docs/worker-queue-design.md.

Doesn't need the PinnacleExport submodule: results/endpoints.py only imports
plans/db_client.py and identity/anon.py, never retrieve/logic.py.
"""
import asyncio
import json
import os
import uuid

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import pytest

from backend.src.common.sse import BatchItem
from backend.src.results import endpoints as results_endpoints
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB

REAL_MRN = "500123"
ANON_MRN = "1001"  # seeded in the anon_test DB -- see test_anon.py's header


@pytest.fixture
def tasks_db():
    return TasksDB()


@pytest.fixture
def status_db():
    return StatusDB()


@pytest.fixture
def job_id(status_db):
    job_id = f"observer-test-{uuid.uuid4()}"
    status_db.create_job(job_id, description="observer test")
    return job_id


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """The real default (1s) would make every test here slow; tests don't
    care about the actual cadence, only that transitions are eventually
    observed."""
    monkeypatch.setattr(results_endpoints, "_OBSERVER_POLL_INTERVAL", 0.01)


def _parse_events(chunks: list[str]) -> list[dict]:
    return [json.loads(c[len("data: "):]) for c in chunks if c.startswith("data: ")]


async def _collect_into(job_id: str, sink: list[str]) -> None:
    async for chunk in results_endpoints._observe_job(job_id):
        sink.append(chunk)


@pytest.mark.asyncio
async def test_observe_job_with_no_tasks_completes_immediately(job_id):
    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    assert [e["type"] for e in parsed] == ["start", "done"]
    assert parsed[0]["total"] == 0


@pytest.mark.asyncio
async def test_observe_job_emits_cancelled_and_stops_once_nothing_pending(tasks_db, status_db, job_id):
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})
    status_db.cancel_job(job_id)
    tasks_db.cancel_queued(job_id)  # mirrors what the real cancel_import endpoint does

    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    assert [e["type"] for e in parsed] == ["start", "cancelled", "done"]


@pytest.mark.asyncio
async def test_observe_job_reports_in_flight_outcome_after_cancellation(tasks_db, status_db, job_id):
    """
    cancel_import only flips still-queued tasks; a task already
    claimed/running when cancellation happens is not interrupted and will
    still complete -- "items already in progress finish" is the same
    promise run_batch_job's own cancellation already makes. The observer
    must keep reporting real outcomes for such tasks rather than going
    quiet the moment it sees jobs.cancelled.
    """
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"], "worker-1")

    status_db.cancel_job(job_id)
    tasks_db.cancel_queued(job_id)  # no-op here (nothing left queued), same call the real endpoint makes

    events: list[str] = []
    collector = asyncio.create_task(_collect_into(job_id, events))
    await asyncio.sleep(0.05)  # let the observer notice "cancelled" while the task is still running

    tasks_db.mark_succeeded(task["task_id"], "worker-1", details={"in_mosaiq": True})
    await asyncio.wait_for(collector, timeout=5)

    parsed = _parse_events(events)
    types = [e["type"] for e in parsed]
    assert "cancelled" in types
    assert "success" in types
    assert types[-1] == "done"
    success_event = next(e for e in parsed if e["type"] == "success")
    assert success_event["mrn"] == "A1"
    assert success_event["in_mosaiq"] is True


@pytest.mark.asyncio
async def test_observe_job_full_success_and_failure_sequence(tasks_db, job_id):
    """
    Drives two tasks through claim -> running -> succeeded/failed
    concurrently with the observer consuming events, confirming the exact
    vocabulary templates/cotton/job_progress.html listens for is produced,
    with display_id (never the real id) in every payload.
    """
    items = [
        BatchItem(real_id="R1", display_id="A1", status_mrn="R1"),
        BatchItem(real_id="R2", display_id="A2", status_mrn="R2"),
    ]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})

    events: list[str] = []
    collector = asyncio.create_task(_collect_into(job_id, events))
    await asyncio.sleep(0.05)  # let the first (empty) tick pass

    t1 = tasks_db.claim("worker-1")
    tasks_db.mark_running(t1["task_id"], "worker-1")
    await asyncio.sleep(0.05)
    tasks_db.mark_succeeded(t1["task_id"], "worker-1", details={"in_mosaiq": True})
    await asyncio.sleep(0.05)

    t2 = tasks_db.claim("worker-1")
    tasks_db.mark_running(t2["task_id"], "worker-1")
    await asyncio.sleep(0.05)
    tasks_db.mark_failed(t2["task_id"], "worker-1", "boom")
    await asyncio.sleep(0.05)

    await asyncio.wait_for(collector, timeout=5)

    parsed = _parse_events(events)
    types = [e["type"] for e in parsed]
    assert types[0] == "start"
    assert parsed[0]["total"] == 2
    assert types[-1] == "done"
    assert "progress" in types
    assert "success" in types
    assert "error" in types

    success_event = next(e for e in parsed if e["type"] == "success")
    assert success_event["mrn"] == "A1"  # display_id, never real_id ("R1")
    assert success_event["in_mosaiq"] is True

    error_event = next(e for e in parsed if e["type"] == "error")
    assert error_event["mrn"] == "A2"
    assert error_event["error"] == "boom"

    # the real ids must never appear anywhere in the emitted stream
    raw = "".join(events)
    assert "R1" not in raw
    assert "R2" not in raw

    # every progress/success/error event carries the task's own stage
    assert all(e["stage"] == "retrieve" for e in parsed if e["type"] in ("progress", "success", "error"))


@pytest.mark.asyncio
async def test_observe_job_reports_export_stage_on_events(tasks_db, job_id):
    """A plain export-stage task's events must be tagged stage='export', not
    left over from whatever the default happened to be for import."""
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="dicom_move", stage="export", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"], "worker-1")
    tasks_db.mark_succeeded(task["task_id"], "worker-1", details={})

    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    success_event = next(e for e in parsed if e["type"] == "success")
    assert success_event["stage"] == "export"


@pytest.mark.asyncio
async def test_observe_job_emits_total_event_when_task_count_grows(tasks_db, job_id):
    """
    Simulates a combined import->export job: a second task (the chained
    export, from backend/worker.py's _maybe_chain_export) is enqueued onto
    the same job_id mid-stream. The observer's initial `total` only ever
    covers what existed when the stream opened -- a new `total` event must
    fire once the second task shows up, and it must fire exactly once (not
    once per poll tick) for the one actual change in count.
    """
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})

    events: list[str] = []
    collector = asyncio.create_task(_collect_into(job_id, events))
    await asyncio.sleep(0.05)  # let a couple of ticks pass with just 1 task

    # simulate the chained export task appearing mid-stream
    tasks_db.enqueue(
        job_id, [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")],
        kind="dicom_move", stage="export", params={},
    )
    await asyncio.sleep(0.05)  # let several more ticks pass with 2 tasks, unchanged

    t1 = tasks_db.claim("worker-1")
    tasks_db.mark_running(t1["task_id"], "worker-1")
    tasks_db.mark_succeeded(t1["task_id"], "worker-1", details={})
    t2 = tasks_db.claim("worker-1")
    tasks_db.mark_running(t2["task_id"], "worker-1")
    tasks_db.mark_succeeded(t2["task_id"], "worker-1", details={})
    await asyncio.sleep(0.05)

    await asyncio.wait_for(collector, timeout=5)

    parsed = _parse_events(events)
    total_events = [e for e in parsed if e["type"] == "total"]
    assert [e["total"] for e in total_events] == [2]  # exactly one change, 1 -> 2
    assert total_events[0]["import_total"] == 1
    assert total_events[0]["export_total"] == 1
    assert parsed[0]["type"] == "start"
    assert parsed[0]["total"] == 1  # only the import task existed when the stream opened
    assert parsed[0]["import_total"] == 1
    assert parsed[0]["export_total"] == 0


@pytest.mark.asyncio
async def test_observe_job_total_split_survives_a_fresh_connection_after_chaining(tasks_db, job_id):
    """
    A second connection to the same job_id (a page refresh, a colleague
    joining, or EventSource's own auto-reconnect) must report the SAME
    import_total/export_total split as the first connection would at that
    point -- it must not re-derive import_total from whatever total happens
    to be at the moment the NEW connection opens, which would silently
    misattribute already-chained export tasks to the import count.
    """
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})
    tasks_db.enqueue(
        job_id, [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")],
        kind="dicom_move", stage="export", params={},
    )

    # Both tasks are left 'queued' -- only the `start` event (the first
    # yielded chunk) is under test here, so there's no need to resolve them
    # and let the generator run to `done`.
    observer = results_endpoints._observe_job(job_id)
    first_chunk = await anext(observer)
    await observer.aclose()

    parsed = _parse_events([first_chunk])
    start_event = parsed[0]
    assert start_event["total"] == 2
    assert start_event["import_total"] == 1
    assert start_event["export_total"] == 1  # not 0, and not folded into import_total


@pytest.mark.asyncio
async def test_observe_job_completes_despite_a_task_queued_after_cancel_swept(tasks_db, status_db, job_id):
    """
    Regression test for a TOCTOU in backend/worker.py's _maybe_chain_export:
    its is_cancelled check and its enqueue call are two separate
    transactions, so a chained export can land 'queued' in the narrow
    window AFTER cancel_import's TasksDB.cancel_queued sweep already ran.
    TasksDB.claim excludes cancelled jobs, so that row can never actually
    run -- but before this fix, _observe_job's has_pending still counted
    'queued' as pending unconditionally, so it would poll forever and never
    emit `done`. This simulates exactly that leftover row (enqueued after
    cancellation, never swept) and asserts the stream still terminates.
    """
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})
    status_db.cancel_job(job_id)
    tasks_db.cancel_queued(job_id)  # sweeps the import task -- runs BEFORE the row below exists

    # The leftover row: enqueued after the sweep, exactly like a chained
    # export task landing in _maybe_chain_export's TOCTOU window.
    tasks_db.enqueue(
        job_id, [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")],
        kind="dicom_move", stage="export", params={},
    )

    events = await asyncio.wait_for(
        _collect_all(job_id), timeout=5,  # would hang forever before this fix
    )
    parsed = _parse_events(events)
    assert parsed[-1]["type"] == "done"
    assert "cancelled" in [e["type"] for e in parsed]

    # The stuck row itself is untouched -- still queued, never claimable
    # (TasksDB.claim excludes cancelled jobs), simply not reported as
    # pending any more.
    stuck = [r for r in tasks_db.job_progress(job_id) if r["kind"] == "dicom_move"]
    assert stuck[0]["state"] == "queued"


async def _collect_all(job_id: str) -> list[str]:
    return [chunk async for chunk in results_endpoints._observe_job(job_id)]


@pytest.mark.asyncio
async def test_observe_job_scrubs_real_mrn_from_error_and_details(tasks_db, job_id):
    """
    error_message/details are worker-generated free text and routinely
    quote the real id (CLAUDE.md's anonymisation-boundary section calls
    this out explicitly) -- with anonymisation configured (see this file's
    module-level ANON_DB_* setup), both must have the real id scrubbed to
    the display id before crossing the SSE boundary, the same way
    job_patients_summary's mosaiq_reason/etc. already are.
    """
    items = [
        BatchItem(real_id=REAL_MRN, display_id=ANON_MRN, status_mrn=REAL_MRN),
    ]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"], "worker-1")
    tasks_db.mark_failed(
        task["task_id"], "worker-1",
        f"Could not query Mosaiq for patient {REAL_MRN}: connection refused",
    )

    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    error_event = next(e for e in parsed if e["type"] == "error")
    assert REAL_MRN not in error_event["error"]
    assert ANON_MRN in error_event["error"]

    raw = "".join(events)
    assert REAL_MRN not in raw


@pytest.mark.asyncio
async def test_observe_job_scrubs_real_mrn_from_success_details(tasks_db, job_id):
    items = [BatchItem(real_id=REAL_MRN, display_id=ANON_MRN, status_mrn=REAL_MRN)]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"], "worker-1")
    tasks_db.mark_succeeded(
        task["task_id"], "worker-1",
        details={"mosaiq_reason": f"Incomplete planning data for {REAL_MRN}"},
    )

    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    success_event = next(e for e in parsed if e["type"] == "success")
    assert REAL_MRN not in success_event["mosaiq_reason"]
    assert ANON_MRN in success_event["mosaiq_reason"]


@pytest.mark.asyncio
async def test_observe_job_preserves_date_shaped_destination_in_success_details(tasks_db, job_id):
    """
    This is the LIVE production path (backend/worker.py -> tasks.details ->
    _observe_job -> the SSE stream frontend/'s job_stream relays for every
    real CSV-upload export job) -- destination/destination_type/
    submitted_by are operational config (an Orthanc AE title, a ProKnow
    collection name, a username), never patient data, so _scrub_json must
    not let the generic date/UID pattern floor mangle one that happens to
    look date-shaped, the same protection redact_dict's default `exclude`
    gives the synchronous run_batch_job path.
    """
    items = [BatchItem(real_id=REAL_MRN, display_id=ANON_MRN, status_mrn=REAL_MRN)]
    tasks_db.enqueue(job_id, items, kind="proknow_upload", stage="export", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"], "worker-1")
    tasks_db.mark_succeeded(
        task["task_id"], "worker-1",
        details={
            "status": "Success", "destination": "Trial_2024-01-15_Cohort",
            "destination_type": "proknow_collection", "submitted_by": "alice",
        },
    )

    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    success_event = next(e for e in parsed if e["type"] == "success")
    assert success_event["destination"] == "Trial_2024-01-15_Cohort"
    assert success_event["destination_type"] == "proknow_collection"
    assert success_event["submitted_by"] == "alice"


@pytest.mark.asyncio
async def test_observe_job_only_emits_each_transition_once(tasks_db, job_id):
    """A state that hasn't changed since the last tick must not be re-emitted."""
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})

    events: list[str] = []
    collector = asyncio.create_task(_collect_into(job_id, events))
    await asyncio.sleep(0.03)

    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"], "worker-1")
    await asyncio.sleep(0.1)  # several poll ticks pass while still "running"
    tasks_db.mark_succeeded(task["task_id"], "worker-1", details={})
    await asyncio.sleep(0.05)

    await asyncio.wait_for(collector, timeout=5)

    parsed = _parse_events(events)
    progress_events = [e for e in parsed if e["type"] == "progress"]
    success_events = [e for e in parsed if e["type"] == "success"]
    assert len(progress_events) == 1  # not once per poll tick
    assert len(success_events) == 1


@pytest.mark.asyncio
async def test_observe_job_reap_and_reclaim_does_not_orphan_a_progress_line(tasks_db, job_id):
    """
    Regression test: TasksDB.reap_stale_claims can put a task that's
    genuinely still running (just slower than HERMES_TASK_STALE_SECONDS,
    not a dead worker) back to 'queued', where a second worker reclaims and
    re-runs it -- the same task_id passes through 'running' twice. Before
    this fix, _observe_job treated each transition into 'running' as its
    own event, so the frontend's (append-only) event log would gain a
    second "Importing X…" line while the first one -- from the reaped
    attempt -- never got a matching success/error and sat there looking
    stuck even once the job finished. Exactly one task_id must still
    produce exactly one progress line and exactly one resolving line.
    """
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})

    events: list[str] = []
    collector = asyncio.create_task(_collect_into(job_id, events))
    await asyncio.sleep(0.03)

    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"], "worker-1")
    await asyncio.sleep(0.03)  # let the observer see it running once

    # Simulate the reap: worker-1 is still genuinely working on it, but
    # reap_stale_claims(0) treats it as stale regardless (mirrors a task
    # that simply outran HERMES_TASK_STALE_SECONDS).
    reaped = tasks_db.reap_stale_claims(0)
    assert reaped == 1
    await asyncio.sleep(0.03)  # observer sees the row back to 'queued'

    # A second worker reclaims and re-runs the SAME task_id.
    reclaimed = tasks_db.claim("worker-2")
    assert reclaimed["task_id"] == task["task_id"]
    tasks_db.mark_running(reclaimed["task_id"], "worker-2")
    await asyncio.sleep(0.03)  # observer sees it running again

    tasks_db.mark_succeeded(reclaimed["task_id"], "worker-2", details={})
    await asyncio.wait_for(collector, timeout=5)

    parsed = _parse_events(events)
    progress_events = [e for e in parsed if e["type"] == "progress"]
    success_events = [e for e in parsed if e["type"] == "success"]
    assert len(progress_events) == 1  # not once per running-phase
    assert len(success_events) == 1
    assert parsed[-1]["type"] == "done"


def test_job_stream_endpoint_streams_event_stream_content(job_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(results_endpoints.router)
    client = TestClient(app)

    with client.stream("GET", f"/results/job/{job_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_bytes())

    lines = body.decode().splitlines()
    parsed = _parse_events(lines)
    assert [e["type"] for e in parsed] == ["start", "done"]
