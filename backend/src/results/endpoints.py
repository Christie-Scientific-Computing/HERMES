"""
Results endpoints to expose job summaries and per-patient event timelines.

Outbound patient identifiers are translated real -> anon at this boundary
(passthrough when anonymisation isn't configured) -- callers outside the
backend must never see a real MRN. Inbound `mrn` path params are the anon
id the caller submitted and are resolved anon -> real before querying
StatusDB, which stores the real id internally.
"""
import asyncio
import json
import os
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB
from backend.src.plans.db_client import PlansDB
from backend.src.identity import anon
from backend.src.common.sse import format_sse

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/results', tags=['results'])

DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    try:
        status_db = StatusDB()
        tasks_db = TasksDB()
        plans_db = PlansDB()
        logger.debug("StatusDB initialized")
    except Exception as e:
        logger.error("Failed to init StatusDB: %s", e)
        raise ValueError(f"Failed to init StatusDB: {e}")

else:
    logger.error("DATABASE_URL not set; status events will not be recorded")
    raise ValueError("DATABASE_URL not set; status events will not be recorded")


def _anonymize_events(events: list[dict]) -> list[dict]:
    """
    Translate every event's `mrn` field (real id) to its display (anon) id, and
    scrub the real id out of the free-text fields alongside it.

    `error_message` is str(exception) from a worker and routinely quotes the
    MRN it was handed; `details` is a worker's own return value. Translating
    only the structured `mrn` column left the real id crossing the boundary in
    prose -- on the timeline, which is the page people read errors on.
    """
    if not events:
        return events
    display_map = anon.to_display_ids([e["mrn"] for e in events])
    return [
        {
            **e,
            "mrn": display_map[e["mrn"]],
            "error_message": _scrub(e.get("error_message"), e["mrn"], display_map[e["mrn"]]),
            "details": _scrub_json(e.get("details"), e["mrn"], display_map[e["mrn"]]),
        }
        for e in events
    ]


def _scrub(text: Optional[str], real_mrn: str, display_mrn: str) -> Optional[str]:
    """
    Replace a real MRN embedded in free text with its anon id.

    Unlike a structured `mrn` column there's nothing to translate here -- the
    real id is just spliced into prose: Pinnacle paths are built from the MRN,
    and exception messages routinely quote it. Left alone, these fields would
    be the one place a real id crosses the API boundary, on precisely the page
    built for reading error text.

    No-op when anonymisation isn't configured (real == display anyway).
    """
    if not text or not anon.is_configured():
        return text
    return text.replace(str(real_mrn), str(display_mrn))


def _scrub_json(value, real_mrn: str, display_mrn: str):
    """
    Same substitution as _scrub, applied to a JSONB value's string content.

    Round-tripping through JSON catches the id wherever it's nested, without
    having to know the shape a worker chose to return.
    """
    if value is None or not anon.is_configured():
        return value
    try:
        return json.loads(json.dumps(value).replace(str(real_mrn), str(display_mrn)))
    except (TypeError, ValueError):
        logger.warning("Could not scrub details payload; dropping it rather than risk a leak")
        return None


# A trailing 'start' with nothing after it means the item is still in flight.
_OUTCOME_BY_EVENT_TYPE = {"success": "success", "failure": "failure", "start": "running"}


@router.get('/job/{job_id}')
async def job_summary(job_id: str):
    """
    Return aggregated counts by stage and event_type for a job, plus the
    job's own metadata (project_id/created_by/description/created_at/
    cancelled) -- additive fields only; `summary` itself is unchanged so
    existing callers (e.g. webui/) aren't affected.

    Also carries `imported_count`/`submitted_count`: the "N/M imported"
    headline figure. Built on §D3's Orthanc ground truth (`details->>'imported'`
    on a retrieve-stage success event), not on `event_type == 'success'` alone
    -- a patient found nowhere still gets a success event (the operation ran
    without raising), so counting those would overstate how many patients
    actually got data. No real ids involved here (just counts), so no
    scrubbing needed.

    `exported_count`/`export_attempted_count` are the same idea for the
    export stage (StatusDB.count_exported_patients) -- additive fields for
    a combined import->export job (backend/worker.py's _maybe_chain_export),
    both simply 0 for a job with no export-stage events at all.
    """
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        summary = status_db.summarize_job(job_id)
        imported_count, submitted_count = status_db.count_imported_patients(job_id)
        exported_count, export_attempted_count = status_db.count_exported_patients(job_id)
        response = {
            "job_id": job_id, "summary": summary,
            "imported_count": imported_count, "submitted_count": submitted_count,
            "exported_count": exported_count, "export_attempted_count": export_attempted_count,
        }
        job = status_db.get_job(job_id)
        if job:
            response.update({
                "project_id": job.get("project_id"),
                "created_by": job.get("created_by"),
                "description": job.get("description"),
                "created_at": job.get("created_at"),
                "cancelled": job.get("cancelled"),
            })
        return response
    except Exception as e:
        logger.exception("Failed to get job summary: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# Poll interval for the observer stream below -- independent of
# HERMES_WORKER_POLL_INTERVAL (backend/worker.py's own claim-loop idle
# sleep), since this is how often a browser sees an update, not how often a
# worker looks for new work.
_OBSERVER_POLL_INTERVAL = float(os.getenv("HERMES_OBSERVER_POLL_INTERVAL", "1"))


_PENDING_TASK_STATES = ("queued", "claimed", "running")


async def _observe_job(job_id: str) -> AsyncIterator[str]:
    """
    Poll the tasks table (backend/src/status/tasks_db.py) and translate
    task state transitions into the exact SSE vocabulary
    templates/cotton/job_progress.html already listens for -- start,
    progress, success, error, cancelled, done, every event carrying "type".
    This is the queue-driven counterpart to run_batch_job
    (backend/src/common/sse.py): that generator emits events as a side
    effect of doing the work itself; this one only ever *observes* work a
    worker process (backend/worker.py) is doing independently, so closing
    this connection (or never opening it) has no effect on whether the job
    actually runs -- the whole point of the queue.

    Every emitted event's `mrn`/`current` field is display_id (the anon id,
    or the same value when anonymisation isn't configured), never
    status_mrn/the real id. `error`/`details` are worker-generated free
    text, though, and routinely quote the real id the same way
    job_patients_summary's mosaiq_reason/pinnacle_reason/proknow_reason do
    (CLAUDE.md's anonymisation-boundary section calls this out explicitly:
    "free text carries real MRNs too") -- both go through the same
    `_scrub`/`_scrub_json` this file already uses elsewhere, keyed off each
    row's own (real_id, display_id) pair.

    Every TasksDB/StatusDB call below is wrapped in asyncio.to_thread --
    these are plain synchronous psycopg2 round-trips (CLAUDE.md's "Async
    threading" section), and unlike a single request/response endpoint,
    this generator stays open and re-polls for a job's entire duration, one
    connection per browser tab watching it. Without to_thread, every poll
    tick of every open connection would block the single event loop every
    other concurrent request also depends on -- the exact hazard
    backend/src/common/sse.py's run_batch_job already wraps its own DB
    calls in asyncio.to_thread to avoid, worse here since this stream is
    long-lived by design rather than one call per batch item.
    """
    total = await asyncio.to_thread(tasks_db.count_tasks, job_id)
    yield format_sse({"type": "start", "total": total})
    last_total = total

    # Tracks each task's last-reported state so a re-read (see
    # TasksDB.job_progress's docstring for why this can't be filtered at
    # the SQL layer) only ever produces one event per actual transition.
    last_state: dict[int, str] = {}
    cancelled_reported = False

    while True:
        if not cancelled_reported and await asyncio.to_thread(status_db.is_cancelled, job_id):
            yield format_sse({"type": "cancelled"})
            cancelled_reported = True
            # Deliberately not a `break`: TasksDB.cancel_queued (called by
            # cancel_import) only flips still-QUEUED tasks -- a task already
            # claimed/running when cancellation happened is not interrupted
            # and will still reach a real success/failure ("items already in
            # progress finish" is the same promise run_batch_job's own
            # cancellation already makes). Keep polling until nothing is
            # actually pending so that outcome still gets reported, instead
            # of the stream silently going quiet on it.

        rows = await asyncio.to_thread(tasks_db.job_progress, job_id)

        # A combined import->export job (backend/worker.py's
        # _maybe_chain_export) grows its task count over the job's
        # lifetime -- export tasks are chained in one at a time as imports
        # succeed, so the initial `total` above only ever covers the
        # import tasks known at submission. Report the new total whenever
        # it changes so the frontend's progress bar can grow its
        # denominator accordingly; this is purely informational, the loop's
        # own termination below (has_pending) already reads live state
        # regardless of what `total` last said.
        if len(rows) != last_total:
            last_total = len(rows)
            yield format_sse({"type": "total", "total": last_total})

        has_pending = False
        for row in rows:
            task_id, state = row["task_id"], row["state"]
            if state in _PENDING_TASK_STATES:
                has_pending = True
            if last_state.get(task_id) == state:
                continue
            last_state[task_id] = state
            real_id, display_id, stage = row["real_id"], row["display_id"], row["stage"]

            if state == "running":
                yield format_sse({"type": "progress", "current": display_id, "stage": stage})
            elif state == "succeeded":
                details = _scrub_json(row["details"], real_id, display_id) or {}
                yield format_sse({"type": "success", "mrn": display_id, "stage": stage, **details})
            elif state == "failed" or (state == "cancelled" and row["error_message"]):
                # 'cancelled' is two different things, distinguished by
                # error_message: TasksDB.cancel_task (backend/worker.py's
                # claim-time ethics-denial path) always sets a reason, so an
                # attempted-then-denied task is reported as an error here.
                # TasksDB.cancel_queued (job-level bulk cancellation) never
                # sets one -- a task cancelled in bulk while still 'queued'
                # never ran at all, and is silently skipped, the same way
                # run_batch_job silently stops on the remaining items of a
                # cancelled job without emitting anything for them.
                error = _scrub(row["error_message"], real_id, display_id) or "Task did not complete"
                yield format_sse({"type": "error", "mrn": display_id, "stage": stage, "error": error})
            # "queued"/"claimed" -> no distinct SSE event; "progress" fires
            # once a task actually starts running, matching the vocabulary
            # table in docs/worker-queue-design.md.

        if not has_pending:
            break
        await asyncio.sleep(_OBSERVER_POLL_INTERVAL)

    yield format_sse({"type": "done"})


@router.get('/job/{job_id}/stream')
async def job_stream(job_id: str):
    """Live progress for a queue-driven job (docs/worker-queue-design.md) --
    the observer counterpart to the synchronous SSE stream run_batch_job
    still serves directly from import/export endpoints. What the frontend's
    EventSource connects to is unchanged either way; only whether the
    backend is watching existing work or doing the work itself differs."""
    if not status_db or not tasks_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    return StreamingResponse(
        _observe_job(job_id), media_type="text/event-stream", headers={"Cache-Control": "no-cache"},
    )


@router.get('/job/{job_id}/patients')
async def job_patients(job_id: str):
    """Return list of MRNs (patients) that have events for a job."""
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        patients = status_db.list_job_patients(job_id)
        display_map = anon.to_display_ids(patients)
        return {"job_id": job_id, "patients": [display_map[p] for p in patients]}
    except Exception as e:
        logger.exception("Failed to list patients: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/job/{job_id}/patients/summary')
async def job_patients_summary(job_id: str):
    """
    Per-patient summary for a job: anon id + retrieve-stage source-system
    presence (in_mosaiq/in_pinnacle/in_proknow), when available. Export-only
    jobs (or any patient with no successful retrieve event) simply have
    those keys come back null -- callers must render that as "unknown", not
    a false negative.

    Also carries `outcome` ('success'/'failure'/'running') and the latest
    `error_message`, derived from the most recent event of any kind. Source
    presence comes only from successful retrieves, so a patient that only ever
    failed has null presence but a 'failure' outcome -- which is the whole
    point: that patient is invisible otherwise.

    `mosaiq_reason`/`pinnacle_reason`/`proknow_reason` are the per-source
    diagnostic strings from Importer.find_patient (backend/src/retrieve/logic.py)
    -- explicitly scrubbed the same way `error_message` is, since they
    routinely quote the real MRN.

    Also carries `imported` (details_by_mrn's own ground-truth flag,
    surfaced directly rather than left for a caller to infer from
    in_mosaiq/in_pinnacle/in_proknow) and, for a combined import->export job
    (backend/worker.py's _maybe_chain_export), stage-specific
    `import_outcome`/`import_error_message` and `export_outcome`/
    `export_error_message` -- unlike the stage-agnostic `outcome`/
    `error_message` above (latest event of either stage), these let a
    caller distinguish "import failed" from "import succeeded, export
    failed" instead of the export's outcome silently shadowing the
    import's. Both simply null for a job that never had that stage.
    """
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        real_mrns = status_db.list_job_patients(job_id)
        details_by_mrn = status_db.get_latest_retrieve_details(job_id)
        latest_by_mrn = status_db.get_latest_event_per_patient(job_id)
        import_by_mrn = status_db.get_latest_event_per_patient(job_id, stage="retrieve")
        export_by_mrn = status_db.get_latest_event_per_patient(job_id, stage="export")
        display_map = anon.to_display_ids(real_mrns)
        patients = [
            {
                "mrn": display_map[real_mrn],
                "in_mosaiq": details_by_mrn.get(real_mrn, {}).get("in_mosaiq"),
                "in_pinnacle": details_by_mrn.get(real_mrn, {}).get("in_pinnacle"),
                "in_proknow": details_by_mrn.get(real_mrn, {}).get("in_proknow"),
                "status": details_by_mrn.get(real_mrn, {}).get("status"),
                "imported": details_by_mrn.get(real_mrn, {}).get("imported"),
                "outcome": _OUTCOME_BY_EVENT_TYPE.get(
                    latest_by_mrn.get(real_mrn, {}).get("event_type")
                ),
                "error_message": _scrub(
                    latest_by_mrn.get(real_mrn, {}).get("error_message"),
                    real_mrn,
                    display_map[real_mrn],
                ),
                "import_outcome": _OUTCOME_BY_EVENT_TYPE.get(
                    import_by_mrn.get(real_mrn, {}).get("event_type")
                ),
                "import_error_message": _scrub(
                    import_by_mrn.get(real_mrn, {}).get("error_message"),
                    real_mrn,
                    display_map[real_mrn],
                ),
                "export_outcome": _OUTCOME_BY_EVENT_TYPE.get(
                    export_by_mrn.get(real_mrn, {}).get("event_type")
                ),
                "export_error_message": _scrub(
                    export_by_mrn.get(real_mrn, {}).get("error_message"),
                    real_mrn,
                    display_map[real_mrn],
                ),
                # These routinely quote the real MRN (Mosaiq/ProKnow exception
                # text; Pinnacle's error_message, built from/quoting the mrn)
                # -- unlike the fields above them, they come straight out of a
                # worker's own return value, not a structured column, so they
                # need the same explicit _scrub() error_message already gets.
                # Reading them unscrubbed would leak a real id across the
                # anonymisation boundary -- see test_results_anon_boundary.py.
                "mosaiq_reason": _scrub(
                    details_by_mrn.get(real_mrn, {}).get("mosaiq_reason"),
                    real_mrn,
                    display_map[real_mrn],
                ),
                "pinnacle_reason": _scrub(
                    details_by_mrn.get(real_mrn, {}).get("pinnacle_reason"),
                    real_mrn,
                    display_map[real_mrn],
                ),
                "proknow_reason": _scrub(
                    details_by_mrn.get(real_mrn, {}).get("proknow_reason"),
                    real_mrn,
                    display_map[real_mrn],
                ),
            }
            for real_mrn in real_mrns
        ]
        return {"job_id": job_id, "patients": patients}
    except Exception as e:
        logger.exception("Failed to build patient summary: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/patient/{mrn}/plans')
async def patient_plans(mrn: str):
    """
    Every plan PinnacleExport recorded for this patient. `mrn` is the anon id.

    Plans belong to a patient, not to a job -- there's no job_id on the table --
    so this isn't job-scoped. Callers that need access control should apply it
    themselves (frontend/jobs/views.py reaches this through a job-scoped URL).

    `available: false` means PinnacleExport's schema isn't present in this
    database at all, which is NOT the same as the patient having no plans.
    """
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        real_mrn = anon.resolve_real_id(mrn)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        plans = plans_db.list_plans_for_patient(real_mrn)
    except Exception as e:
        logger.exception("Failed to fetch plans: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    if plans is None:
        return {"mrn": mrn, "plans": [], "available": False}

    # path/comment/error_message are free text built from, or quoting, the real
    # MRN -- scrub all three, not just the obvious one.
    scrubbed = [
        {
            **plan,
            "path": _scrub(plan.get("path"), real_mrn, mrn),
            "comment": _scrub(plan.get("comment"), real_mrn, mrn),
            "error_message": _scrub(plan.get("error_message"), real_mrn, mrn),
        }
        for plan in plans
    ]
    return {"mrn": mrn, "plans": scrubbed, "available": True}


@router.get('/patient/{job_id}/{mrn}')
async def patient_timeline(job_id: str, mrn: str):
    """Return chronological events for a patient in a job. `mrn` is the anon id."""

    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        real_mrn = anon.resolve_real_id(mrn)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    try:
        events = status_db.get_patient_history(job_id, real_mrn)
        return {"job_id": job_id, "mrn": mrn, "events": _anonymize_events(events)}
    except Exception as e:
        logger.exception("Failed to fetch patient timeline: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/patient/timeline/{mrn}/all')
async def patient_timeline_all(mrn: str):
    """Return chronological events for a patient across all jobs. `mrn` is the anon id."""
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")

    try:
        real_mrn = anon.resolve_real_id(mrn)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    try:
        events = status_db.get_patient_history_all_jobs(real_mrn)
        return {"mrn": mrn, "events": _anonymize_events(events)}
    except Exception as e:
        logger.exception("Failed to fetch patient timeline (all jobs): %s", e)
        raise HTTPException(status_code=500, detail=str(e))
