"""
Results endpoints to expose job summaries and per-patient event timelines.
"""
import os
import logging
from fastapi import APIRouter, HTTPException

from backend.src.status.db_client import StatusDB

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/results', tags=['results'])

STATUS_DB = os.getenv('STATUS_DB')
if STATUS_DB:
    try:
        status_db = StatusDB(STATUS_DB)
        logger.debug("StatusDB initialized")
    except Exception as e:
        logger.error("Failed to init StatusDB: %s", e)
        raise ValueError(f"Failed to init StatusDB: {e}")
     
else:
    logger.error("STATUS_DB not set; status events will not be recorded")
    raise ValueError("STATUS_DB not set; status events will not be recorded")


@router.get('/job/{job_id}')
async def job_summary(job_id: str):
    """Return aggregated counts by stage and event_type for a job."""
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        summary = status_db.summarize_job(job_id)
        return {"job_id": job_id, "summary": summary}
    except Exception as e:
        logger.exception("Failed to get job summary: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/job/{job_id}/patients')
async def job_patients(job_id: str):
    """Return list of MRNs (patients) that have events for a job."""
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        conn = status_db._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT mrn FROM events WHERE job_id=? ORDER BY mrn", (job_id,))
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return {"job_id": job_id, "patients": rows}
    except Exception as e:
        logger.exception("Failed to list patients: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/patient/{job_id}/{mrn}')
async def patient_timeline(job_id: str, mrn: str):
    """Return chronological events for a patient in a job."""
    
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        events = status_db.get_patient_history(job_id, mrn)
        return {"job_id": job_id, "mrn": mrn, "events": events}
    except Exception as e:
        logger.exception("Failed to fetch patient timeline: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/patient/timeline/{mrn}/all')
async def patient_timeline_all(mrn: str):
    """Return chronological events for a patient across all jobs."""
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    
    try:
        conn = status_db._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM events WHERE mrn=? ORDER BY ts", (mrn,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return {"mrn": mrn, "events": rows}
    except Exception as e:
        logger.exception("Failed to fetch patient timeline (all jobs): %s", e)
        raise HTTPException(status_code=500, detail=str(e))
