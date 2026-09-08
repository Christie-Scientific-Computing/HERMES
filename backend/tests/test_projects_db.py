import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.src.projects.db_client import ProjectsDB, ProjectNotFoundError


@pytest.fixture
def db():
    return ProjectsDB()


@pytest.fixture
def owner():
    return f"owner-{uuid.uuid4()}"


def _make_project(db, owner, title="Test project"):
    project_id = str(uuid.uuid4())
    db.create_project(project_id, title, owner, description="desc", ethics_reference="IRAS-123")
    return project_id


def test_create_project_sets_draft_status_and_owner_membership(db, owner):
    project_id = _make_project(db, owner)

    project = db.get_project(project_id)
    assert project["status"] == "draft"
    assert project["created_by"] == owner

    members = db.list_members(project_id)
    assert [m["username"] for m in members] == [owner]
    assert members[0]["role"] == "owner"
    assert db.is_member(project_id, owner) is True


def test_get_project_unknown_raises(db):
    with pytest.raises(ProjectNotFoundError):
        db.get_project(f"nonexistent-{uuid.uuid4()}")


def test_submit_then_review_approve_makes_project_active(db, owner):
    project_id = _make_project(db, owner)
    db.submit_project(project_id, owner)
    assert db.get_project(project_id)["status"] == "submitted"

    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    db.review_project(project_id, approved=True, reviewer="admin", comment="looks fine", expiry_date=expiry)

    project = db.get_project(project_id)
    assert project["status"] == "approved"
    assert project["reviewed_by"] == "admin"
    assert project["approved_at"] is not None

    assert db.is_project_active(project_id) is True
    assert db.is_active_member(project_id, owner) is True
    assert db.has_any_active_project(owner) is True
    assert project_id in [p["project_id"] for p in db.list_user_active_projects(owner)]


def test_review_reject_is_not_active(db, owner):
    project_id = _make_project(db, owner)
    db.submit_project(project_id, owner)
    db.review_project(project_id, approved=False, reviewer="admin", comment="not enough detail")

    project = db.get_project(project_id)
    assert project["status"] == "rejected"
    assert db.is_project_active(project_id) is False
    assert db.is_active_member(project_id, owner) is False
    assert db.has_any_active_project(owner) is False


def test_submit_requires_draft_status(db, owner):
    project_id = _make_project(db, owner)
    db.submit_project(project_id, owner)
    with pytest.raises(ProjectNotFoundError):
        db.submit_project(project_id, owner)  # already submitted, not draft


def test_review_requires_submitted_status(db, owner):
    project_id = _make_project(db, owner)
    with pytest.raises(ProjectNotFoundError):
        db.review_project(project_id, approved=True, reviewer="admin", expiry_date=datetime.now(timezone.utc))


def test_expired_project_is_not_active(db, owner):
    project_id = _make_project(db, owner)
    db.submit_project(project_id, owner)
    past_expiry = datetime.now(timezone.utc) - timedelta(days=1)
    db.review_project(project_id, approved=True, reviewer="admin", expiry_date=past_expiry)

    assert db.is_project_active(project_id) is False
    assert db.is_active_member(project_id, owner) is False
    assert db.has_any_active_project(owner) is False


def test_approval_with_no_expiry_date_never_expires(db, owner):
    project_id = _make_project(db, owner)
    db.submit_project(project_id, owner)
    db.review_project(project_id, approved=True, reviewer="admin", expiry_date=None)

    assert db.is_project_active(project_id) is True
    assert db.is_active_member(project_id, owner) is True


def test_revoke_deactivates_an_approved_project(db, owner):
    project_id = _make_project(db, owner)
    db.submit_project(project_id, owner)
    db.review_project(project_id, approved=True, reviewer="admin", expiry_date=None)
    assert db.is_project_active(project_id) is True

    db.revoke_project(project_id, revoked_by="admin", comment="funding withdrawn")
    assert db.get_project(project_id)["status"] == "revoked"
    assert db.is_project_active(project_id) is False


def test_revoke_requires_approved_status(db, owner):
    project_id = _make_project(db, owner)
    with pytest.raises(ProjectNotFoundError):
        db.revoke_project(project_id, revoked_by="admin")


def test_add_and_remove_member(db, owner):
    project_id = _make_project(db, owner)
    colleague = f"colleague-{uuid.uuid4()}"

    db.add_member(project_id, colleague, role="member", added_by=owner)
    usernames = [m["username"] for m in db.list_members(project_id)]
    assert set(usernames) == {owner, colleague}
    assert db.is_member(project_id, colleague) is True

    db.remove_member(project_id, colleague, removed_by=owner)
    assert db.is_member(project_id, colleague) is False


def test_member_of_one_project_not_active_member_of_another(db, owner):
    project_a = _make_project(db, owner, title="A")
    project_b = _make_project(db, owner, title="B")
    db.submit_project(project_a, owner)
    db.review_project(project_a, approved=True, reviewer="admin", expiry_date=None)
    # project_b stays in draft

    assert db.is_active_member(project_a, owner) is True
    assert db.is_active_member(project_b, owner) is False


def test_list_projects_filters_by_username_and_status(db, owner):
    other_owner = f"other-{uuid.uuid4()}"
    mine = _make_project(db, owner, title="mine")
    theirs = _make_project(db, other_owner, title="theirs")

    mine_projects = db.list_projects(username=owner)
    assert project_ids(mine_projects) == {mine}

    all_drafts = db.list_projects(status="draft")
    assert {mine, theirs}.issubset(project_ids(all_drafts))


def test_list_project_jobs_empty_when_no_jobs(db, owner):
    project_id = _make_project(db, owner)
    assert db.list_project_jobs(project_id) == []


def test_audit_log_records_lifecycle_actions(db, owner):
    project_id = _make_project(db, owner)
    db.submit_project(project_id, owner)
    db.review_project(project_id, approved=True, reviewer="admin", expiry_date=None)
    db.add_member(project_id, "colleague", added_by=owner)
    db.revoke_project(project_id, revoked_by="admin")

    actions = [entry["action"] for entry in db.list_audit_log(project_id)]
    assert actions == ["created", "submitted", "approved", "member_added", "revoked"]


def project_ids(projects: list[dict]) -> set:
    return {p["project_id"] for p in projects}


# ---- list_expiring_projects (Phase 4 admin dashboard) ----

def _approved_project(db, owner, expiry_date):
    project_id = _make_project(db, owner)
    db.submit_project(project_id, owner)
    db.review_project(project_id, approved=True, reviewer="admin", expiry_date=expiry_date)
    return project_id


def test_list_expiring_projects_includes_one_inside_the_window(db, owner):
    soon = datetime.now(timezone.utc) + timedelta(days=10)
    project_id = _approved_project(db, owner, soon)

    result = project_ids(db.list_expiring_projects(within_days=30))

    assert project_id in result


def test_list_expiring_projects_excludes_one_outside_the_window(db, owner):
    far = datetime.now(timezone.utc) + timedelta(days=90)
    project_id = _approved_project(db, owner, far)

    result = project_ids(db.list_expiring_projects(within_days=30))

    assert project_id not in result


def test_list_expiring_projects_includes_one_exactly_at_the_boundary(db, owner):
    # A few seconds INSIDE the 30-day window (not exactly +30d): the query's
    # own now() runs slightly after this now(), so a value timed to land
    # exactly on +30d would non-deterministically fall just outside the
    # window depending on that gap -- this asserts the boundary is honored
    # without being sensitive to that inherent skew.
    at_boundary = datetime.now(timezone.utc) + timedelta(days=30) - timedelta(seconds=5)
    project_id = _approved_project(db, owner, at_boundary)

    result = project_ids(db.list_expiring_projects(within_days=30))

    assert project_id in result


def test_list_expiring_projects_excludes_open_ended_approval(db, owner):
    """No expiry_date at all -- nothing to warn about, never qualifies."""
    project_id = _approved_project(db, owner, None)

    result = project_ids(db.list_expiring_projects(within_days=30))

    assert project_id not in result


def test_list_expiring_projects_excludes_non_approved_projects(db, owner):
    """A draft project with no expiry_date can't accidentally qualify --
    and a revoked project (which may still carry a future expiry_date from
    its now-void approval) must not appear either."""
    draft_id = _make_project(db, owner)

    soon = datetime.now(timezone.utc) + timedelta(days=10)
    revoked_id = _approved_project(db, owner, soon)
    db.revoke_project(revoked_id, revoked_by="admin")

    result = project_ids(db.list_expiring_projects(within_days=30))

    assert draft_id not in result
    assert revoked_id not in result
