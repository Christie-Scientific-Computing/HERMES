"""
Recompute the `events` hash chain from scratch and report whether it's
intact, or exactly where it first breaks (docs/safety-plan.md §D1).

A hash chain is only useful if something checks it -- this is that check.
`StatusDB.add_event` (backend/src/status/db_client.py) writes the chain on
every insert; this script is the independent read side, using the same
canonical-JSON + sha256 logic from backend/src/status/hash_chain.py so the
two can never silently drift apart.

Run standalone against DATABASE_URL, from the repo root:

    python backend/scripts/verify_audit_chain.py

Exit code 0 means the chain is fully intact (or there's nothing to verify
yet); exit code 1 means a mismatch was found, printed with the offending
event's id (or, for a stale event_chain_state, without one -- see below)
and the reason.

Checks performed:
1. Every row's row_hash is recomputed from scratch and compared to what's
   stored, and every row's prev_hash is compared to the previous chained
   row's row_hash (or GENESIS_HASH for the first chained row). This catches
   any row being altered after it was written, or the chain linkage
   between two rows being broken.
2. The live `event_chain_state.last_hash` pointer -- the value the *next*
   add_event call will read under `FOR UPDATE` and chain from -- is
   compared against the row_hash of the actual last row in `events` (or
   GENESIS_HASH if there are no chained rows at all). This is a distinct
   check from (1): deleting the most recent N rows *without* touching
   event_chain_state leaves every remaining row perfectly self-consistent
   under check (1) alone, while the state table silently points at a hash
   that belongs to no row at all. Check (2) is what catches that.

Known limitation (stated in the safety plan, not solved here): even with
both checks, this proves a row wasn't silently altered *after* being
written, and that the chain's tail hasn't been truncated without
re-seeding event_chain_state. It does not stop someone with direct DB
access from truncating `events` *and* correctly re-seeding
`event_chain_state` to match, or from disabling the application logic that
maintains the chain in the first place -- see docs/known-issues.md.
"""
import sys
from pathlib import Path
from typing import Iterable, Optional

# Allow `python backend/scripts/verify_audit_chain.py` to run directly
# (i.e. without the repo root already on sys.path / being invoked as
# `python -m`), matching how a cron job or admin would actually run this.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from psycopg2.extras import RealDictCursor  # noqa: E402

from backend.src.db import get_conn  # noqa: E402
from backend.src.status.hash_chain import GENESIS_HASH, compute_row_hash  # noqa: E402


def fetch_events_in_order() -> list[dict]:
    """All events, ascending by id (i.e. insertion order) -- the order the
    chain was built in, and the only order it can be verified in."""
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, job_id, mrn, stage, event_type, ts, attempt,
                   error_message, details, prev_hash, row_hash
            FROM events
            ORDER BY id
            """
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_chain_state() -> Optional[str]:
    """The live event_chain_state.last_hash -- what the *next* add_event
    call will read under FOR UPDATE and chain from. None if the singleton
    row is somehow missing entirely (shouldn't happen post-migration)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_hash FROM event_chain_state WHERE id = 1")
        row = cur.fetchone()
        return row[0] if row else None


def verify_chain(
    rows: Iterable[dict], chain_state_last_hash: Optional[str] = None
) -> tuple[bool, Optional[dict], Optional[str]]:
    """
    Walk `rows` (must already be in ascending id order) and recompute each
    row_hash from scratch. Returns (ok, first_bad_row, reason).

    Rows with prev_hash AND row_hash both NULL predate the chain (either
    written before this migration, or the migration boundary itself) and
    are skipped -- there's nothing to verify about them, the chain simply
    starts fresh at the first post-migration row. That first chained row is
    expected to chain from GENESIS_HASH (sha256('').hexdigest()), matching
    how the migration seeded event_chain_state.last_hash.

    If `chain_state_last_hash` is given (the live event_chain_state value,
    from fetch_chain_state()), it's also checked against the row_hash of
    the last chained row seen (or GENESIS_HASH if none were chained). This
    is a genuinely separate check from the per-row walk above: deleting the
    most recent N rows from `events` *without* touching event_chain_state
    leaves every remaining row perfectly self-consistent -- the per-row
    walk alone would report "intact" -- while the state table silently
    points at a hash that belongs to no row at all. Passing
    chain_state_last_hash is what catches that. A mismatch here has no
    single offending row (the problem is the *absence* of one), so
    first_bad_row is None in that case; reason still explains what's wrong.
    """
    expected_prev = None
    for row in rows:
        if row["prev_hash"] is None and row["row_hash"] is None:
            continue

        if expected_prev is None:
            expected_prev = GENESIS_HASH

        if row["prev_hash"] != expected_prev:
            return False, row, (
                f"prev_hash mismatch on event id={row['id']}: "
                f"expected {expected_prev!r}, found {row['prev_hash']!r} "
                f"-- the chain link into this row is broken"
            )

        recomputed = compute_row_hash(
            row["prev_hash"], row["job_id"], row["mrn"], row["stage"],
            row["event_type"], row["ts"], row["attempt"],
            row["error_message"], row["details"],
        )
        if recomputed != row["row_hash"]:
            return False, row, (
                f"row_hash mismatch on event id={row['id']}: "
                f"stored {row['row_hash']!r}, recomputed {recomputed!r} "
                f"-- this row's contents were altered after being written"
            )

        expected_prev = row["row_hash"]

    if chain_state_last_hash is not None:
        expected_state = expected_prev if expected_prev is not None else GENESIS_HASH
        if chain_state_last_hash != expected_state:
            return False, None, (
                f"event_chain_state.last_hash={chain_state_last_hash!r} does not match "
                f"the last chained row's row_hash={expected_state!r} -- the chain's tail "
                f"was likely truncated (rows deleted from events) without re-seeding "
                f"event_chain_state to match"
            )

    return True, None, None


def main() -> int:
    rows = fetch_events_in_order()
    chain_state_last_hash = fetch_chain_state()
    ok, bad_row, reason = verify_chain(rows, chain_state_last_hash=chain_state_last_hash)
    if ok:
        print(f"OK: hash chain intact across {len(rows)} event(s).")
        return 0
    print(f"TAMPERED: {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
