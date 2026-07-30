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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.identity import anon
from backend.src.studies import endpoints as studies_endpoints

REAL_MRN = "500123"
ANON_MRN = "1001"


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
            return {"MainDicomTags": {"Modality": "CT", "SeriesInstanceUID": "1.2.3.4"}, "Instances": ["i1"]}
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
