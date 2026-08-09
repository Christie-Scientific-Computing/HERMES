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
- `default=str`: anything not natively JSON-serializable -- in practice
  just `ts`, a timezone-aware `datetime` -- is stringified with its own
  `__str__`. A tz-aware datetime's `str()` is a fixed ISO-8601-with-offset
  format, and Postgres TIMESTAMPTZ round-trips microsecond precision
  exactly, so the same instant always renders as the same string whether
  it's the value passed to INSERT or the value read back by
  `verify_audit_chain.py`.
"""
import hashlib
import json

# sha256('') hex digest -- the fixed genesis value `event_chain_state.last_hash`
# is seeded to by the migration (backend/alembic/versions/27bcb338ace5_*.py),
# and what the very first post-migration event's prev_hash must equal.
GENESIS_HASH = hashlib.sha256(b"").hexdigest()


def canonical_event_json(job_id, mrn, stage, event_type, ts, attempt, error_message, details) -> str:
    """Deterministic serialization of one event's identity fields."""
    payload = {
        "job_id": job_id,
        "mrn": mrn,
        "stage": stage,
        "event_type": event_type,
        "ts": ts,
        "attempt": attempt,
        "error_message": error_message,
        "details": details,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_row_hash(prev_hash: str, job_id, mrn, stage, event_type, ts, attempt, error_message, details) -> str:
    """row_hash = sha256(prev_hash || canonical_json(...))."""
    canonical = canonical_event_json(job_id, mrn, stage, event_type, ts, attempt, error_message, details)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
