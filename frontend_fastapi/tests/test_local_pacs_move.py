"""
Tests for F004 (routers/local_pacs.py's move action). Both F001
(conquest_client) and F005 (backend_client.audit_local_pacs_move) are
monkeypatched at their boundaries -- this is the integration point, so its
own tests exercise the chain between them, not a live Conquest/backend
(per F004's own Testing considerations).
"""
from unittest.mock import AsyncMock, Mock

import pytest

from frontend_fastapi import backend_client
from frontend_fastapi.local_pacs import conquest_client as cc
from frontend_fastapi.models import LocalPacsDestination


@pytest.fixture()
def destination(db):
    d = LocalPacsDestination(ae_title="THIRDPARTY", display_name="Third Party PACS", created_by="system")
    db.add(d)
    db.commit()
    return d


@pytest.fixture()
def mock_audit(monkeypatch):
    m = AsyncMock(return_value={"ok": True, "job_id": "job-1"})
    monkeypatch.setattr(backend_client, "audit_local_pacs_move", m)
    return m


def _move(client, csrf_token, destination, study_instance_uid="1.2.3", patient_id="ANON1"):
    return client.post("/local_pacs/move", data={
        "csrf_token": csrf_token(), "patient_id": patient_id, "study_instance_uid": study_instance_uid,
        "destination_id": str(destination.id),
    }, follow_redirects=False)


def test_non_custodian_cannot_move(client, make_user, login, csrf_token, destination, mock_audit):
    make_user("alice", is_staff=False)
    login("alice")

    resp = _move(client, csrf_token, destination)

    assert resp.status_code == 403
    mock_audit.assert_not_called()


def test_successful_move_is_relayed_and_audited(client, make_user, login, csrf_token, destination, mock_audit, monkeypatch):
    make_user("bob", is_staff=True)
    login("bob")
    monkeypatch.setattr(cc, "move_study", Mock(return_value=cc.MoveResult(completed=1, failed=0, warning=0, status_code=0x0000)))

    resp = _move(client, csrf_token, destination)

    assert resp.status_code == 303
    cc.move_study.assert_called_once_with(study_instance_uid="1.2.3", destination_ae="THIRDPARTY")
    mock_audit.assert_called_once()
    call_kwargs = mock_audit.call_args.kwargs
    assert call_kwargs["outcome"] == "success"
    assert call_kwargs["performed_by"] == "bob"
    assert call_kwargs["anon_patient_id"] == "ANON1"


def test_dicom_level_failure_is_reported_and_still_audited(client, make_user, login, csrf_token, destination, mock_audit, monkeypatch):
    make_user("bob", is_staff=True)
    login("bob")
    monkeypatch.setattr(cc, "move_study", Mock(side_effect=cc.ConquestUnreachable("destination host down")))

    resp = client.post("/local_pacs/move", data={
        "csrf_token": csrf_token(), "patient_id": "ANON1", "study_instance_uid": "1.2.3",
        "destination_id": str(destination.id),
    }, follow_redirects=True)

    assert resp.status_code == 200
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["outcome"] == "failure"
    assert "destination host down" in mock_audit.call_args.kwargs["detail"]
    assert b"failed" in resp.content.lower() or b"destination host down" in resp.content


def test_move_succeeds_but_audit_fails_still_reports_success_with_a_warning(
    client, make_user, login, csrf_token, destination, monkeypatch,
):
    make_user("bob", is_staff=True)
    login("bob")
    monkeypatch.setattr(cc, "move_study", Mock(return_value=cc.MoveResult(completed=1, status_code=0x0000)))
    monkeypatch.setattr(
        backend_client, "audit_local_pacs_move",
        AsyncMock(side_effect=backend_client.BackendError(503, "HermesDB unreachable")),
    )

    resp = client.post("/local_pacs/move", data={
        "csrf_token": csrf_token(), "patient_id": "ANON1", "study_instance_uid": "1.2.3",
        "destination_id": str(destination.id),
    }, follow_redirects=True)

    assert resp.status_code == 200
    body = resp.content.lower()
    assert b"relayed" in body or b"success" in body
    assert b"audit" in body


def test_no_destinations_configured_shows_a_clear_message_not_a_broken_dropdown(client, make_user, login, monkeypatch):
    make_user("bob", is_staff=True)
    login("bob")
    monkeypatch.setattr(cc, "is_configured", Mock(return_value=True))
    monkeypatch.setattr(cc, "find_studies", Mock(return_value=[{
        "patient_id": "ANON1", "study_instance_uid": "1.2.3", "study_date": "20260101",
        "study_description": "Planning CT", "modalities_in_study": "CT", "accession_number": "ACC1",
        "series_count": "1",
    }]))

    resp = client.get("/local_pacs", params={"patient_id": "ANON1", "submitted": "1"})

    assert resp.status_code == 200
    assert b"No destinations configured" in resp.content
