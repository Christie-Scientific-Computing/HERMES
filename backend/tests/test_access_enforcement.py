import uuid

import pytest
from fastapi import HTTPException

from backend.src.access.db_client import AccessDB
from backend.src.projects import enforcement


@pytest.fixture
def db():
    return AccessDB()


@pytest.fixture
def username():
    return f"user-{uuid.uuid4()}"


def test_require_allowed_destination_allows_when_unrestricted(username):
    # No rows at all for this user -- must not raise.
    enforcement.require_allowed_destination(username, "dicom_modality", "AE_ONE")


def test_require_allowed_destination_allows_a_listed_destination(db, username):
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    enforcement.require_allowed_destination(username, "dicom_modality", "AE_ONE")  # must not raise


def test_require_allowed_destination_denies_an_unlisted_destination(db, username):
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    with pytest.raises(HTTPException) as exc:
        enforcement.require_allowed_destination(username, "dicom_modality", "AE_TWO")
    assert exc.value.status_code == 403


def test_require_allowed_destination_fails_closed_on_db_error(username, monkeypatch):
    """
    A DB error checking the allow-list must deny (503), never silently
    allow -- same fail-closed discipline as require_project_member /
    require_any_active_project (see test_projects_enforcement.py).
    """
    def boom(self, *args, **kwargs):
        raise ConnectionError("db is down")

    monkeypatch.setattr(AccessDB, "is_allowed", boom)
    with pytest.raises(HTTPException) as exc:
        enforcement.require_allowed_destination(username, "dicom_modality", "AE_ONE")
    assert exc.value.status_code == 503
