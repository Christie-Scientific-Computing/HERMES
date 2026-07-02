"""
Endpoints for the export page
"""
import os
import csv as csv_mod
import json
import logging
import time
import asyncio
import numpy as np
import requests as http_requests
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form
from pydantic import BaseModel
from proknow import ProKnow
from dotenv import load_dotenv
from pyorthanc import Orthanc, find_series
from fastapi.responses import StreamingResponse
from backend.src.export.logic import Exporter
import threading
from backend.src.status.db_client import StatusDB
from backend.src.pacs import client as pacs_client

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

async def proknow_upload_stream(job_id: str, path_to_csv: str, collection: str=None, **kwargs):
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



@router.post("/dicom_move_file")
async def dicom_move_file(
    file: UploadFile = File(..., description="CSV with a patient_id column"),
    job_id: str = Form(...),
    destination: str = Form(..., description="Orthanc modality AE title"),
):
    """Accept a CSV file upload and stream DICOM C-MOVE progress via SSE. Used by the gateway frontend."""
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    return StreamingResponse(
        export_event_stream(job_id=job_id, path_to_csv=str(tmp_path), destination=destination),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/proknow_upload_file")
async def proknow_upload_file(
    file: UploadFile = File(..., description="CSV with a patient_id column"),
    job_id: str = Form(...),
    collection: str = Form(..., description="ProKnow collection name"),
):
    """Accept a CSV file upload and stream ProKnow upload progress via SSE. Used by the gateway frontend."""
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    return StreamingResponse(
        proknow_upload_stream(job_id=job_id, path_to_csv=str(tmp_path), collection=collection),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _c_move_by_uid(ae_title: str, study_uid: str, series_uid: str | None = None):
    """Trigger an Orthanc C-MOVE for a specific study or series by DICOM UID."""
    level    = "Series" if series_uid else "Study"
    resource = {"StudyInstanceUID": study_uid}
    if series_uid:
        resource["SeriesInstanceUID"] = series_uid
    resp = http_requests.post(
        f"{ORTHANC_URL}/modalities/{ae_title}/move",
        auth=(ORTHANC_USER, ORTHANC_PASS),
        verify=False,
        json={"Level": level, "Resources": [resource]},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


async def export_by_uid_stream(
    job_id: str,
    path_to_csv: str,
    destination: str,
    level: str = "study",
    skip_if_on_pacs: bool = False,
):
    """
    SSE generator for UID-based C-MOVE.
    Reads a CSV with study_instance_uid / series_instance_uid columns,
    deduplicates, and moves each unique study or series.
    When skip_if_on_pacs is True, items already present on the remote PACS are skipped.
    """
    with cancel_lock:
        cancel_flags[job_id] = False

    # Register PACS modality once upfront if we'll need it
    pacs_check = skip_if_on_pacs and pacs_client.is_configured()
    if pacs_check:
        try:
            pacs_client.ensure_registered()
        except Exception as exc:
            logger.warning("Could not register PACS modality — skip-if-on-PACS disabled: %s", exc)
            pacs_check = False

    seen: set = set()
    items: list[dict] = []
    with open(path_to_csv, newline="") as f:
        for row in csv_mod.DictReader(f):
            study_uid  = (row.get("study_instance_uid")  or "").strip()
            series_uid = (row.get("series_instance_uid") or "").strip()
            if not study_uid:
                continue
            use_series = level == "series" and bool(series_uid)
            key   = (study_uid, series_uid) if use_series else study_uid
            label = series_uid if use_series else study_uid
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "study_uid":  study_uid,
                "series_uid": series_uid if use_series else None,
                "label":      label,
                "patient_id": (row.get("patient_id") or study_uid).strip(),
            })

    total = len(items)

    if status_db:
        try:
            status_db.create_job(job_id, description=f"UID C-MOVE ({level}) to {destination}")
        except Exception as e:
            logger.warning("Could not create job: %s", e)

    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

    for item in items:
        with cancel_lock:
            if cancel_flags.get(job_id):
                yield f"data: {json.dumps({'type': 'cancelled'})}\n\n"
                break

        yield f"data: {json.dumps({'type': 'progress', 'current': item['label']})}\n\n"

        # PACS pre-check: skip if the series/study already exists on the remote PACS
        if pacs_check:
            try:
                if item["series_uid"]:
                    on_pacs = await asyncio.to_thread(pacs_client.series_on_pacs, item["series_uid"])
                else:
                    on_pacs = await asyncio.to_thread(pacs_client.study_on_pacs, item["study_uid"])
                if on_pacs:
                    logger.info("Skipping %s — already on PACS", item["label"])
                    yield f"data: {json.dumps({'type': 'skipped', 'mrn': item['label'], 'reason': 'already on PACS'})}\n\n"
                    continue
            except Exception as exc:
                logger.warning("PACS check failed for %s, proceeding with export: %s", item["label"], exc)

        start = time.time()
        try:
            await asyncio.to_thread(_c_move_by_uid, destination, item["study_uid"], item["series_uid"])

            if status_db:
                try:
                    status_db.add_patient(job_id, item["patient_id"], input_path=path_to_csv)
                    status_db.add_event(job_id, item["patient_id"], stage="export", event_type="success")
                except Exception as e:
                    logger.warning("Status DB write failed: %s", e)

            yield f"data: {json.dumps({'type': 'success', 'mrn': item['label'], 'execution_time': np.round(time.time() - start, 2)})}\n\n"

        except Exception as e:
            logger.error("C-MOVE failed for %s: %s", item["label"], e)

            if status_db:
                try:
                    status_db.add_event(job_id, item["patient_id"], stage="export", event_type="failure", error_message=str(e))
                except Exception as ex:
                    logger.warning("Status DB write failed: %s", ex)

            yield f"data: {json.dumps({'type': 'error', 'mrn': item['label'], 'error': str(e), 'execution_time': np.round(time.time() - start, 2)})}\n\n"

    with cancel_lock:
        if job_id in cancel_flags:
            del cancel_flags[job_id]
    yield f"data: {json.dumps({'done': True})}\n\n"


@router.post("/dicom_move_uids_file")
async def dicom_move_uids_file(
    file: UploadFile = File(..., description="CSV with study_instance_uid and optionally series_instance_uid columns"),
    job_id: str = Form(...),
    destination: str = Form(..., description="Orthanc modality AE title"),
    level: str = Form("study", description="'study' to move whole study, 'series' to move individual series"),
    skip_if_on_pacs: str = Form("false", description="Pass 'true' to skip series/studies already present on the remote PACS"),
):
    """
    C-MOVE specific studies or series identified by DICOM UIDs.
    Accepts the CSV produced by the gateway Studies page.
    Deduplicates rows before moving.
    Set skip_if_on_pacs=true to skip any item that already exists on the configured remote PACS.
    """
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    return StreamingResponse(
        export_by_uid_stream(
            job_id=job_id,
            path_to_csv=str(tmp_path),
            destination=destination,
            level=level,
            skip_if_on_pacs=skip_if_on_pacs.lower() == "true",
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/cancel/{job_id}")
async def cancel_export(job_id: str):
    with cancel_lock:
        cancel_flags[job_id] = True
    logger.info(f"Cancelling: {job_id}")
    return {"cancelled": True}

