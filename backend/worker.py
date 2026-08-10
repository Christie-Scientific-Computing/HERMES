"""
python -m backend.worker

Claims tasks from the `tasks` table (backend/src/status/tasks_db.py) and
runs them via the same worker factories the synchronous batch endpoints
(backend/src/retrieve/endpoints.py, backend/src/export/endpoints.py)
already use -- this process adds no new business logic, only a
claim/execute/terminal-write loop around existing factories. See
docs/worker-queue-design.md for the full design.

Deliberately does NOT run migrations (unlike backend/main.py's
setup_status_db, which does init_pool + `alembic upgrade head`) -- N worker
processes racing Alembic on boot is a real hazard, so the API process stays
the sole migrator. This calls backend.src.db.init_pool directly instead.
"""
import os
import signal
import socket
import time
import logging

from dotenv import load_dotenv

from backend.src.db import init_pool
from backend.src.status.tasks_db import TasksDB
from backend.src.status.db_client import StatusDB
from backend.src.projects import enforcement
from backend.src.retrieve.logic import Importer
from backend.src.retrieve.endpoints import Response as ImportResponse

load_dotenv()

logging.basicConfig(
    filename=None,
    level=os.getenv("HERMES_LOG_LEVEL", "INFO"),
    format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_SHUTDOWN = False


def _handle_sigterm(signum, frame):
    global _SHUTDOWN
    logger.info("Received signal %s; will exit after the in-flight task finishes", signum)
    _SHUTDOWN = True


# Importer is cached per process, per import_level -- Exporter is NOT.
# Importer.__init__ (retrieve/logic.py) opens a ProKnow client AND an
# Orthanc client eagerly; under a worker claiming one task after another,
# building a fresh one per task would be a connection storm (the same
# concern retrieve/logic.py's own comment names for the synchronous
# per-patient case). Exporter.__init__ (export/logic.py), by contrast, only
# sets self.destination/self.tmp_dir -- no I/O happens until a method is
# actually called -- so it needs no such cache; a fresh Exporter per task
# costs nothing and keeps its destination scoped to exactly one task.
_importer_cache: dict[str, Importer] = {}


def _get_importer(import_level: str) -> Importer:
    if import_level not in _importer_cache:
        _importer_cache[import_level] = Importer(import_level)
    return _importer_cache[import_level]


def _run_import(task: dict) -> dict:
    res = _get_importer(task["params"]["import_level"]).handle_patient(task["real_id"])
    return ImportResponse(mrn=task["real_id"], **res).model_dump(exclude={"mrn"}, exclude_none=True)


# kind -> factory dispatch. Only "import" is wired today (step 2 of
# docs/worker-queue-design.md's sequencing); the three export flows join
# this dict in a later step, once their endpoints are converted to enqueue
# rather than run inline. Extending with a new flow -- or, later, a
# per-user destination-allow-list check (docs/safety-plan.md §A) -- means
# adding one entry/one call here, not touching the claim/execute loop below.
_HANDLERS = {
    "import": _run_import,
}


def _handle_one(tasks_db: TasksDB, status_db: StatusDB, task: dict) -> None:
    job_id, task_id, stage = task["job_id"], task["task_id"], task["stage"]
    status_mrn, display_id = task["status_mrn"], task["display_id"]
    params = task["params"]
    # This run's attempt number: task["attempts"] is the count of PRIOR
    # failed attempts as of claim time (0 for a first try), so this run is
    # attempt task["attempts"] + 1 -- matches add_event's 1-indexed default.
    attempt = task["attempts"] + 1

    # Re-check the ethics gate at claim time (docs/worker-queue-design.md
    # point #5): a project can be revoked or expire between enqueue and
    # execution, and projects/enforcement.py is explicit that membership is
    # re-checked live, never cached. Denial cancels the task outright --
    # it is not retried.
    try:
        enforcement.require_project_member(params["project_id"], params["username"])
    except Exception as e:
        logger.warning("Task %s (job %s) denied at claim time: %s", task_id, job_id, e)
        tasks_db.cancel_task(task_id, reason=f"project membership check failed: {e}")
        status_db.add_event(
            job_id, status_mrn, stage=stage, event_type="failure",
            error_message="project membership revoked or could not be verified",
            attempt=attempt, task_id=task_id,
        )
        return

    if not tasks_db.mark_running(task_id):
        # Lost a race with a reaper or another worker in the tiny window
        # between claim() and here -- the task is no longer ours to run.
        logger.warning("Task %s (job %s) could not transition to running; skipping", task_id, job_id)
        return

    status_db.add_event(job_id, status_mrn, stage=stage, event_type="start", attempt=attempt, task_id=task_id)

    try:
        handler = _HANDLERS[task["kind"]]
        details = handler(task)
    except Exception as e:
        logger.exception("Task %s (job %s, display_id=%s) failed", task_id, job_id, display_id)
        outcome = tasks_db.mark_failed(task_id, str(e))
        if outcome != "unchanged":
            status_db.add_event(
                job_id, status_mrn, stage=stage, event_type="failure",
                error_message=str(e), attempt=attempt, task_id=task_id,
            )
        return

    if tasks_db.mark_succeeded(task_id, details):
        status_db.add_event(
            job_id, status_mrn, stage=stage, event_type="success",
            details=details, attempt=attempt, task_id=task_id,
        )


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not set; ABORTING!")
        return
    init_pool(database_url)  # NOT setup_status_db -- no alembic here, see module docstring

    tasks_db = TasksDB()
    status_db = StatusDB()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    poll_interval = float(os.getenv("HERMES_WORKER_POLL_INTERVAL", "2"))
    stale_seconds = int(os.getenv("HERMES_TASK_STALE_SECONDS", "1800"))
    reap_interval_seconds = 60.0

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    logger.info(
        "Worker %s starting (poll_interval=%.1fs, stale_seconds=%d)",
        worker_id, poll_interval, stale_seconds,
    )

    last_reap = 0.0
    while not _SHUTDOWN:
        now = time.monotonic()
        if now - last_reap > reap_interval_seconds:
            reaped = tasks_db.reap_stale_claims(stale_seconds)
            if reaped:
                logger.warning("Reaped %d stale claim(s)", reaped)
            last_reap = now

        task = tasks_db.claim(worker_id)
        if task is None:
            time.sleep(poll_interval)
            continue

        logger.info(
            "Worker %s claimed task %s (job %s, kind=%s, display_id=%s)",
            worker_id, task["task_id"], task["job_id"], task["kind"], task["display_id"],
        )
        _handle_one(tasks_db, status_db, task)

    logger.info("Worker %s shutting down", worker_id)


if __name__ == "__main__":
    main()
