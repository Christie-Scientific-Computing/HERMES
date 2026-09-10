"""Tests for F003 (routers/local_pacs.py's destination CRUD)."""
from frontend_fastapi.models import LocalPacsDestination


def test_non_custodian_is_forbidden(client, make_user, login):
    make_user("alice", is_staff=False)
    login("alice")

    resp = client.get("/local_pacs/destinations")

    assert resp.status_code == 403


def test_custodian_adds_a_destination(client, make_user, login, csrf_token, db):
    make_user("bob", is_staff=True)
    login("bob")

    resp = client.post("/local_pacs/destinations/add", data={
        "ae_title": "THIRDPARTY", "display_name": "Third Party PACS", "description": "External site",
        "csrf_token": csrf_token(),
    }, follow_redirects=False)

    assert resp.status_code == 303
    destination = db.query(LocalPacsDestination).filter_by(ae_title="THIRDPARTY").one()
    assert destination.display_name == "Third Party PACS"
    assert destination.created_by == "bob"


def test_invalid_ae_title_is_rejected_with_a_form_error(client, make_user, login, csrf_token):
    make_user("bob", is_staff=True)
    login("bob")

    resp = client.post("/local_pacs/destinations/add", data={
        "ae_title": "bad/title!!", "display_name": "X", "csrf_token": csrf_token(),
    })

    assert resp.status_code == 200
    assert b"AE titles may only contain" in resp.content


def test_duplicate_ae_title_is_rejected(client, make_user, login, csrf_token, db):
    make_user("bob", is_staff=True)
    login("bob")
    db.add(LocalPacsDestination(ae_title="DEST1", display_name="First", created_by="bob"))
    db.commit()

    resp = client.post("/local_pacs/destinations/add", data={
        "ae_title": "DEST1", "display_name": "Second", "csrf_token": csrf_token(),
    })

    assert resp.status_code == 200
    assert b"already exists" in resp.content


def test_custodian_edits_a_destination(client, make_user, login, csrf_token, db):
    make_user("bob", is_staff=True)
    login("bob")
    destination = LocalPacsDestination(ae_title="DEST1", display_name="Old name", created_by="bob")
    db.add(destination)
    db.commit()

    resp = client.post(f"/local_pacs/destinations/{destination.id}/edit", data={
        "ae_title": "DEST1", "display_name": "New name", "csrf_token": csrf_token(),
    }, follow_redirects=False)

    assert resp.status_code == 303
    db.refresh(destination)
    assert destination.display_name == "New name"


def test_custodian_removes_a_destination(client, make_user, login, csrf_token, db):
    make_user("bob", is_staff=True)
    login("bob")
    destination = LocalPacsDestination(ae_title="DEST1", display_name="Gone soon", created_by="bob")
    db.add(destination)
    db.commit()
    destination_id = destination.id

    resp = client.post(f"/local_pacs/destinations/{destination_id}/delete", data={
        "csrf_token": csrf_token(),
    }, follow_redirects=False)

    assert resp.status_code == 303
    assert db.query(LocalPacsDestination).filter_by(id=destination_id).one_or_none() is None
