"""
Real-ID <-> anon-ID translation for the backend's API boundary.

Queries an EXISTING, EXTERNALLY-OWNED PostgreSQL database that this project
does not control and must never write to. It already exists on a secure
Postgres server reachable from the backend's network (confirmed by the
Christie team). This module only ever contains SELECT statements against
its `key_value` table -- there is no write path, by construction.

This is a completely separate database from HermesDB (backend/src/db.py) --
never conflate the two, never point them at the same instance.

Configuration (backend .env):
    ANON_DB_HOST, ANON_DB_PORT, ANON_DB_NAME, ANON_DB_USER, ANON_DB_PASS

If ANON_DB_HOST is not set, is_configured() returns False and callers should
operate in passthrough mode (no anonymisation) -- e.g. internal-only
deployments that don't need this at all.
"""
import os
import logging
from typing import Optional

import psycopg2
from psycopg2.pool import SimpleConnectionPool

logger = logging.getLogger(__name__)

ANON_DB_HOST = os.getenv("ANON_DB_HOST")
ANON_DB_PORT = int(os.getenv("ANON_DB_PORT", "5432"))
ANON_DB_NAME = os.getenv("ANON_DB_NAME", "")
ANON_DB_USER = os.getenv("ANON_DB_USER", "")
ANON_DB_PASS = os.getenv("ANON_DB_PASS", "")

# ── Production schema, confirmed by the Christie team ────────────────────────
# `key_value` is a multi-purpose table; key_type_id = 1 selects the
# patient-ID mapping rows specifically. Confusingly, the `key_value` *column*
# holds the real ID and `patient_id` holds the anon ID.
_SQL_ANON_TO_REAL = """
    SELECT patient_id as anon_id, key_value as real_id
    FROM   key_value
    WHERE  patient_id = ANY(%s::bigint[]) AND key_type_id = 1
"""
_SQL_REAL_TO_ANON = """
    SELECT key_value as real_id, patient_id as anon_id
    FROM   key_value
    WHERE  key_value = ANY(%s::bigint[]) AND key_type_id = 1
"""
# ─────────────────────────────────────────────────────────────────────────────

_pool: Optional[SimpleConnectionPool] = None


class AnonLookupError(Exception):
    """Raised when an anonymised ID has no mapping in the external database."""


def is_configured() -> bool:
    """Return True if the anonymisation DB is configured in the environment."""
    return bool(ANON_DB_HOST)


def _get_pool() -> SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = SimpleConnectionPool(
            1, 5,
            host=ANON_DB_HOST, port=ANON_DB_PORT, dbname=ANON_DB_NAME,
            user=ANON_DB_USER, password=ANON_DB_PASS, connect_timeout=5,
        )
    return _pool


def _to_bigints(ids: list[str]) -> dict[str, int]:
    """Map each id to its int form, silently dropping ones that aren't valid
    bigints -- those can never match a row in `key_value` anyway, so they're
    treated as unmapped by the callers below rather than raising here."""
    out = {}
    for i in ids:
        try:
            out[i] = int(i)
        except (TypeError, ValueError):
            continue
    return out


def _query(sql: str, values: list[int]) -> list[tuple]:
    if not values:
        return []
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (values,))
            rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        pool.putconn(conn)


def lookup_real_ids(anon_ids: list[str]) -> dict[str, str]:
    """
    Batch convert anonymised patient IDs to real patient IDs.

    Returns: {anon_id: real_id, ...}
    Raises AnonLookupError if any of the provided IDs has no mapping.
    """
    if not anon_ids:
        return {}

    unique = list(dict.fromkeys(anon_ids))
    as_ints = _to_bigints(unique)
    rows = _query(_SQL_ANON_TO_REAL, list(as_ints.values()))

    mapping = {str(row[0]): str(row[1]) for row in rows}
    missing = [aid for aid in unique if aid not in mapping]
    if missing:
        raise AnonLookupError(
            f"Unknown anonymised patient ID{'s' if len(missing) > 1 else ''}: "
            + ", ".join(missing)
        )
    return mapping


def lookup_anon_ids(real_ids: list[str]) -> dict[str, str]:
    """
    Batch convert real patient IDs to anonymised patient IDs.

    Returns: {real_id: anon_id, ...}
    Real IDs with no mapping return the placeholder "[unknown]" so that
    a gap in the mapping never causes a real ID to be shown to the user.
    """
    if not real_ids:
        return {}

    unique = list(dict.fromkeys(real_ids))
    as_ints = _to_bigints(unique)
    rows = _query(_SQL_REAL_TO_ANON, list(as_ints.values()))

    mapping = {str(row[0]): str(row[1]) for row in rows}
    for rid in unique:
        if rid not in mapping:
            logger.warning("Real patient ID has no anonymised mapping — substituting [unknown]")
            mapping[rid] = "[unknown]"
    return mapping


def resolve_real_id(anon_id: str) -> str:
    """Single-ID inbound resolution; passthrough if anonymisation isn't configured."""
    if not is_configured():
        return anon_id
    return lookup_real_ids([anon_id])[anon_id]


def resolve_real_ids(anon_ids: list[str]) -> dict[str, str]:
    """Batch inbound resolution; passthrough if anonymisation isn't configured."""
    if not is_configured():
        return {a: a for a in anon_ids}
    return lookup_real_ids(anon_ids)


def to_display_id(real_id: str) -> str:
    """Single-ID outbound translation; passthrough if anonymisation isn't configured."""
    if not is_configured():
        return real_id
    return lookup_anon_ids([real_id])[real_id]


def to_display_ids(real_ids: list[str]) -> dict[str, str]:
    """Batch outbound translation; passthrough if anonymisation isn't configured."""
    if not is_configured():
        return {r: r for r in real_ids}
    return lookup_anon_ids(real_ids)
