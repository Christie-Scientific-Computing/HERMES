"""
Persists the outcome of periodic hash-chain verification runs (Phase 4,
docs/plans/frontend-rewrite-implementation-plan.md §6). backend/worker.py's
main loop calls backend/scripts/verify_audit_chain.py's own pure functions
(fetch_events_in_order/fetch_chain_state/verify_chain) directly on a timer
and records the result here -- this module has no verification logic of its
own, only storage, so the chain can never be checked two different ways.
"""
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import RealDictCursor

from backend.src.db import get_conn


class AuditChainDB:
    def record_check(self, ok: bool, bad_event_id: Optional[int] = None, reason: Optional[str] = None) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_chain_checks(checked_at, ok, bad_event_id, reason) VALUES (%s, %s, %s, %s)",
                (datetime.now(timezone.utc), ok, bad_event_id, reason),
            )

    def latest_check(self) -> Optional[dict]:
        """Most recent check, or None if the worker's periodic timer hasn't
        run yet (a fresh deployment) -- the admin dashboard renders this as
        "never verified yet" rather than erroring."""
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM audit_chain_checks ORDER BY checked_at DESC LIMIT 1")
            row = cur.fetchone()
            return dict(row) if row else None
