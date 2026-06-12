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
        logger.debug("StatusDB initialized for results endpoints")
    except Exception as e:
        logger.warning("Failed to init StatusDB for results endpoints: %s", e)
        status_db = None
else:
    status_db = None
    logger.warning("STATUS_DB not set; results endpoints will be unavailable")


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


@router.get('/patient/all/{mrn}')
async def patient_timeline_all(mrn: str):
    """Return chronological events for a patient across all jobs.

    Falls back to legacy tables (status, errors, plans, uploads) if no events found in new events table.
    """
    if not status_db:
        raise HTTPException(status_code=503, detail="Status DB not configured")
    try:
        conn = status_db._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM events WHERE mrn=? ORDER BY ts", (mrn,))
        rows = [dict(r) for r in cur.fetchall()]

        # If no canonical events, try to reconstruct from legacy tables
        if not rows:
            legacy = []
            # status table
            try:
                cur.execute("SELECT process_datetime as ts, status as event_type, path FROM status WHERE mrn=? ORDER BY process_datetime", (mrn,))
                for r in cur.fetchall():
                    legacy.append({
                        'ts': r[0],
                        'stage': 'retrieve',
                        'event_type': r[1],
                        'details': json.dumps({'path': r[2]}) if r[2] else None,
                    })
            except Exception:
                # table may not exist in older DBs
                pass

            # errors table
            try:
                cur.execute("SELECT error_message, path FROM errors WHERE mrn=?", (mrn,))
                for r in cur.fetchall():
                    legacy.append({
                        'ts': None,
                        'stage': 'retrieve',
                        'event_type': 'failure',
                        'error_message': r[0],
                        'details': json.dumps({'path': r[1]}) if r[1] else None,
                    })
            except Exception:
                pass

            # plans table (may indicate plan-level outcomes)
            try:
                cur.execute("SELECT plan_name, status, error_message FROM plans WHERE mrn=?", (mrn,))
                for r in cur.fetchall():
                    legacy.append({
                        'ts': None,
                        'stage': 'retrieve',
                        'event_type': r[1],
                        'details': json.dumps({'plan_name': r[0], 'error': r[2]})
                    })
            except Exception:
                pass

            # uploads table (where they were sent)
            try:
                cur.execute("SELECT path, was_sent_to_remote, remote_ip, remote_AE_title, was_uploaded_to_proknow, proknow_collection FROM uploads WHERE mrn=?", (mrn,))
                for r in cur.fetchall():
                    legacy.append({
                        'ts': None,
                        'stage': 'export',
                        'event_type': 'upload',
                        'details': json.dumps({
                            'path': r[0],
                            'was_sent_to_remote': bool(r[1]),
                            'remote_ip': r[2],
                            'remote_AE_title': r[3],
                            'was_uploaded_to_proknow': bool(r[4]),
                            'proknow_collection': r[5]
                        })
                    })
            except Exception:
                pass

            rows = legacy

        conn.close()
        return {"mrn": mrn, "events": rows}
    except Exception as e:
        logger.exception("Failed to fetch patient timeline (all jobs): %s", e)
        raise HTTPException(status_code=500, detail=str(e))
