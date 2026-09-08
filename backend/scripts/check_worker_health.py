"""
Post-deploy smoke test for backend/worker.py's periodic hooks (audit-chain
verification, job-completion notifications) -- Phase 5 cutover
(docs/plans/frontend-rewrite-implementation-plan.md's "Confirm, don't just
merge" instruction). Neither hook has any frontend-visible symptom if it
silently isn't running (e.g. an old worker process still on pre-Phase-4
code after a partial deploy) -- this turns that into something you can
actually run and get a yes/no answer from, instead of trusting that
merging the code means it's executing.

Run standalone against DATABASE_URL, from the repo root:

    python backend/scripts/check_worker_health.py

Exit code 0 means both hooks look healthy; exit code 1 means at least one
looks stalled, with a printed reason.

Checks performed:
1. Audit-chain check staleness -- AuditChainDB.latest_check() should be no
   older than 2x HERMES_AUDIT_CHECK_INTERVAL_SECONDS (worker.py's own
   timer period); a fresh deployment with no check yet is reported
   separately, not as a failure (there may genuinely be no worker uptime
   yet to have run one).
2. Notification-hook staleness -- StatusDB.list_completed_jobs_missing_notification
   finds jobs that finished (every task terminal) more than
   --notification-lag-minutes ago but never got completed_notified_at set,
   which is exactly the symptom of _maybe_notify_job_complete never having
   run for them.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python backend/scripts/check_worker_health.py` to run directly,
# matching verify_audit_chain.py's own convention.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.src.status.audit_chain_db import AuditChainDB  # noqa: E402
from backend.src.status.db_client import StatusDB  # noqa: E402

_DEFAULT_AUDIT_CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # worker.py's own default


def check_audit_chain(audit_chain_db: AuditChainDB, max_staleness_seconds: float) -> tuple[bool, str]:
    latest = audit_chain_db.latest_check()
    if latest is None:
        return True, "audit chain: never checked yet (no worker uptime long enough to run one, or a fresh deployment)"

    age_seconds = (datetime.now(timezone.utc) - latest["checked_at"]).total_seconds()
    if age_seconds > max_staleness_seconds:
        return False, (
            f"audit chain: last checked {age_seconds / 3600:.1f}h ago, "
            f"expected within {max_staleness_seconds / 3600:.1f}h -- worker.py's "
            f"periodic timer may not be running"
        )
    if not latest["ok"]:
        return False, f"audit chain: last check reported TAMPERED -- {latest['reason']}"
    return True, f"audit chain: OK, last checked {age_seconds / 60:.0f}m ago"


def check_notifications(status_db: StatusDB, older_than_minutes: int) -> tuple[bool, str]:
    stuck = status_db.list_completed_jobs_missing_notification(older_than_minutes=older_than_minutes)
    if stuck:
        job_ids = ", ".join(row["job_id"] for row in stuck[:5])
        more = f" (+{len(stuck) - 5} more)" if len(stuck) > 5 else ""
        return False, (
            f"notifications: {len(stuck)} job(s) finished over {older_than_minutes}m ago with no "
            f"completion notification -- worker.py's _maybe_notify_job_complete hook may not be "
            f"running: {job_ids}{more}"
        )
    return True, "notifications: OK, no completed jobs missing a notification"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--max-audit-staleness-seconds", type=float,
        default=_DEFAULT_AUDIT_CHECK_INTERVAL_SECONDS * 2,
        help="fail if the last audit-chain check is older than this (default: 2x worker.py's own interval)",
    )
    parser.add_argument(
        "--notification-lag-minutes", type=int, default=30,
        help="grace window for a job to go from all-tasks-terminal to notified before flagging it",
    )
    args = parser.parse_args()

    audit_ok, audit_msg = check_audit_chain(AuditChainDB(), args.max_audit_staleness_seconds)
    notif_ok, notif_msg = check_notifications(StatusDB(), args.notification_lag_minutes)

    print(audit_msg)
    print(notif_msg)
    return 0 if (audit_ok and notif_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
