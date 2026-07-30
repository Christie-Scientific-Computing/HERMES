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
