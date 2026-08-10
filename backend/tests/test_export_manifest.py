"""
Coverage for docs/safety-plan.md §D2 -- a real export manifest instead of
`{'status': 'Success'}`. Three workers, three different starting points:

- ProKnow (Exporter.upload_to_proknow -> download_data): checksums are
  computed locally (hashlib.sha256) over bytes already pulled to disk --
  never re-queried from Orthanc.
- DICOM C-MOVE (Exporter.dicom_c_move): only had series-level enumeration
  before this change; instance counts/checksums are net-new Orthanc calls,
  and checksums come from Orthanc's own stored MD5 (never re-hashed locally,
  since the bytes never land in this process for a C-MOVE).
- UID-based C-MOVE (_uid_move_worker/_build_uid_manifest in
  export/endpoints.py): didn't call find_studies/find_series at all before
  this change -- this is genuinely new enumeration, done purely to build the
  audit manifest, with no bearing on the move itself.

No live Orthanc needed anywhere below -- Orthanc/find_series/find_studies
are faked, mirroring the FakeStudy/FakeSeries pattern already established in
test_cleanup_orthanc.py, and the end-to-end SSE test mocks Exporter itself
the same way test_export_anon_boundary.py does.
"""
import os

# Set before anything imports backend.src.identity.anon (module-level
# constants read once at import time) so the end-to-end test's anon
# resolution is live regardless of whether this file runs standalone or as
# part of the full suite -- same pattern test_anon.py / test_export_anon_boundary.py
# use, and the same seeded anon_test DB / mapping (1001 <-> 500123).
os.environ.setdefault("ANON_DB_HOST", "localhost")
os.environ.setdefault("ANON_DB_PORT", "55433")
os.environ.setdefault("ANON_DB_NAME", "anon_test")
os.environ.setdefault("ANON_DB_USER", "postgres")
os.environ.setdefault("ANON_DB_PASS", "test")

import csv
import hashlib
import json
import uuid

import pytest


class FakeStudy:
    def __init__(self, uid, identifier="study-id"):
        self.main_dicom_tags = {"StudyInstanceUID": uid}
        self.identifier = identifier


class FakeSeries:
    def __init__(self, identifier, tags):
        self.identifier = identifier
        self.main_dicom_tags = tags


# ---------------------------------------------------------------------------
# DICOM C-MOVE: Exporter.dicom_c_move / Exporter._build_manifest
# ---------------------------------------------------------------------------

class _FakeOrthancMoveClient:
    """Stands in for pyorthanc's Orthanc client for the DICOM C-MOVE path."""

    def __init__(self, instances_by_series, md5_by_instance):
        self.instances_by_series = instances_by_series
        self.md5_by_instance = md5_by_instance
        self.move_calls = []

    def get_series_id_instances(self, series_id):
        return self.instances_by_series[series_id]

    def get_instances_id_attachments_name_md5(self, id_, name):
        assert name == "dicom"
        return self.md5_by_instance[id_]

    def post_modalities_id_store(self, id_, json):
        self.move_calls.append({"id_": id_, "json": json})
        return {"ID": "orthanc-job-1"}


def test_dicom_c_move_manifest_uses_orthanc_reported_checksums(monkeypatch):
    from backend.src.export import logic

    study = FakeStudy(uid="1.2.840.study.1")
    series_a = FakeSeries("series-a", {"SeriesInstanceUID": "1.2.840.series.a"})
    series_b = FakeSeries("series-b", {"SeriesInstanceUID": "1.2.840.series.b"})

    instances_by_series = {
        "series-a": [
            {"ID": "inst-1", "MainDicomTags": {"SOPInstanceUID": "1.2.840.sop.1"}},
            {"ID": "inst-2", "MainDicomTags": {"SOPInstanceUID": "1.2.840.sop.2"}},
        ],
        "series-b": [
            {"ID": "inst-3", "MainDicomTags": {"SOPInstanceUID": "1.2.840.sop.3"}},
        ],
    }
    md5_by_instance = {"inst-1": "md5-one", "inst-2": "md5-two", "inst-3": "md5-three"}
    fake_client = _FakeOrthancMoveClient(instances_by_series, md5_by_instance)

    monkeypatch.setattr(logic, "Orthanc", lambda **kwargs: fake_client)
    monkeypatch.setattr(logic, "find_series", lambda client, query: [series_a, series_b])
    monkeypatch.setattr(logic, "find_studies", lambda client, query: [study])

    exp = logic.Exporter(destination="SOME_AE")
    res = exp.dicom_c_move("500123")

    assert res["status"] == "Success"
    assert res["series_count"] == 2
    assert res["instance_count"] == 3
    assert res["study_uids"] == ["1.2.840.study.1"]
    assert set(res["series_uids"]) == {"1.2.840.series.a", "1.2.840.series.b"}
    assert res["checksums"] == {
        "1.2.840.sop.1": "md5-one",
        "1.2.840.sop.2": "md5-two",
        "1.2.840.sop.3": "md5-three",
    }
    # Manifest is computed before the actual (async, fire-and-forget) move.
    assert fake_client.move_calls == [
        {"id_": "SOME_AE", "json": {"Resources": ["series-a", "series-b"], "Synchronous": False}}
    ]


def test_dicom_c_move_manifest_survives_a_checksum_lookup_failure(monkeypatch):
    """A single instance's MD5 lookup failing shouldn't blow up the whole
    export -- it's an audit nicety, not load-bearing for the move itself."""
    from backend.src.export import logic

    study = FakeStudy(uid="1.2.840.study.2")
    series_a = FakeSeries("series-a", {"SeriesInstanceUID": "1.2.840.series.a"})
    instances_by_series = {
        "series-a": [{"ID": "inst-1", "MainDicomTags": {"SOPInstanceUID": "1.2.840.sop.1"}}],
    }

    class BoomClient(_FakeOrthancMoveClient):
        def get_instances_id_attachments_name_md5(self, id_, name):
            raise ConnectionError("orthanc unreachable for attachment lookup")

    fake_client = BoomClient(instances_by_series, {})
    monkeypatch.setattr(logic, "Orthanc", lambda **kwargs: fake_client)
    monkeypatch.setattr(logic, "find_series", lambda client, query: [series_a])
    monkeypatch.setattr(logic, "find_studies", lambda client, query: [study])

    exp = logic.Exporter(destination="SOME_AE")
    res = exp.dicom_c_move("500124")

    assert res["status"] == "Success"
    assert res["instance_count"] == 1
    assert res["checksums"] == {}  # lookup failed, but the move still happened
    assert fake_client.move_calls  # the move itself was not blocked


# ---------------------------------------------------------------------------
# ProKnow: Exporter.upload_to_proknow / Exporter.download_data
# ---------------------------------------------------------------------------

def test_proknow_upload_checksums_computed_from_local_bytes(monkeypatch, tmp_path):
    from backend.src.export import logic

    study = FakeStudy(uid="1.2.840.study.9")
    series_a = FakeSeries("series-a", {"SeriesInstanceUID": "1.2.840.series.a"})

    dicom_bytes_1 = b"FAKE DICOM BYTES ONE"
    dicom_bytes_2 = b"FAKE DICOM BYTES TWO"
    instances = [
        {"ID": "inst-1", "MainDicomTags": {"SOPInstanceUID": "1.2.840.sop.1"}},
        {"ID": "inst-2", "MainDicomTags": {"SOPInstanceUID": "1.2.840.sop.2"}},
    ]
    bytes_by_instance = {"inst-1": dicom_bytes_1, "inst-2": dicom_bytes_2}

    class FakeClient:
        def get_series_id_instances(self, series_id):
            return instances

        def get_instances_id_file(self, instance_id):
            return bytes_by_instance[instance_id]

    fake_client = FakeClient()
    monkeypatch.setattr(logic, "Orthanc", lambda **kwargs: fake_client)
    monkeypatch.setattr(logic, "find_studies", lambda client, query: [study])
    monkeypatch.setattr(logic, "find_series", lambda client, query: [series_a])

    exp = logic.Exporter(destination="COLLECTION")
    exp.tmp_dir = tmp_path  # isolate downloads from the repo's ./tmp
    monkeypatch.setattr(exp, "upload_study_to_proknow", lambda path, collection: {"status": "Success"})

    res = exp.upload_to_proknow("500999")

    assert res["status"] == "Success"
    assert res["series_count"] == 1
    assert res["instance_count"] == 2
    assert res["study_uids"] == ["1.2.840.study.9"]
    assert res["series_uids"] == ["1.2.840.series.a"]
    assert res["checksums"] == {
        "1.2.840.sop.1": hashlib.sha256(dicom_bytes_1).hexdigest(),
        "1.2.840.sop.2": hashlib.sha256(dicom_bytes_2).hexdigest(),
    }
    # Genuinely local hashes, not whatever Orthanc happens to report.
    assert res["checksums"]["1.2.840.sop.1"] != "md5-one"


# ---------------------------------------------------------------------------
# UID-based C-MOVE: _build_uid_manifest / _uid_move_worker
# (export/endpoints.py -- this path never touches Exporter at all)
# ---------------------------------------------------------------------------

def test_uid_move_worker_performs_its_own_find_lookup_and_builds_manifest(monkeypatch):
    from backend.src.export import endpoints as export_endpoints
    from backend.src.common.sse import BatchItem

    study = FakeStudy(uid="1.2.840.study.5")
    series = FakeSeries("series-5", {"SeriesInstanceUID": "1.2.840.series.5"})
    instances = [{"ID": "inst-9", "MainDicomTags": {"SOPInstanceUID": "1.2.840.sop.9"}}]

    class FakeClient:
        def get_series_id_instances(self, series_id):
            return instances

        def get_instances_id_attachments_name_md5(self, id_, name):
            assert name == "dicom"
            return "uid-path-md5"

    fake_client = FakeClient()
    find_studies_calls = []
    find_series_calls = []
    monkeypatch.setattr(export_endpoints, "Orthanc", lambda **kwargs: fake_client)

    def fake_find_studies(client, query):
        find_studies_calls.append(query)
        return [study]

    def fake_find_series(client, query):
        find_series_calls.append(query)
        return [series]

    monkeypatch.setattr(export_endpoints, "find_studies", fake_find_studies)
    monkeypatch.setattr(export_endpoints, "find_series", fake_find_series)

    move_calls = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ID": "orthanc-job-2"}

    def fake_post(url, auth, verify, json, timeout):
        move_calls.append({"url": url, "json": json})
        return FakeResp()

    monkeypatch.setattr(export_endpoints.http_requests, "post", fake_post)

    worker = export_endpoints._uid_move_worker("SOME_AE", submitted_by="alice")
    item = BatchItem(
        real_id="1.2.840.study.5",
        display_id="1.2.840.study.5",
        status_mrn="bookkeeping-mrn",
        extra={"study_uid": "1.2.840.study.5", "series_uid": None},
    )
    res = worker(item)

    # This path never calls Exporter/dicom_c_move -- the find lookup is
    # entirely new, done purely to build the manifest.
    assert find_studies_calls == [{"StudyInstanceUID": "1.2.840.study.5"}]
    assert find_series_calls == [{"StudyInstanceUID": "1.2.840.study.5"}]

    assert res["status"] == "Success"
    assert res["destination"] == "SOME_AE"
    assert res["destination_type"] == "dicom_modality"
    assert res["submitted_by"] == "alice"
    assert res["series_count"] == 1
    assert res["instance_count"] == 1
    assert res["study_uids"] == ["1.2.840.study.5"]
    assert res["series_uids"] == ["1.2.840.series.5"]
    assert res["checksums"] == {"1.2.840.sop.9": "uid-path-md5"}

    # The move itself still happened, after the manifest was built.
    assert len(move_calls) == 1
    assert move_calls[0]["json"]["Resources"] == [{"StudyInstanceUID": "1.2.840.study.5"}]


# ---------------------------------------------------------------------------
# End-to-end: manifest fields actually survive into events.details / the SSE
# payload via run_batch_job, for the DICOM C-MOVE batch endpoint.
# ---------------------------------------------------------------------------

def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.src.export import endpoints as export_endpoints
    from backend.src.export.logic import Exporter as RealExporter

    class FakeExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            self.destination = destination

        def dicom_c_move(self, patient_id):
            return {
                "status": "Success",
                "series_count": 2,
                "instance_count": 5,
                "study_uids": ["1.2.840.study.end2end"],
                "series_uids": ["1.2.840.series.end2end.a", "1.2.840.series.end2end.b"],
                "checksums": {"1.2.840.sop.end2end.1": "deadbeef"},
            }

    monkeypatch.setattr(export_endpoints, "Exporter", FakeExporter)

    app = FastAPI()
    app.include_router(export_endpoints.router)
    return TestClient(app)


def test_manifest_fields_reach_sse_payload_and_events_details(client, tmp_path, active_project):
    """
    Integration-style: exercises the real run_batch_job path (not just the
    worker function in isolation) so a regression in the Response
    round-trip (§D0) or the details write (StatusDB.add_event) would show
    up here even if the unit tests above still pass.

    Uses the same anon/real MRN pair test_export_anon_boundary.py does
    (1001 -> 500123, seeded in the anon_test DB -- see that file's header)
    rather than an arbitrary id: test_anon.py, collected earlier in the
    same session, sets ANON_DB_HOST etc. as module-level side effects at
    import time, so anon resolution is live process-wide by the time this
    module runs regardless of whether this file's own fixtures touch it.
    """
    from backend.src.export import endpoints as export_endpoints

    ANON_MRN, REAL_MRN = "1001", "500123"

    project_id, username = active_project
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        writer.writerow([ANON_MRN])

    job_id = f"export-manifest-{uuid.uuid4()}"
    resp = client.post("/export/dicom_move", json={
        "job_id": job_id, "path_to_csv": str(csv_path), "destination": "SOME_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    success_events = [e for e in events if e["type"] == "success"]
    assert len(success_events) == 1
    success = success_events[0]
    assert success["series_count"] == 2
    assert success["instance_count"] == 5
    assert success["study_uids"] == ["1.2.840.study.end2end"]
    assert success["series_uids"] == ["1.2.840.series.end2end.a", "1.2.840.series.end2end.b"]
    assert success["checksums"] == {"1.2.840.sop.end2end.1": "deadbeef"}
    assert success["destination"] == "SOME_AE"
    assert success["destination_type"] == "dicom_modality"
    assert success["submitted_by"] == username
    assert success["mrn"] == ANON_MRN  # outbound boundary: anon id, never the real one
    assert REAL_MRN not in resp.text

    # And the same fields landed in events.details via StatusDB.add_event
    # (backend-internal storage, real id).
    history = export_endpoints.status_db.get_patient_history(job_id, REAL_MRN)
    success_db_events = [e for e in history if e["event_type"] == "success"]
    assert len(success_db_events) == 1
    details = success_db_events[0]["details"]
    assert details["series_count"] == 2
    assert details["instance_count"] == 5
    assert details["checksums"] == {"1.2.840.sop.end2end.1": "deadbeef"}
    assert details["destination"] == "SOME_AE"
    assert details["destination_type"] == "dicom_modality"
    assert details["submitted_by"] == username
