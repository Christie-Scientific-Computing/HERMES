"""
Endpoints for the import page
"""
import os
import json
import time
import asyncio
import logging
import numpy as np
from pydantic import BaseModel
from pathlib import Path
from backend.src.retrieve.logic import Importer
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from backend.src.status.db_client import StatusDB
import threading

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/import', tags=["import"])

# StatusDB init
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

# Cancellation flags and lock
cancel_lock = threading.Lock()
cancel_flags: dict[str, bool] = {} ## Holds cancellation status for every job


class Request(BaseModel):
    job_id: str
    mrn: str | None = None
    path_to_csv: str | None = None
    import_level: str | None = None

class Response(BaseModel):
    mrn: str | int
    status: str | None = None
    in_mosaiq: bool | None = None
    in_pinnacle: bool | None = None
    in_proknow: bool | None = None

async def import_event_stream(job_id: str, path_to_csv: str, import_level: str):
    """
    Generator that yields SSE-formatted events, one per patient.
    """
    with cancel_lock:
        cancel_flags[job_id] = False

    rows = Importer.read_input_file(path_to_csv)
    total = len(rows)

    # Create job record if available
    if status_db:
        try:
            status_db.create_job(job_id, description=f"Batch import from {path_to_csv}")
        except Exception as e:
            logger.warning("Could not create job in status DB: %s", e)

    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

    for row in rows:
        with cancel_lock:
            if cancel_flags.get(job_id):
                logger.info("Client cancelled request, aborting")
                yield f"data: {json.dumps({'type': 'cancelled'})}\n\n"
                break

        patient_id = row['patient_id']

        # Record patient & starting event
        if status_db:
            try:
                status_db.add_patient(job_id, str(patient_id), input_path=path_to_csv)
                status_db.add_event(job_id, str(patient_id), stage='retrieve', event_type='start')
            except Exception as e:
                logger.warning("Status DB write failed: %s", e)

        # Starting patient
        yield f"data: {json.dumps({'type': 'progress', 'current': patient_id})}\n\n"
        start = time.time()
        try:
            res = await asyncio.to_thread(
                Importer(import_level).handle_patient, patient_id
            )
            response = Response(mrn=patient_id, **res)
            
            # Record success
            if status_db:
                try:
                    status_db.add_event(job_id, str(patient_id), stage='retrieve', event_type='success', details=res)
                except Exception as e:
                    logger.warning("Status DB write failed: %s", e)

            yield f"data: {json.dumps({'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

        except Exception as e:
            logger.error("Failed to import patient %s: %s", patient_id, e)

            # Record failure
            if status_db:
                try:
                    status_db.add_event(job_id, str(patient_id), stage='retrieve', event_type='failure', error_message=str(e))
                except Exception as ex:
                    logger.warning("Status DB write failed: %s", ex)

            yield f"data: {json.dumps({'type': 'error', 'execution_time': np.round(time.time() - start, 2), 'mrn': patient_id, 'error': str(e)})}\n\n"
    
    with cancel_lock:
        if job_id in cancel_flags:
            del cancel_flags[job_id]
    yield f"data: {json.dumps({'done': True})}\n\n"
    


### Import page
@router.post("/batch_import")
async def batch_import(body: Request):
    """
    Main method
    """
    req = body.model_dump()
    logger.info(f"Received: {req}")

    return StreamingResponse(
        import_event_stream(req['job_id'], req['path_to_csv'], req['import_level']),
        media_type="text/event-stream",
        headers = {
            "Cache-Control": "no-cache",
        }
    )

    # responses = []
    # for row in Importer.read_input_file(req['path_to_csv']):
    #     res = Importer(req['import_level']).handle_patient(row['patient_id'])
    #     responses.append(Response(mrn=row['patient_id'], **res))
    # return responses


@router.post('/single_import')
async def single_import(body: Request):
    req = body.model_dump()
    logger.info("Importing %s", req['mrn'])
    imp = Importer(req['import_level'])
    start = time.time()
    try:
        res = imp.handle_patient(req['mrn'])
        response = Response(mrn=req['mrn'], **res)
        return f"data: {json.dumps({'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}"

    except Exception as e:
        return f"data: {json.dumps({'type': 'error', 'execution_time': np.round(time.time() - start, 2), 'mrn': req['mrn'], 'error': str(e)})}\n\n"


@router.get('/find_patient')
async def find_patient(body: Request) -> Response:
    req = body.model_dump()
    logger.info("Searching for %s", req['mrn'])
    imp = Importer(req['import_level'])
    start = time.time()
    try:
        res = imp.find_patient(req['mrn'])
        response = Response(mrn=req['mrn'], **res)
        return f"data: {json.dumps({'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}"

    except Exception as e:
        return f"data: {json.dumps({'type': 'error', 'execution_time': np.round(time.time() - start, 2), 'mrn': req['mrn'], 'error': str(e)})}\n\n"
    


@router.post("/batch_import_file")
async def batch_import_file(
    file: UploadFile = File(..., description="CSV with a patient_id column"),
    job_id: str = Form(...),
    import_level: str = Form("Planning data"),
):
    """Accept a CSV file upload and stream import progress via SSE. Used by the gateway frontend."""
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    return StreamingResponse(
        import_event_stream(job_id, str(tmp_path), import_level),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/cancel/{job_id}")
async def cancel_import(job_id: str):
    with cancel_lock:
        cancel_flags[job_id] = True
    logger.info(f"Cancelling: {job_id}")
    return {"cancelled": True}
