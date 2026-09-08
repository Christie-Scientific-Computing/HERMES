import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from frontend_fastapi import security
from frontend_fastapi.models import ProjectDocument, User
from frontend_fastapi.scripts.migrate_from_django import (
    _open_django_db,
    _parse_django_datetime,
    fetch_django_documents,
    fetch_django_users,
    migrate_documents,
    migrate_users,
)


@pytest.fixture
def django_db_path(tmp_path) -> Path:
    """A throwaway sqlite file with just enough of Django's schema
    (auth_user, accounts_profile, research_projects_projectdocument) to
    exercise the fetch/migrate functions -- not a real Django-managed DB,
    but the same column shapes this script actually reads."""
    path = tmp_path / "django.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE auth_user (
            id INTEGER PRIMARY KEY, username TEXT, email TEXT,
            first_name TEXT, last_name TEXT,
            is_staff INTEGER, is_superuser INTEGER, is_active INTEGER
        );
        CREATE TABLE accounts_profile (
            id INTEGER PRIMARY KEY, user_id INTEGER, department TEXT
        );
        CREATE TABLE research_projects_projectdocument (
            id INTEGER PRIMARY KEY, project_id TEXT, file TEXT,
            uploaded_by TEXT, uploaded_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO auth_user VALUES (1, 'alice', 'alice@example.com', 'Alice', 'Smith', 0, 0, 1)"
    )
    conn.execute("INSERT INTO accounts_profile VALUES (1, 1, 'Oncology')")
    conn.execute(
        "INSERT INTO auth_user VALUES (2, 'bob', 'bob@example.com', 'Bob', 'Jones', 1, 1, 1)"
    )
    # bob has no accounts_profile row -- department should default to ''.
    conn.execute(
        "INSERT INTO research_projects_projectdocument VALUES "
        "(1, 'proj-1', 'ethics_documents/proj-1/cert.pdf', 'alice', '2026-01-15 10:30:00.123456')"
    )
    conn.commit()
    conn.close()
    return path


def test_open_django_db_is_read_only(django_db_path):
    conn = _open_django_db(django_db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO auth_user VALUES (99, 'evil', '', '', '', 0, 0, 1)")
    finally:
        conn.close()


def test_open_django_db_missing_file_raises(tmp_path):
    with pytest.raises(SystemExit):
        _open_django_db(tmp_path / "nope.sqlite3")


def test_fetch_django_users_reads_profile_department_via_left_join(django_db_path):
    conn = _open_django_db(django_db_path)
    try:
        users = fetch_django_users(conn)
    finally:
        conn.close()

    by_username = {row["username"]: row for row in users}
    assert by_username["alice"]["department"] == "Oncology"
    assert by_username["bob"]["department"] == ""  # no accounts_profile row for bob
    assert by_username["bob"]["is_staff"] == 1
    assert by_username["bob"]["is_superuser"] == 1


def test_fetch_django_documents(django_db_path):
    conn = _open_django_db(django_db_path)
    try:
        docs = fetch_django_documents(conn)
    finally:
        conn.close()

    assert len(docs) == 1
    assert docs[0]["file"] == "ethics_documents/proj-1/cert.pdf"


def test_parse_django_datetime_attaches_utc():
    dt = _parse_django_datetime("2026-01-15 10:30:00.123456")
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).replace(tzinfo=None) == datetime(2026, 1, 15, 10, 30, 0, 123456)


class TestMigrateUsers:
    def test_dry_run_reports_but_does_not_write(self, db, django_db_path):
        conn = _open_django_db(django_db_path)
        try:
            django_users = fetch_django_users(conn)
        finally:
            conn.close()

        report = migrate_users(db, django_users, apply=False)

        assert sorted(report["to_create"]) == ["alice", "bob"]
        assert report["skipped_existing"] == []
        assert db.query(User).count() == 0

    def test_apply_creates_users_with_unusable_passwords_and_activation_links(self, db, django_db_path):
        conn = _open_django_db(django_db_path)
        try:
            django_users = fetch_django_users(conn)
        finally:
            conn.close()

        report = migrate_users(db, django_users, apply=True)
        db.commit()

        alice = db.query(User).filter_by(username="alice").one()
        assert alice.department == "Oncology"
        assert alice.email == "alice@example.com"
        assert not security.is_usable_password(alice.password_hash)
        assert dict(report["activation_links"])["alice"]  # a non-empty token was recorded

        bob = db.query(User).filter_by(username="bob").one()
        assert bob.is_staff is True
        assert bob.is_superuser is True

    def test_rerun_skips_already_migrated_usernames(self, db, django_db_path):
        conn = _open_django_db(django_db_path)
        try:
            django_users = fetch_django_users(conn)
        finally:
            conn.close()
        migrate_users(db, django_users, apply=True)
        db.commit()

        report = migrate_users(db, django_users, apply=True)
        db.commit()

        assert report["to_create"] == []
        assert sorted(report["skipped_existing"]) == ["alice", "bob"]
        assert db.query(User).count() == 2  # no duplicates


class TestMigrateDocuments:
    def test_dry_run_reports_missing_file_without_writing(self, db, tmp_path, django_db_path):
        conn = _open_django_db(django_db_path)
        try:
            django_documents = fetch_django_documents(conn)
        finally:
            conn.close()
        empty_media_root = tmp_path / "media"  # file deliberately not created here

        report = migrate_documents(db, django_documents, empty_media_root, apply=False)

        assert report["to_create"] == ["ethics_documents/proj-1/cert.pdf"]
        assert report["missing_files"] == ["ethics_documents/proj-1/cert.pdf"]
        assert db.query(ProjectDocument).count() == 0

    def test_apply_creates_document_row_when_file_present(self, db, tmp_path, django_db_path):
        conn = _open_django_db(django_db_path)
        try:
            django_documents = fetch_django_documents(conn)
        finally:
            conn.close()
        media_root = tmp_path / "media"
        (media_root / "ethics_documents" / "proj-1").mkdir(parents=True)
        (media_root / "ethics_documents" / "proj-1" / "cert.pdf").write_bytes(b"%PDF-fake")

        report = migrate_documents(db, django_documents, media_root, apply=True)
        db.commit()

        assert report["missing_files"] == []
        doc = db.query(ProjectDocument).one()
        assert doc.project_id == "proj-1"
        assert doc.file_path == "ethics_documents/proj-1/cert.pdf"
        assert doc.original_filename == "cert.pdf"
        assert doc.uploaded_by == "alice"

    def test_rerun_skips_already_migrated_file_paths(self, db, tmp_path, django_db_path):
        conn = _open_django_db(django_db_path)
        try:
            django_documents = fetch_django_documents(conn)
        finally:
            conn.close()
        media_root = tmp_path / "media"
        (media_root / "ethics_documents" / "proj-1").mkdir(parents=True)
        (media_root / "ethics_documents" / "proj-1" / "cert.pdf").write_bytes(b"%PDF-fake")
        migrate_documents(db, django_documents, media_root, apply=True)
        db.commit()

        report = migrate_documents(db, django_documents, media_root, apply=True)
        db.commit()

        assert report["to_create"] == []
        assert report["skipped_existing"] == ["ethics_documents/proj-1/cert.pdf"]
        assert db.query(ProjectDocument).count() == 1
