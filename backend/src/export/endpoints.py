"""
Endpoints for the export page
"""
import os
import json
import logging
import time
import asyncio
import numpy as np
from fastapi import APIRouter
from pydantic import BaseModel
from proknow import ProKnow
from dotenv import load_dotenv
from pyorthanc import Orthanc, find_series
from fastapi.responses import StreamingResponse
from backend.src.export.logic import Exporter
import threading
from backend.src.status.db_client import StatusDB

load_dotenv()

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/export', tags=["export"])

# Cancellation flags
cancel_lock = threading.Lock()
cancel_flags: dict[str, bool] = {} ## Holds cancellation status for every job

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


ORTHANC_URL = os.getenv('ORTHANC_URL')
ORTHANC_USER = os.getenv('ORTHANC_USER')
ORTHANC_PASS = os.getenv('ORTHANC_PASS')

PROKNOW_URL = 'https://nhs.proknow.com' #Hard-coded on purpose
PROKNOW_WORKSPACE = 'RBV - Christie'

class Request(BaseModel):
    job_id: str
    mrn: str | None = None
    path_to_csv: str | None = None
    destination: str | None = None # DICOM AE
    collection: str | None = None # ProKnow collection

class Response(BaseModel):
    mrn: str | int
    status: str | None = None

@router.get("/get_orthanc_modalities")
async def get_orthanc_modalities():
    try:
        client = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER,
                password=ORTHANC_PASS, verify=False,
                timeout=14000.0,)
        logger.debug("Connected to Orthanc")
    except Exception as exc:
        logger.error(f"Failed to connect to Orthanc: {exc}")
        raise

    res = client.get_modalities()

    return res

@router.get("/get_proknow_collections")
async def get_proknow_collections():
    try:
        pk = ProKnow(PROKNOW_URL, credentials_file='credentials.json')
        logger.debug("Connected to Proknow")
    except Exception as exc:
        logger.error(f"Failed to connect to ProKnow: {exc}")
        raise
    
    return [x.name for x in pk.collections.query(workspace=PROKNOW_WORKSPACE)]


async def export_event_stream(job_id: str, path_to_csv: str, destination: str, **kwargs):
    """ 
    Generator that yields SSE-formatted events, one per patient.
    """
    with cancel_lock:
        cancel_flags[job_id] = False

    rows = Exporter.read_input_file(path_to_csv)
    total = len(rows)
    logger.info(f"Exporting {total} rows")

    # Create job
    if status_db:
        try:
            status_db.create_job(job_id, description=f"Batch export to {destination}")
        except Exception as e:
            logger.warning("Could not create export job in status DB: %s", e)

    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

    for row in rows:
        with cancel_lock:
            if cancel_flags.get(job_id):
                logger.info("Client cancelled request, aborting")
                yield f"data: {json.dumps({'type': 'cancelled'})}\n\n"
                break

        patient_id = row['patient_id']

        # Record patient & start event
        if status_db:
            try:
                status_db.add_patient(job_id, str(patient_id), input_path=path_to_csv)
                status_db.add_event(job_id, str(patient_id), stage='export', event_type='start')
            except Exception as e:
                logger.warning("Status DB write failed: %s", e)

        # Starting patient
        yield f"data: {json.dumps({'type': 'progress', 'current': patient_id})}\n\n"
        start = time.time()
        try:
            res = await asyncio.to_thread(
                Exporter(destination=destination).dicom_c_move, patient_id
            )
            response = Response(mrn=patient_id, **res)

            # Record success
            if status_db:
                try:
                    status_db.add_event(job_id, str(patient_id), stage='export', event_type='success', details=res)
                except Exception as e:
                    logger.warning("Status DB write failed: %s", e)

            yield f"data: {json.dumps({
                'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

        except Exception as e:
            logger.error("Failed to export patient %s: %s", patient_id, e)

            # Record failure
            if status_db:
                try:
                    status_db.add_event(job_id, str(patient_id), stage='export', event_type='failure', error_message=str(e))
                except Exception as ex:
                    logger.warning("Status DB write failed: %s", ex)

            yield f"data: {json.dumps({'type': 'error',
                'execution_time': np.round(time.time() - start, 2),
                'mrn': patient_id,
                'error': str(e)})}\n\n"
    
    with cancel_lock:
        if job_id in cancel_flags:
            del cancel_flags[job_id]
    yield f"data: {json.dumps({'done': True})}\n\n"
    


@router.post("/dicom_move")
async def dicom_move(body: Request):
    req = body.model_dump()
    logger.info(req)
    return StreamingResponse(
        export_event_stream(**req),
        media_type="text/event-stream",
        headers = {
            "Cache-Control": "no-cache",
        }
    ) 

async def proknow_upload_stream(job_id: str, path_to_csv: str, collection: str, **kwargs):
    """
    Generator for uploading to ProKnow that records status events.
    """
    with cancel_lock:
        cancel_flags[job_id] = False

    rows = Exporter.read_input_file(path_to_csv)
    total = len(rows)
    logger.info(f"Exporting {total} rows")

    # Create job
    if status_db:
        try:
            status_db.create_job(job_id, description=f"Batch ProKnow upload to {collection}")
        except Exception as e:
            logger.warning("Could not create export job in status DB: %s", e)

    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

    for row in rows:
        with cancel_lock:
            if cancel_flags.get(job_id):
                logger.info("Client cancelled request, aborting")
                yield f"data: {json.dumps({'type': 'cancelled'})}\n\n"
                break

        patient_id = row['patient_id']

        # Record patient & start event
        if status_db:
            try:
                status_db.add_patient(job_id, str(patient_id), input_path=path_to_csv)
                status_db.add_event(job_id, str(patient_id), stage='export', event_type='start')
            except Exception as e:
                logger.warning("Status DB write failed: %s", e)

        # Starting patient
        yield f"data: {json.dumps({'type': 'progress', 'current': patient_id})}\n\n"
        start = time.time()
        try:
            res = await asyncio.to_thread(
                Exporter(destination=collection).upload_to_proknow, patient_id
            )
            response = Response(mrn=patient_id, **res)

            # Record success
            if status_db:
                try:
                    status_db.add_event(job_id, str(patient_id), stage='export', event_type='success', details=res)
                except Exception as e:
                    logger.warning("Status DB write failed: %s", e)

            yield f"data: {json.dumps({
                'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

        except Exception as e:
            logger.error("Failed to export patient %s: %s", patient_id, e)

            # Record failure
            if status_db:
                try:
                    status_db.add_event(job_id, str(patient_id), stage='export', event_type='failure', error_message=str(e))
                except Exception as ex:
                    logger.warning("Status DB write failed: %s", ex)

            yield f"data: {json.dumps({'type': 'error',
                'execution_time': np.round(time.time() - start, 2),
                'mrn': patient_id,
                'error': str(e)})}\n\n"
    
    with cancel_lock:
        if job_id in cancel_flags:
            del cancel_flags[job_id]
    yield f"data: {json.dumps({'done': True})}\n\n"
    
    
    
@router.post("/proknow_upload")
async def proknow_upload(body: Request):
    req = body.model_dump()
    logger.info(req)
    
    return StreamingResponse(
        proknow_upload_stream(**req),
        media_type="text/event-stream",
        headers = {
            "Cache-Control": "no-cache",
        }
    ) 


@router.post("/proknow_upload_patient")
async def proknow_upload_patient(body: Request):
    req = body.model_dump()
    logger.info(req)
    job_id = req.get('job_id', 'manual')
    mrn = req['mrn']

    # Record patient and start
    if status_db:
        try:
            status_db.create_job(job_id, description=f"Single ProKnow upload to {req.get('collection')}")
            status_db.add_patient(job_id, str(mrn), input_path=None)
            status_db.add_event(job_id, str(mrn), stage='export', event_type='start')
        except Exception as e:
            logger.warning("Status DB write failed: %s", e)

    exp = Exporter(destination=req['collection'])
    start = time.time()
    try: 
        res = exp.upload_to_proknow(req['mrn'])
        response = Response(mrn=req['mrn'], **res)

        # Record success
        if status_db:
            try:
                status_db.add_event(job_id, str(mrn), stage='export', event_type='success', details=res)
            except Exception as e:
                logger.warning("Status DB write failed: %s", e)

        return f"data: {json.dumps({
                    'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

    except Exception as e:
        # Record failure
        if status_db:
            try:
                status_db.add_event(job_id, str(mrn), stage='export', event_type='failure', error_message=str(e))
            except Exception as ex:
                logger.warning("Status DB write failed: %s", ex)

        return f"data: {json.dumps({'type': 'error',
                'execution_time': np.round(time.time() - start, 2),
                'mrn': req['mrn'],
                'error': str(e)})}\n\n"



@router.post("/cancel/{job_id}")
async def cancel_export(job_id: str):
    with cancel_lock:
        cancel_flags[job_id] = True
    logger.info(f"Cancelling: {job_id}")
    return {"cancelled": True}

