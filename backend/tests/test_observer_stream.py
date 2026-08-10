"""
Tests for the queue-driven observer stream (backend/src/results/endpoints.py's
_observe_job / GET /results/job/{job_id}/stream) -- docs/worker-queue-design.md.

Doesn't need the PinnacleExport submodule: results/endpoints.py only imports
plans/db_client.py and identity/anon.py, never retrieve/logic.py.
"""
import json
import uuid

import pytest

from backend.src.common.sse import BatchItem
from backend.src.results import endpoints as results_endpoints
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB


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


@pytest.mark.asyncio
async def test_observe_job_with_no_tasks_completes_immediately(job_id):
    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    assert [e["type"] for e in parsed] == ["start", "done"]
    assert parsed[0]["total"] == 0


@pytest.mark.asyncio
async def test_observe_job_emits_cancelled_and_stops(tasks_db, status_db, job_id):
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})
    status_db.cancel_job(job_id)

    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    assert [e["type"] for e in parsed] == ["start", "cancelled", "done"]


@pytest.mark.asyncio
async def test_observe_job_full_success_and_failure_sequence(tasks_db, job_id):
    """
    Drives two tasks through claim -> running -> succeeded/failed
    concurrently with the observer consuming events, confirming the exact
    vocabulary templates/cotton/job_progress.html listens for is produced,
    with display_id (never the real id) in every payload.
    """
    import asyncio

    items = [
        BatchItem(real_id="R1", display_id="A1", status_mrn="R1"),
        BatchItem(real_id="R2", display_id="A2", status_mrn="R2"),
    ]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})

    events: list[str] = []

    async def _collect():
        async for chunk in results_endpoints._observe_job(job_id):
            events.append(chunk)

    collector = asyncio.create_task(_collect())
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


@pytest.mark.asyncio
async def test_observe_job_only_emits_each_transition_once(tasks_db, job_id):
    """A state that hasn't changed since the last tick must not be re-emitted."""
    import asyncio

    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]
    tasks_db.enqueue(job_id, items, kind="import", stage="retrieve", params={})

    events: list[str] = []

    async def _collect():
        async for chunk in results_endpoints._observe_job(job_id):
            events.append(chunk)

    collector = asyncio.create_task(_collect())
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
