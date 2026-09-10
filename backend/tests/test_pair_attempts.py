"""
Unit tests for results/endpoints.py's _pair_attempts -- the core logic
behind the patient-timeline reshape (F003): raw `events` rows collapsed
into one record per attempt, {mrn, stage, attempt, start_ts, end_ts,
outcome, error_message}. Pure function, no DB needed.
"""
import os

# backend.src.identity.anon reads ANON_DB_* into module-level constants
# once, at import time -- and importing results.endpoints below transitively
# imports anon. Every other test file that touches results.endpoints sets
# these first, for the same reason: whichever test file's import happens to
# run first in the process otherwise "locks in" anon as unconfigured for
# every test that follows, including in other files (see
# test_results_anon_boundary.py's identical block).
os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

from backend.src.results.endpoints import _pair_attempts


def _event(**overrides):
    base = {
        "job_id": "job-1", "mrn": "500123", "stage": "retrieve", "event_type": "start",
        "ts": "t0", "attempt": 1, "error_message": None, "task_id": None,
    }
    return {**base, **overrides}


def test_sync_start_and_success_are_paired_into_one_attempt():
    events = [_event(event_type="start", ts="t0"), _event(event_type="success", ts="t1")]
    paired = _pair_attempts(events, tasks_by_id={}, cancelled_jobs=set())
    assert paired == [{
        "mrn": "500123", "stage": "retrieve", "attempt": 1,
        "start_ts": "t0", "end_ts": "t1", "outcome": "success", "error_message": None,
    }]


def test_sync_start_and_failure_carries_the_error_message():
    events = [_event(event_type="start"), _event(event_type="failure", ts="t1", error_message="boom")]
    paired = _pair_attempts(events, tasks_by_id={}, cancelled_jobs=set())
    assert paired[0]["outcome"] == "failure"
    assert paired[0]["error_message"] == "boom"


def test_sync_unresolved_attempt_is_in_progress_by_default():
    events = [_event(event_type="start")]
    paired = _pair_attempts(events, tasks_by_id={}, cancelled_jobs=set())
    assert paired[0]["outcome"] == "in_progress"
    assert paired[0]["end_ts"] is None


def test_sync_unresolved_attempt_is_cancelled_when_its_job_is_cancelled():
    events = [_event(event_type="start", job_id="job-1")]
    paired = _pair_attempts(events, tasks_by_id={}, cancelled_jobs={"job-1"})
    assert paired[0]["outcome"] == "cancelled"
    assert paired[0]["end_ts"] is None


def test_task_linked_attempt_needs_no_pairing_and_uses_the_task_row():
    events = [_event(event_type="start", task_id=42), _event(event_type="success", task_id=42, ts="t1")]
    tasks_by_id = {42: {"started_at": "started", "finished_at": "finished", "state": "succeeded", "error_message": None}}
    paired = _pair_attempts(events, tasks_by_id, cancelled_jobs=set())
    assert paired == [{
        "mrn": "500123", "stage": "retrieve", "attempt": 1,
        "start_ts": "started", "end_ts": "finished", "outcome": "success", "error_message": None,
    }]


def test_task_linked_states_map_to_the_outcome_vocabulary():
    for state, outcome in [("succeeded", "success"), ("failed", "failure"), ("cancelled", "cancelled"),
                            ("running", "in_progress"), ("claimed", "in_progress"), ("queued", "in_progress")]:
        events = [_event(task_id=1)]
        tasks_by_id = {1: {"started_at": "s", "finished_at": None, "state": state, "error_message": None}}
        paired = _pair_attempts(events, tasks_by_id, cancelled_jobs=set())
        assert paired[0]["outcome"] == outcome, state


def test_task_linked_attempt_with_no_matching_task_row_defaults_to_in_progress():
    # get_tasks_by_ids didn't return this task_id at all (e.g. the caller
    # only fetched a subset) -- must degrade safely, not KeyError.
    events = [_event(task_id=99)]
    paired = _pair_attempts(events, tasks_by_id={}, cancelled_jobs=set())
    assert paired[0]["outcome"] == "in_progress"
    assert paired[0]["start_ts"] is None
    assert paired[0]["end_ts"] is None


def test_task_linked_error_message_falls_back_to_the_event_when_task_has_none():
    events = [_event(task_id=7, error_message="event-level error")]
    tasks_by_id = {7: {"started_at": "s", "finished_at": "f", "state": "failed", "error_message": None}}
    paired = _pair_attempts(events, tasks_by_id, cancelled_jobs=set())
    assert paired[0]["error_message"] == "event-level error"


def test_attempts_are_ordered_by_first_appearance():
    events = [
        _event(stage="retrieve", attempt=1, event_type="start", ts="t0"),
        _event(stage="retrieve", attempt=1, event_type="success", ts="t1"),
        _event(stage="export", attempt=1, event_type="start", ts="t2"),
    ]
    paired = _pair_attempts(events, tasks_by_id={}, cancelled_jobs=set())
    assert [p["stage"] for p in paired] == ["retrieve", "export"]
