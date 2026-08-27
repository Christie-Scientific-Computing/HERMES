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

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.db import get_conn
from backend.src.identity import anon
from backend.src.plans.db_client import PINNACLE_SCHEMA
from backend.src.results.endpoints import router as results_router, status_db

REAL_MRN = "500123"
ANON_MRN = "1001"


def _anon_conn():
    return psycopg2.connect(host="localhost", port=55433, dbname="anon_test", user="postgres", password="test")


@pytest.fixture(scope="module", autouse=True)
def _ensure_date_perturbation_column():
    conn = _anon_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE key_value ADD COLUMN IF NOT EXISTS date_perturbation INT")
    finally:
        conn.close()


@pytest.fixture
def perturbation():
    conn = _anon_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE key_value SET date_perturbation = %s WHERE key_value = %s AND key_type_id = 1",
                (10, int(REAL_MRN)),
            )
    finally:
        conn.close()
    yield 10
    conn = _anon_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE key_value SET date_perturbation = NULL WHERE key_value = %s AND key_type_id = 1",
                (int(REAL_MRN),),
            )
    finally:
        conn.close()


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
        "imported": None, "outcome": "success", "error_message": None,
        "import_outcome": "success", "import_error_message": None,
        "export_outcome": None, "export_error_message": None,
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
    # The trailing filesystem path is ALSO redacted now, on top of the real
    # MRN it contained -- _scrub goes through pii_patterns.redact(), whose
    # generic pattern floor forbids a server filesystem path from crossing
    # this boundary at all (docs/pii-boundary-safety.md §0), not just the
    # real id embedded in it.
    assert patient["pinnacle_reason"] == f"Could not reconstruct DICOM: no RTSTRUCT for {ANON_MRN} at [redacted]"
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


def test_job_summary_includes_exported_counts(client, job_id):
    """The export-side counterpart of test_job_summary_includes_imported_and_submitted_counts."""
    status_db.create_job(job_id)
    status_db.add_event(job_id, "MRN1", stage="export", event_type="success", details={"status": "exported"})
    status_db.add_event(job_id, "MRN2", stage="export", event_type="failure", error_message="boom")

    resp = client.get(f"/results/job/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exported_count"] == 1
    assert body["export_attempted_count"] == 2


def test_job_summary_is_combined_true_when_chain_export_submitted(client, job_id):
    from backend.src.status.tasks_db import TasksDB
    from backend.src.common.sse import BatchItem

    status_db.create_job(job_id)
    TasksDB().enqueue(
        job_id, [BatchItem(real_id=REAL_MRN, display_id=ANON_MRN, status_mrn=REAL_MRN)],
        kind="import", stage="retrieve",
        params={"chain_export": {"kind": "dicom_move", "destination": "AE1"}},
    )

    resp = client.get(f"/results/job/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["is_combined"] is True


def test_job_summary_is_combined_false_for_plain_import(client, job_id):
    status_db.create_job(job_id)
    status_db.add_event(job_id, REAL_MRN, stage="retrieve", event_type="success", details={"imported": True})

    resp = client.get(f"/results/job/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["is_combined"] is False


def test_job_summary_export_counts_are_zero_for_import_only_job(client, job_id):
    status_db.create_job(job_id)
    status_db.add_event(job_id, REAL_MRN, stage="retrieve", event_type="success", details={"imported": True})

    resp = client.get(f"/results/job/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exported_count"] == 0
    assert body["export_attempted_count"] == 0


def test_job_patients_summary_distinguishes_import_and_export_outcome(client, job_id):
    """
    A combined import->export job: this patient's import succeeded but its
    chained export failed. The stage-agnostic outcome/error_message (latest
    event of either stage) would only ever show the export's failure --
    import_outcome/export_outcome must each report their own stage's result
    so the import success isn't shadowed.
    """
    status_db.create_job(job_id)
    status_db.add_event(
        job_id, REAL_MRN, stage="retrieve", event_type="success",
        details={"imported": True, "in_mosaiq": True},
    )
    status_db.add_event(
        job_id, REAL_MRN, stage="export", event_type="failure",
        error_message=f"C-MOVE failed for {REAL_MRN}",
    )

    resp = client.get(f"/results/job/{job_id}/patients/summary")
    assert resp.status_code == 200
    patient = resp.json()["patients"][0]

    assert patient["imported"] is True
    assert patient["import_outcome"] == "success"
    assert patient["import_error_message"] is None
    assert patient["export_outcome"] == "failure"
    assert patient["export_error_message"] == f"C-MOVE failed for {ANON_MRN}"
    # stage-agnostic fields still reflect the latest event overall (export)
    assert patient["outcome"] == "failure"
    assert REAL_MRN not in resp.text


def test_job_patients_summary_export_outcome_null_when_no_export_ran(client, job_id):
    """A patient not found on import (imported=False) never gets a chained
    export task -- export_outcome must be null, not a stuck 'running'."""
    status_db.create_job(job_id)
    status_db.add_event(
        job_id, REAL_MRN, stage="retrieve", event_type="success",
        details={"imported": False, "status": "not found"},
    )

    resp = client.get(f"/results/job/{job_id}/patients/summary")
    assert resp.status_code == 200
    patient = resp.json()["patients"][0]
    assert patient["imported"] is False
    assert patient["import_outcome"] == "success"
    assert patient["export_outcome"] is None
    assert patient["export_error_message"] is None


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


def test_timeline_preserves_multiple_distinct_checksums_entries(client, job_id):
    """
    _scrub_json walks string LEAVES only, never dict keys. This matters
    because `checksums` (dict[SOPInstanceUID, hash]) has UID-shaped keys by
    construction: had _scrub_json instead serialized `details` to a JSON
    string, run it through pii_patterns.redact() (whose generic UID-pattern
    floor would turn every UID-shaped key into the same placeholder
    string), and re-parsed the result, multiple checksum entries would
    silently collapse into one via a dict-key collision on re-parse -- a
    real risk considered and rejected while choosing this implementation,
    not something the shipped `main` version (a plain real-MRN substring
    replace with no pattern floor) ever exhibited. This test guards the
    structural-walk design directly: two genuinely distinct SOPInstanceUIDs
    here must both survive.
    """
    status_db.create_job(job_id)
    status_db.add_event(
        job_id, REAL_MRN, stage="export", event_type="success",
        details={
            "checksums": {
                "1.2.840.10008.5.1.4.1.1.481.1": "aaa111",
                "1.2.840.10008.5.1.4.1.1.481.2": "bbb222",
            },
        },
    )

    resp = client.get(f"/results/patient/{job_id}/{ANON_MRN}")
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    assert len(event["details"]["checksums"]) == 2
    assert set(event["details"]["checksums"].values()) == {"aaa111", "bbb222"}
    assert REAL_MRN not in resp.text


def test_plans_scrub_the_real_mrn_out_of_path_comment_and_error(client, plans_schema):
    """
    Plan rows have no patient-id column, so nothing gets *translated* here --
    but `path` is built from the MRN and `comment`/`error_message` quote it.
    Those three free-text fields are the only way a real id could cross this
    boundary, on precisely the page built for reading error text.

    `path` is a genuine server filesystem path -- pii_patterns.redact()'s
    generic pattern floor (routed through here via _scrub) forbids that from
    crossing the boundary at all, not just the real id it happened to
    contain (docs/pii-boundary-safety.md §0 names "server filesystem paths"
    alongside real MRNs/dates/UIDs as forbidden) -- so the whole field comes
    back as the redaction placeholder, not a real-id-for-anon-id swap.
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
    assert plan["path"] == "[redacted]"  # a real path, forbidden regardless of the id inside it
    assert plan["comment"] == f"re-run for {ANON_MRN}"
    assert plan["error_message"] == f"RTSTRUCT missing for patient {ANON_MRN}"
    assert plan["plan_name"] == "Prostate"  # untouched
    assert REAL_MRN not in resp.text


def test_plans_shifts_plan_date(client, plans_schema, perturbation):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {PINNACLE_SCHEMA}.plans
                (mrn, path, plan_id, plan_name, plan_date, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (REAL_MRN, "dev-seed/plan-1", 1, "Prostate", "2026-01-01", "complete"),
        )

    resp = client.get(f"/results/patient/{ANON_MRN}/plans")
    assert resp.status_code == 200
    plan = resp.json()["plans"][0]
    assert plan["plan_date"] == "2026-01-11"  # 2026-01-01 + 10 days
    assert "2026-01-01" not in resp.text  # the raw date never appears


def test_plans_null_plan_date_stays_null(client, plans_schema, perturbation):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {PINNACLE_SCHEMA}.plans
                (mrn, path, plan_id, plan_name, plan_date, status)
            VALUES (%s, %s, %s, %s, NULL, %s)
            """,
            (REAL_MRN, "dev-seed/plan-2", 2, "Undated", "complete"),
        )

    resp = client.get(f"/results/patient/{ANON_MRN}/plans")
    assert resp.status_code == 200
    plan = resp.json()["plans"][0]
    assert plan["plan_date"] is None


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
