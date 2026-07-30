"""
Confirms retrieve/endpoints.py returns clean error codes/details instead of
bare 500s for: missing CSV file, unreachable anon-mapping DB. Needs the
PinnacleExport submodule (retrieve/endpoints.py imports retrieve/logic.py,
which imports it) -- skips gracefully if it isn't checked out.
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

from backend.src.identity import anon
from backend.src.retrieve import endpoints as retrieve_endpoints

ANON_MRN = "1001"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(retrieve_endpoints.router)
    return TestClient(app)


def test_batch_import_missing_csv_returns_400_not_500(client):
    resp = client.post("/import/batch_import", json={
        "job_id": f"import-err-{uuid.uuid4()}",
        "path_to_csv": "/nonexistent/path/patients.csv",
        "import_level": "Planning data",
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]


def test_find_patient_anon_db_unreachable_returns_503(client, monkeypatch):
    monkeypatch.setattr(anon, "ANON_DB_PORT", 1)
    monkeypatch.setattr(anon, "_pool", None)
    try:
        resp = client.get(f"/import/find_patient?mrn={ANON_MRN}")
        assert resp.status_code == 503
        assert resp.json()["detail"]
    finally:
        monkeypatch.setattr(anon, "_pool", None)


def test_batch_import_anon_db_unreachable_returns_503(client, tmp_path, monkeypatch):
    import csv
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        writer.writerow([ANON_MRN])

    monkeypatch.setattr(anon, "ANON_DB_PORT", 1)
    monkeypatch.setattr(anon, "_pool", None)
    try:
        resp = client.post("/import/batch_import", json={
            "job_id": f"import-err-{uuid.uuid4()}", "path_to_csv": str(csv_path), "import_level": "Planning data",
        })
        assert resp.status_code == 503
        assert resp.json()["detail"]
    finally:
        monkeypatch.setattr(anon, "_pool", None)
