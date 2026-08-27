"""
Confirms single_import/find_patient (retrieve/endpoints.py) and
proknow_upload_patient (export/endpoints.py) redact free-text fields
(mosaiq_reason/pinnacle_reason/proknow_reason) and exception messages that
routinely quote the real MRN, on both the success and error paths --
docs/plans/pii-boundary-test-suite.md §C findings #2/#6, plus scope
expansions found while implementing (single_import/find_patient's success
path wasn't explicitly named in the plan's own decision table, but carries
the same leak class).

Needs the PinnacleExport submodule (retrieve/endpoints.py -> retrieve/logic.py
-> PinnacleExport) -- skips gracefully if it isn't checked out, same as
test_retrieve_endpoints_errors.py.
"""
import os
import uuid

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import pytest

pytest.importorskip("backend.src.retrieve.PinnacleExport", reason="PinnacleExport submodule not checked out")

from fastapi import FastAPI
from fastapi.testclient import TestClient

REAL_MRN = "500123"
ANON_MRN = "1001"


@pytest.fixture
def retrieve_client(monkeypatch):
    from backend.src.retrieve import endpoints as retrieve_endpoints

    class FakeImporter:
        def __init__(self, import_level):
            self.import_level = import_level

        def handle_patient(self, mrn):
            return {
                "status": "success", "in_mosaiq": False,
                "mosaiq_reason": f"connection refused for patient {mrn}",
                "imported": True,
            }

        def find_patient(self, mrn):
            return {
                "in_mosaiq": False, "mosaiq_reason": f"connection refused for patient {mrn}",
                "in_pinnacle": True, "pinnacle_reason": f"no RTSTRUCT for {mrn} at /pinnacle/{mrn}/Plan_1",
                "in_proknow": False, "proknow_reason": "not found",
            }

    monkeypatch.setattr(retrieve_endpoints, "Importer", FakeImporter)
    app = FastAPI()
    app.include_router(retrieve_endpoints.router)
    return TestClient(app), retrieve_endpoints


def test_single_import_success_redacts_mosaiq_reason(retrieve_client, active_project):
    client, retrieve_endpoints = retrieve_client
    project_id, username = active_project
    job_id = f"single-import-{uuid.uuid4()}"

    resp = client.post("/import/single_import", json={
        "job_id": job_id, "mrn": ANON_MRN, "import_level": "Planning data",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert REAL_MRN not in resp.text
    assert body["mosaiq_reason"] == f"connection refused for patient {ANON_MRN}"

    history = retrieve_endpoints.status_db.get_patient_history(job_id, REAL_MRN)
    details = next(e for e in history if e["event_type"] == "success")["details"]
    assert REAL_MRN in details["mosaiq_reason"]  # DB keeps full fidelity


def test_single_import_mrn_survives_when_display_id_looks_date_shaped(retrieve_client, active_project, monkeypatch):
    # redact_dict's generic pattern floor runs over EVERY string value in
    # response.model_dump() -- if "mrn" itself were included in that pass,
    # an anon id that happens to be 8 digits parsing as a valid calendar
    # date (the anon-id scheme is an externally-owned table HERMES doesn't
    # control the format of) would get silently overwritten with the
    # redaction placeholder instead of the real display id.
    client, retrieve_endpoints = retrieve_client
    project_id, username = active_project
    job_id = f"single-import-datelike-{uuid.uuid4()}"

    monkeypatch.setattr(retrieve_endpoints.anon, "to_display_id", lambda real_id: "20260115")
    monkeypatch.setattr(retrieve_endpoints.anon, "resolve_real_id", lambda anon_id: REAL_MRN)

    resp = client.post("/import/single_import", json={
        "job_id": job_id, "mrn": "20260115", "import_level": "Planning data",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    assert resp.json()["mrn"] == "20260115"  # not "[redacted]"


def test_single_import_error_redacts_exception_message(retrieve_client, active_project, monkeypatch):
    client, retrieve_endpoints = retrieve_client
    project_id, username = active_project
    job_id = f"single-import-err-{uuid.uuid4()}"

    class BoomImporter:
        def __init__(self, import_level):
            pass

        def handle_patient(self, mrn):
            raise ValueError(f"lookup failed for {mrn} reading ./tmp/{job_id}_patients.csv")

    monkeypatch.setattr(retrieve_endpoints, "Importer", BoomImporter)

    resp = client.post("/import/single_import", json={
        "job_id": job_id, "mrn": ANON_MRN, "import_level": "Planning data",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "error"
    assert REAL_MRN not in body["error"]
    assert ANON_MRN in body["error"]
    assert "patients.csv" not in body["error"]  # generic path floor

    history = retrieve_endpoints.status_db.get_patient_history(job_id, REAL_MRN)
    failure = next(e for e in history if e["event_type"] == "failure")
    assert REAL_MRN in failure["error_message"]  # DB keeps full fidelity


def test_find_patient_redacts_reason_fields(retrieve_client, active_project):
    client, retrieve_endpoints = retrieve_client
    _, username = active_project

    resp = client.get(f"/import/find_patient?mrn={ANON_MRN}&username={username}")
    assert resp.status_code == 200
    body = resp.json()
    assert REAL_MRN not in resp.text
    assert body["mosaiq_reason"] == f"connection refused for patient {ANON_MRN}"
    assert body["pinnacle_reason"] == f"no RTSTRUCT for {ANON_MRN} at [redacted]"  # path floor


@pytest.fixture
def export_client(monkeypatch):
    from backend.src.export import endpoints as export_endpoints

    class FakeExporter:
        def __init__(self, destination):
            self.destination = destination

        def upload_to_proknow(self, patient_id):
            return {"status": "Success", "series_count": 1}

    monkeypatch.setattr(export_endpoints, "Exporter", FakeExporter)
    app = FastAPI()
    app.include_router(export_endpoints.router)
    return TestClient(app), export_endpoints


def test_proknow_upload_patient_success_redacts_free_text_fields(export_client, active_project):
    client, export_endpoints = export_client
    project_id, username = active_project
    job_id = f"pk-single-{uuid.uuid4()}"

    def upload(self, patient_id):
        return {"status": f"Success for {patient_id}", "series_count": 1}

    export_endpoints.Exporter.upload_to_proknow = upload

    resp = client.post("/export/proknow_upload_patient", json={
        "job_id": job_id, "mrn": ANON_MRN, "collection": "SOME_COLLECTION",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert REAL_MRN not in resp.text
    assert body["status"] == f"Success for {ANON_MRN}"

    history = export_endpoints.status_db.get_patient_history(job_id, REAL_MRN)
    details = next(e for e in history if e["event_type"] == "success")["details"]
    assert REAL_MRN in details["status"]  # DB keeps full fidelity


def test_proknow_upload_patient_mrn_survives_when_display_id_looks_date_shaped(export_client, active_project, monkeypatch):
    client, export_endpoints = export_client
    project_id, username = active_project
    job_id = f"pk-single-datelike-{uuid.uuid4()}"

    export_endpoints.Exporter.upload_to_proknow = lambda self, patient_id: {"status": "Success"}
    monkeypatch.setattr(export_endpoints.anon, "resolve_real_id", lambda anon_id: REAL_MRN)
    monkeypatch.setattr(export_endpoints.anon, "to_display_id", lambda real_id: "20260115")

    resp = client.post("/export/proknow_upload_patient", json={
        "job_id": job_id, "mrn": "20260115", "collection": "SOME_COLLECTION",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    assert resp.json()["mrn"] == "20260115"  # not "[redacted]"


def test_proknow_upload_patient_collection_name_survives_when_date_shaped(export_client, active_project):
    # Same class of bug as the mrn one above, for a different field:
    # destination (the ProKnow collection name here) is operational config,
    # never patient data -- redact_dict's generic floor must not mangle it
    # just because it happens to contain a date-shaped substring.
    client, export_endpoints = export_client
    project_id, username = active_project
    job_id = f"pk-single-collection-{uuid.uuid4()}"

    export_endpoints.Exporter.upload_to_proknow = lambda self, patient_id: {"status": "Success"}

    resp = client.post("/export/proknow_upload_patient", json={
        "job_id": job_id, "mrn": ANON_MRN, "collection": "Trial_2024-01-15_Cohort",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    assert resp.json()["destination"] == "Trial_2024-01-15_Cohort"


def test_proknow_upload_patient_error_redacts_exception_message(export_client, active_project):
    client, export_endpoints = export_client
    project_id, username = active_project
    job_id = f"pk-single-err-{uuid.uuid4()}"

    def boom(self, patient_id):
        raise ValueError(f"upload failed for {patient_id} reading ./tmp/{job_id}_patients.csv")

    export_endpoints.Exporter.upload_to_proknow = boom

    resp = client.post("/export/proknow_upload_patient", json={
        "job_id": job_id, "mrn": ANON_MRN, "collection": "SOME_COLLECTION",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "error"
    assert REAL_MRN not in body["error"]
    assert ANON_MRN in body["error"]
    assert "patients.csv" not in body["error"]

    history = export_endpoints.status_db.get_patient_history(job_id, REAL_MRN)
    failure = next(e for e in history if e["event_type"] == "failure")
    assert REAL_MRN in failure["error_message"]


# ---------------------------------------------------------------------------
# CSV-read error path redaction (finding #6): str(e) can embed the server's
# ./tmp/{job_id}_{filename} path.
# ---------------------------------------------------------------------------

def test_build_import_items_redacts_csv_read_error(monkeypatch):
    from fastapi import HTTPException
    from backend.src.retrieve import endpoints as retrieve_endpoints

    def boom(path_to_csv):
        raise ValueError(f"[Errno 2] No such file or directory: 'tmp/{uuid.uuid4()}_patients.csv'")

    monkeypatch.setattr(retrieve_endpoints.Importer, "read_input_file", staticmethod(boom))

    with pytest.raises(HTTPException) as exc_info:
        retrieve_endpoints._build_import_items("irrelevant.csv")
    assert "patients.csv" not in exc_info.value.detail
    assert "Could not read CSV" in exc_info.value.detail


def test_build_export_items_redacts_csv_read_error(monkeypatch):
    from fastapi import HTTPException
    from backend.src.export import endpoints as export_endpoints

    def boom(path_to_csv):
        raise ValueError(f"[Errno 2] No such file or directory: 'tmp/{uuid.uuid4()}_patients.csv'")

    monkeypatch.setattr(export_endpoints.Exporter, "read_input_file", staticmethod(boom))

    with pytest.raises(HTTPException) as exc_info:
        export_endpoints._build_export_items("irrelevant.csv")
    assert "patients.csv" not in exc_info.value.detail
    assert "Could not read CSV" in exc_info.value.detail
