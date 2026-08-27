"""
Confirms the anon boundary in studies/endpoints.py without a live Orthanc:
Orthanc's HTTP calls are mocked, and we check that real PatientIDs never
reach the HTTP response once anonymisation is configured, and that
patient_name (which has no mapping at all) is redacted rather than leaked.
"""
import os

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.identity import anon
from backend.src.studies import endpoints as studies_endpoints

REAL_MRN = "500123"
ANON_MRN = "1001"


def _conn():
    return psycopg2.connect(host="localhost", port=55433, dbname="anon_test", user="postgres", password="test")


@pytest.fixture(scope="module", autouse=True)
def _ensure_date_perturbation_column():
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE key_value ADD COLUMN IF NOT EXISTS date_perturbation INT")
    finally:
        conn.close()


@pytest.fixture
def perturbation():
    """Seeds a known, non-zero perturbation for REAL_MRN; resets to NULL
    afterward so other tests in this module see "nothing on record" (the
    baseline pre-existing state), same convention as test_anon_date_shift.py."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE key_value SET date_perturbation = %s WHERE key_value = %s AND key_type_id = 1",
                (10, int(REAL_MRN)),
            )
    finally:
        conn.close()
    yield 10
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE key_value SET date_perturbation = NULL WHERE key_value = %s AND key_type_id = 1",
                (int(REAL_MRN),),
            )
    finally:
        conn.close()


@pytest.fixture
def client(monkeypatch):
    def fake_orthanc(method, path, **kwargs):
        if path == "/tools/find":
            return [{
                "ID": "orthanc-abc",
                "PatientMainDicomTags": {"PatientID": REAL_MRN, "PatientName": "Doe^Jane"},
                "MainDicomTags": {
                    "StudyDate": "20260101",
                    "StudyDescription": "Planning CT",
                    "StudyInstanceUID": "1.2.3",
                },
                "Series": ["s1"],
            }]
        if path == "/studies/orthanc-abc":
            return {
                "Series": ["s1"],
                "MainDicomTags": {
                    "StudyDate": "20260101",
                    "StudyDescription": "Planning CT",
                    "StudyInstanceUID": "1.2.3",
                },
                "PatientMainDicomTags": {"PatientID": REAL_MRN, "PatientName": "Doe^Jane"},
            }
        if path == "/series/s1":
            return {
                "MainDicomTags": {
                    "Modality": "CT", "SeriesInstanceUID": "1.2.3.4",
                    "SeriesDescription": "Axial CT", "SeriesDate": "20260101",
                },
                "Instances": ["i1"],
            }
        raise AssertionError(f"unexpected orthanc path {path}")

    monkeypatch.setattr(studies_endpoints, "_orthanc", fake_orthanc)

    app = FastAPI()
    app.include_router(studies_endpoints.router)
    return TestClient(app)


def test_list_studies_translates_patient_id_and_redacts_name(client):
    resp = client.get("/studies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["studies"][0]["patient_id"] == ANON_MRN
    assert body["studies"][0]["patient_name"] is None
    assert REAL_MRN not in resp.text
    assert "Doe" not in resp.text


def test_list_studies_shifts_study_date(client, perturbation):
    resp = client.get("/studies")
    assert resp.status_code == 200
    study = resp.json()["studies"][0]
    assert study["study_date"] == "20260111"  # 20260101 + 10 days
    assert study["study_date"] != "20260101"  # never the raw date


def test_list_studies_redacts_study_description_and_uid_when_configured(client):
    resp = client.get("/studies")
    assert resp.status_code == 200
    study = resp.json()["studies"][0]
    assert study["study_description"] is None
    assert study["study_instance_uid"] is None
    assert "Planning CT" not in resp.text
    assert "1.2.3" not in resp.text


def test_list_studies_inbound_filter_resolves_anon_to_real(client, monkeypatch):
    captured = {}

    def fake_orthanc(method, path, **kwargs):
        if path == "/tools/find":
            captured["query"] = kwargs["json"]["Query"]
            return []
        raise AssertionError("unexpected call")

    monkeypatch.setattr(studies_endpoints, "_orthanc", fake_orthanc)
    resp = client.get(f"/studies?patient_id={ANON_MRN}")
    assert resp.status_code == 200
    assert captured["query"]["PatientID"] == REAL_MRN


def test_list_studies_unknown_anon_filter_returns_422(client):
    resp = client.get("/studies?patient_id=999999999")
    assert resp.status_code == 422


def test_get_study_translates_and_redacts(client):
    resp = client.get("/studies/orthanc-abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["patient_id"] == ANON_MRN
    assert body["patient_name"] is None
    assert REAL_MRN not in resp.text
    assert "Doe" not in resp.text


def test_get_study_shifts_study_and_series_date(client, perturbation):
    resp = client.get("/studies/orthanc-abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["study_date"] == "20260111"  # 20260101 + 10 days
    assert body["series"][0]["series_date"] == "20260111"
    assert "20260101" not in resp.text


def test_get_study_redacts_descriptions_and_uids_when_configured(client):
    resp = client.get("/studies/orthanc-abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["study_description"] is None
    assert body["study_instance_uid"] is None
    assert body["series"][0]["series_description"] is None
    assert body["series"][0]["series_instance_uid"] is None
    assert "Planning CT" not in resp.text
    assert "1.2.3" not in resp.text


def test_list_studies_anon_db_unreachable_returns_503(client, monkeypatch):
    monkeypatch.setattr(anon, "ANON_DB_PORT", 1)
    monkeypatch.setattr(anon, "_pool", None)
    try:
        resp = client.get("/studies")
        assert resp.status_code == 503
        assert resp.json()["detail"]
    finally:
        monkeypatch.setattr(anon, "_pool", None)


def test_get_study_anon_db_unreachable_returns_503(client, monkeypatch):
    monkeypatch.setattr(anon, "ANON_DB_PORT", 1)
    monkeypatch.setattr(anon, "_pool", None)
    try:
        resp = client.get("/studies/orthanc-abc")
        assert resp.status_code == 503
        assert resp.json()["detail"]
    finally:
        monkeypatch.setattr(anon, "_pool", None)


def test_list_studies_passthrough_shows_raw_dates_uids_and_descriptions(client, monkeypatch):
    # Internal-only deployment (no ANON_DB_HOST): dates/UIDs/descriptions
    # were never redacted before this change, and shift_date's own 0-day
    # passthrough means study_date comes back unchanged rather than
    # redacted -- this must keep working exactly as before.
    monkeypatch.setattr(anon, "ANON_DB_HOST", None)
    resp = client.get("/studies")
    assert resp.status_code == 200
    study = resp.json()["studies"][0]
    assert study["study_date"] == "20260101"
    assert study["study_description"] == "Planning CT"
    assert study["study_instance_uid"] == "1.2.3"
    assert study["patient_name"] == "Doe^Jane"
