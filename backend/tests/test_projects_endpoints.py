"""
Integration test for the review_project endpoint's Phase 4 addition:
notifying every current member of a project once a review decision lands
(backend/src/projects/endpoints.py). Doesn't need the PinnacleExport
submodule.
"""
import os
import uuid

# backend.src.identity.anon reads ANON_DB_* into module-level constants once,
# at import time -- and projects/endpoints.py (imported below) now imports
# anon too (F005's requested_patients anon-resolution). Every other test
# file that touches a module importing anon sets these first, for the same
# reason: whichever file's import happens to run first in the process
# otherwise "locks in" anon as unconfigured for every test that follows,
# including in other files (see test_results_anon_boundary.py's identical
# block, and test_pair_attempts.py's for the exact same fragility class).
os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.projects import endpoints as projects_endpoints
from backend.src.notifications.db_client import NotificationsDB

# Fixed real<->anon pairs seeded by backend/scripts/seed_anon_test_db.py --
# same ids test_results_anon_boundary.py's own header comment uses.
REAL_MRN = "500123"
ANON_MRN = "1001"
REAL_MRN_2 = "500456"
ANON_MRN_2 = "1002"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(projects_endpoints.router)
    return TestClient(app)


def _create_and_submit(client, owner):
    resp = client.post("/projects", json={"title": "Test project", "created_by": owner})
    project_id = resp.json()["project_id"]
    client.post(f"/projects/{project_id}/submit", json={"username": owner})
    return project_id


def test_review_approve_notifies_every_current_member(client):
    owner = f"owner-{uuid.uuid4()}"
    colleague = f"colleague-{uuid.uuid4()}"
    project_id = _create_and_submit(client, owner)
    client.post(f"/projects/{project_id}/members", json={"username": colleague, "added_by": owner})

    resp = client.post(f"/projects/{project_id}/review", json={
        "reviewer": "admin", "approved": True, "expiry_date": "2027-01-01T00:00:00Z",
    })
    assert resp.status_code == 200

    notifications_db = NotificationsDB()
    for member in (owner, colleague):
        notifications = notifications_db.list_for_user(member)
        assert len(notifications) == 1
        assert notifications[0]["kind"] == "project_reviewed"
        assert "approved" in notifications[0]["message"]
        assert notifications[0]["project_id"] == project_id


def test_review_reject_notifies_members_with_rejected_wording(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_and_submit(client, owner)

    resp = client.post(f"/projects/{project_id}/review", json={"reviewer": "admin", "approved": False})
    assert resp.status_code == 200

    notifications = NotificationsDB().list_for_user(owner)
    assert len(notifications) == 1
    assert "rejected" in notifications[0]["message"]


def test_review_does_not_notify_a_former_member_who_was_removed_before_review(client):
    owner = f"owner-{uuid.uuid4()}"
    former_member = f"former-{uuid.uuid4()}"
    project_id = _create_and_submit(client, owner)
    client.post(f"/projects/{project_id}/members", json={"username": former_member, "added_by": owner})
    client.delete(f"/projects/{project_id}/members/{former_member}", params={"removed_by": owner})

    resp = client.post(f"/projects/{project_id}/review", json={
        "reviewer": "admin", "approved": True, "expiry_date": "2027-01-01T00:00:00Z",
    })
    assert resp.status_code == 200

    assert NotificationsDB().list_for_user(former_member) == []
    assert len(NotificationsDB().list_for_user(owner)) == 1


# ---- Destinations / message_id at creation, amendments, requested patients (F005) ----

def _create_approve(client, owner, destinations=None, message_id=None):
    body = {"title": "Test project", "created_by": owner}
    if destinations is not None:
        body["destinations"] = destinations
    if message_id is not None:
        body["message_id"] = message_id
    project_id = client.post("/projects", json=body).json()["project_id"]
    client.post(f"/projects/{project_id}/submit", json={"username": owner})
    client.post(f"/projects/{project_id}/review", json={
        "reviewer": "admin", "approved": True, "expiry_date": "2027-01-01T00:00:00Z",
    })
    return project_id


def test_create_project_with_destinations_and_message_id_end_to_end(client):
    owner = f"owner-{uuid.uuid4()}"
    resp = client.post("/projects", json={
        "title": "Trial project", "created_by": owner, "message_id": 42,
        "destinations": [{"destination_type": "dicom", "destination_value": "TRIAL_AE"}],
    })
    assert resp.status_code == 200
    project_id = resp.json()["project_id"]

    project = client.get(f"/projects/{project_id}").json()
    assert project["message_id"] == 42
    assert project["destinations"] == [
        {"id": project["destinations"][0]["id"], "project_id": project_id,
         "destination_type": "dicom", "destination_value": "TRIAL_AE",
         "status": "active", "added_at": project["destinations"][0]["added_at"]},
    ]


def test_create_project_rejects_an_unknown_destination_type(client):
    owner = f"owner-{uuid.uuid4()}"
    resp = client.post("/projects", json={
        "title": "Trial project", "created_by": owner,
        "destinations": [{"destination_type": "carrier_pigeon", "destination_value": "x"}],
    })
    assert resp.status_code == 422


def test_propose_amendment_requires_membership(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    resp = client.post(f"/projects/{project_id}/amendments", json={
        "proposed_by": f"stranger-{uuid.uuid4()}", "message_id": 7,
    })
    assert resp.status_code == 403


def test_propose_amendment_rejects_a_second_one_while_first_still_pending(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    first = client.post(f"/projects/{project_id}/amendments", json={"proposed_by": owner, "message_id": 7})
    assert first.status_code == 200

    second = client.post(f"/projects/{project_id}/amendments", json={"proposed_by": owner, "message_id": 8})
    assert second.status_code == 409


def test_amendment_approve_and_reject_require_a_pending_amendment(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    assert client.post(f"/projects/{project_id}/amendments/approve", json={"reviewed_by": "admin"}).status_code == 404
    assert client.post(f"/projects/{project_id}/amendments/reject", json={"reviewed_by": "admin"}).status_code == 404


def test_amendment_approve_notifies_every_current_member(client):
    owner = f"owner-{uuid.uuid4()}"
    colleague = f"colleague-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)
    client.post(f"/projects/{project_id}/members", json={"username": colleague, "added_by": owner})
    client.post(f"/projects/{project_id}/amendments", json={"proposed_by": owner, "message_id": 7})

    resp = client.post(f"/projects/{project_id}/amendments/approve", json={"reviewed_by": "admin"})
    assert resp.status_code == 200
    assert resp.json()["message_id"] == 7

    for member in (owner, colleague):
        notifications = NotificationsDB().list_for_user(member)
        assert any(n["kind"] == "amendment_reviewed" and "approved" in n["message"] for n in notifications)


def test_pending_amendments_endpoint_lists_only_projects_with_one(client):
    owner = f"owner-{uuid.uuid4()}"
    with_amendment = _create_approve(client, owner)
    without_amendment = _create_approve(client, owner)
    client.post(f"/projects/{with_amendment}/amendments", json={"proposed_by": owner, "message_id": 7})

    result = client.get("/projects/pending_amendments").json()["projects"]
    ids = {p["project_id"] for p in result}
    assert with_amendment in ids
    assert without_amendment not in ids


def test_add_requested_patients_requires_membership(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    resp = client.post(f"/projects/{project_id}/requested_patients", json={
        "added_by": f"stranger-{uuid.uuid4()}", "mrns": ["MRN1"],
    })
    assert resp.status_code == 403


def test_add_requested_patients_end_to_end(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    # ANON_DB_* is set for this whole file (see module header), so these
    # must be real, resolvable anon ids -- the two fixed seeded pairs.
    resp = client.post(f"/projects/{project_id}/requested_patients", json={
        "added_by": owner, "mrns": [ANON_MRN, ANON_MRN_2],
    })
    assert resp.status_code == 200
    assert resp.json() == {"added": 2}


def test_add_requested_patients_resolves_anon_ids_to_real_ones(client):
    """project_requested_patients.mrn must hold the REAL id, matching
    every other mrn column in HermesDB (identity/anon.py's module
    docstring) -- not the anon id a caller submits."""
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    resp = client.post(f"/projects/{project_id}/requested_patients", json={
        "added_by": owner, "mrns": [ANON_MRN],
    })
    assert resp.status_code == 200

    from backend.src.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT mrn FROM project_requested_patients WHERE project_id = %s", (project_id,))
        stored = [row[0] for row in cur.fetchall()]
    assert stored == [REAL_MRN]


def test_add_requested_patients_rejects_an_unknown_anon_id(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    resp = client.post(f"/projects/{project_id}/requested_patients", json={
        "added_by": owner, "mrns": ["no-such-anon-id"],
    })
    assert resp.status_code == 422


def test_project_stats_reflects_requested_restored_and_sent(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    client.post(f"/projects/{project_id}/requested_patients", json={"added_by": owner, "mrns": [ANON_MRN, ANON_MRN_2]})

    from backend.src.status.db_client import StatusDB
    status_db = StatusDB()
    job_id = f"job-{uuid.uuid4()}"
    status_db.create_job(job_id, project_id=project_id)
    status_db.add_event(job_id, mrn=REAL_MRN, stage="retrieve", event_type="success", details={"imported": True})
    status_db.add_event(job_id, mrn=REAL_MRN, stage="export", event_type="success", details={"destination": "SCANNER_A"})

    resp = client.get(f"/projects/{project_id}/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["requested_count"] == 2
    assert body["restored_count"] == 1
    assert body["sent_by_destination"] == [{"destination": "SCANNER_A", "count": 1}]


def test_project_stats_zero_for_a_project_with_no_activity(client):
    owner = f"owner-{uuid.uuid4()}"
    project_id = _create_approve(client, owner)

    resp = client.get(f"/projects/{project_id}/stats")
    assert resp.status_code == 200
    assert resp.json() == {"requested_count": 0, "restored_count": 0, "sent_by_destination": []}
