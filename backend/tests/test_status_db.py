import uuid

import pytest

from backend.src.status.db_client import StatusDB


@pytest.fixture
def db():
    return StatusDB()


@pytest.fixture
def job_id():
    return f"test-{uuid.uuid4()}"


def test_create_job_is_idempotent(db, job_id):
    db.create_job(job_id, description="first", created_by="tester")
    db.create_job(job_id, description="second call should be ignored", created_by="tester")

    summary = db.summarize_job(job_id)
    assert summary == []  # no events yet, but no error/duplicate-row crash either


def test_add_patient_is_idempotent(db, job_id):
    db.create_job(job_id)
    db.add_patient(job_id, mrn="MRN1", input_path="/tmp/a.csv")
    db.add_patient(job_id, mrn="MRN1", input_path="/tmp/a.csv")  # re-run, should not raise

    patients = db.list_job_patients(job_id)
    # add_event hasn't been called, so list_job_patients (sourced from events) is still empty;
    # this just asserts the re-run didn't raise a duplicate-key error.
    assert patients == []


def test_add_event_and_read_back(db, job_id):
    mrn = f"MRN-{uuid.uuid4()}"
    db.create_job(job_id)
    db.add_patient(job_id, mrn=mrn)
    db.add_event(job_id, mrn=mrn, stage="retrieve", event_type="start")
    db.add_event(
        job_id,
        mrn=mrn,
        stage="retrieve",
        event_type="success",
        details={"execution_time": 1.23, "in_mosaiq": True},
    )

    history = db.get_patient_history(job_id, mrn)
    assert [e["event_type"] for e in history] == ["start", "success"]
    # JSONB round-trips as a native dict, not a JSON string
    assert history[1]["details"] == {"execution_time": 1.23, "in_mosaiq": True}

    # unique mrn per test run means this job is the only source of events for it,
    # even though the test DB persists rows across separate pytest invocations
    all_jobs_history = db.get_patient_history_all_jobs(mrn)
    assert len(all_jobs_history) == 2

    patients = db.list_job_patients(job_id)
    assert patients == [mrn]

    summary = db.summarize_job(job_id)
    summary_map = {(row["stage"], row["event_type"]): row["cnt"] for row in summary}
    assert summary_map[("retrieve", "start")] == 1
    assert summary_map[("retrieve", "success")] == 1


def test_cancel_job(db, job_id):
    db.create_job(job_id)
    assert db.is_cancelled(job_id) is False

    db.cancel_job(job_id)
    assert db.is_cancelled(job_id) is True


def test_is_cancelled_unknown_job_returns_false(db):
    assert db.is_cancelled(f"nonexistent-{uuid.uuid4()}") is False


def test_concurrent_jobs_do_not_interfere(db):
    job_a, job_b = f"a-{uuid.uuid4()}", f"b-{uuid.uuid4()}"
    db.create_job(job_a)
    db.create_job(job_b)
    db.add_event(job_a, mrn="MRN_A", stage="retrieve", event_type="success")
    db.add_event(job_b, mrn="MRN_B", stage="export", event_type="failure", error_message="boom")

    assert db.list_job_patients(job_a) == ["MRN_A"]
    assert db.list_job_patients(job_b) == ["MRN_B"]
    assert db.summarize_job(job_a) != db.summarize_job(job_b)


def test_get_job_returns_metadata(db, job_id):
    db.create_job(job_id, description="a batch", created_by="alice", project_id=None)
    job = db.get_job(job_id)
    assert job["job_id"] == job_id
    assert job["description"] == "a batch"
    assert job["created_by"] == "alice"
    assert job["cancelled"] is False


def test_get_job_unknown_returns_none(db):
    assert db.get_job(f"nonexistent-{uuid.uuid4()}") is None


def test_get_latest_retrieve_details_returns_most_recent_success_per_patient(db, job_id):
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="start")
    db.add_event(
        job_id, mrn="MRN1", stage="retrieve", event_type="success",
        details={"in_mosaiq": True, "in_pinnacle": False, "in_proknow": False, "status": "imported"},
    )
    # a later, distinct success event for the same patient should win
    db.add_event(
        job_id, mrn="MRN1", stage="retrieve", event_type="success",
        details={"in_mosaiq": True, "in_pinnacle": True, "in_proknow": False, "status": "re-imported"},
    )
    db.add_event(job_id, mrn="MRN2", stage="retrieve", event_type="failure", error_message="boom")

    details = db.get_latest_retrieve_details(job_id)
    assert details["MRN1"] == {"in_mosaiq": True, "in_pinnacle": True, "in_proknow": False, "status": "re-imported"}
    assert "MRN2" not in details  # only failures recorded -- no success details to show


def test_get_latest_retrieve_details_ignores_export_stage(db, job_id):
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="export", event_type="success", details={"status": "exported"})
    assert db.get_latest_retrieve_details(job_id) == {}


def test_get_latest_event_per_patient_sees_failure_only_patients(db, job_id):
    """
    The gap get_latest_retrieve_details leaves: a patient that only ever failed
    is absent from that dict entirely, which is exactly the patient someone
    debugging is looking for.
    """
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="start")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="failure", error_message="boom")

    assert "MRN1" not in db.get_latest_retrieve_details(job_id)

    latest = db.get_latest_event_per_patient(job_id)
    assert latest["MRN1"]["event_type"] == "failure"
    assert latest["MRN1"]["error_message"] == "boom"


def test_get_latest_event_per_patient_reports_a_trailing_start_as_in_flight(db, job_id):
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="start")

    latest = db.get_latest_event_per_patient(job_id)
    assert latest["MRN1"]["event_type"] == "start"
    assert latest["MRN1"]["error_message"] is None


def test_get_latest_event_per_patient_picks_the_newest_across_stages(db, job_id):
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success")
    db.add_event(job_id, mrn="MRN1", stage="export", event_type="failure", error_message="c-move refused")
    db.add_event(job_id, mrn="MRN2", stage="retrieve", event_type="success")

    latest = db.get_latest_event_per_patient(job_id)
    assert latest["MRN1"]["stage"] == "export"
    assert latest["MRN1"]["event_type"] == "failure"
    assert latest["MRN2"]["event_type"] == "success"
    assert set(latest) == {"MRN1", "MRN2"}


def test_count_imported_patients_zero_imported(db, job_id):
    db.create_job(job_id)
    db.add_patient(job_id, mrn="MRN1")
    db.add_patient(job_id, mrn="MRN2")
    # both ran without raising, but found nothing -- event_type='success'
    # alone must not be mistaken for "imported"
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"imported": False})
    db.add_event(job_id, mrn="MRN2", stage="retrieve", event_type="failure", error_message="boom")

    assert db.count_imported_patients(job_id) == (0, 2)


def test_count_imported_patients_all_imported(db, job_id):
    db.create_job(job_id)
    db.add_patient(job_id, mrn="MRN1")
    db.add_patient(job_id, mrn="MRN2")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"imported": True})
    db.add_event(job_id, mrn="MRN2", stage="retrieve", event_type="success", details={"imported": True})

    assert db.count_imported_patients(job_id) == (2, 2)


def test_count_imported_patients_some_imported(db, job_id):
    db.create_job(job_id)
    db.add_patient(job_id, mrn="MRN1")
    db.add_patient(job_id, mrn="MRN2")
    db.add_patient(job_id, mrn="MRN3")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"imported": True})
    db.add_event(job_id, mrn="MRN2", stage="retrieve", event_type="success", details={"imported": False})
    db.add_event(job_id, mrn="MRN3", stage="retrieve", event_type="failure", error_message="boom")

    assert db.count_imported_patients(job_id) == (1, 3)


def test_count_imported_patients_ignores_export_stage_success(db, job_id):
    """An export-stage success must not count toward "imported" -- only a
    retrieve-stage success with details.imported == true counts."""
    db.create_job(job_id)
    db.add_patient(job_id, mrn="MRN1")
    db.add_event(job_id, mrn="MRN1", stage="export", event_type="success", details={"imported": True})

    assert db.count_imported_patients(job_id) == (0, 1)


def test_count_imported_patients_counts_distinct_mrn_once(db, job_id):
    """A patient re-imported (e.g. retried) more than once in the same job
    must only count once toward imported_count, not once per success event."""
    db.create_job(job_id)
    db.add_patient(job_id, mrn="MRN1")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"imported": True})
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"imported": True})

    assert db.count_imported_patients(job_id) == (1, 1)


def test_count_imported_patients_unknown_job_returns_zeroes(db):
    assert db.count_imported_patients(f"nonexistent-{uuid.uuid4()}") == (0, 0)
