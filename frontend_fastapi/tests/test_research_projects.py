"""
Tests for routers/research_projects.py -- the ethics/research-project
workflow (list/create/detail/submit/review/revoke/membership) plus the
document-access-control fix and ethics-workflow polish added in Phase 2
(see that router's module docstring and
docs/frontend-rewrite-implementation-plan.md Phase 2).

backend_client's project functions are monkeypatched directly with
AsyncMock, one function at a time -- mirroring frontend/research_projects/
tests.py's existing `mock.patch("research_projects.views.backend_client")`
approach (a full module stub), just scoped to whichever calls a given test
actually needs. The autouse `_isolated_backend_client` fixture in
conftest.py already gives every test a safe default for
list_user_active_projects (called by get_template_context on every
authenticated render); tests below override individual functions as needed.
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from frontend_fastapi import backend_client
from frontend_fastapi.models import ProjectDocument

PROJECT_ID = "proj-1"


def _project(
    project_id=PROJECT_ID, title="Test Project", status="draft", created_by="alice",
    members=None, ethics_reference=None, description="A test project.",
    expiry_date=None, audit_log=None,
):
    return {
        "project_id": project_id,
        "title": title,
        "description": description,
        "ethics_reference": ethics_reference,
        "status": status,
        "created_by": created_by,
        "reviewed_by": None,
        "review_comment": None,
        "submitted_at": None,
        "approved_at": None,
        "expiry_date": expiry_date,
        "created_at": "2026-01-01T00:00:00+00:00",
        "members": members if members is not None else [{"username": created_by, "role": "owner"}],
        "audit_log": audit_log if audit_log is not None else [],
    }


@pytest.fixture()
def mock_backend(monkeypatch):
    """Monkeypatches every backend_client function research_projects.py
    calls with an AsyncMock, returning the mocks in a dict keyed by name.
    list_user_active_projects/list_project_jobs get an empty-list default
    since almost every test renders a page that calls them incidentally;
    everything else is per-test."""
    mocks = {}
    for name in (
        "list_projects", "get_project", "create_project", "submit_project",
        "review_project", "revoke_project", "add_member", "remove_member",
        "list_project_jobs", "list_user_active_projects",
    ):
        m = AsyncMock()
        monkeypatch.setattr(backend_client, name, m)
        mocks[name] = m
    mocks["list_user_active_projects"].return_value = []
    mocks["list_project_jobs"].return_value = []
    return mocks


@pytest.fixture()
def media_root(monkeypatch, tmp_path):
    """Redirects document storage to a throwaway directory -- research_projects.py
    imported MEDIA_ROOT by value (`from ... import MEDIA_ROOT`), so patching
    settings.MEDIA_ROOT itself wouldn't be seen; the router module's own
    name has to be patched instead."""
    from frontend_fastapi.routers import research_projects
    monkeypatch.setattr(research_projects, "MEDIA_ROOT", tmp_path)
    return tmp_path


# ---- project_list ----

def test_staff_sees_every_project_and_can_filter_by_status(client, make_user, login, csrf_token, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["list_projects"].return_value = [_project(status="approved")]

    resp = client.get("/projects?status=approved")

    assert resp.status_code == 200
    mock_backend["list_projects"].assert_awaited_once_with(status="approved")


def test_non_staff_sees_only_their_own_projects_and_status_filter_is_ignored(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = [_project()]

    resp = client.get("/projects?status=approved")

    assert resp.status_code == 200
    mock_backend["list_projects"].assert_awaited_once_with(username="alice")


def test_backend_error_shows_inline_message_instead_of_crashing(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].side_effect = backend_client.BackendError(503, "projects service down")

    resp = client.get("/projects")

    assert resp.status_code == 200
    assert "projects service down" in resp.text


def test_expiring_soon_banner_shown_for_project_within_window(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = []
    soon = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    mock_backend["list_user_active_projects"].return_value = [_project(title="Expiring Soon", status="approved", expiry_date=soon)]

    resp = client.get("/projects")

    assert "Expiring Soon" in resp.text
    assert "expires in" in resp.text


def test_expiring_soon_banner_absent_outside_window(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = []
    far = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
    mock_backend["list_user_active_projects"].return_value = [_project(title="Far Off", status="approved", expiry_date=far)]

    resp = client.get("/projects")

    assert "expires in" not in resp.text


def test_expiring_soon_banner_absent_for_open_ended_approval(client, make_user, login, mock_backend):
    """A project with no expiry_date at all is never flagged -- nothing to warn about."""
    make_user(username="alice")
    login("alice")
    mock_backend["list_projects"].return_value = []
    mock_backend["list_user_active_projects"].return_value = [_project(status="approved", expiry_date=None)]

    resp = client.get("/projects")

    assert "expires in" not in resp.text


# ---- project_create ----

def test_create_form_renders(client, make_user, login):
    make_user(username="alice")
    login("alice")
    resp = client.get("/projects/new")
    assert resp.status_code == 200
    assert "Request a new project" in resp.text


def test_create_submits_and_redirects_to_detail(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["create_project"].return_value = {"project_id": PROJECT_ID}

    resp = client.post("/projects/new", data={
        "title": "New Project", "description": "desc", "ethics_reference": "",
        "csrf_token": csrf_token(),
    }, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"http://localhost/projects/{PROJECT_ID}"
    mock_backend["create_project"].assert_awaited_once_with(
        title="New Project", created_by="alice", description="desc", ethics_reference="",
    )


def test_create_requires_a_title(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")

    resp = client.post("/projects/new", data={"title": "", "csrf_token": csrf_token()})

    assert resp.status_code == 400
    mock_backend["create_project"].assert_not_awaited()


def test_create_backend_error_shown_inline(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["create_project"].side_effect = backend_client.BackendError(500, "boom")

    resp = client.post("/projects/new", data={"title": "New Project", "csrf_token": csrf_token()})

    assert resp.status_code == 400
    assert "boom" in resp.text


# ---- review_queue ----

def test_review_queue_requires_staff(client, make_user, login):
    make_user(username="alice", is_staff=False)
    login("alice")
    resp = client.get("/projects/review")
    assert resp.status_code == 403


def test_review_queue_lists_submitted_projects(client, make_user, login, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["list_projects"].return_value = [_project(status="submitted", title="Needs Review")]

    resp = client.get("/projects/review")

    assert resp.status_code == 200
    assert "Needs Review" in resp.text
    mock_backend["list_projects"].assert_awaited_once_with(status="submitted")


# ---- project_detail ----

def test_detail_shows_submit_button_for_draft_member(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["get_project"].return_value = _project(status="draft", members=[{"username": "alice", "role": "owner"}])

    resp = client.get(f"/projects/{PROJECT_ID}")

    assert resp.status_code == 200
    # The timeline stepper also renders the *label* "Submit for review" for
    # any draft project regardless of viewer -- check for the actual button
    # (its form action), not just the phrase, to avoid a false positive.
    assert f'action="/projects/{PROJECT_ID}/submit"' in resp.text


def test_detail_hides_submit_button_for_non_member(client, make_user, login, mock_backend):
    make_user(username="bob")
    login("bob")
    mock_backend["get_project"].return_value = _project(status="draft", members=[{"username": "alice", "role": "owner"}])

    resp = client.get(f"/projects/{PROJECT_ID}")

    assert resp.status_code == 200
    assert f'action="/projects/{PROJECT_ID}/submit"' not in resp.text


def test_detail_shows_review_form_only_for_staff_on_submitted_project(client, make_user, login, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["get_project"].return_value = _project(status="submitted")

    resp = client.get(f"/projects/{PROJECT_ID}")

    assert "Review decision" in resp.text


def test_detail_hides_review_form_for_non_staff(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["get_project"].return_value = _project(status="submitted", members=[{"username": "alice", "role": "owner"}])

    resp = client.get(f"/projects/{PROJECT_ID}")

    assert "Review decision" not in resp.text


def test_detail_backend_error_redirects_to_list_with_flash(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["get_project"].side_effect = backend_client.BackendError(404, "no such project")

    resp = client.get(f"/projects/{PROJECT_ID}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "http://localhost/projects"
    resp2 = client.get("/projects")
    assert "no such project" in resp2.text


def test_detail_days_remaining_banner_for_member_of_expiring_project(client, make_user, login, mock_backend):
    make_user(username="alice")
    login("alice")
    # +1 hour of margin: days_remaining is a floor (timedelta.days truncates
    # toward zero), so a bare "+5 days" computed here can read as 4 by the
    # time the route recomputes "now" a moment later.
    soon = (datetime.now(timezone.utc) + timedelta(days=5, hours=1)).isoformat()
    mock_backend["get_project"].return_value = _project(
        status="approved", expiry_date=soon, members=[{"username": "alice", "role": "owner"}],
    )

    resp = client.get(f"/projects/{PROJECT_ID}")

    assert "This project expires in 5 days" in resp.text


def test_detail_days_remaining_banner_absent_for_non_member(client, make_user, login, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    mock_backend["get_project"].return_value = _project(
        status="approved", expiry_date=soon, members=[{"username": "alice", "role": "owner"}],
    )

    resp = client.get(f"/projects/{PROJECT_ID}")

    assert "This project expires" not in resp.text


def test_detail_shows_uploader_and_date_on_documents(client, make_user, login, mock_backend, db):
    make_user(username="alice")
    login("alice")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])
    db.add(ProjectDocument(
        project_id=PROJECT_ID, file_path="ethics_documents/proj-1/x.pdf",
        original_filename="ethics-approval.pdf", uploaded_by="bob",
    ))
    db.commit()

    resp = client.get(f"/projects/{PROJECT_ID}")

    assert "ethics-approval.pdf" in resp.text
    assert "bob" in resp.text


# ---- project_submit / project_review / project_revoke ----

def test_submit_calls_backend_and_flashes_success(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")

    resp = client.post(f"/projects/{PROJECT_ID}/submit", data={"csrf_token": csrf_token()}, follow_redirects=False)

    assert resp.status_code == 303
    mock_backend["submit_project"].assert_awaited_once_with(PROJECT_ID, "alice")


def test_submit_backend_error_flashes_error(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")
    mock_backend["submit_project"].side_effect = backend_client.BackendError(403, "not a member")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    # POST redirects to project_detail, which the client auto-follows --
    # the flash is popped and rendered on THAT page, not /projects.
    resp = client.post(f"/projects/{PROJECT_ID}/submit", data={"csrf_token": csrf_token()})

    assert "not a member" in resp.text


def test_review_requires_staff(client, make_user, login, csrf_token):
    make_user(username="alice", is_staff=False)
    login("alice")
    resp = client.post(f"/projects/{PROJECT_ID}/review", data={"decision": "approve", "csrf_token": csrf_token()})
    assert resp.status_code == 403


def test_review_approve_without_expiry_is_rejected_without_calling_backend(client, make_user, login, csrf_token, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["get_project"].return_value = _project(status="submitted")

    resp = client.post(f"/projects/{PROJECT_ID}/review", data={"decision": "approve", "csrf_token": csrf_token()})

    mock_backend["review_project"].assert_not_awaited()
    assert "expiry date is required" in resp.text.lower()


def test_review_approve_with_expiry_calls_backend(client, make_user, login, csrf_token, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")

    client.post(f"/projects/{PROJECT_ID}/review", data={
        "decision": "approve", "expiry_date": "2027-01-01", "comment": "looks good", "csrf_token": csrf_token(),
    })

    mock_backend["review_project"].assert_awaited_once_with(
        PROJECT_ID, reviewer="admin", approved=True, comment="looks good", expiry_date=date(2027, 1, 1),
    )


def test_review_reject_does_not_require_expiry(client, make_user, login, csrf_token, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")

    client.post(f"/projects/{PROJECT_ID}/review", data={"decision": "reject", "csrf_token": csrf_token()})

    mock_backend["review_project"].assert_awaited_once_with(
        PROJECT_ID, reviewer="admin", approved=False, comment="", expiry_date=None,
    )


def test_revoke_requires_staff(client, make_user, login, csrf_token):
    make_user(username="alice", is_staff=False)
    login("alice")
    resp = client.post(f"/projects/{PROJECT_ID}/revoke", data={"csrf_token": csrf_token()})
    assert resp.status_code == 403


def test_revoke_calls_backend_with_comment(client, make_user, login, csrf_token, mock_backend):
    make_user(username="admin", is_staff=True)
    login("admin")

    client.post(f"/projects/{PROJECT_ID}/revoke", data={"comment": "superseded", "csrf_token": csrf_token()})

    mock_backend["revoke_project"].assert_awaited_once_with(PROJECT_ID, revoked_by="admin", comment="superseded")


# ---- membership ----

def test_add_member_calls_backend(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")

    client.post(f"/projects/{PROJECT_ID}/members", data={
        "username": "carol", "role": "member", "csrf_token": csrf_token(),
    })

    mock_backend["add_member"].assert_awaited_once_with(PROJECT_ID, "carol", added_by="alice", role="member")


def test_remove_member_calls_backend(client, make_user, login, csrf_token, mock_backend):
    make_user(username="alice")
    login("alice")

    client.post(f"/projects/{PROJECT_ID}/members/carol/remove", data={"csrf_token": csrf_token()})

    mock_backend["remove_member"].assert_awaited_once_with(PROJECT_ID, "carol", removed_by="alice")


# ---- documents: upload/download/delete access control ----

def test_upload_rejected_for_non_member_non_staff(client, make_user, login, csrf_token, mock_backend, media_root):
    make_user(username="mallory")
    login("mallory")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.post(
        f"/projects/{PROJECT_ID}/documents/upload",
        data={"csrf_token": csrf_token()}, files={"file": ("doc.pdf", b"content", "application/pdf")},
    )

    assert resp.status_code == 403


def test_upload_succeeds_for_member_and_saves_row(client, make_user, login, csrf_token, mock_backend, media_root, db):
    make_user(username="alice")
    login("alice")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.post(
        f"/projects/{PROJECT_ID}/documents/upload",
        data={"csrf_token": csrf_token()}, files={"file": ("doc.pdf", b"content", "application/pdf")},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    doc = db.query(ProjectDocument).filter_by(project_id=PROJECT_ID).one()
    assert doc.original_filename == "doc.pdf"
    assert doc.uploaded_by == "alice"
    assert (media_root / doc.file_path).is_file()


def test_upload_rejects_a_file_over_the_size_limit(client, make_user, login, csrf_token, mock_backend, media_root, monkeypatch, db):
    from frontend_fastapi.routers import research_projects
    monkeypatch.setattr(research_projects, "_MAX_DOCUMENT_SIZE_BYTES", 10)  # tiny cap so the test body stays small
    make_user(username="alice")
    login("alice")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.post(
        f"/projects/{PROJECT_ID}/documents/upload",
        data={"csrf_token": csrf_token()}, files={"file": ("big.pdf", b"x" * 100, "application/pdf")},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert db.query(ProjectDocument).filter_by(project_id=PROJECT_ID).one_or_none() is None
    assert list((media_root / "ethics_documents" / PROJECT_ID).iterdir()) == []


def test_upload_succeeds_for_staff_non_member(client, make_user, login, csrf_token, mock_backend, media_root):
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.post(
        f"/projects/{PROJECT_ID}/documents/upload",
        data={"csrf_token": csrf_token()}, files={"file": ("doc.pdf", b"content", "application/pdf")},
        follow_redirects=False,
    )

    assert resp.status_code == 303


def _existing_doc(db, uploaded_by="alice", filename="doc.pdf", content=b"hello"):
    import io
    from frontend_fastapi.routers.research_projects import _save_document_sync
    file_path = _save_document_sync(io.BytesIO(content), PROJECT_ID, filename)
    doc = ProjectDocument(project_id=PROJECT_ID, file_path=file_path, original_filename=filename, uploaded_by=uploaded_by)
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def test_download_rejected_for_non_member_non_staff(client, make_user, login, mock_backend, media_root, db):
    doc = _existing_doc(db)
    make_user(username="mallory")
    login("mallory")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.get(f"/projects/{PROJECT_ID}/documents/{doc.id}/download")

    assert resp.status_code == 403


def test_download_succeeds_for_member(client, make_user, login, mock_backend, media_root, db):
    doc = _existing_doc(db)
    make_user(username="alice")
    login("alice")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.get(f"/projects/{PROJECT_ID}/documents/{doc.id}/download")

    assert resp.status_code == 200
    assert resp.content == b"hello"


def test_download_succeeds_for_staff_non_member(client, make_user, login, mock_backend, media_root, db):
    """The scenario the corrected design specifically exists to keep
    working: a data custodian who is not a project member must still be
    able to open a submitted project's documents before approving/
    rejecting it."""
    doc = _existing_doc(db)
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.get(f"/projects/{PROJECT_ID}/documents/{doc.id}/download")

    assert resp.status_code == 200


def test_download_unauthenticated_redirects_to_login(client, media_root, db):
    doc = _existing_doc(db)
    resp = client.get(f"/projects/{PROJECT_ID}/documents/{doc.id}/download", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/accounts/login")


def test_download_404s_for_unknown_document(client, make_user, login, media_root):
    make_user(username="alice")
    login("alice")
    resp = client.get(f"/projects/{PROJECT_ID}/documents/999999/download")
    assert resp.status_code == 404


def test_download_404s_when_doc_belongs_to_a_different_project(client, make_user, login, media_root, db):
    doc = _existing_doc(db)
    make_user(username="alice")
    login("alice")
    resp = client.get(f"/projects/other-project/documents/{doc.id}/download")
    assert resp.status_code == 404


def test_delete_rejected_for_member_who_did_not_upload_it(client, make_user, login, csrf_token, mock_backend, media_root, db):
    doc = _existing_doc(db, uploaded_by="alice")
    make_user(username="carol")
    login("carol")
    mock_backend["get_project"].return_value = _project(members=[
        {"username": "alice", "role": "owner"}, {"username": "carol", "role": "member"},
    ])

    resp = client.post(f"/projects/{PROJECT_ID}/documents/{doc.id}/delete", data={"csrf_token": csrf_token()})

    assert resp.status_code == 403
    assert (media_root / doc.file_path).is_file()


def test_delete_succeeds_for_uploader(client, make_user, login, csrf_token, mock_backend, media_root, db):
    doc = _existing_doc(db, uploaded_by="alice")
    make_user(username="alice")
    login("alice")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.post(
        f"/projects/{PROJECT_ID}/documents/{doc.id}/delete", data={"csrf_token": csrf_token()}, follow_redirects=False,
    )

    assert resp.status_code == 303
    assert db.query(ProjectDocument).filter_by(id=doc.id).one_or_none() is None
    assert not (media_root / doc.file_path).is_file()


def test_delete_succeeds_for_staff_even_if_not_uploader(client, make_user, login, csrf_token, mock_backend, media_root, db):
    doc = _existing_doc(db, uploaded_by="alice")
    make_user(username="admin", is_staff=True)
    login("admin")
    mock_backend["get_project"].return_value = _project(members=[{"username": "alice", "role": "owner"}])

    resp = client.post(
        f"/projects/{PROJECT_ID}/documents/{doc.id}/delete", data={"csrf_token": csrf_token()}, follow_redirects=False,
    )

    assert resp.status_code == 303
