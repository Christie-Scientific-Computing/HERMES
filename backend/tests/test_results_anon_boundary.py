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

from backend.src.db import get_conn
from backend.src.identity import anon
from backend.src.plans.db_client import PINNACLE_SCHEMA
from backend.src.results.endpoints import router as results_router, status_db

REAL_MRN = "500123"
ANON_MRN = "1001"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(results_router)
    return TestClient(app)


def _drop_plans_schema():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {PINNACLE_SCHEMA} CASCADE")


@pytest.fixture
def plans_schema():
    """PinnacleExport owns this schema; HERMES has no migration for it, so the
    test creates it (matching PinnacleExport's own migration) and tears it down."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {PINNACLE_SCHEMA}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PINNACLE_SCHEMA}.plans (
                id SERIAL PRIMARY KEY,
                mrn TEXT NOT NULL,
                path TEXT NOT NULL,
                plan_id INTEGER NOT NULL,
                plan_name TEXT NOT NULL,
                plan_date DATE,
                primary_image_set INTEGER,
                pinnacle_version TEXT,
                comment TEXT,
                status TEXT NOT NULL,
                error_message TEXT
            )
            """
        )
    yield
    _drop_plans_schema()


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


def test_job_summary_includes_job_metadata(client, job_id):
    status_db.create_job(job_id, description="a batch", created_by="alice", project_id=None)

    resp = client.get(f"/results/job/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["description"] == "a batch"
    assert body["created_by"] == "alice"
    assert body["project_id"] is None
    assert body["cancelled"] is False


def test_job_summary_includes_imported_and_submitted_counts(client, job_id):
    """The "N/M imported" headline figure, end-to-end through /results/job/{job_id}."""
    status_db.create_job(job_id)
    status_db.add_patient(job_id, "MRN1")
    status_db.add_patient(job_id, "MRN2")
    status_db.add_patient(job_id, "MRN3")
    status_db.add_event(job_id, "MRN1", stage="retrieve", event_type="success", details={"imported": True})
    status_db.add_event(job_id, "MRN2", stage="retrieve", event_type="success", details={"imported": False})
    status_db.add_event(job_id, "MRN3", stage="retrieve", event_type="failure", error_message="boom")

    resp = client.get(f"/results/job/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported_count"] == 1
    assert body["submitted_count"] == 3


def test_job_patients_summary_includes_source_presence_and_anon_id(client, job_id):
    status_db.create_job(job_id)
    status_db.add_event(
        job_id, REAL_MRN, stage="retrieve", event_type="success",
        details={"in_mosaiq": True, "in_pinnacle": False, "in_proknow": True, "status": "imported"},
    )

    resp = client.get(f"/results/job/{job_id}/patients/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["patients"] == [{
        "mrn": ANON_MRN, "in_mosaiq": True, "in_pinnacle": False, "in_proknow": True, "status": "imported",
        "outcome": "success", "error_message": None,
        "mosaiq_reason": None, "pinnacle_reason": None, "proknow_reason": None,
    }]
    assert REAL_MRN not in resp.text


def test_job_patients_summary_scrubs_the_real_mrn_out_of_reason_fields(client, job_id):
    """
    THE anonymisation-boundary regression test for §E. mosaiq_reason/
    pinnacle_reason/proknow_reason come straight out of a worker's own
    return value (Importer.find_patient), not a structured column -- exactly
    like error_message, they routinely quote the real MRN (Mosaiq/ProKnow
    exception text; Pinnacle's error_message, built from/quoting the mrn).
    Reading them unscrubbed would leak a real patient identifier straight
    across the anonymisation boundary -- precisely the failure mode this
    whole plan exists to prevent. Every one of the three must come back with
    the anon id substituted in, and the real MRN must not appear anywhere in
    the raw response body.
    """
    status_db.create_job(job_id)
    status_db.add_event(
        job_id, REAL_MRN, stage="retrieve", event_type="success",
        details={
            "in_mosaiq": False,
            "mosaiq_reason": f"Could not query AE_ONE: connection refused for patient {REAL_MRN}",
            "in_pinnacle": True,
            "pinnacle_reason": f"Could not reconstruct DICOM: no RTSTRUCT for {REAL_MRN} at /pinnacle/{REAL_MRN}/Plan_1",
            "in_proknow": False,
            "proknow_reason": "Patient not found on ProKnow",
        },
    )

    resp = client.get(f"/results/job/{job_id}/patients/summary")
    assert resp.status_code == 200
    patient = resp.json()["patients"][0]

    assert patient["mosaiq_reason"] == f"Could not query AE_ONE: connection refused for patient {ANON_MRN}"
    assert patient["pinnacle_reason"] == (
        f"Could not reconstruct DICOM: no RTSTRUCT for {ANON_MRN} at /pinnacle/{ANON_MRN}/Plan_1"
    )
    assert patient["proknow_reason"] == "Patient not found on ProKnow"

    # The load-bearing assertion: the real MRN must not appear ANYWHERE in
    # the raw response body, not just in the fields we happened to check above.
    assert REAL_MRN not in resp.text


def test_job_patients_summary_surfaces_failure_only_patients(client, job_id):
    """
    Source presence comes only from successful retrieves, so a patient that
    only ever failed has null presence -- but must still report its failure,
    or it's indistinguishable from an export-only patient in the UI.
    """
    status_db.create_job(job_id)
    status_db.add_event(job_id, REAL_MRN, stage="retrieve", event_type="start")
    status_db.add_event(
        job_id, REAL_MRN, stage="retrieve", event_type="failure",
        error_message=f"Pinnacle export failed for {REAL_MRN}",
    )

    resp = client.get(f"/results/job/{job_id}/patients/summary")
    assert resp.status_code == 200
    patient = resp.json()["patients"][0]
    assert patient["outcome"] == "failure"
    assert patient["in_mosaiq"] is None
    # the error text quoted the real id -- it must come back anonymised
    assert patient["error_message"] == f"Pinnacle export failed for {ANON_MRN}"
    assert REAL_MRN not in resp.text


def test_job_patients_summary_null_for_export_only_patient(client, job_id):
    status_db.create_job(job_id)
    status_db.add_event(job_id, REAL_MRN, stage="export", event_type="success", details={"status": "exported"})

    resp = client.get(f"/results/job/{job_id}/patients/summary")
    assert resp.status_code == 200
    patient = resp.json()["patients"][0]
    assert patient["mrn"] == ANON_MRN
    assert patient["in_mosaiq"] is None
    assert patient["in_pinnacle"] is None
    assert patient["in_proknow"] is None


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


def test_timeline_scrubs_the_real_mrn_out_of_error_message_and_details(client, job_id):
    """
    Translating only the structured `mrn` column isn't enough: error_message is
    str(exception) from a worker and routinely quotes the MRN, and details is
    whatever the worker returned. Both must be scrubbed too.
    """
    status_db.create_job(job_id)
    status_db.add_event(
        job_id, REAL_MRN, stage="retrieve", event_type="failure",
        error_message=f"no studies found for {REAL_MRN}",
        details={"searched": [f"mosaiq:{REAL_MRN}"], "nested": {"id": REAL_MRN}},
    )

    resp = client.get(f"/results/patient/{job_id}/{ANON_MRN}")
    assert resp.status_code == 200
    event = resp.json()["events"][0]

    assert event["error_message"] == f"no studies found for {ANON_MRN}"
    assert event["details"]["searched"] == [f"mosaiq:{ANON_MRN}"]
    assert event["details"]["nested"]["id"] == ANON_MRN
    assert REAL_MRN not in resp.text


def test_plans_scrub_the_real_mrn_out_of_path_comment_and_error(client, plans_schema):
    """
    Plan rows have no patient-id column, so nothing gets *translated* here --
    but `path` is built from the MRN and `comment`/`error_message` quote it.
    Those three free-text fields are the only way a real id could cross this
    boundary, on precisely the page built for reading error text.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {PINNACLE_SCHEMA}.plans
                (mrn, path, plan_id, plan_name, plan_date, status, comment, error_message)
            VALUES (%s, %s, %s, %s, NULL, %s, %s, %s)
            """,
            (
                REAL_MRN,
                f"/pinnacle/patients/{REAL_MRN}/Plan_1",
                1,
                "Prostate",
                "failed",
                f"re-run for {REAL_MRN}",
                f"RTSTRUCT missing for patient {REAL_MRN}",
            ),
        )

    resp = client.get(f"/results/patient/{ANON_MRN}/plans")
    assert resp.status_code == 200
    body = resp.json()

    assert body["available"] is True
    plan = body["plans"][0]
    assert plan["path"] == f"/pinnacle/patients/{ANON_MRN}/Plan_1"
    assert plan["comment"] == f"re-run for {ANON_MRN}"
    assert plan["error_message"] == f"RTSTRUCT missing for patient {ANON_MRN}"
    assert plan["plan_name"] == "Prostate"  # untouched
    assert REAL_MRN not in resp.text


def test_plans_unavailable_when_pinnacle_schema_absent(client):
    """HERMES must work against a database PinnacleExport never touched."""
    _drop_plans_schema()

    resp = client.get(f"/results/patient/{ANON_MRN}/plans")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["plans"] == []


def test_plans_unknown_anon_id_returns_422(client):
    resp = client.get("/results/patient/999999999/plans")
    assert resp.status_code == 422


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
