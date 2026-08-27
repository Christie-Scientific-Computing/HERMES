"""
Tests for backend/src/common/sse.py's to_public_details -- the reshape that
drops real DICOM UIDs (study_uids/series_uids, and checksums' dict keys)
from every outbound success emission (docs/plans/pii-boundary-test-suite.md
decision 6 + §C), while leaving what's written to events.details/
tasks.details (the audit trail) at full fidelity.

test_export_manifest.py's end-to-end test already covers the synchronous
run_batch_job path for a DICOM C-MOVE batch; this file covers to_public_details
itself, the queue-driven observer path (_observe_job), and the two
single-item endpoints (proknow_upload_patient, single_import) that build
their own outbound dict independently of run_batch_job.
"""
import asyncio
import csv
import json
import os
import uuid

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.common.sse import to_public_details
from backend.src.results import endpoints as results_endpoints
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB
from backend.src.common.sse import BatchItem

REAL_MRN = "500123"
ANON_MRN = "1001"  # seeded in the anon_test DB -- see test_anon.py's header


# ---------------------------------------------------------------------------
# to_public_details unit tests
# ---------------------------------------------------------------------------

class TestToPublicDetails:
    def test_drops_study_uids_and_series_uids(self):
        result = to_public_details({"study_uids": ["1.2.3"], "series_uids": ["1.2.3.1"], "status": "Success"})
        assert "study_uids" not in result
        assert "series_uids" not in result
        assert result["status"] == "Success"

    def test_rekeys_checksums_dict_to_list(self):
        result = to_public_details({"checksums": {"1.2.3.1": "abc123", "1.2.3.2": "def456"}})
        assert set(result["checksums"]) == {"abc123", "def456"}
        assert isinstance(result["checksums"], list)

    def test_passthrough_for_dict_with_neither_key(self):
        result = to_public_details({"in_mosaiq": True, "status": "success"})
        assert result == {"in_mosaiq": True, "status": "success"}

    def test_none_input_returns_empty_dict(self):
        assert to_public_details(None) == {}

    def test_empty_dict_input_returns_empty_dict(self):
        assert to_public_details({}) == {}

    def test_does_not_mutate_the_input(self):
        original = {"study_uids": ["1.2.3"], "checksums": {"1.2.3.1": "abc"}}
        to_public_details(original)
        assert original["study_uids"] == ["1.2.3"]  # untouched -- events.details keeps full fidelity
        assert original["checksums"] == {"1.2.3.1": "abc"}

    def test_full_import_response_shape(self):
        # retrieve/endpoints.py's Response -- the same leak class as the
        # export manifest, found on the import side while wiring this up.
        result = to_public_details({
            "status": "success", "in_mosaiq": True, "imported": True,
            "study_count": 2, "study_uids": ["1.2.3", "1.2.4"],
        })
        assert "study_uids" not in result
        assert result["study_count"] == 2
        assert result["imported"] is True


# ---------------------------------------------------------------------------
# Queue-driven path: _observe_job reshapes tasks.details on the way out,
# while the raw DB row keeps full fidelity.
# ---------------------------------------------------------------------------

def _parse_events(chunks: list[str]) -> list[dict]:
    return [json.loads(c[len("data: "):]) for c in chunks if c.startswith("data: ")]


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(results_endpoints, "_OBSERVER_POLL_INTERVAL", 0.01)


@pytest.mark.asyncio
async def test_observe_job_strips_uids_from_sse_but_not_from_the_task_row():
    tasks_db = TasksDB()
    status_db = StatusDB()
    job_id = f"manifest-shape-{uuid.uuid4()}"
    status_db.create_job(job_id)

    items = [BatchItem(real_id=REAL_MRN, display_id=ANON_MRN, status_mrn=REAL_MRN)]
    tasks_db.enqueue(job_id, items, kind="dicom_move", stage="export", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"], "worker-1")

    raw_details = {
        "status": "Success", "series_count": 1, "instance_count": 1,
        "study_uids": ["1.2.840.study.shape"], "series_uids": ["1.2.840.series.shape"],
        "checksums": {"1.2.840.sop.shape": "cafebabe"},
    }
    tasks_db.mark_succeeded(task["task_id"], "worker-1", details=raw_details)

    events = [chunk async for chunk in results_endpoints._observe_job(job_id)]
    parsed = _parse_events(events)
    success = next(e for e in parsed if e["type"] == "success")

    assert "study_uids" not in success
    assert "series_uids" not in success
    assert success["checksums"] == ["cafebabe"]
    assert success["series_count"] == 1
    assert success["mrn"] == ANON_MRN

    # Raw DB row: full fidelity, untouched by the SSE-side reshape.
    db_row = tasks_db.get_task(task["task_id"])
    assert db_row["details"]["study_uids"] == ["1.2.840.study.shape"]
    assert db_row["details"]["series_uids"] == ["1.2.840.series.shape"]
    assert db_row["details"]["checksums"] == {"1.2.840.sop.shape": "cafebabe"}


# ---------------------------------------------------------------------------
# Single-item endpoints: proknow_upload_patient, single_import
# ---------------------------------------------------------------------------

@pytest.fixture
def export_client(monkeypatch):
    from backend.src.export import endpoints as export_endpoints

    class FakeExporter:
        def __init__(self, destination):
            self.destination = destination

        def upload_to_proknow(self, patient_id):
            return {
                "status": "Success", "series_count": 1, "instance_count": 1,
                "study_uids": ["1.2.840.study.pk"], "series_uids": ["1.2.840.series.pk"],
                "checksums": {"1.2.840.sop.pk": "beadfeed"},
            }

    monkeypatch.setattr(export_endpoints, "Exporter", FakeExporter)
    app = FastAPI()
    app.include_router(export_endpoints.router)
    return TestClient(app), export_endpoints


def test_proknow_upload_patient_strips_uids_but_db_keeps_them(export_client, active_project):
    client, export_endpoints = export_client
    project_id, username = active_project
    job_id = f"pk-shape-{uuid.uuid4()}"

    resp = client.post("/export/proknow_upload_patient", json={
        "job_id": job_id, "mrn": ANON_MRN, "collection": "SOME_COLLECTION",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "success"
    assert "study_uids" not in body
    assert "series_uids" not in body
    assert body["checksums"] == ["beadfeed"]
    assert REAL_MRN not in resp.text

    history = export_endpoints.status_db.get_patient_history(job_id, REAL_MRN)
    details = next(e for e in history if e["event_type"] == "success")["details"]
    assert details["study_uids"] == ["1.2.840.study.pk"]
    assert details["series_uids"] == ["1.2.840.series.pk"]
    assert details["checksums"] == {"1.2.840.sop.pk": "beadfeed"}


@pytest.fixture
def import_client(monkeypatch):
    from backend.src.retrieve import endpoints as retrieve_endpoints

    class FakeImporter:
        def __init__(self, import_level):
            self.import_level = import_level

        def handle_patient(self, mrn):
            return {
                "status": "success", "in_mosaiq": True, "imported": True,
                "study_count": 1, "study_uids": ["1.2.840.study.import"],
            }

    monkeypatch.setattr(retrieve_endpoints, "Importer", FakeImporter)
    app = FastAPI()
    app.include_router(retrieve_endpoints.router)
    return TestClient(app), retrieve_endpoints


def test_single_import_strips_study_uids_but_db_keeps_them(import_client, active_project):
    client, retrieve_endpoints = import_client
    project_id, username = active_project
    job_id = f"import-shape-{uuid.uuid4()}"

    resp = client.post("/import/single_import", json={
        "job_id": job_id, "mrn": ANON_MRN, "import_level": "Planning data",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "success"
    assert "study_uids" not in body
    assert body["imported"] is True
    assert REAL_MRN not in resp.text

    history = retrieve_endpoints.status_db.get_patient_history(job_id, REAL_MRN)
    details = next(e for e in history if e["event_type"] == "success")["details"]
    assert details["study_uids"] == ["1.2.840.study.import"]


# ---------------------------------------------------------------------------
# Patient timeline (_anonymize_events, results/endpoints.py): a fifth
# emission point found in review -- reads events.details straight from the
# DB and previously only ran it through _scrub_json (real-MRN substring
# substitution), which never touches a DICOM UID.
# ---------------------------------------------------------------------------

@pytest.fixture
def results_client():
    app = FastAPI()
    app.include_router(results_endpoints.router)
    return TestClient(app)


def _seed_export_success_event(job_id: str) -> None:
    status_db = StatusDB()
    status_db.create_job(job_id)
    status_db.add_patient(job_id, REAL_MRN)
    status_db.add_event(
        job_id, REAL_MRN, stage="export", event_type="success",
        details={
            "status": "Success", "series_count": 1, "instance_count": 1,
            "study_uids": ["1.2.840.study.timeline"], "series_uids": ["1.2.840.series.timeline"],
            "checksums": {"1.2.840.sop.timeline": "0ddba11"},
        },
    )


def test_patient_timeline_strips_uids_from_details_but_db_keeps_them(results_client):
    job_id = f"timeline-shape-{uuid.uuid4()}"
    _seed_export_success_event(job_id)

    resp = results_client.get(f"/results/patient/{job_id}/{ANON_MRN}")
    assert resp.status_code == 200
    body = resp.json()
    success_event = next(e for e in body["events"] if e.get("event_type") == "success")
    assert "study_uids" not in success_event["details"]
    assert "series_uids" not in success_event["details"]
    assert success_event["details"]["checksums"] == ["0ddba11"]
    assert REAL_MRN not in resp.text
    assert "1.2.840.study.timeline" not in resp.text
    assert "1.2.840.series.timeline" not in resp.text
    assert "1.2.840.sop.timeline" not in resp.text

    status_db = StatusDB()
    db_events = status_db.get_patient_history(job_id, REAL_MRN)
    db_details = next(e for e in db_events if e["event_type"] == "success")["details"]
    assert db_details["study_uids"] == ["1.2.840.study.timeline"]
    assert db_details["series_uids"] == ["1.2.840.series.timeline"]
    assert db_details["checksums"] == {"1.2.840.sop.timeline": "0ddba11"}


def test_patient_timeline_all_jobs_strips_uids(results_client):
    # REAL_MRN is a fixed id shared across many test files, and this
    # endpoint aggregates across every job for that mrn -- filter to this
    # test's own job_id rather than assuming the first "success" event is
    # the one just seeded.
    job_id = f"timeline-all-shape-{uuid.uuid4()}"
    _seed_export_success_event(job_id)

    resp = results_client.get(f"/results/patient/timeline/{ANON_MRN}/all")
    assert resp.status_code == 200
    body = resp.json()
    success_event = next(
        e for e in body["events"] if e.get("event_type") == "success" and e.get("job_id") == job_id
    )
    assert "study_uids" not in success_event["details"]
    assert "series_uids" not in success_event["details"]
    assert success_event["details"]["checksums"] == ["0ddba11"]


def test_patient_timeline_preserves_none_details_as_none(results_client):
    # A 'start' event has no details at all -- must stay None, not become
    # {} as a side effect of the UID-stripping fix.
    job_id = f"timeline-none-{uuid.uuid4()}"
    status_db = StatusDB()
    status_db.create_job(job_id)
    status_db.add_event(job_id, REAL_MRN, stage="retrieve", event_type="start")

    resp = results_client.get(f"/results/patient/{job_id}/{ANON_MRN}")
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    assert event["details"] is None
