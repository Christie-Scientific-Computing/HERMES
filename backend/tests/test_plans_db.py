"""
PlansDB reads PinnacleExport's `plans` table, which lives in the same Postgres
as HermesDB but is owned and migrated by PinnacleExport -- so these tests
create and drop that schema themselves rather than relying on any HERMES
migration (there is none, and there should never be one).

The important case is the LAST one: HERMES must run fine against a database
where PinnacleExport has never been deployed.
"""
import datetime
import uuid

import pytest

from backend.src.db import get_conn
from backend.src.plans.db_client import PINNACLE_SCHEMA, PlansDB


def _create_schema():
    """Mirrors PinnacleExport's own initial migration for the `plans` table."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {PINNACLE_SCHEMA}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PINNACLE_SCHEMA}.plans (
                id SERIAL PRIMARY KEY,
                mrn TEXT NOT NULL,
                path TEXT NOT NULL,
                plan_id INTEGER NOT NULL,
                plan_name TEXT NOT NULL,
                plan_date DATE,
                primary_image_set INTEGER,
                pinnacle_version TEXT,
                comment TEXT,
                status TEXT NOT NULL,
                error_message TEXT
            )
            """
        )


def _create_status_schema():
    """Mirrors PinnacleExport's own status/errors tables (see CLAUDE.md's
    HermesDB Schema section for the documented shape)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {PINNACLE_SCHEMA}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PINNACLE_SCHEMA}.status (
                id SERIAL PRIMARY KEY,
                mrn TEXT NOT NULL,
                path TEXT,
                process_datetime TIMESTAMP NOT NULL,
                status TEXT
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PINNACLE_SCHEMA}.errors (
                id SERIAL PRIMARY KEY,
                status_id INTEGER NOT NULL,
                mrn TEXT,
                path TEXT,
                error_message TEXT
            )
            """
        )


def _drop_schema():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {PINNACLE_SCHEMA} CASCADE")


@pytest.fixture
def plans_schema():
    _create_schema()
    yield
    _drop_schema()


@pytest.fixture
def status_schema():
    _create_status_schema()
    yield
    _drop_schema()


def _insert_status(mrn, process_datetime, status="processing", path=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {PINNACLE_SCHEMA}.status (mrn, path, process_datetime, status)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (mrn, path or f"/pinnacle/{mrn}", process_datetime, status),
        )
        return cur.fetchone()[0]


def _insert_error(status_id, mrn, error_message, path=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {PINNACLE_SCHEMA}.errors (status_id, mrn, path, error_message)
            VALUES (%s, %s, %s, %s)
            """,
            (status_id, mrn, path or f"/pinnacle/{mrn}", error_message),
        )


@pytest.fixture
def db():
    return PlansDB()


@pytest.fixture
def mrn():
    return f"plan-test-{uuid.uuid4().hex[:8]}"


def _insert(mrn, plan_id, plan_name, status, plan_date=None, **kwargs):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {PINNACLE_SCHEMA}.plans
                (mrn, path, plan_id, plan_name, plan_date, primary_image_set,
                 pinnacle_version, comment, status, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                mrn,
                kwargs.get("path", f"/pinnacle/{mrn}/Plan_{plan_id}"),
                plan_id,
                plan_name,
                plan_date,
                kwargs.get("primary_image_set"),
                kwargs.get("pinnacle_version"),
                kwargs.get("comment"),
                status,
                kwargs.get("error_message"),
            ),
        )


def test_returns_every_column(plans_schema, db, mrn):
    _insert(
        mrn, 1, "Prostate", "exported",
        plan_date=datetime.date(2026, 3, 1),
        primary_image_set=7, pinnacle_version="16.2",
        comment="looks fine", error_message=None,
    )

    plans = db.list_plans_for_patient(mrn)

    assert len(plans) == 1
    plan = plans[0]
    assert plan["plan_id"] == 1
    assert plan["plan_name"] == "Prostate"
    assert plan["plan_date"] == datetime.date(2026, 3, 1)
    assert plan["primary_image_set"] == 7
    assert plan["pinnacle_version"] == "16.2"
    assert plan["comment"] == "looks fine"
    assert plan["status"] == "exported"
    assert plan["error_message"] is None
    assert plan["path"].endswith("Plan_1")
    # mrn is deliberately not selected -- the caller already knows it, and not
    # returning it keeps the real id out of the response by construction.
    assert "mrn" not in plan


def test_newest_first_with_undated_plans_last(plans_schema, db, mrn):
    _insert(mrn, 1, "Old", "exported", plan_date=datetime.date(2025, 1, 1))
    _insert(mrn, 2, "New", "exported", plan_date=datetime.date(2026, 6, 1))
    _insert(mrn, 3, "Undated", "failed", plan_date=None)

    plans = db.list_plans_for_patient(mrn)

    assert [p["plan_name"] for p in plans] == ["New", "Old", "Undated"]


def test_repeated_plan_id_from_a_different_path_is_its_own_row(plans_schema, db, mrn):
    """(mrn, plan_id) has no unique constraint -- a re-export adds a row."""
    _insert(mrn, 1, "Prostate", "failed", path="/pinnacle/run1/Plan_1")
    _insert(mrn, 1, "Prostate", "exported", path="/pinnacle/run2/Plan_1")

    plans = db.list_plans_for_patient(mrn)

    assert len(plans) == 2
    assert {p["path"] for p in plans} == {"/pinnacle/run1/Plan_1", "/pinnacle/run2/Plan_1"}


def test_other_patients_plans_are_not_returned(plans_schema, db, mrn):
    _insert(mrn, 1, "Mine", "exported")
    _insert(f"{mrn}-other", 1, "Theirs", "exported")

    plans = db.list_plans_for_patient(mrn)

    assert [p["plan_name"] for p in plans] == ["Mine"]


def test_patient_with_no_plans_returns_empty_list_not_none(plans_schema, db, mrn):
    assert db.list_plans_for_patient(mrn) == []


def test_missing_schema_returns_none_not_an_exception(db, mrn):
    """
    The state HERMES is actually in until PinnacleExport is deployed against
    this database. None (not []) so the UI can say "unavailable" rather than
    the misleading "this patient has no plans".
    """
    _drop_schema()
    assert db.list_plans_for_patient(mrn) is None


# ---- latest_status_for_patient (backs search_pinnacle_db's reason string) ----

def test_latest_status_no_linked_error_reports_success(status_schema, db, mrn):
    since = datetime.datetime(2026, 1, 1)
    _insert_status(mrn, datetime.datetime(2026, 1, 2), status="exported")

    row = db.latest_status_for_patient(mrn, since)

    assert row["status"] == "exported"
    assert row["error_message"] is None


def test_latest_status_with_linked_error_returns_error_message(status_schema, db, mrn):
    since = datetime.datetime(2026, 1, 1)
    status_id = _insert_status(mrn, datetime.datetime(2026, 1, 2), status="failed")
    _insert_error(status_id, mrn, "no RTSTRUCT found")

    row = db.latest_status_for_patient(mrn, since)

    assert row["status"] == "failed"
    assert row["error_message"] == "no RTSTRUCT found"


def test_latest_status_ignores_rows_before_since(status_schema, db, mrn):
    """A status row that predates `since` is exactly the stale-history case
    latest_status_for_patient exists to filter out -- the caller
    (search_pinnacle_db) must fall back to "pending" rather than report on
    an old, unrelated run."""
    _insert_status(mrn, datetime.datetime(2020, 1, 1), status="exported")

    since = datetime.datetime(2026, 1, 1)
    assert db.latest_status_for_patient(mrn, since) is None


def test_latest_status_picks_the_most_recent_row_after_since(status_schema, db, mrn):
    since = datetime.datetime(2026, 1, 1)
    _insert_status(mrn, datetime.datetime(2026, 1, 2), status="processing")
    later_id = _insert_status(mrn, datetime.datetime(2026, 1, 3), status="failed")
    _insert_error(later_id, mrn, "second attempt failed")

    row = db.latest_status_for_patient(mrn, since)

    assert row["status"] == "failed"
    assert row["error_message"] == "second attempt failed"


def test_latest_status_other_patients_not_returned(status_schema, db, mrn):
    since = datetime.datetime(2026, 1, 1)
    _insert_status(f"{mrn}-other", datetime.datetime(2026, 1, 2), status="exported")

    assert db.latest_status_for_patient(mrn, since) is None


def test_latest_status_missing_schema_returns_none(db, mrn):
    """Same schema-presence-tolerant posture as list_plans_for_patient --
    HERMES must run fine against a database PinnacleExport never touched."""
    _drop_schema()
    since = datetime.datetime(2026, 1, 1)
    assert db.latest_status_for_patient(mrn, since) is None
