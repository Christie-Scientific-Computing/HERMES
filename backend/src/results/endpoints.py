"""
Results endpoints to expose job summaries and per-patient event timelines.

Outbound patient identifiers are translated real -> anon at this boundary
(passthrough when anonymisation isn't configured) -- callers outside the
backend must never see a real MRN. Inbound `mrn` path params are the anon
id the caller submitted and are resolved anon -> real before querying
StatusDB, which stores the real id internally.
"""
import json
import os
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.src.status.db_client import StatusDB
from backend.src.plans.db_client import PlansDB
from backend.src.identity import anon

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/results', tags=['results'])

DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    try:
        status_db = StatusDB()
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
    """
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        summary = status_db.summarize_job(job_id)
        response = {"job_id": job_id, "summary": summary}
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
    """
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        real_mrns = status_db.list_job_patients(job_id)
        details_by_mrn = status_db.get_latest_retrieve_details(job_id)
        latest_by_mrn = status_db.get_latest_event_per_patient(job_id)
        display_map = anon.to_display_ids(real_mrns)
        patients = [
            {
                "mrn": display_map[real_mrn],
                "in_mosaiq": details_by_mrn.get(real_mrn, {}).get("in_mosaiq"),
                "in_pinnacle": details_by_mrn.get(real_mrn, {}).get("in_pinnacle"),
                "in_proknow": details_by_mrn.get(real_mrn, {}).get("in_proknow"),
                "status": details_by_mrn.get(real_mrn, {}).get("status"),
                "outcome": _OUTCOME_BY_EVENT_TYPE.get(
                    latest_by_mrn.get(real_mrn, {}).get("event_type")
                ),
                "error_message": _scrub(
                    latest_by_mrn.get(real_mrn, {}).get("error_message"),
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
