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

`key_value` also carries a `date_perturbation INT` column (one value per
key_type_id=1 row, alongside the id mapping) -- a per-patient day offset,
positive shifting a date into the future, negative into the past. See
get_date_perturbation(s)/shift_date below (docs/plans/pii-boundary-test-suite.md
§B): clinical dates crossing the API boundary are shifted by this amount
rather than redacted outright, preserving the relative time intervals
between a patient's scans while breaking the link to real calendar dates.

Optional hardening (see docs/safety-plan.md §B1), both opt-in and unset by
default -- matching this module's existing idiom of "unset means today's
behavior, unchanged":
    ANON_DB_SSLMODE      -- standard libpq sslmode (e.g. "require",
                            "verify-full"). This is standard TLS opt-in, NOT
                            certificate pinning -- normal PKI validation
                            against whatever certificate/CA the server
                            already presents.
    ANON_DB_SSLROOTCERT  -- filesystem path to a CA/root certificate, used
                            alongside ANON_DB_SSLMODE="verify-full" (or
                            "verify-ca") to confirm server identity.
    ANON_LOOKUP_WARN_THRESHOLD, ANON_LOOKUP_WARN_WINDOW_SECONDS -- app-side
                            monitoring: a rolling in-process counter of IDs
                            looked up, logged as a warning if it exceeds the
                            threshold within the window. A sudden spike in ID
                            resolutions is a plausible signal of bulk
                            re-identification/exfiltration; this is purely
                            informational (nothing is blocked or rejected).
"""
import os
import re
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
from psycopg2.pool import SimpleConnectionPool

logger = logging.getLogger(__name__)

ANON_DB_HOST = os.getenv("ANON_DB_HOST")
ANON_DB_PORT = int(os.getenv("ANON_DB_PORT", "5432"))
ANON_DB_NAME = os.getenv("ANON_DB_NAME", "")
ANON_DB_USER = os.getenv("ANON_DB_USER", "")
ANON_DB_PASS = os.getenv("ANON_DB_PASS", "")

# Standard TLS opt-in -- NOT certificate pinning. Both unset by default,
# preserving today's behavior unchanged. See module docstring above.
ANON_DB_SSLMODE = os.getenv("ANON_DB_SSLMODE")
ANON_DB_SSLROOTCERT = os.getenv("ANON_DB_SSLROOTCERT")

# Application-side lookup-volume monitoring -- sane defaults, both overridable.
ANON_LOOKUP_WARN_THRESHOLD = int(os.getenv("ANON_LOOKUP_WARN_THRESHOLD", "500"))
ANON_LOOKUP_WARN_WINDOW_SECONDS = int(
    os.getenv("ANON_LOOKUP_WARN_WINDOW_SECONDS", str(60 * 60))
)

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
_SQL_DATE_PERTURBATION = """
    SELECT key_value as real_id, date_perturbation
    FROM   key_value
    WHERE  key_value = ANY(%s::bigint[]) AND key_type_id = 1
"""
# ─────────────────────────────────────────────────────────────────────────────

_pool: Optional[SimpleConnectionPool] = None

# Rolling lookup-volume counter state (see note_lookup_volume below). Reset
# whenever the window elapses; deliberately in-process only, no new
# infrastructure -- see module docstring.
_lookup_window_start: Optional[float] = None
_lookup_window_count: int = 0
_lookup_window_warned: bool = False
# Guards the three globals above. Safe today without it, since every current
# caller runs synchronously on the single asyncio event-loop thread (no
# `await` between reading and writing this state) -- but that's an unstated
# invariant elsewhere in this module's own callers, not an enforced one, and
# the lock is nearly free. Cheap defense-in-depth against a future change
# (e.g. anon lookups moving into asyncio.to_thread the way §D1 does for
# StatusDB calls in docs/safety-plan.md), which would make concurrent OS
# threads race on this read-modify-write for the first time.
_lookup_lock = threading.Lock()


class AnonLookupError(Exception):
    """Raised when an anonymised ID has no mapping in the external database."""


class AnonServiceError(Exception):
    """Raised when the anon-mapping database itself can't be reached/queried
    (connection refused, auth failure, timeout, ...) -- distinct from
    AnonLookupError, which means the DB answered but the ID isn't in it.
    Callers should treat this as a 503, not a 422."""


def is_configured() -> bool:
    """Return True if the anonymisation DB is configured in the environment."""
    return bool(ANON_DB_HOST)


def _connection_kwargs() -> dict:
    """Build the kwargs passed to psycopg2 for the anon DB connection.

    Split out from _get_pool so it's independently testable: sslmode/
    sslrootcert must be present only when their env vars are actually set --
    psycopg2 should never see e.g. sslmode=None, matching how ANON_DB_HOST
    unset already means passthrough elsewhere in this module.
    """
    kwargs = {
        "host": ANON_DB_HOST, "port": ANON_DB_PORT, "dbname": ANON_DB_NAME,
        "user": ANON_DB_USER, "password": ANON_DB_PASS, "connect_timeout": 5,
    }
    if ANON_DB_SSLMODE:
        kwargs["sslmode"] = ANON_DB_SSLMODE
    if ANON_DB_SSLROOTCERT:
        kwargs["sslrootcert"] = ANON_DB_SSLROOTCERT
    return kwargs


def _get_pool() -> SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = SimpleConnectionPool(1, 5, **_connection_kwargs())
    return _pool


def _note_lookup_volume(count: int) -> None:
    """Track lookups over a rolling time window and warn once a configurable
    threshold is exceeded within it. Purely observational -- never raises,
    never blocks a lookup. A sudden spike here is a plausible signal of bulk
    re-identification/exfiltration; right now nothing else surfaces that."""
    global _lookup_window_start, _lookup_window_count, _lookup_window_warned
    if count <= 0:
        return

    should_warn = False
    with _lookup_lock:
        now = time.monotonic()
        if (
            _lookup_window_start is None
            or now - _lookup_window_start >= ANON_LOOKUP_WARN_WINDOW_SECONDS
        ):
            _lookup_window_start = now
            _lookup_window_count = 0
            _lookup_window_warned = False

        _lookup_window_count += count

        if _lookup_window_count > ANON_LOOKUP_WARN_THRESHOLD and not _lookup_window_warned:
            _lookup_window_warned = True
            should_warn = True
        window_count = _lookup_window_count

    if should_warn:
        logger.warning(
            "Anonymisation ID lookup volume (%d) exceeded threshold (%d) "
            "within the last %ds -- possible bulk re-identification attempt",
            window_count, ANON_LOOKUP_WARN_THRESHOLD,
            ANON_LOOKUP_WARN_WINDOW_SECONDS,
        )


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
    try:
        pool = _get_pool()
        conn = pool.getconn()
    except Exception as exc:
        raise AnonServiceError(f"Cannot reach anonymisation DB: {exc}") from exc
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (values,))
                rows = cur.fetchall()
            conn.commit()
            return rows
        except Exception as exc:
            raise AnonServiceError(f"Anonymisation DB query failed: {exc}") from exc
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
    _note_lookup_volume(len(unique))
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
    _note_lookup_volume(len(unique))
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


def get_date_perturbations(real_ids: list[str]) -> dict[str, int]:
    """
    Batch fetch each real id's day offset (key_value.date_perturbation,
    key_type_id=1) -- positive shifts a date into the future, negative into
    the past. Mirrors lookup_real_ids' shape (batched, dict return, id-list
    dedup, lookup-volume tracking) since it hits the same table via the same
    pool.

    Passthrough (ANON_DB_HOST unset): every id maps to 0 (no shift) --
    consistent with dates not being touched at all in an internal-only
    deployment, same as the existing id-mapping passthrough.

    Raises AnonLookupError if any id has no row, OR has a row with a NULL
    date_perturbation -- deliberately NOT defaulted to 0 in either case. A
    silent "no shift" default here would mean the caller falls through to
    returning the raw, unshifted real date -- exactly the leak this
    mechanism exists to prevent (see shift_date below, which instead
    redacts to None on this error). Raises AnonServiceError if the DB
    itself can't be reached/queried, same as every other lookup in this
    module.
    """
    if not real_ids:
        return {}
    if not is_configured():
        return {r: 0 for r in real_ids}

    unique = list(dict.fromkeys(real_ids))
    _note_lookup_volume(len(unique))
    as_ints = _to_bigints(unique)
    rows = _query(_SQL_DATE_PERTURBATION, list(as_ints.values()))

    mapping = {str(row[0]): row[1] for row in rows}
    missing = [rid for rid in unique if mapping.get(rid) is None]
    if missing:
        raise AnonLookupError(
            f"No date_perturbation on record for real id{'s' if len(missing) > 1 else ''}: "
            + ", ".join(missing)
        )
    return {rid: int(mapping[rid]) for rid in unique}


def get_date_perturbation(real_id: str) -> int:
    """Single-id convenience wrapper around get_date_perturbations."""
    return get_date_perturbations([real_id])[real_id]


_DA_FORMAT = "%Y%m%d"
_ISO_FORMAT = "%Y-%m-%d"
_DA_SHAPE = re.compile(r"\d{8}")
_ISO_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")


def shift_date(real_id: str, date_str: Optional[str]) -> Optional[str]:
    """
    Shift a clinical date by real_id's per-patient day offset
    (get_date_perturbation), preserving the relative time interval between
    a patient's scans while breaking the link to the real calendar date --
    see docs/plans/pii-boundary-test-suite.md §B.

    Detects DICOM DA (YYYYMMDD) vs ISO (YYYY-MM-DD) format from the input
    and re-formats the output the same way, so callers never need to know
    which format a given field uses.

    Returns None -- fail-SAFE, never the unshifted raw value -- for an
    empty/absent input date, an input that isn't DA or ISO shaped, an input
    that doesn't parse as a real calendar date, or a failed perturbation
    lookup (AnonLookupError/AnonServiceError). A caller getting None back
    must render it as "unknown"/omit the field, never fall back to the raw
    `date_str` it was given.
    """
    if not date_str:
        return None

    if _DA_SHAPE.fullmatch(date_str):
        fmt = _DA_FORMAT
    elif _ISO_SHAPE.fullmatch(date_str):
        fmt = _ISO_FORMAT
    else:
        logger.warning("shift_date given a value in neither DA nor ISO format; redacting")
        return None

    try:
        parsed = datetime.strptime(date_str, fmt).date()
    except ValueError:
        logger.warning("shift_date given a value that isn't a real calendar date; redacting")
        return None

    try:
        offset_days = get_date_perturbation(real_id)
    except (AnonLookupError, AnonServiceError):
        logger.warning("Could not determine date perturbation for real id; redacting date")
        return None

    return (parsed + timedelta(days=offset_days)).strftime(fmt)
