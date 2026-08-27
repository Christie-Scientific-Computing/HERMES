"""
python -m backend.scripts.dev_seed

One-shot, idempotent dev-data seeder for the local docker-compose.dev.yml
stack -- NOT part of the application, never imported by backend/main.py.
Run manually after `docker compose -f docker-compose.dev.yml up` (migrations
must have already run, which happens automatically when the `backend`
service starts):

    docker compose -f docker-compose.dev.yml exec backend python -m backend.scripts.dev_seed

Seeds, all through the classes that already own their schemas rather than
reimplementing their SQL:

  - research_projects/project_memberships/project_audit_log, via
    ProjectsDB -- one project per lifecycle status (draft, submitted,
    approved+active, approved+future-expiry, approved+past-expiry
    ["expired" is computed at query time, not a stored status], rejected,
    revoked). create_project/submit_project/review_project/revoke_project
    already write the audit log as a side effect.
  - jobs/patients/events, via StatusDB -- a handful of jobs on the active
    project covering success, mixed success/failure, an export, one left
    "in progress" (only a start event), and one cancelled.
  - the mock pinnacle_export schema/tables -- in production this schema is
    owned and migrated entirely by PinnacleExport, never by HERMES's own
    Alembic (see backend/src/plans/db_client.py's module docstring); this
    seed hand-creates it the same way, purely so the patient-detail Plans
    panel has something to render locally.
  - the anon-db's key_value table (backend/src/identity/anon.py's schema)
    -- normally an externally-owned database HERMES only ever SELECTs
    against; docker-compose.dev.yml stands up a throwaway local Postgres
    for it, seeded here, mapping fake anon ids to the same fake real MRNs
    used above. Skipped automatically if ANON_DB_HOST isn't set (passthrough).

Every id/username below is fixed, and every step checks for existing state
first, so re-running this is a no-op on top of an already-seeded database.
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2 import sql

from backend.src.db import get_conn
from backend.src.projects.db_client import ProjectNotFoundError, ProjectsDB
from backend.src.status.db_client import StatusDB

ALICE = "alice"
BOB = "bob"
ADMIN = "admin"

# Fixed fake MRNs (real ids) <-> fake anon ids, kept in lockstep with the
# anon-db seed below so the anon boundary translates them consistently.
MRNS = [f"900000{i}" for i in range(1, 7)]  # 9000001..9000006
ANON_IDS = list(range(1001, 1007))  # 1001..1006

# One non-zero date_perturbation (day offset, key_value.date_perturbation)
# per seeded MRN, in lockstep with MRNS/ANON_IDS above -- so local dev/
# testing can exercise identity/anon.py's shift_date, not just the id
# mapping. Deliberately varied (mix of past/future, small/large) rather
# than one repeated value, so a bug that only shows up for a particular
# sign or magnitude of offset isn't hidden.
DATE_PERTURBATIONS = [-45, 12, 200, -7, 30, -100]

PROJECTS = {
    "dev-proj-draft": "Draft project",
    "dev-proj-submitted": "Submitted, awaiting review",
    "dev-proj-active": "Approved, active",
    "dev-proj-active-future-expiry": "Approved, expires in 90 days",
    "dev-proj-expired": "Approved, already expired",
    "dev-proj-rejected": "Rejected project",
    "dev-proj-revoked": "Revoked project",
}

ACTIVE_PROJECT = "dev-proj-active"

PINNACLE_SCHEMA = os.getenv("PINNACLE_SCHEMA", "pinnacle_export")


def _ensure_created(db: ProjectsDB, project_id: str, title: str, owner: str = ALICE) -> None:
    try:
        db.get_project(project_id)
    except ProjectNotFoundError:
        db.create_project(project_id, title, owner, description=f"Dev seed: {title}")
        db.add_member(project_id, BOB, role="member", added_by=owner)


def _ensure_submitted(db: ProjectsDB, project_id: str) -> None:
    if db.get_project(project_id)["status"] == "draft":
        db.submit_project(project_id, ALICE)


def seed_projects(db: ProjectsDB) -> None:
    now = datetime.now(timezone.utc)

    for project_id, title in PROJECTS.items():
        _ensure_created(db, project_id, title)

    _ensure_submitted(db, "dev-proj-submitted")

    for project_id, expiry in [
        (ACTIVE_PROJECT, None),
        ("dev-proj-active-future-expiry", now + timedelta(days=90)),
        ("dev-proj-expired", now - timedelta(days=30)),
    ]:
        _ensure_submitted(db, project_id)
        if db.get_project(project_id)["status"] == "submitted":
            db.review_project(project_id, approved=True, reviewer=ADMIN, expiry_date=expiry)

    _ensure_submitted(db, "dev-proj-rejected")
    if db.get_project("dev-proj-rejected")["status"] == "submitted":
        db.review_project("dev-proj-rejected", approved=False, reviewer=ADMIN, comment="Dev seed: not needed")

    _ensure_submitted(db, "dev-proj-revoked")
    if db.get_project("dev-proj-revoked")["status"] == "submitted":
        db.review_project("dev-proj-revoked", approved=True, reviewer=ADMIN)
    if db.get_project("dev-proj-revoked")["status"] == "approved":
        db.revoke_project("dev-proj-revoked", revoked_by=ADMIN, comment="Dev seed: superseded")


def _job_seeded(status_db: StatusDB, job_id: str) -> bool:
    return bool(status_db.list_job_patients(job_id))


def seed_jobs(status_db: StatusDB) -> None:
    job_id = "dev-job-import-success"
    if not _job_seeded(status_db, job_id):
        status_db.create_job(job_id, description="Dev seed: successful import batch", created_by=ALICE, project_id=ACTIVE_PROJECT)
        for mrn in MRNS[0:2]:
            status_db.add_patient(job_id, mrn)
            status_db.add_event(job_id, mrn, stage="retrieve", event_type="start")
            status_db.add_event(job_id, mrn, stage="retrieve", event_type="success", details={"imported": ["CT", "RTSTRUCT", "RTPLAN", "RTDOSE"]})

    job_id = "dev-job-import-mixed"
    if not _job_seeded(status_db, job_id):
        status_db.create_job(job_id, description="Dev seed: mixed-outcome import batch", created_by=ALICE, project_id=ACTIVE_PROJECT)
        status_db.add_patient(job_id, MRNS[2])
        status_db.add_event(job_id, MRNS[2], stage="retrieve", event_type="start")
        status_db.add_event(job_id, MRNS[2], stage="retrieve", event_type="success", details={"imported": ["CT"]})
        status_db.add_patient(job_id, MRNS[3])
        status_db.add_event(job_id, MRNS[3], stage="retrieve", event_type="start")
        status_db.add_event(
            job_id, MRNS[3], stage="retrieve", event_type="failure",
            error_message=f"No planning data found in Mosaiq or Pinnacle for patient {MRNS[3]}",
        )

    job_id = "dev-job-export-success"
    if not _job_seeded(status_db, job_id):
        status_db.create_job(job_id, description="Dev seed: successful export batch", created_by=BOB, project_id=ACTIVE_PROJECT)
        status_db.add_patient(job_id, MRNS[0])
        status_db.add_event(job_id, MRNS[0], stage="export", event_type="start")
        status_db.add_event(job_id, MRNS[0], stage="export", event_type="success", details={"destination": "DEV_MODALITY"})

    job_id = "dev-job-in-progress"
    if not _job_seeded(status_db, job_id):
        status_db.create_job(job_id, description="Dev seed: job left in progress", created_by=ALICE, project_id=ACTIVE_PROJECT)
        status_db.add_patient(job_id, MRNS[4])
        status_db.add_event(job_id, MRNS[4], stage="retrieve", event_type="start")

    job_id = "dev-job-cancelled"
    if not _job_seeded(status_db, job_id):
        status_db.create_job(job_id, description="Dev seed: cancelled job", created_by=ALICE, project_id=ACTIVE_PROJECT)
        status_db.add_patient(job_id, MRNS[5])
        status_db.add_event(job_id, MRNS[5], stage="retrieve", event_type="start")
        status_db.cancel_job(job_id)


def seed_pinnacle_plans() -> None:
    schema = sql.Identifier(PINNACLE_SCHEMA)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
        cur.execute(sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {}.plans (
                id SERIAL PRIMARY KEY, mrn TEXT NOT NULL, path TEXT NOT NULL,
                plan_id INT NOT NULL, plan_name TEXT NOT NULL, plan_date DATE,
                primary_image_set INT, pinnacle_version TEXT, comment TEXT,
                status TEXT NOT NULL, error_message TEXT
            )
            """
        ).format(schema))
        cur.execute(sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {}.status (
                id SERIAL PRIMARY KEY, mrn TEXT, path TEXT,
                process_datetime TIMESTAMP, status TEXT
            )
            """
        ).format(schema))
        cur.execute(sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {}.errors (
                id SERIAL PRIMARY KEY, status_id INT, mrn TEXT, path TEXT,
                error_message TEXT
            )
            """
        ).format(schema))

        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}.plans WHERE path LIKE %s").format(schema), ("dev-seed/%",))
        if cur.fetchone()[0] == 0:
            plans = [
                (MRNS[0], "dev-seed/plan-1", 1, "Prostate 60Gy/20fx", "2026-01-15", 1, "16.2", "Dev seed plan", "complete", None),
                (MRNS[0], "dev-seed/plan-2", 2, "Prostate boost", "2026-02-01", 1, "16.2", "Dev seed plan", "complete", None),
                (MRNS[2], "dev-seed/plan-3", 1, "Lung SBRT", "2026-01-20", 2, "16.2", "Dev seed plan", "failed", "DICOM export timed out"),
            ]
            cur.executemany(
                sql.SQL(
                    """
                    INSERT INTO {}.plans
                        (mrn, path, plan_id, plan_name, plan_date, primary_image_set,
                         pinnacle_version, comment, status, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                ).format(schema),
                plans,
            )


def seed_anon_db() -> None:
    conn = psycopg2.connect(
        host=os.environ["ANON_DB_HOST"],
        port=os.getenv("ANON_DB_PORT", "5432"),
        dbname=os.environ["ANON_DB_NAME"],
        user=os.environ["ANON_DB_USER"],
        password=os.environ["ANON_DB_PASS"],
    )
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS key_value (
                    id SERIAL PRIMARY KEY,
                    patient_id BIGINT NOT NULL,
                    key_value BIGINT NOT NULL,
                    key_type_id INT NOT NULL,
                    date_perturbation INT
                )
                """
            )
            # ALTER ... ADD COLUMN IF NOT EXISTS covers a database seeded by
            # an older version of this script, before date_perturbation
            # existed -- CREATE TABLE IF NOT EXISTS above is a no-op against
            # an already-existing table, so the column would otherwise never
            # get added to a dev database that predates this change.
            cur.execute("ALTER TABLE key_value ADD COLUMN IF NOT EXISTS date_perturbation INT")
            cur.execute("SELECT COUNT(*) FROM key_value WHERE key_type_id = 1")
            if cur.fetchone()[0] == 0:
                rows = [
                    (anon_id, int(mrn), 1, perturbation)
                    for anon_id, mrn, perturbation in zip(ANON_IDS, MRNS, DATE_PERTURBATIONS)
                ]
                cur.executemany(
                    "INSERT INTO key_value (patient_id, key_value, key_type_id, date_perturbation) "
                    "VALUES (%s, %s, %s, %s)",
                    rows,
                )
    finally:
        conn.close()


def main() -> None:
    seed_projects(ProjectsDB())
    seed_jobs(StatusDB())
    seed_pinnacle_plans()

    if os.getenv("ANON_DB_HOST"):
        seed_anon_db()
        print(f"Seeded anon-db: anon ids {ANON_IDS[0]}..{ANON_IDS[-1]} -> real MRNs {MRNS[0]}..{MRNS[-1]}")
    else:
        print("ANON_DB_HOST not set; skipping anon-db seed (passthrough mode).")

    print("Dev seed complete.")
    print(f"  Projects: {', '.join(PROJECTS)}")
    print("  Jobs: dev-job-import-success, dev-job-import-mixed, dev-job-export-success, "
          "dev-job-in-progress, dev-job-cancelled")
    print(f"  Mock MRNs: {', '.join(MRNS)}")
    print(f"  Project members: {ALICE} (owner), {BOB} (member) -- log in as one of these on either frontend.")


if __name__ == "__main__":
    main()
