"""
python -m frontend_fastapi.scripts.migrate_from_django [--django-db PATH] [--media-root PATH] [--apply] [--base-url URL]

One-off data migration for Phase 5 cutover
(docs/plans/frontend-rewrite-implementation-plan.md's "Migrate db.sqlite3's
auth_user/accounts_profile rows into the new users table" +
"ProjectDocument moves from Django's ORM to the new SQLAlchemy model").

Dry-run by default -- reports what it *would* do (counts, and every row it
would skip and why) without writing anything to this project's own DB.
Pass --apply to actually write. Safe to run more than once: already-migrated
usernames/documents are skipped, so a dry-run -> review -> apply -> (rerun
later to pick up anything added to Django in between) -> apply sequence
works without creating duplicates.

Opens the Django sqlite database read-only (a "file:...?mode=ro" URI
connection) -- this script must never be able to write back to the system
it's migrating FROM.

Password hashes are NOT carried over. Django hashes passwords with PBKDF2;
this project verifies with argon2 (security.verify_password), which never
matches a foreign hash format (see security.py's own comment on this) --
there is no way to convert one into the other without the plaintext
password. Every migrated user instead gets security.unusable_password()
(the same sentinel invite_submit uses for a brand-new invite) plus a signed
activation token, printed to stdout -- mirroring invite_submit's own
"the flash message is the only path" pattern (accounts.py), just via this
script's output instead of a flash message, since there's no admin request
in flight to attach one to. Distribute these out-of-band (the same way an
admin today copies an invite's activation link out of a flash message).

ProjectDocument rows are matched to Django's FileField VALUE (the path
relative to MEDIA_ROOT) as the dedup key -- Django's FileField already
renames on any storage collision, so this is unique within the source DB.
Only the DB row is created here; per the plan, the file itself is expected
to already be reachable at the same relative path under this project's own
HERMES_FRONTEND_MEDIA_ROOT (no copy is performed) -- this script verifies
that expectation per row and reports (never fails on) a missing file, since
a missing file is exactly the kind of surprise this dry-run-first design
exists to surface before cutover, not after.
"""
import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from frontend_fastapi import security
from frontend_fastapi.database import SessionLocal
from frontend_fastapi.models import ProjectDocument, User

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DJANGO_DB = _REPO_ROOT / "frontend" / "db.sqlite3"
_DEFAULT_MEDIA_ROOT = _REPO_ROOT / "frontend" / "media"


def _parse_django_datetime(value: str) -> datetime:
    """Django's sqlite backend stores USE_TZ=True datetimes as UTC, formatted
    without an offset ("YYYY-MM-DD HH:MM:SS[.ffffff]") -- attach UTC
    explicitly rather than leaving these naive, so they compare correctly
    against this project's own timezone-aware columns."""
    dt = datetime.fromisoformat(value.replace(" ", "T"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _open_django_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"Django database not found: {path}")
    # quote(): a path containing "?"/"#"/spaces would otherwise corrupt the
    # "file:...?mode=ro" URI's own query-string syntax -- not attacker input
    # (an operator-supplied --django-db path), but still worth getting right.
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_django_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT u.id, u.username, u.email, u.first_name, u.last_name,
               u.is_staff, u.is_superuser, u.is_active,
               COALESCE(p.department, '') AS department
        FROM auth_user u
        LEFT JOIN accounts_profile p ON p.user_id = u.id
        ORDER BY u.id
        """
    ).fetchall()


def fetch_django_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, project_id, file, uploaded_by, uploaded_at FROM research_projects_projectdocument ORDER BY id"
    ).fetchall()


def migrate_users(db, django_users: list[sqlite3.Row], *, apply: bool) -> dict:
    """Returns a report dict and, if apply, actually creates the rows.
    Existing usernames are skipped either way (idempotent rerun)."""
    existing = {row[0] for row in db.query(User.username).all()}
    report = {"to_create": [], "skipped_existing": [], "activation_links": []}

    for row in django_users:
        if row["username"] in existing:
            report["skipped_existing"].append(row["username"])
            continue
        report["to_create"].append(row["username"])
        if not apply:
            continue

        new_user = User(
            username=row["username"], email=row["email"] or "",
            first_name=row["first_name"] or "", last_name=row["last_name"] or "",
            department=row["department"], password_hash=security.unusable_password(),
            is_staff=bool(row["is_staff"]), is_superuser=bool(row["is_superuser"]),
            is_active=bool(row["is_active"]),
        )
        db.add(new_user)
        db.flush()  # need new_user.id for the activation token below
        token = security.make_account_token(new_user.id, new_user.password_hash)
        report["activation_links"].append((new_user.username, token))

    return report


def migrate_documents(db, django_documents: list[sqlite3.Row], media_root: Path, *, apply: bool) -> dict:
    """Returns a report dict and, if apply, actually creates the rows.
    Existing file_paths are skipped either way (idempotent rerun)."""
    existing = {row[0] for row in db.query(ProjectDocument.file_path).all()}
    report = {"to_create": [], "skipped_existing": [], "missing_files": []}

    for row in django_documents:
        file_path = row["file"]
        if file_path in existing:
            report["skipped_existing"].append(file_path)
            continue
        report["to_create"].append(file_path)
        if not (media_root / file_path).exists():
            report["missing_files"].append(file_path)
        if not apply:
            continue

        db.add(ProjectDocument(
            project_id=row["project_id"], file_path=file_path,
            original_filename=Path(file_path).name, uploaded_by=row["uploaded_by"],
            uploaded_at=_parse_django_datetime(row["uploaded_at"]),
        ))

    return report


def _print_report(users_report: dict, documents_report: dict, *, apply: bool) -> None:
    mode = "APPLIED" if apply else "DRY RUN (nothing written -- pass --apply to write)"
    print(f"=== {mode} ===")
    print()
    print(f"Users: {len(users_report['to_create'])} to create, {len(users_report['skipped_existing'])} already present (skipped)")
    for username in users_report["to_create"]:
        print(f"  + {username}")
    for username in users_report["skipped_existing"]:
        print(f"  = {username} (already exists, skipped)")
    print()
    print(
        f"Documents: {len(documents_report['to_create'])} to create, "
        f"{len(documents_report['skipped_existing'])} already present (skipped)"
    )
    for file_path in documents_report["to_create"]:
        flag = " [FILE NOT FOUND under --media-root]" if file_path in documents_report["missing_files"] else ""
        print(f"  + {file_path}{flag}")
    for file_path in documents_report["skipped_existing"]:
        print(f"  = {file_path} (already exists, skipped)")
    print()

    if documents_report["missing_files"]:
        print(
            f"WARNING: {len(documents_report['missing_files'])} document(s) reference a file not found under "
            f"--media-root. The DB row is still created if --apply is set (per the plan, files are expected to "
            f"already live at the same relative path -- this script never copies them), but downloads for these "
            f"will 404 until the file is actually present there."
        )
        print()

    if apply and users_report["activation_links"]:
        print("Activation links for newly-migrated users (distribute out-of-band, same as an ordinary invite):")
        for username, token in users_report["activation_links"]:
            print(f"  {username}: /accounts/activate/{token}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--django-db", type=Path, default=_DEFAULT_DJANGO_DB, help="path to Django's db.sqlite3")
    parser.add_argument(
        "--media-root", type=Path, default=_DEFAULT_MEDIA_ROOT,
        help="Django's MEDIA_ROOT -- checked (not copied from) to confirm each document's file is reachable",
    )
    parser.add_argument("--apply", action="store_true", help="actually write; default is dry-run/report-only")
    args = parser.parse_args()

    django_conn = _open_django_db(args.django_db)
    try:
        django_users = fetch_django_users(django_conn)
        django_documents = fetch_django_documents(django_conn)
    finally:
        django_conn.close()

    db = SessionLocal()
    try:
        users_report = migrate_users(db, django_users, apply=args.apply)
        documents_report = migrate_documents(db, django_documents, args.media_root, apply=args.apply)
        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()

    _print_report(users_report, documents_report, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
