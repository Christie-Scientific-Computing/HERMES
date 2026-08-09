import uuid

import pytest

from backend.src.access.db_client import AccessDB


@pytest.fixture
def db():
    return AccessDB()


@pytest.fixture
def username():
    return f"user-{uuid.uuid4()}"


def test_new_user_has_no_destinations(db, username):
    assert db.list_for_user(username) == []


def test_add_records_a_destination(db, username):
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    rows = db.list_for_user(username)
    assert len(rows) == 1
    assert rows[0]["username"] == username
    assert rows[0]["destination_type"] == "dicom_modality"
    assert rows[0]["destination"] == "AE_ONE"
    assert rows[0]["added_by"] == "admin"
    assert rows[0]["added_at"] is not None
    assert rows[0]["id"] is not None


def test_add_is_idempotent_on_duplicate(db, username):
    """Same (username, destination_type, destination) added twice must not
    error or duplicate -- the unique constraint + ON CONFLICT DO NOTHING."""
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    rows = db.list_for_user(username)
    assert len(rows) == 1


def test_add_allows_same_destination_name_different_type(db, username):
    """(username, 'dicom_modality', 'X') and (username, 'proknow_collection', 'X')
    are distinct rows -- the unique constraint is on the full triple."""
    db.add(username, "dicom_modality", "X", added_by="admin")
    db.add(username, "proknow_collection", "X", added_by="admin")
    rows = db.list_for_user(username)
    assert len(rows) == 2


def test_remove_deletes_the_row(db, username):
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    row_id = db.list_for_user(username)[0]["id"]
    db.remove(username, row_id)
    assert db.list_for_user(username) == []


def test_remove_only_affects_the_given_username(db, username):
    other_username = f"other-{uuid.uuid4()}"
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    db.add(other_username, "dicom_modality", "AE_ONE", added_by="admin")
    row_id = db.list_for_user(other_username)[0]["id"]

    # Attempting to remove other_username's row while scoped to `username`
    # must not touch it -- the DELETE is scoped by username AND id.
    db.remove(username, row_id)
    assert len(db.list_for_user(other_username)) == 1


def test_remove_nonexistent_id_is_a_noop(db, username):
    db.remove(username, 999999999)  # must not raise


# ---- is_allowed: the core opt-in allow-list semantics ----

def test_is_allowed_true_for_anything_when_user_has_zero_rows(db, username):
    """Zero rows means 'no restriction configured' -- today's unrestricted
    behavior, unchanged. This is the boundary case the plan calls out
    explicitly and it must not be fail-closed."""
    assert db.is_allowed(username, "dicom_modality", "ANYTHING") is True
    assert db.is_allowed(username, "proknow_collection", "ANY_COLLECTION") is True


def test_is_allowed_true_for_listed_destination_once_restricted(db, username):
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    assert db.is_allowed(username, "dicom_modality", "AE_ONE") is True


def test_is_allowed_false_for_unlisted_destination_once_restricted(db, username):
    """The moment a user has >=1 row, the allow-list becomes exhaustive --
    any destination not explicitly listed is denied, including destinations
    that would have been fine before the first row was added."""
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    assert db.is_allowed(username, "dicom_modality", "AE_TWO") is False
    assert db.is_allowed(username, "proknow_collection", "SOME_COLLECTION") is False


def test_is_allowed_respects_destination_type(db, username):
    """Same destination string, different type, must not cross-match."""
    db.add(username, "dicom_modality", "SHARED_NAME", added_by="admin")
    assert db.is_allowed(username, "dicom_modality", "SHARED_NAME") is True
    assert db.is_allowed(username, "proknow_collection", "SHARED_NAME") is False


def test_is_allowed_reverts_to_unrestricted_after_last_row_removed(db, username):
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    assert db.is_allowed(username, "dicom_modality", "AE_TWO") is False

    row_id = db.list_for_user(username)[0]["id"]
    db.remove(username, row_id)

    assert db.is_allowed(username, "dicom_modality", "AE_TWO") is True


def test_is_allowed_is_per_user(db, username):
    other_username = f"other-{uuid.uuid4()}"
    db.add(username, "dicom_modality", "AE_ONE", added_by="admin")
    # other_username has no rows of its own -- unrestricted, independent of
    # what username is restricted to.
    assert db.is_allowed(other_username, "dicom_modality", "AE_TWO") is True
