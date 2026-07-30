"""
Endpoints for the export page
"""
import os
import csv as csv_mod
import json
import logging
import time
import numpy as np
import requests as http_requests
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from proknow import ProKnow
from dotenv import load_dotenv
from pyorthanc import Orthanc
from fastapi.responses import StreamingResponse
from backend.src.export.logic import Exporter
from backend.src.status.db_client import StatusDB
from backend.src.common.sse import BatchItem, run_batch_job, build_patient_id_batch
from backend.src.identity import anon

load_dotenv()

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/export', tags=["export"])

# StatusDB init — connects via the shared pool in backend/src/db.py (DATABASE_URL)
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


def _dicom_move_worker(destination: str):
    def worker(item: BatchItem) -> dict:
        res = Exporter(destination=destination).dicom_c_move(item.real_id)
        return Response(mrn=item.real_id, **res).model_dump(exclude={"mrn"})
    return worker


def _proknow_worker(collection: str):
    def worker(item: BatchItem) -> dict:
        res = Exporter(destination=collection).upload_to_proknow(item.real_id)
        return Response(mrn=item.real_id, **res).model_dump(exclude={"mrn"})
    return worker


def _build_export_items(path_to_csv: str) -> list[BatchItem]:
    rows = Exporter.read_input_file(path_to_csv)
    try:
        return build_patient_id_batch(rows, input_path=path_to_csv)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/dicom_move")
async def dicom_move(body: Request):
    req = body.model_dump()
    logger.info(req)
    items = _build_export_items(req['path_to_csv'])
    return StreamingResponse(
        run_batch_job(
            req['job_id'], items, stage='export',
            worker=_dicom_move_worker(req['destination']),
            status_db=status_db,
            description=f"Batch export to {req['destination']}",
        ),
        media_type="text/event-stream",
        headers = {
            "Cache-Control": "no-cache",
        }
    )


@router.post("/proknow_upload")
async def proknow_upload(body: Request):
    req = body.model_dump()
    logger.info(req)
    items = _build_export_items(req['path_to_csv'])
    return StreamingResponse(
        run_batch_job(
            req['job_id'], items, stage='export',
            worker=_proknow_worker(req['collection']),
            status_db=status_db,
            description=f"Batch ProKnow upload to {req['collection']}",
        ),
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
    try:
        real_mrn = anon.resolve_real_id(req['mrn'])
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    display_mrn = anon.to_display_id(real_mrn)

    # Record patient and start
    if status_db:
        try:
            status_db.create_job(job_id, description=f"Single ProKnow upload to {req.get('collection')}")
            status_db.add_patient(job_id, real_mrn, input_path=None)
            status_db.add_event(job_id, real_mrn, stage='export', event_type='start')
        except Exception as e:
            logger.warning("Status DB write failed: %s", e)

    exp = Exporter(destination=req['collection'])
    start = time.time()
    try:
        res = exp.upload_to_proknow(real_mrn)
        response = Response(mrn=display_mrn, **res)

        # Record success
        if status_db:
            try:
                status_db.add_event(job_id, real_mrn, stage='export', event_type='success', details=res)
            except Exception as e:
                logger.warning("Status DB write failed: %s", e)

        return f"data: {json.dumps({
                    'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

    except Exception as e:
        # Record failure
        if status_db:
            try:
                status_db.add_event(job_id, real_mrn, stage='export', event_type='failure', error_message=str(e))
            except Exception as ex:
                logger.warning("Status DB write failed: %s", ex)

        return f"data: {json.dumps({'type': 'error',
                'execution_time': np.round(time.time() - start, 2),
                'mrn': display_mrn,
                'error': str(e)})}\n\n"



@router.post("/dicom_move_file")
async def dicom_move_file(
    file: UploadFile = File(..., description="CSV with a patient_id column"),
    job_id: str = Form(...),
    destination: str = Form(..., description="Orthanc modality AE title"),
):
    """Accept a CSV file upload and stream DICOM C-MOVE progress via SSE. Used by the proxy/frontend."""
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    items = _build_export_items(str(tmp_path))
    return StreamingResponse(
        run_batch_job(
            job_id, items, stage='export',
            worker=_dicom_move_worker(destination),
            status_db=status_db,
            description=f"Batch export to {destination}",
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/proknow_upload_file")
async def proknow_upload_file(
    file: UploadFile = File(..., description="CSV with a patient_id column"),
    job_id: str = Form(...),
    collection: str = Form(..., description="ProKnow collection name"),
):
    """Accept a CSV file upload and stream ProKnow upload progress via SSE. Used by the proxy/frontend."""
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    items = _build_export_items(str(tmp_path))
    return StreamingResponse(
        run_batch_job(
            job_id, items, stage='export',
            worker=_proknow_worker(collection),
            status_db=status_db,
            description=f"Batch ProKnow upload to {collection}",
        ),
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


def _build_uid_items(path_to_csv: str, level: str) -> list[BatchItem]:
    """
    Read a study/series-UID CSV. Study/series UIDs are DICOM identifiers,
    not part of the patient anon-mapping scheme, so they pass straight
    through as both the real id and the display id (there is nothing to
    anonymise them against here). The optional `patient_id` column is pure
    bookkeeping metadata for StatusDB -- best-effort resolved through the
    anon boundary, falling back to the value as submitted if it doesn't
    resolve (e.g. because no patient_id column was supplied and it defaulted
    to the study UID, which was never a patient id to begin with).
    """
    seen: set = set()
    items: list[BatchItem] = []
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

            submitted_patient_id = (row.get("patient_id") or "").strip()
            if submitted_patient_id:
                try:
                    status_mrn = anon.resolve_real_id(submitted_patient_id)
                except anon.AnonLookupError:
                    logger.warning(
                        "patient_id %r in UID CSV has no anon mapping; using as-is for StatusDB bookkeeping only",
                        submitted_patient_id,
                    )
                    status_mrn = submitted_patient_id
            else:
                status_mrn = study_uid

            items.append(BatchItem(
                real_id=study_uid,       # not used directly; worker reads extra["study_uid"]/["series_uid"]
                display_id=label,
                status_mrn=status_mrn,
                input_path=path_to_csv,
                extra={"study_uid": study_uid, "series_uid": series_uid if use_series else None},
            ))

    return items


def _uid_move_worker(destination: str):
    def worker(item: BatchItem) -> dict:
        _c_move_by_uid(destination, item.extra["study_uid"], item.extra["series_uid"])
        return {}
    return worker


@router.post("/dicom_move_uids_file")
async def dicom_move_uids_file(
    file: UploadFile = File(..., description="CSV with study_instance_uid and optionally series_instance_uid columns"),
    job_id: str = Form(...),
    destination: str = Form(..., description="Orthanc modality AE title"),
    level: str = Form("study", description="'study' to move whole study, 'series' to move individual series"),
):
    """
    C-MOVE specific studies or series identified by DICOM UIDs.
    Accepts the CSV produced by the Studies page (already PACS-filtered if requested).
    Deduplicates rows before moving.
    """
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    items = _build_uid_items(str(tmp_path), level)

    return StreamingResponse(
        run_batch_job(
            job_id, items, stage='export',
            worker=_uid_move_worker(destination),
            status_db=status_db,
            description=f"UID C-MOVE ({level}) to {destination}",
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/cancel/{job_id}")
async def cancel_export(job_id: str):
    status_db.cancel_job(job_id)
    logger.info(f"Cancelling: {job_id}")
    return {"cancelled": True}
