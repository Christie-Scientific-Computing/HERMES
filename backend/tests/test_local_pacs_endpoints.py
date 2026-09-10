"""
Tests for F005 (backend/src/local_pacs/endpoints.py) -- the local-PACS move
audit-recording endpoint. Modeled on test_error_reports_endpoints.py's shape.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.identity import anon
from backend.src.local_pacs import endpoints as local_pacs_endpoints
from backend.src.local_pacs.db_client import SENTINEL_PROJECT_ID
from backend.src.projects.db_client import ProjectsDB
from backend.src.status.db_client import StatusDB


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(local_pacs_endpoints.router)
    return TestClient(app)


@pytest.fixture
def mrn():
    return f"mrn-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
def _default_passthrough_anon(monkeypatch):
    """Pins anon.resolve_real_id to identity passthrough regardless of
    whatever ANON_DB_* env state another test module in this same pytest
    session may have left globally set (several anon-boundary test files
    set os.environ["ANON_DB_HOST"] etc. at import time and deliberately
    never unset it, per identity/anon.py's own passthrough convention) --
    this endpoint's own anon-translation behaviour is exercised explicitly
    below (test_anon_id_is_translated_before_storage,
    test_unknown_anon_id_fails_closed_with_422), not incidentally by
    whatever order pytest happens to collect files in."""
    monkeypatch.setattr(anon, "resolve_real_id", lambda anon_id: anon_id)


def _body(mrn, **overrides):
    body = {
        "anon_patient_id": mrn,
        "study_instance_uid": "1.2.3.4.5",
        "destination_ae_title": "THIRDPARTY",
        "destination_display_name": "Third Party PACS",
        "performed_by": "custodian1",
        "outcome": "success",
    }
    body.update(overrides)
    return body


def test_successful_move_is_recorded(client, mrn):
    resp = client.post("/local_pacs/audit_move", json=_body(mrn))

    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    job = StatusDB().get_job(job_id)
    assert job["project_id"] == SENTINEL_PROJECT_ID

    history = StatusDB().get_patient_history(job_id, mrn)
    assert len(history) == 1
    event = history[0]
    assert event["stage"] == "export"
    assert event["event_type"] == "success"
    assert event["details"]["destination"] == "THIRDPARTY"
    assert event["details"]["study_instance_uid"] == "1.2.3.4.5"
    assert event["row_hash"] is not None


def test_failed_move_is_recorded_as_a_failure_event(client, mrn):
    resp = client.post("/local_pacs/audit_move", json=_body(mrn, outcome="failure", detail="Destination unreachable"))

    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    history = StatusDB().get_patient_history(job_id, mrn)
    assert history[0]["event_type"] == "failure"
    assert history[0]["error_message"] == "Destination unreachable"


def test_sentinel_project_is_created_once_and_reused(client, mrn):
    client.post("/local_pacs/audit_move", json=_body(mrn))
    client.post("/local_pacs/audit_move", json=_body(f"{mrn}-2"))

    projects = [p for p in ProjectsDB().list_projects() if p["project_id"] == SENTINEL_PROJECT_ID]
    assert len(projects) == 1
    assert projects[0]["status"] == "approved"


def test_anon_id_is_translated_before_storage(client, mrn, monkeypatch):
    monkeypatch.setattr(anon, "resolve_real_id", lambda anon_id: f"real-{anon_id}")

    resp = client.post("/local_pacs/audit_move", json=_body("anon-123"))

    job_id = resp.json()["job_id"]
    history = StatusDB().get_patient_history(job_id, "real-anon-123")
    assert len(history) == 1


def test_unknown_anon_id_fails_closed_with_422(client, monkeypatch):
    def _raise(anon_id):
        raise anon.AnonLookupError(f"No mapping for {anon_id!r}")

    monkeypatch.setattr(anon, "resolve_real_id", _raise)
    resp = client.post("/local_pacs/audit_move", json=_body("unknown-id"))

    assert resp.status_code == 422
