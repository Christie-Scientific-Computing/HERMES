import json
import uuid

import pytest

from backend.src.common.sse import BatchItem, run_batch_job
from backend.src.status.db_client import StatusDB


@pytest.fixture
def db():
    return StatusDB()


@pytest.fixture
def job_id():
    return f"sse-test-{uuid.uuid4()}"


def _parse_events(lines: list[str]) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in lines if line.startswith("data: ")]


async def _collect(gen):
    return [chunk async for chunk in gen]


@pytest.mark.asyncio
async def test_success_and_failure_events_all_carry_type(db, job_id):
    items = [
        BatchItem(real_id="R1", display_id="A1", status_mrn="R1"),
        BatchItem(real_id="R2", display_id="A2", status_mrn="R2"),
    ]

    def worker(item: BatchItem) -> dict:
        if item.real_id == "R2":
            raise ValueError("boom")
        return {"in_mosaiq": True}

    chunks = await _collect(run_batch_job(job_id, items, stage="retrieve", worker=worker, status_db=db, description="test"))
    events = _parse_events(chunks)

    assert [e["type"] for e in events] == ["start", "progress", "success", "progress", "error", "done"]
    assert all("type" in e for e in events)  # the old {"done": true}-with-no-type bug is gone

    success_event = events[2]
    assert success_event["mrn"] == "A1"  # display_id, not real_id
    assert success_event["in_mosaiq"] is True

    error_event = events[4]
    assert error_event["mrn"] == "A2"
    assert error_event["error"] == "boom"

    # StatusDB rows use the real id (backend-internal storage may contain real IDs)
    history_r1 = db.get_patient_history(job_id, "R1")
    assert [e["event_type"] for e in history_r1] == ["start", "success"]
    history_r2 = db.get_patient_history(job_id, "R2")
    assert [e["event_type"] for e in history_r2] == ["start", "failure"]


@pytest.mark.asyncio
async def test_error_event_redacts_real_id_and_path_from_exception_message(db, job_id):
    items = [BatchItem(real_id="500123", display_id="1001", status_mrn="500123")]

    def worker(item: BatchItem) -> dict:
        raise ValueError(f"Could not read ./tmp/job1_500123.csv for patient {item.real_id}")

    chunks = await _collect(run_batch_job(job_id, items, stage="retrieve", worker=worker, status_db=db))
    events = _parse_events(chunks)
    error_event = next(e for e in events if e["type"] == "error")

    assert "500123" not in error_event["error"]
    assert "1001" in error_event["error"]  # real id -> display id substitution
    assert "./tmp/job1_500123.csv" not in error_event["error"]  # generic path floor

    # StatusDB keeps the raw, unredacted message for the audit trail.
    history = db.get_patient_history(job_id, "500123")
    failure = next(e for e in history if e["event_type"] == "failure")
    assert "500123" in failure["error_message"]


@pytest.mark.asyncio
async def test_success_event_redacts_free_text_fields_but_db_keeps_them_raw(db, job_id):
    items = [BatchItem(real_id="500123", display_id="1001", status_mrn="500123")]

    def worker(item: BatchItem) -> dict:
        return {
            "in_mosaiq": False,
            "mosaiq_reason": f"connection refused for patient {item.real_id}",
        }

    chunks = await _collect(run_batch_job(job_id, items, stage="retrieve", worker=worker, status_db=db))
    events = _parse_events(chunks)
    success_event = next(e for e in events if e["type"] == "success")

    assert "500123" not in success_event["mosaiq_reason"]
    assert success_event["mosaiq_reason"] == "connection refused for patient 1001"
    assert success_event["in_mosaiq"] is False

    history = db.get_patient_history(job_id, "500123")
    success = next(e for e in history if e["event_type"] == "success")
    assert "500123" in success["details"]["mosaiq_reason"]


@pytest.mark.asyncio
async def test_success_event_preserves_date_shaped_destination_field(db, job_id):
    # destination/destination_type/submitted_by (present on export worker
    # results) are operational config, not patient data -- redact_dict's
    # generic floor must not mangle a destination name that happens to
    # contain a date-shaped substring.
    items = [BatchItem(real_id="500123", display_id="1001", status_mrn="500123")]

    def worker(item: BatchItem) -> dict:
        return {"status": "Success", "destination": "Trial_2024-01-15_Cohort", "destination_type": "proknow_collection"}

    chunks = await _collect(run_batch_job(job_id, items, stage="export", worker=worker, status_db=db))
    events = _parse_events(chunks)
    success_event = next(e for e in events if e["type"] == "success")
    assert success_event["destination"] == "Trial_2024-01-15_Cohort"


@pytest.mark.asyncio
async def test_worker_cannot_override_display_id_via_res(db, job_id):
    items = [BatchItem(real_id="R1", display_id="A1", status_mrn="R1")]

    def worker(item: BatchItem) -> dict:
        # a buggy/malicious worker trying to smuggle the real id back out
        return {"mrn": "R1-leaked"}

    chunks = await _collect(run_batch_job(job_id, items, stage="retrieve", worker=worker, status_db=db))
    events = _parse_events(chunks)
    success_event = next(e for e in events if e["type"] == "success")
    assert success_event["mrn"] == "A1"


@pytest.mark.asyncio
async def test_cancellation_stops_remaining_items(db, job_id):
    items = [
        BatchItem(real_id="R1", display_id="A1", status_mrn="R1"),
        BatchItem(real_id="R2", display_id="A2", status_mrn="R2"),
        BatchItem(real_id="R3", display_id="A3", status_mrn="R3"),
    ]

    call_count = 0

    def worker(item: BatchItem) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            db.cancel_job(job_id)
        return {}

    chunks = await _collect(run_batch_job(job_id, items, stage="retrieve", worker=worker, status_db=db))
    events = _parse_events(chunks)

    assert call_count == 1  # only the first item's worker ran
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "cancelled" for e in events)
