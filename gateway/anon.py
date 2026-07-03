"""
Patient ID anonymisation utilities for the HERMES gateway.

Users submit anonymised patient IDs; this module converts them to real patient IDs
for internal use, and converts real IDs back to anonymised IDs for display.

Configuration (gateway .env):
    ANON_DB_HOST, ANON_DB_PORT, ANON_DB_NAME, ANON_DB_USER, ANON_DB_PASS

If ANON_DB_HOST is not set, is_configured() returns False and callers operate
in passthrough mode (no conversion). Set it in production to enforce anonymisation.
"""
import os
import csv as _csv
import io as _io
import logging
import psycopg2
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ANON_DB_HOST = os.getenv("ANON_DB_HOST")
ANON_DB_PORT = int(os.getenv("ANON_DB_PORT", "5432"))
ANON_DB_NAME = os.getenv("ANON_DB_NAME", "anon_mapping")
ANON_DB_USER = os.getenv("ANON_DB_USER", "gateway")
ANON_DB_PASS = os.getenv("ANON_DB_PASS", "")


# ── SQL queries ── EDIT THESE to match your database schema ──────────────────
#
# anon_id : the anonymised patient identifier that external users submit
# real_id : the real patient ID stored in HERMES / Orthanc / StatusDB
#
# Both queries use PostgreSQL's ANY operator for efficient batch lookups.

_SQL_ANON_TO_REAL = """
    SELECT anon_id, real_id
    FROM   anon_mapping
    WHERE  anon_id = ANY(%s)
"""
# TODO: replace `anon_mapping` with your table name
# TODO: replace `anon_id`      with your anonymised-ID column name
# TODO: replace `real_id`      with your real-patient-ID column name

_SQL_REAL_TO_ANON = """
    SELECT real_id, anon_id
    FROM   anon_mapping
    WHERE  real_id = ANY(%s)
"""
# TODO: replace `anon_mapping` with your table name
# TODO: replace `real_id`      with your real-patient-ID column name
# TODO: replace `anon_id`      with your anonymised-ID column name

# ─────────────────────────────────────────────────────────────────────────────


class AnonLookupError(Exception):
    """Raised when an anonymised ID has no mapping in the database."""


def is_configured() -> bool:
    """Return True if the anonymisation DB is configured in the environment."""
    return bool(ANON_DB_HOST)


def _connect():
    try:
        return psycopg2.connect(
            host=ANON_DB_HOST,
            port=ANON_DB_PORT,
            dbname=ANON_DB_NAME,
            user=ANON_DB_USER,
            password=ANON_DB_PASS,
            connect_timeout=5,
        )
    except Exception as exc:
        raise ConnectionError(f"Cannot connect to anonymisation DB at {ANON_DB_HOST}: {exc}") from exc


def lookup_real_ids(anon_ids: list[str]) -> dict[str, str]:
    """
    Batch convert anonymised patient IDs to real patient IDs.

    Returns: {anon_id: real_id, ...}
    Raises AnonLookupError if any of the provided IDs has no mapping.
    """
    if not anon_ids:
        return {}

    unique = list(dict.fromkeys(anon_ids))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_ANON_TO_REAL, (unique,))
            rows = cur.fetchall()
    finally:
        conn.close()

    mapping = {row[0]: row[1] for row in rows}
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
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_REAL_TO_ANON, (unique,))
            rows = cur.fetchall()
    finally:
        conn.close()

    mapping = {row[0]: row[1] for row in rows}
    # Fill in a safe placeholder for any unmapped real IDs
    for rid in unique:
        if rid not in mapping:
            logger.warning("Real patient ID %r has no anonymised mapping — substituting [unknown]", rid)
            mapping[rid] = "[unknown]"
    return mapping


def rewrite_csv_patient_ids(csv_bytes: bytes, id_map: dict[str, str]) -> bytes:
    """
    Return CSV bytes with the `patient_id` column replaced using id_map.
    Rows whose patient_id is not in id_map are passed through unchanged.
    """
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = _csv.DictReader(text.splitlines())
    if not reader.fieldnames or "patient_id" not in reader.fieldnames:
        return csv_bytes

    rows = list(reader)
    out = _io.StringIO()
    writer = _csv.DictWriter(out, fieldnames=reader.fieldnames)
    writer.writeheader()
    for row in rows:
        pid = (row.get("patient_id") or "").strip()
        row["patient_id"] = id_map.get(pid, pid)
        writer.writerow(row)
    return out.getvalue().encode("utf-8")
