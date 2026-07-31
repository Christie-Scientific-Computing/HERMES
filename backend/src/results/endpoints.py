"""
Results endpoints to expose job summaries and per-patient event timelines.

Outbound patient identifiers are translated real -> anon at this boundary
(passthrough when anonymisation isn't configured) -- callers outside the
backend must never see a real MRN. Inbound `mrn` path params are the anon
id the caller submitted and are resolved anon -> real before querying
StatusDB, which stores the real id internally.
"""
import os
import logging
from fastapi import APIRouter, HTTPException

from backend.src.status.db_client import StatusDB
from backend.src.identity import anon

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/results', tags=['results'])

DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    try:
        status_db = StatusDB()
        logger.debug("StatusDB initialized")
    except Exception as e:
        logger.error("Failed to init StatusDB: %s", e)
        raise ValueError(f"Failed to init StatusDB: {e}")

else:
    logger.error("DATABASE_URL not set; status events will not be recorded")
    raise ValueError("DATABASE_URL not set; status events will not be recorded")


def _anonymize_events(events: list[dict]) -> list[dict]:
    """Translate every event's `mrn` field (real id) to its display (anon) id."""
    if not events:
        return events
    display_map = anon.to_display_ids([e["mrn"] for e in events])
    return [{**e, "mrn": display_map[e["mrn"]]} for e in events]


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
    """
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        real_mrns = status_db.list_job_patients(job_id)
        details_by_mrn = status_db.get_latest_retrieve_details(job_id)
        display_map = anon.to_display_ids(real_mrns)
        patients = [
            {
                "mrn": display_map[real_mrn],
                "in_mosaiq": details_by_mrn.get(real_mrn, {}).get("in_mosaiq"),
                "in_pinnacle": details_by_mrn.get(real_mrn, {}).get("in_pinnacle"),
                "in_proknow": details_by_mrn.get(real_mrn, {}).get("in_proknow"),
                "status": details_by_mrn.get(real_mrn, {}).get("status"),
            }
            for real_mrn in real_mrns
        ]
        return {"job_id": job_id, "patients": patients}
    except Exception as e:
        logger.exception("Failed to build patient summary: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
