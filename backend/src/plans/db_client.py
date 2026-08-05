"""
Read-only client for PinnacleExport's `plans` table.

This lives in the SAME Postgres database as HermesDB (DATABASE_URL), in its
own schema, but it is NOT HERMES-owned: PinnacleExport creates, migrates and
writes it. HERMES only ever runs SELECTs here -- the same read-only posture
as the anon-mapping DB (backend/src/identity/anon.py), just reachable through
the shared pool rather than a separate one.

Nothing in backend/alembic/versions/ touches this schema, and nothing should.

Schema (from PinnacleExport's own initial migration):

    plans(id PK, mrn TEXT NOT NULL, path TEXT NOT NULL, plan_id INT NOT NULL,
          plan_name TEXT NOT NULL, plan_date DATE, primary_image_set INT,
          pinnacle_version TEXT, comment TEXT, status TEXT NOT NULL,
          error_message TEXT)

`mrn` is the REAL patient id, same as events.mrn -- callers must resolve the
anon boundary themselves before calling in, and scrub the free-text columns
on the way back out (see results/endpoints.py). There is no job_id here: plans
belong to a patient, not to a HERMES job.

Sibling tables `status` and `errors` exist in the same schema and aren't read
yet -- `errors` (joined via status_id) is the natural next increment.
"""
import logging
import os

from psycopg2 import errors as pg_errors, sql
from psycopg2.extras import RealDictCursor

from backend.src.db import get_conn

logger = logging.getLogger(__name__)

# PinnacleExport takes its schema name from its own src.db.models.SCHEMA;
# override here if that deployment uses something other than the default.
PINNACLE_SCHEMA = os.getenv("PINNACLE_SCHEMA", "pinnacle_export")

_PLAN_COLUMNS = (
    "id", "path", "plan_id", "plan_name", "plan_date",
    "primary_image_set", "pinnacle_version", "comment", "status", "error_message",
)


class PlansDB:
    def list_plans_for_patient(self, mrn: str) -> list[dict] | None:
        """
        Every plan PinnacleExport recorded for one patient (REAL mrn), newest
        first.

        Returns None -- deliberately distinct from [] -- when the schema or
        table isn't present, so callers can render "not available" rather than
        the misleading "this patient has no plans". PinnacleExport may not have
        been deployed against this database yet.

        Note (mrn, plan_id) is not unique: re-exporting the same plan from a
        different `path` adds another row. Callers should show `path` rather
        than assuming one row per plan_id.
        """
        query = sql.SQL(
            """
            SELECT {columns}
            FROM {schema}.plans
            WHERE mrn = %s
            ORDER BY plan_date DESC NULLS LAST, plan_id
            """
        ).format(
            columns=sql.SQL(", ").join(sql.Identifier(c) for c in _PLAN_COLUMNS),
            schema=sql.Identifier(PINNACLE_SCHEMA),
        )
        try:
            with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, (str(mrn),))
                return [dict(r) for r in cur.fetchall()]
        except (pg_errors.UndefinedTable, pg_errors.InvalidSchemaName):
            logger.info(
                "%s.plans not present; reporting plans as unavailable", PINNACLE_SCHEMA
            )
            return None
