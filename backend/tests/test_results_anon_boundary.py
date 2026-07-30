"""
Confirms the outbound anon boundary in results/endpoints.py: real MRNs
stored in StatusDB never appear in an HTTP response once anonymisation
is configured -- only the anon id (1001, mapped to real id 500123 in the
seeded key_value test data) should ever cross the boundary.
"""
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

from backend.src.identity import anon
from backend.src.results.endpoints import router as results_router, status_db

REAL_MRN = "500123"
ANON_MRN = "1001"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(results_router)
    return TestClient(app)


@pytest.fixture
def job_id():
    return f"anon-boundary-{uuid.uuid4()}"


def test_job_patients_returns_anon_id_never_real(client, job_id):
    status_db.create_job(job_id)
    status_db.add_event(job_id, REAL_MRN, stage="retrieve", event_type="success")

    resp = client.get(f"/results/job/{job_id}/patients")
    assert resp.status_code == 200
    body = resp.json()
    assert body["patients"] == [ANON_MRN]
    assert REAL_MRN not in resp.text


def test_patient_timeline_translates_inbound_and_outbound(client, job_id):
    status_db.create_job(job_id)
    status_db.add_event(job_id, REAL_MRN, stage="retrieve", event_type="start")
    status_db.add_event(job_id, REAL_MRN, stage="retrieve", event_type="success", details={"in_mosaiq": True})

    # caller submits the ANON id in the path -- backend must resolve it internally
    resp = client.get(f"/results/patient/{job_id}/{ANON_MRN}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["mrn"] == ANON_MRN
    assert [e["mrn"] for e in body["events"]] == [ANON_MRN, ANON_MRN]
    assert REAL_MRN not in resp.text  # the real id must never appear in the response body


def test_patient_timeline_all_jobs_boundary(client, job_id):
    status_db.create_job(job_id)
    status_db.add_event(job_id, REAL_MRN, stage="export", event_type="success")

    resp = client.get(f"/results/patient/timeline/{ANON_MRN}/all")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mrn"] == ANON_MRN
    assert all(e["mrn"] == ANON_MRN for e in body["events"])
    assert REAL_MRN not in resp.text


def test_unknown_anon_id_returns_422(client, job_id):
    resp = client.get(f"/results/patient/{job_id}/999999999")
    assert resp.status_code == 422


def test_anon_db_unreachable_returns_503_not_bare_500(client, job_id, monkeypatch):
    """If the anon-mapping DB itself is down, callers must get a clean 503
    with a detail message, not FastAPI's generic no-detail 500."""
    monkeypatch.setattr(anon, "ANON_DB_PORT", 1)
    monkeypatch.setattr(anon, "_pool", None)
    try:
        resp = client.get(f"/results/patient/{job_id}/{ANON_MRN}")
        assert resp.status_code == 503
        assert resp.json()["detail"]  # non-empty detail, not a bare error page
    finally:
        monkeypatch.setattr(anon, "_pool", None)
