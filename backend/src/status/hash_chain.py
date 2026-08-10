"""
Canonical JSON + hashing for the events hash chain (docs/safety-plan.md §D1).

Both `StatusDB.add_event` (which writes the chain) and
`backend/scripts/verify_audit_chain.py` (which verifies it after the fact)
import `compute_row_hash`/`GENESIS_HASH` from here, so the two can never
drift out of sync with each other -- the whole point of the chain is that
"recompute the hash the same way it was computed originally" has exactly
one implementation.

Canonical JSON choice, documented here since the hash is only meaningful if
the same logical event always serializes identically:
- `json.dumps(..., sort_keys=True, separators=(",", ":"), default=str)`
- `sort_keys=True`: dict key order (including inside `details`) is never
  part of an event's identity.
- `separators=(",", ":")`: no incidental whitespace differences between
  what's serialized at write time vs. re-serialized at verify time.
- `ts` is explicitly normalized to UTC before stringifying (`_normalize_ts`)
  rather than left to bare `default=str`. `add_event` always constructs it
  as `datetime.now(timezone.utc)`, but `verify_audit_chain.py` reads it
  back from Postgres, and psycopg2 renders a TIMESTAMPTZ using the
  *session's* `TimeZone` GUC, not necessarily UTC -- `str()` of the exact
  same instant differs between a UTC session (`...+00:00`) and e.g.
  `Europe/London` (`...+01:00` in summer). `_normalize_ts` calls
  `.astimezone(timezone.utc)` before `str()`, deliberately keeping the same
  `str(datetime)` format (space-separated, not `isoformat()`'s `T`) that
  `default=str` already produced -- since `add_event` only ever hashes a
  value that's already UTC, this is a no-op for every hash computed so
  far, and only changes behavior for a *different-timezone* read, which is
  exactly the bug being fixed.
- `details` is normalized (`_normalize_numbers`) to fold `-0.0` to `0.0`
  before serializing. Postgres NUMERIC/JSONB has no signed zero, so a
  `-0.0` in a `details` dict passed to `add_event` round-trips back as
  `0.0` once read from the DB -- two different JSON strings for values
  that are `==` to each other and were never actually "tampered" with.
- `default=str`: anything else not natively JSON-serializable is
  stringified with its own `__str__` as a last resort.
"""
import hashlib
import json
from datetime import datetime, timezone

# sha256('') hex digest -- the fixed genesis value `event_chain_state.last_hash`
# is seeded to by the migration (backend/alembic/versions/27bcb338ace5_*.py),
# and what the very first post-migration event's prev_hash must equal.
GENESIS_HASH = hashlib.sha256(b"").hexdigest()


def _normalize_ts(ts) -> str:
    """
    Render a timestamp as a fixed-UTC string, independent of whatever
    session/connection it was read back over.

    `add_event` always passes a tz-aware `datetime.now(timezone.utc)`.
    Postgres TIMESTAMPTZ columns store an absolute instant, but psycopg2
    attaches the *reading connection's* session timezone when converting
    back to a Python `datetime` -- so the same stored instant can come back
    tagged UTC in one place and e.g. +01:00 (Europe/London, summer) in
    another. `.astimezone(timezone.utc)` normalizes to the same absolute
    instant with a fixed UTC offset before formatting.

    Deliberately `str(dt)`, not `dt.isoformat()`: `str()` on a datetime is
    `isoformat(sep=' ')` -- exactly what plain `default=str` already
    produced for every hash computed before this fix, since every write
    happens through a UTC connection/value already. Keeping the same
    format means this change is a no-op for anything hashed so far and
    only changes behavior for a genuinely different-timezone read, which
    is the actual bug.
    """
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            # Shouldn't happen for a TIMESTAMPTZ column / datetime.now(timezone.utc),
            # but don't guess a timezone for a naive value -- treat it as
            # already being the value to hash, same as historical default=str.
            return str(ts)
        return str(ts.astimezone(timezone.utc))
    return str(ts)


def _normalize_numbers(obj):
    """
    Recursively fold `-0.0` to `0.0` inside `details` (a JSON-able dict).
    `-0.0 + 0.0 == 0.0` under IEEE 754, so this only changes signed-zero
    floats' *serialized form* to agree with what they become after a round
    trip through Postgres JSONB -- values that already compare equal now
    also serialize identically, regardless of whether the caller is
    add_event (using the object passed straight from Python) or
    verify_audit_chain.py (using the object psycopg2 deserialized from
    JSONB).
    """
    if isinstance(obj, float):
        return obj + 0.0
    if isinstance(obj, dict):
        return {k: _normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_numbers(v) for v in obj]
    return obj


def canonical_event_json(job_id, mrn, stage, event_type, ts, attempt, error_message, details) -> str:
    """Deterministic serialization of one event's identity fields."""
    payload = {
        "job_id": job_id,
        "mrn": mrn,
        "stage": stage,
        "event_type": event_type,
        "ts": _normalize_ts(ts),
        "attempt": attempt,
        "error_message": error_message,
        "details": _normalize_numbers(details),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_row_hash(prev_hash: str, job_id, mrn, stage, event_type, ts, attempt, error_message, details) -> str:
    """row_hash = sha256(prev_hash || canonical_json(...))."""
    canonical = canonical_event_json(job_id, mrn, stage, event_type, ts, attempt, error_message, details)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
