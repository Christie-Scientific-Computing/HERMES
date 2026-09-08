"""
Tests for routers/jobs.py -- import/export/results (Phase 3a, see
docs/plans/frontend-rewrite-implementation-plan.md).

backend_client's jobs-related functions are monkeypatched with AsyncMock,
mirroring test_research_projects.py's established pattern. Focuses on the
core logic carrying real risk of being wrong: the patient filter's tri-state
logic, live job-visibility checks, submit_job's cross-field validation and
single/batch/combined branching, job_watch's is_combined read, and
job_stream's SSE re-framing -- not exhaustive coverage of every template.
"""
import json
from unittest.mock import AsyncMock

import pytest

from frontend_fastapi import backend_client
from frontend_fastapi.routers.jobs import _filter_patient_rows, _patient_rows

PROJECT_ID = "proj-1"


def _project(project_id=PROJECT_ID, title="Test Project"):
    return {"project_id": project_id, "title": title}


def _job_info(project_id=PROJECT_ID, is_combined=False, summary=None):
    return {"project_id": project_id, "is_combined": is_combined, "summary": summary or []}


@pytest.fixture()
def mock_backend(monkeypatch):
    mocks = {}
    for name in (
        "list_projects", "list_user_active_projects", "list_project_jobs",
        "ensure_superuser_bypass_project", "get_orthanc_modalities", "get_proknow_collections",
        "batch_import_file", "combined_import_export_file", "dicom_move_file", "proknow_upload_file",
        "job_summary", "job_patients", "job_patients_summary",
        "patient_timeline", "patient_timeline_all", "patient_plans", "cancel_import",
    ):
        m = AsyncMock()
        monkeypatch.setattr(backend_client, name, m)
        mocks[name] = m
    mocks["list_user_active_projects"].return_value = []
    mocks["list_projects"].return_value = []
    mocks["list_project_jobs"].return_value = []
    mocks["get_orthanc_modalities"].return_value = ["AE1"]
    mocks["get_proknow_collections"].return_value = ["Collection1"]
    return mocks


# ---- _patient_rows / _filter_patient_rows: the tri-state filter logic ----

def test_patient_filters_are_tri_state_and_pill_counts_come_from_unfiltered_rows():
    patients = ["MRN1", "MRN2", "MRN3"]
    summary = {
        "MRN1": {"in_mosaiq": True, "in_pinnacle": True, "in_proknow": None, "outcome": "success"},
        "MRN2": {"in_mosaiq": False, "in_pinnacle": False, "in_proknow": False, "outcome": "failure"},
        "MRN3": {"in_mosaiq": None, "in_pinnacle": None, "in_proknow": None},
    }
    rows = _patient_rows(patients, summary)
    assert len(rows) == 3

    visible, pills = _filter_patient_rows(rows, "not_found")
    assert [r["mrn"] for r in visible] == ["MRN2"]
    pill_counts = {p["key"]: p["count"] for p in pills}
    # Counts reflect the UNFILTERED rows, not the current selection.
    assert pill_counts[""] == 3
    assert pill_counts["failed"] == 1
    assert pill_counts["not_found"] == 1
    assert pill_counts["missing_mosaiq"] == 1


def test_never_checked_none_does_not_count_as_missing():
    # MRN3's in_mosaiq is None ("never checked"), not False -- must not be
    # swept up by missing_mosaiq, which is is-False only.
    rows = _patient_rows(["MRN3"], {"MRN3": {"in_mosaiq": None}})
    visible, _ = _filter_patient_rows(rows, "missing_mosaiq")
    assert visible == []


# ---- submit_job ----

def test_submit_single_import_stays_on_page_and_does_not_redirect(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]
    mock_backend["batch_import_file"].return_value = {"job_id": "ignored", "total": 1}

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "single", "mrn": "MRN1",
        "do_import": "y", "import_level": "Planning data",
    }, follow_redirects=False)

    assert resp.status_code == 200
    mock_backend["batch_import_file"].assert_awaited_once()
    kwargs = mock_backend["batch_import_file"].call_args.kwargs
    assert kwargs["project_id"] == PROJECT_ID
    assert kwargs["username"] == "alice"
    assert kwargs["import_level"] == "Planning data"
    assert kwargs["filename"] == "single_patient.csv"
    assert kwargs["content"] == b"patient_id\nMRN1\n"
    assert "Starting" in resp.text  # the progress widget rendered inline


def test_submit_batch_import_redirects_to_job_watch(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]
    mock_backend["batch_import_file"].return_value = {"job_id": "ignored", "total": 2}

    resp = client.post(
        "/submit",
        data={
            "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "batch",
            "do_import": "y", "import_level": "Planning data",
        },
        files={"file": ("patients.csv", b"patient_id\nMRN1\nMRN2\n", "text/csv")},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("http://localhost/jobs/")
    kwargs = mock_backend["batch_import_file"].call_args.kwargs
    assert kwargs["filename"] == "patients.csv"
    assert kwargs["content"] == b"patient_id\nMRN1\nMRN2\n"


def test_submit_requires_import_or_export(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "single", "mrn": "MRN1",
    })

    assert resp.status_code == 200
    assert "Choose to import, export, or both." in resp.text
    mock_backend["batch_import_file"].assert_not_awaited()


def test_submit_batch_scope_requires_a_file(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "batch", "do_import": "y",
    })

    assert resp.status_code == 200
    assert "Required for a batch (CSV) job." in resp.text
    mock_backend["batch_import_file"].assert_not_awaited()


def test_submit_single_scope_requires_mrn(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "single", "do_import": "y",
    })

    assert resp.status_code == 200
    assert "Required for a single patient." in resp.text
    mock_backend["batch_import_file"].assert_not_awaited()


def test_submit_export_only_dicom_requires_destination(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]
    mock_backend["get_orthanc_modalities"].return_value = []  # nothing to pick, mirrors an unreachable Orthanc

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "single", "mrn": "MRN1",
        "do_export": "y", "export_kind": "dicom_move",
    })

    assert resp.status_code == 200
    assert "Required when exporting via DICOM." in resp.text
    mock_backend["dicom_move_file"].assert_not_awaited()


def test_submit_combined_import_and_dicom_export(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]
    mock_backend["get_orthanc_modalities"].return_value = ["AE1"]
    mock_backend["combined_import_export_file"].return_value = {"job_id": "ignored", "total": 1}

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "single", "mrn": "MRN1",
        "do_import": "y", "import_level": "Planning data",
        "do_export": "y", "export_kind": "dicom_move", "destination": "AE1",
    }, follow_redirects=False)

    assert resp.status_code == 200
    mock_backend["combined_import_export_file"].assert_awaited_once()
    kwargs = mock_backend["combined_import_export_file"].call_args.kwargs
    assert kwargs["export_kind"] == "dicom_move"
    assert kwargs["destination_or_collection"] == "AE1"
    assert kwargs["message_id"] is None
    mock_backend["batch_import_file"].assert_not_awaited()
    mock_backend["dicom_move_file"].assert_not_awaited()


def test_submit_export_only_proknow_calls_proknow_upload(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]
    mock_backend["get_proknow_collections"].return_value = ["Coll1"]
    mock_backend["proknow_upload_file"].return_value = {"job_id": "ignored", "total": 1}

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "single", "mrn": "MRN1",
        "do_export": "y", "export_kind": "proknow_upload", "collection": "Coll1",
    }, follow_redirects=False)

    assert resp.status_code == 200
    mock_backend["proknow_upload_file"].assert_awaited_once()
    assert mock_backend["proknow_upload_file"].call_args.kwargs["collection"] == "Coll1"


def test_submit_export_only_dicom_calls_dicom_move_file(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]
    mock_backend["get_orthanc_modalities"].return_value = ["AE1"]
    mock_backend["dicom_move_file"].return_value = {"job_id": "ignored", "total": 1}

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "single", "mrn": "MRN1",
        "do_export": "y", "export_kind": "dicom_move", "destination": "AE1", "message_id": "42",
    }, follow_redirects=False)

    assert resp.status_code == 200
    mock_backend["dicom_move_file"].assert_awaited_once()
    kwargs = mock_backend["dicom_move_file"].call_args.kwargs
    assert kwargs["destination"] == "AE1"
    assert kwargs["message_id"] == 42
    mock_backend["batch_import_file"].assert_not_awaited()
    mock_backend["combined_import_export_file"].assert_not_awaited()


def test_submit_choose_neither_import_nor_export_short_circuits_other_checks(client, make_user, login, csrf_token, mock_backend):
    """Matches Django's clean(), which `raise`s on this check before ever
    evaluating the scope/mrn/file cross-field rules -- a batch submission
    with no file AND neither do_import nor do_export shows only the one
    "choose..." message, not that one plus "Required for a batch" too."""
    make_user(username="alice")
    login("alice")
    mock_backend["list_user_active_projects"].return_value = [_project()]

    resp = client.post("/submit", data={
        "csrf_token": csrf_token(), "project_id": PROJECT_ID, "scope": "batch",
    })

    assert resp.status_code == 200
    assert "Choose to import, export, or both." in resp.text
    assert "Required for a batch (CSV) job." not in resp.text


# ---- job_watch ----

def test_job_watch_renders_two_stage_progress_for_a_combined_job(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = _job_info(is_combined=True)

    resp = client.get("/jobs/job-1/watch")

    assert resp.status_code == 200
    assert 'id="import-bar-job-1"' in resp.text
    # is_combined must be read off the SAME job_summary call, not a second one.
    mock_backend["job_summary"].assert_awaited_once_with("job-1")


def test_job_watch_renders_single_stage_progress_for_a_plain_job(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = _job_info(is_combined=False)

    resp = client.get("/jobs/job-1/watch")

    assert resp.status_code == 200
    assert 'id="import-bar-job-1"' not in resp.text
    assert 'id="progress-bar-job-1"' in resp.text


def test_job_watch_missing_is_combined_key_defaults_to_plain(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = {"project_id": PROJECT_ID, "summary": []}

    resp = client.get("/jobs/job-1/watch")

    assert resp.status_code == 200
    assert 'id="import-bar-job-1"' not in resp.text


def test_job_watch_404s_for_a_job_belonging_to_a_project_the_user_is_not_in(client, make_user, login, mock_backend):
    make_user(username="bob")
    login("bob")
    mock_backend["list_projects"].return_value = []
    mock_backend["job_summary"].return_value = _job_info(project_id="other-project")

    resp = client.get("/jobs/job-1/watch")

    assert resp.status_code == 404


def test_job_watch_404s_for_an_unknown_job(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["job_summary"].side_effect = backend_client.BackendError(404, "not found")

    resp = client.get("/jobs/job-1/watch")

    assert resp.status_code == 404


# ---- job_stream ----

def test_job_stream_reframes_backend_events_as_named_sse_events(client, make_user, login, mock_backend, monkeypatch):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = _job_info()

    async def fake_stream_sse(path):
        for event in ({"type": "start", "total": 1}, {"type": "done"}):
            yield f"data: {json.dumps(event)}\n\n".encode()

    monkeypatch.setattr(backend_client, "stream_sse", fake_stream_sse)

    resp = client.get("/jobs/job-1/stream")

    assert resp.status_code == 200
    assert "event: start" in resp.text
    assert "event: done" in resp.text


def test_job_stream_404s_for_a_job_the_user_cannot_see(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["job_summary"].side_effect = backend_client.BackendError(404, "not found")

    resp = client.get("/jobs/job-1/stream")

    assert resp.status_code == 404


def test_job_stream_404s_for_an_anonymous_request(client):
    resp = client.get("/jobs/job-1/stream")
    assert resp.status_code == 404


# ---- cancel_job ----

def test_cancel_job_calls_backend_and_redirects_to_watch(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")

    resp = client.post("/jobs/job-1/cancel", data={"csrf_token": csrf_token()}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "http://localhost/jobs/job-1/watch"
    mock_backend["cancel_import"].assert_awaited_once_with("job-1")


# ---- job_detail ----

def test_job_detail_renders_patient_table_and_stat_line(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = {
        "project_id": PROJECT_ID, "summary": [], "imported_count": 1, "submitted_count": 2,
        "exported_count": None, "export_attempted_count": None,
    }
    mock_backend["job_patients"].return_value = {"patients": ["MRN1", "MRN2"]}
    mock_backend["job_patients_summary"].return_value = {"patients": [
        {"mrn": "MRN1", "in_mosaiq": True, "in_pinnacle": True, "in_proknow": True},
        {"mrn": "MRN2", "in_mosaiq": False, "in_pinnacle": False, "in_proknow": False},
    ]}

    resp = client.get("/jobs/job-1")

    assert resp.status_code == 200
    assert "MRN1" in resp.text
    assert "1 / 2" in resp.text


def test_job_detail_redirects_with_flash_for_non_member(client, make_user, login, mock_backend):
    make_user(username="bob")
    login("bob")
    mock_backend["list_projects"].return_value = []
    mock_backend["job_summary"].return_value = {"project_id": "other-project", "summary": []}

    resp = client.get("/jobs/job-1", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "http://localhost/"
    resp2 = client.get("/")
    assert "have access to that job" in resp2.text


# ---- patient_detail ----

def _plan(plan_id=1, plan_name="Plan A", status="completed", plan_date="2026-01-01"):
    return {"plan_id": plan_id, "plan_name": plan_name, "status": status, "plan_date": plan_date, "path": "/x/y"}


def test_patient_detail_renders_plans_and_timeline(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = _job_info()
    mock_backend["patient_timeline"].return_value = {
        "events": [{"ts": "t", "stage": "retrieve", "event_type": "success", "attempt": 1}],
    }
    mock_backend["patient_plans"].return_value = {"available": True, "plans": [_plan()]}
    mock_backend["job_patients_summary"].return_value = {"patients": [{"mrn": "MRN1", "in_mosaiq": True}]}

    resp = client.get("/jobs/job-1/patients/MRN1")

    assert resp.status_code == 200
    assert "Plan A" in resp.text
    mock_backend["patient_timeline"].assert_awaited_once_with("job-1", "MRN1")


def test_patient_detail_shows_unavailable_notice_when_plans_not_available(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = _job_info()
    mock_backend["patient_timeline"].return_value = {"events": []}
    mock_backend["patient_plans"].return_value = {"available": False, "plans": []}

    resp = client.get("/jobs/job-1/patients/MRN1")

    assert resp.status_code == 200
    assert "aren't available for this deployment" in resp.text


def test_patient_detail_status_filter_narrows_plans_and_pill_counts_are_stable(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = _job_info()
    mock_backend["patient_timeline"].return_value = {"events": []}
    mock_backend["patient_plans"].return_value = {
        "available": True,
        "plans": [
            _plan(plan_id=1, plan_name="Completed Plan", status="completed"),
            _plan(plan_id=2, plan_name="Failed Plan", status="failed"),
        ],
    }

    resp = client.get("/jobs/job-1/patients/MRN1?status=failed")

    assert resp.status_code == 200
    assert "Failed Plan" in resp.text
    assert "Completed Plan" not in resp.text  # filtered out
    # The "All" pill's count must still reflect both plans, not the one visible after filtering.
    all_pill_pos = resp.text.find("All")
    assert all_pill_pos != -1
    assert ">2<" in resp.text[all_pill_pos:all_pill_pos + 200]


def test_patient_detail_plans_failure_does_not_blank_the_timeline(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = _job_info()
    mock_backend["patient_timeline"].return_value = {
        "events": [{"ts": "t", "stage": "retrieve", "event_type": "success", "attempt": 1}],
    }
    mock_backend["patient_plans"].side_effect = backend_client.BackendError(500, "plans db down")

    resp = client.get("/jobs/job-1/patients/MRN1")

    assert resp.status_code == 200
    assert "plans db down" in resp.text
    assert "1 event" in resp.text  # the timeline still rendered


def test_patient_detail_timeline_failure_does_not_blank_the_plans(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = _job_info()
    mock_backend["patient_timeline"].side_effect = backend_client.BackendError(500, "events db down")
    mock_backend["patient_plans"].return_value = {"available": True, "plans": [_plan()]}

    resp = client.get("/jobs/job-1/patients/MRN1")

    assert resp.status_code == 200
    assert "events db down" in resp.text
    assert "Plan A" in resp.text  # the plans table still rendered


def test_patient_detail_redirects_with_flash_for_non_member(client, make_user, login, mock_backend):
    make_user(username="bob")
    login("bob")
    mock_backend["list_projects"].return_value = []
    mock_backend["job_summary"].return_value = _job_info(project_id="other-project")

    resp = client.get("/jobs/job-1/patients/MRN1", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "http://localhost/"


# ---- results_lookup ----

def test_results_lookup_by_job_shows_the_patient_table(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]
    mock_backend["job_summary"].return_value = {"project_id": PROJECT_ID, "summary": []}
    mock_backend["job_patients"].return_value = {"patients": ["MRN1"]}
    mock_backend["job_patients_summary"].return_value = {"patients": [{"mrn": "MRN1"}]}

    resp = client.get("/results?lookup=job&job_id=job-1")

    assert resp.status_code == 200
    assert "MRN1" in resp.text


def test_results_lookup_by_patient_requires_job_id_for_non_staff(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = []

    resp = client.get("/results?lookup=patient&mrn=MRN1")

    assert resp.status_code == 200
    assert "You must specify a job ID" in resp.text
    mock_backend["patient_timeline_all"].assert_not_awaited()


def test_results_lookup_by_patient_staff_can_search_without_a_job_id(client, make_user, login, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["list_projects"].return_value = []
    mock_backend["patient_timeline_all"].return_value = {
        "events": [{"ts": "t", "stage": "retrieve", "event_type": "success", "attempt": 1}],
    }

    resp = client.get("/results?lookup=patient&mrn=MRN1")

    assert resp.status_code == 200
    mock_backend["patient_timeline_all"].assert_awaited_once_with("MRN1")
