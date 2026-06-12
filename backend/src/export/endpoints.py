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

load_dotenv()

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/export', tags=["export"])
import threading
cancel_lock = threading.Lock()
cancel_flags: dict[str, bool] = {} ## Holds cancellation status for every job

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
    mrn: str 
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
        with cancel_lock:
        cancel_flags[job_id] = False
    rows = Exporter.read_input_file(path_to_csv)
    total = len(rows)
    logger.info(f"Exporting {total} rows")

    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

    for row in rows:
        with cancel_lock:
            if cancel_flags.get(job_id):
                logger.info("Client cancelled request, aborting")
                yield f"data: {json.dumps({'type': 'cancelled'})}\n\n"
                break
        patient_id = row['patient_id']

        # Starting patient
        yield f"data: {json.dumps({'type': 'progress', 'current': patient_id})}\n\n"
        start = time.time()
        try:
            
            res = await asyncio.to_thread(
                Exporter(destination=destination).dicom_c_move, patient_id
            )
            response = Response(mrn=patient_id, **res)
            
            yield f"data: {json.dumps({
                'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

        except Exception as e:
            logger.error("Failed to export patient %s: %s", patient_id, e)
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
    with cancel_lock:
        cancel_flags[job_id] = False
    rows = Exporter.read_input_file(path_to_csv)
    total = len(rows)
    logger.info(f"Exporting {total} rows")

    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

    for row in rows:
        with cancel_lock:
            if cancel_flags.get(job_id):
                logger.info("Client cancelled request, aborting")
                yield f"data: {json.dumps({'type': 'cancelled'})}\n\n"
                break

        patient_id = row['patient_id']

        # Starting patient
        yield f"data: {json.dumps({'type': 'progress', 'current': patient_id})}\n\n"
        start = time.time()
        try:
            
            res = await asyncio.to_thread(
                Exporter(destination=collection).upload_to_proknow, patient_id
            )
            response = Response(mrn=patient_id, **res)
            
            yield f"data: {json.dumps({
                'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

        except Exception as e:
            logger.error("Failed to export patient %s: %s", patient_id, e)
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
    exp = Exporter(destination=req['collection'])
    start = time.time()
    try: 
        res = exp.upload_to_proknow(req['mrn'])
        response = Response(mrn=req['mrn'], **res)

        return f"data: {json.dumps({
                    'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

    except Exception as e:
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

