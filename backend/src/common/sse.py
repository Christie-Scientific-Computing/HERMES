"""
Shared SSE batch-job machinery.

Consolidates what used to be four near-identical generators
(import_event_stream, export_event_stream, proknow_upload_stream,
export_by_uid_stream) into one generic runner. Domain-specific endpoints
build a list of BatchItem and a `worker` callable, and get consistent
start/progress/success/error/cancelled/done events, StatusDB bookkeeping,
and cancellation for free.

Every event -- including the terminal one -- carries a "type" key (the old
`{"done": true}` shape with no "type" is gone; there's no legacy frontend
left to keep compatible with).

Cancellation is backed by StatusDB.cancel_job/is_cancelled (a column on the
`jobs` table) rather than an in-process dict, so it works correctly even if
the backend ever runs as more than one worker process.
"""
import json
import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend.src.identity import anon

logger = logging.getLogger(__name__)


@dataclass
class BatchItem:
    """One unit of work in a batch job.

    real_id: the actual identifier passed to `worker` and used for any
        external-system call (Mosaiq/Pinnacle/ProKnow/Orthanc) -- always
        the REAL id, never the anon id.
    display_id: what appears in progress/success/error events sent back
        across the API boundary -- the anon id when anonymisation is
        configured, otherwise the same as real_id.
    status_mrn: what gets written to StatusDB's patients/events tables --
        always the real id (backend-internal storage may contain real
        IDs; only the outbound boundary must never see them).
    input_path: recorded alongside the patient in StatusDB, if relevant.
    extra: worker-specific data that doesn't fit real_id/display_id/status_mrn
        (e.g. a study/series UID pair for the UID-based export flow, where
        the "identifier" being worked on isn't a patient id at all).
    """
    real_id: str
    display_id: str
    status_mrn: str
    input_path: Optional[str] = None
    extra: dict = None

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}


def format_sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def to_public_details(details: Optional[dict]) -> dict:
    """
    Strip real DICOM UIDs from a worker's result dict before it crosses the
    API boundary -- an SSE success event's spread fields, or
    {"type": "success", **response} for a single-item endpoint.

    `study_uids`/`series_uids` (export/endpoints.py's and
    retrieve/endpoints.py's Response models, and the audit manifest built
    for a DICOM C-MOVE/UID-move export) are dropped entirely -- unlike a
    clinical date, there's no legitimate "shifted UID" to return instead
    (docs/plans/pii-boundary-test-suite.md decision 6: DICOM UIDs forbidden
    everywhere, no exceptions). `checksums` -- dict[SOPInstanceUID, hash],
    a real UID on every key -- is re-keyed to a plain list[str] of hash
    values only, keeping the count/values without the identifiers.

    Applied here, at each outbound emission point, NOT on the Response
    model itself and NOT on what StatusDB.add_event/TasksDB.mark_succeeded
    store -- events.details/tasks.details keep full real-UID fidelity for
    audit (docs/plans/safety-plan.md §D2); only what actually crosses the
    HTTP/SSE boundary is reshaped.

    A plain dict in, a plain dict out (never a Response instance) -- this
    runs on both the import Response (study_uids/study_count) and every
    export Response (study_uids/series_uids/checksums) shape, and is a
    no-op passthrough for a dict with neither key, so it's safe to call
    unconditionally at every success-emission site rather than needing each
    caller to know which worker kind actually produces UIDs.
    """
    if not details:
        return details or {}
    public = {k: v for k, v in details.items() if k not in ("study_uids", "series_uids")}
    checksums = public.get("checksums")
    if isinstance(checksums, dict):
        public["checksums"] = list(checksums.values())
    return public


def build_patient_id_batch(rows: list[dict], input_path: Optional[str] = None) -> list[BatchItem]:
    """
    Turn CSV rows with a `patient_id` column into BatchItems, resolving every
    submitted id through the anon-mapping boundary. `patient_id` as submitted
    is always treated as an anon id (passthrough when anonymisation isn't
    configured) -- callers outside the backend must never need to know a
    real MRN.

    Raises anon.AnonLookupError if any submitted id has no mapping -- callers
    should catch this and turn it into a 422 before starting an SSE stream.
    """
    submitted_ids = [str(row['patient_id']) for row in rows]
    real_id_map = anon.resolve_real_ids(submitted_ids)  # raises AnonLookupError on unknown ids

    return [
        BatchItem(
            real_id=real_id_map[str(row['patient_id'])],
            display_id=str(row['patient_id']),
            status_mrn=real_id_map[str(row['patient_id'])],
            input_path=input_path,
        )
        for row in rows
    ]


async def run_batch_job(
    job_id: str,
    items: list[BatchItem],
    stage: str,
    worker: Callable[[BatchItem], Optional[dict]],
    status_db,
    description: Optional[str] = None,
    created_by: Optional[str] = None,
    project_id: Optional[str] = None,
):
    """
    Generic SSE batch-job generator.

    `worker` is a sync callable (run via asyncio.to_thread) that does the
    actual work for one item and returns a dict of extra fields to merge
    into the success event (or None/{} if there's nothing extra to report).
    It should raise on failure.

    `created_by`/`project_id` trace the job back to the Django user and
    ethics-approved project that authorized it (see
    backend/src/projects/enforcement.py, which callers must have already
    checked before calling this) -- purely bookkeeping here, not enforcement.

    status_db.create_job/add_patient/add_event calls are wrapped in
    asyncio.to_thread here (not inside StatusDB itself): add_event now takes
    a `SELECT ... FOR UPDATE` row lock as part of the events hash chain
    (docs/safety-plan.md §D1, backend/src/status/db_client.py), so without
    this, lock contention across concurrently-running batch jobs would block
    the single asyncio event loop that every other concurrent request also
    depends on, not just a thread-pool worker. StatusDB's own methods stay
    plain synchronous calls -- backend/src/retrieve/endpoints.py and
    backend/src/export/endpoints.py's single-item endpoints
    (single_import, proknow_upload_patient) call them directly the same way
    they always have, so this file is the only thing that changed shape.
    """
    if status_db:
        try:
            await asyncio.to_thread(
                status_db.create_job, job_id, description=description, created_by=created_by, project_id=project_id
            )
        except Exception as e:
            logger.warning("Could not create job in status DB: %s", e)

    yield format_sse({"type": "start", "total": len(items)})

    for item in items:
        if status_db:
            try:
                if status_db.is_cancelled(job_id):
                    logger.info("Client cancelled request, aborting")
                    yield format_sse({"type": "cancelled"})
                    break
            except Exception as e:
                logger.warning("Could not check cancellation status: %s", e)

        if status_db:
            try:
                await asyncio.to_thread(status_db.add_patient, job_id, item.status_mrn, input_path=item.input_path)
                await asyncio.to_thread(status_db.add_event, job_id, item.status_mrn, stage=stage, event_type="start")
            except Exception as e:
                logger.warning("Status DB write failed: %s", e)

        yield format_sse({"type": "progress", "current": item.display_id})
        start = time.time()
        try:
            res = await asyncio.to_thread(worker, item)
            res = res or {}

            if status_db:
                try:
                    await asyncio.to_thread(
                        status_db.add_event, job_id, item.status_mrn, stage=stage, event_type="success", details=res
                    )
                except Exception as e:
                    logger.warning("Status DB write failed: %s", e)

            # "mrn" is set last, after spreading res, so nothing worker
            # returns can ever override it with a real id. res itself keeps
            # full fidelity (including any real UIDs) in the add_event call
            # above -- only this outbound yield goes through
            # to_public_details, which strips study_uids/series_uids and
            # reshapes checksums (see its docstring).
            yield format_sse({
                "type": "success",
                "execution_time": round(time.time() - start, 2),
                **to_public_details(res),
                "mrn": item.display_id,
            })

        except Exception as e:
            logger.error("Batch job item failed (%s): %s", item.display_id, e)

            if status_db:
                try:
                    await asyncio.to_thread(
                        status_db.add_event, job_id, item.status_mrn, stage=stage, event_type="failure",
                        error_message=str(e),
                    )
                except Exception as ex:
                    logger.warning("Status DB write failed: %s", ex)

            yield format_sse({
                "type": "error",
                "execution_time": round(time.time() - start, 2),
                "mrn": item.display_id,
                "error": str(e),
            })

    yield format_sse({"type": "done"})
