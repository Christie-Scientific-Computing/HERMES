"""
Endpoints for the export page
"""
import os
import csv as csv_mod
import logging
import time
import numpy as np
import requests as http_requests
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, Form, Query, HTTPException
from pydantic import BaseModel, Field
from proknow import ProKnow
from dotenv import load_dotenv
from pyorthanc import Orthanc, find_series, find_studies
from fastapi.responses import StreamingResponse
from backend.src.export.logic import Exporter, checksummed_series_manifest
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB
from backend.src.common.sse import BatchItem, run_batch_job, build_patient_id_batch
from backend.src.common import pii_patterns
from backend.src.identity import anon
from backend.src.projects import enforcement
from backend.src.projects.enforcement import verify_internal_key

load_dotenv()

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/export', tags=["export"], dependencies=[Depends(verify_internal_key)])

# StatusDB init — connects via the shared pool in backend/src/db.py (DATABASE_URL)
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    try:
        status_db = StatusDB()
        tasks_db = TasksDB()
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
    project_id: str
    username: str
    mrn: str | None = None
    path_to_csv: str | None = None
    destination: str | None = None # DICOM AE
    collection: str | None = None # ProKnow collection
    # DICOM C-MOVE only (dicom_move) -- see Exporter.dicom_c_move's
    # docstring. Sent to Orthanc as MoveOriginatorID so a receiving
    # anonymising node on the DMZ can pick the right pseudonymisation table
    # (e.g. a clinical-trial patient) from the incoming DIMSE message.
    message_id: int | None = Field(default=None, ge=0, le=65535)

# Note: _dicom_move_worker/_proknow_worker/_uid_move_worker (below) dump this
# with exclude_none=True, since their output feeds events.details/the SSE
# payload for every batch endpoint -- exclude_none keeps those payloads from
# filling up with nulls on any field a given worker doesn't populate (e.g.
# checksums is always set by all three workers as of SS D2, but a future
# field might not be). proknow_upload_patient, which doesn't route through
# run_batch_job, doesn't do this -- not a deliberate inconsistency, just that
# path has no live caller today (per D0 review) so it wasn't worth
# reconciling yet. Revisit if/when it grows one.
class Response(BaseModel):
    mrn: str | int
    status: str | None = None
    series_count: int | None = None
    instance_count: int | None = None
    study_uids: list[str] | None = None
    series_uids: list[str] | None = None
    # Hex digest per instance, keyed by SOPInstanceUID (or "orthanc:<id>" if
    # that tag isn't indexed) -- but the ALGORITHM varies by destination_type
    # and is not comparable across events/jobs: "dicom_modality" (both the
    # plain and UID-based C-MOVE paths) reports Orthanc's own stored MD5,
    # since the bytes are never downloaded to this process for an async
    # C-MOVE; "proknow_collection" reports a locally-computed SHA-256 over
    # bytes already pulled to disk for upload. Same instance exported via
    # both paths will therefore never produce matching checksums by
    # construction -- that's expected, not a bug.
    checksums: dict[str, str] | None = None
    destination: str | None = None
    destination_type: str | None = None
    submitted_by: str | None = None

@router.get("/get_orthanc_modalities")
async def get_orthanc_modalities(username: str = Query(...)):
    enforcement.require_any_active_project(username)
    try:
        client = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER,
                password=ORTHANC_PASS, verify=False,
                timeout=14000.0,)
        logger.debug("Connected to Orthanc")
        return client.get_modalities()
    except Exception as exc:
        logger.exception("Failed to fetch Orthanc modalities")
        raise HTTPException(status_code=502, detail=f"Orthanc query failed: {exc}")

@router.get("/get_proknow_collections")
async def get_proknow_collections(username: str = Query(...)):
    enforcement.require_any_active_project(username)
    try:
        pk = ProKnow(PROKNOW_URL, credentials_file='credentials.json')
        logger.debug("Connected to Proknow")
        return [x.name for x in pk.collections.query(workspace=PROKNOW_WORKSPACE)]
    except Exception as exc:
        logger.exception("Failed to fetch ProKnow collections")
        raise HTTPException(status_code=502, detail=f"ProKnow query failed: {exc}")


def _dicom_move_worker(destination: str, submitted_by: str | None = None, message_id: int | None = None):
    def worker(item: BatchItem) -> dict:
        res = Exporter(destination=destination).dicom_c_move(item.real_id, message_id=message_id)
        return Response(
            mrn=item.real_id,
            destination=destination,
            destination_type="dicom_modality",
            submitted_by=submitted_by,
            **res,
        ).model_dump(exclude={"mrn"}, exclude_none=True)
    return worker


def _proknow_worker(collection: str, submitted_by: str | None = None):
    def worker(item: BatchItem) -> dict:
        res = Exporter(destination=collection).upload_to_proknow(item.real_id)
        return Response(
            mrn=item.real_id,
            destination=collection,
            destination_type="proknow_collection",
            submitted_by=submitted_by,
            **res,
        ).model_dump(exclude={"mrn"}, exclude_none=True)
    return worker


def _build_export_items(path_to_csv: str) -> list[BatchItem]:
    try:
        rows = Exporter.read_input_file(path_to_csv)
    except Exception as e:
        # str(e) can embed the server's ./tmp/{job_id}_{filename} path
        # (including a user-supplied filename) -- no specific real id is in
        # scope yet at this point, so this is the generic pattern floor
        # only, not a real-id-aware substitution.
        raise HTTPException(status_code=400, detail=f"Could not read CSV: {pii_patterns.redact(str(e))}")
    try:
        return build_patient_id_batch(rows, input_path=path_to_csv)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))


def _enqueue_export_job(job_id: str, items: list[BatchItem], kind: str, description: str,
                         project_id: str, username: str, params: dict) -> dict:
    """
    Shared by all three file-upload export endpoints below (dicom_move_file,
    proknow_upload_file, dicom_move_uids_file): create the job, register
    every submitted patient at enqueue time (not deferred to the worker --
    see batch_import_file's identical comment in retrieve/endpoints.py for
    why), and enqueue onto the tasks table for backend/worker.py.
    `project_id`/`username` are merged into `params` so a claim needs no
    join back to `jobs` for the worker's claim-time ethics re-check.

    create_job/add_patient/enqueue are three separate, non-transactional
    writes (StatusDB and TasksDB each borrow their own connection from the
    pool per call) -- if enqueue fails after the job/patients were already
    created (a transient DB blip mid-request), the job would otherwise be
    left dangling: visible in job_summary, with patients registered, but no
    tasks and no way to cancel/complete it. Marking it cancelled on any
    failure here puts it into a well-defined terminal state instead of
    limbo -- not a rollback (patients already registered stay registered),
    but the job stops looking like it's still going to run.
    """
    status_db.create_job(job_id, description=description, created_by=username, project_id=project_id)
    try:
        for item in items:
            status_db.add_patient(job_id, item.status_mrn, input_path=item.input_path)
        tasks_db.enqueue(job_id, items, kind=kind, stage="export",
                          params={**params, "project_id": project_id, "username": username})
    except Exception:
        status_db.cancel_job(job_id)
        raise
    return {"job_id": job_id, "total": len(items)}


@router.post("/dicom_move")
async def dicom_move(body: Request):
    """
    JSON-bodied variant of dicom_move_file -- stays on the synchronous
    run_batch_job SSE path. See retrieve/endpoints.py's batch_import
    docstring for why: no live caller, and real dedicated test coverage
    (test_export_anon_boundary.py, test_export_manifest.py's integration
    test) exercising this exact synchronous path.
    """
    req = body.model_dump()
    logger.info(req)
    enforcement.require_project_member(req['project_id'], req['username'])
    items = _build_export_items(req['path_to_csv'])
    return StreamingResponse(
        run_batch_job(
            req['job_id'], items, stage='export',
            worker=_dicom_move_worker(req['destination'], submitted_by=req['username'], message_id=req.get('message_id')),
            status_db=status_db,
            description=f"Batch export to {req['destination']}",
            created_by=req['username'],
            project_id=req['project_id'],
        ),
        media_type="text/event-stream",
        headers = {
            "Cache-Control": "no-cache",
        }
    )


@router.post("/proknow_upload")
async def proknow_upload(body: Request):
    """JSON-bodied variant of proknow_upload_file -- stays on the
    synchronous run_batch_job SSE path, same reasoning as dicom_move above."""
    req = body.model_dump()
    logger.info(req)
    enforcement.require_project_member(req['project_id'], req['username'])
    items = _build_export_items(req['path_to_csv'])
    return StreamingResponse(
        run_batch_job(
            req['job_id'], items, stage='export',
            worker=_proknow_worker(req['collection'], submitted_by=req['username']),
            status_db=status_db,
            description=f"Batch ProKnow upload to {req['collection']}",
            created_by=req['username'],
            project_id=req['project_id'],
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
    enforcement.require_project_member(req['project_id'], req['username'])
    job_id = req.get('job_id', 'manual')
    try:
        real_mrn = anon.resolve_real_id(req['mrn'])
        display_mrn = anon.to_display_id(real_mrn)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Record patient and start
    if status_db:
        try:
            status_db.create_job(
                job_id, description=f"Single ProKnow upload to {req.get('collection')}",
                created_by=req['username'], project_id=req['project_id'],
            )
            status_db.add_patient(job_id, real_mrn, input_path=None)
            status_db.add_event(job_id, real_mrn, stage='export', event_type='start')
        except Exception as e:
            logger.warning("Status DB write failed: %s", e)

    exp = Exporter(destination=req['collection'])
    start = time.time()
    try:
        res = exp.upload_to_proknow(real_mrn)
        response = Response(
            mrn=display_mrn,
            destination=req['collection'],
            destination_type="proknow_collection",
            submitted_by=req['username'],
            **res,
        )

        # Record success
        if status_db:
            try:
                status_db.add_event(job_id, real_mrn, stage='export', event_type='success', details=res)
            except Exception as e:
                logger.warning("Status DB write failed: %s", e)

        # response.model_dump() keeps full fidelity in the add_event call
        # above -- only this outbound return goes through redact_dict.
        return {
            'type': 'success', 'execution_time': np.round(time.time() - start, 2),
            **pii_patterns.redact_dict(response.model_dump(), real_id=real_mrn, display_id=display_mrn),
        }

    except Exception as e:
        # Record failure
        if status_db:
            try:
                status_db.add_event(job_id, real_mrn, stage='export', event_type='failure', error_message=str(e))
            except Exception as ex:
                logger.warning("Status DB write failed: %s", ex)

        # str(e) routinely quotes the real MRN -- StatusDB above already has
        # the raw message for the audit trail.
        return {
            'type': 'error', 'execution_time': np.round(time.time() - start, 2), 'mrn': display_mrn,
            'error': pii_patterns.redact(str(e), real_id=real_mrn, display_id=display_mrn),
        }



@router.post("/dicom_move_file")
async def dicom_move_file(
    file: UploadFile = File(..., description="CSV with a patient_id column"),
    job_id: str = Form(...),
    project_id: str = Form(...),
    username: str = Form(...),
    destination: str = Form(..., description="Orthanc modality AE title"),
    message_id: int | None = Form(
        None, ge=0, le=65535,
        description="Optional DICOM Move Originator Message ID, sent to Orthanc as "
                     "MoveOriginatorID -- lets a receiving anonymising node pick the right "
                     "pseudonymisation table (e.g. for clinical-trial patients).",
    ),
):
    """Accept a CSV file upload and enqueue a DICOM C-MOVE batch job for
    backend/worker.py to execute (docs/worker-queue-design.md). Used by the frontend."""
    enforcement.require_project_member(project_id, username)
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    items = _build_export_items(str(tmp_path))
    params = {"destination": destination}
    if message_id is not None:
        params["message_id"] = message_id
    return _enqueue_export_job(
        job_id, items, kind="dicom_move", description=f"Batch export to {destination}",
        project_id=project_id, username=username, params=params,
    )


@router.post("/proknow_upload_file")
async def proknow_upload_file(
    file: UploadFile = File(..., description="CSV with a patient_id column"),
    job_id: str = Form(...),
    project_id: str = Form(...),
    username: str = Form(...),
    collection: str = Form(..., description="ProKnow collection name"),
):
    """Accept a CSV file upload and enqueue a ProKnow upload batch job for
    backend/worker.py to execute (docs/worker-queue-design.md). Used by the frontend."""
    enforcement.require_project_member(project_id, username)
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    items = _build_export_items(str(tmp_path))
    return _enqueue_export_job(
        job_id, items, kind="proknow_upload", description=f"Batch ProKnow upload to {collection}",
        project_id=project_id, username=username, params={"collection": collection},
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
    try:
        with open(path_to_csv, newline="") as f:
            rows = list(csv_mod.DictReader(f))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read CSV: {pii_patterns.redact(str(e))}")

    for row in rows:
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
            except anon.AnonServiceError as e:
                # Unlike an unknown id, a dead anon service is a real problem worth
                # failing the whole request over, not silently degrading every row.
                raise HTTPException(status_code=503, detail=str(e))
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


def _build_uid_manifest(study_uid: str, series_uid: str | None) -> dict:
    """
    This path posts CSV-supplied study/series UIDs straight to Orthanc's
    C-MOVE endpoint and, unlike Exporter.dicom_c_move, never otherwise calls
    find_studies/find_series -- so unlike the other two export workers,
    there's no already-enumerated data to extend here. This is net-new
    lookup work, done purely to build the audit manifest before the move is
    triggered (docs/safety-plan.md SS D2); it plays no role in the move
    itself. Checksums come from Orthanc's own stored MD5, same as the
    non-UID DICOM C-MOVE path -- these bytes never land locally either.
    """
    try:
        client = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER,
                password=ORTHANC_PASS, verify=False,
                timeout=14000.0,)
        logger.info("Connected to Orthanc")
    except Exception as exc:
        logger.error(f"Failed to connect to Orthanc: {exc}")
        raise ValueError("Could not connect to Orthanc")

    studies = find_studies(client=client, query={"StudyInstanceUID": study_uid})
    found_study_uids = [s.main_dicom_tags.get("StudyInstanceUID") for s in studies]
    found_study_uids = [uid for uid in found_study_uids if uid]

    series_query = {"StudyInstanceUID": study_uid}
    if series_uid:
        series_query["SeriesInstanceUID"] = series_uid
    series_list = find_series(client=client, query=series_query)

    manifest = checksummed_series_manifest(client, series_list)
    manifest["study_uids"] = found_study_uids or [study_uid]
    return manifest


def _uid_move_worker(destination: str, submitted_by: str | None = None):
    def worker(item: BatchItem) -> dict:
        study_uid = item.extra["study_uid"]
        series_uid = item.extra["series_uid"]

        manifest = _build_uid_manifest(study_uid, series_uid)
        _c_move_by_uid(destination, study_uid, series_uid)

        return Response(
            mrn=item.real_id,
            status="Success",
            destination=destination,
            destination_type="dicom_modality",
            submitted_by=submitted_by,
            **manifest,
        ).model_dump(exclude={"mrn"}, exclude_none=True)
    return worker


@router.post("/dicom_move_uids_file")
async def dicom_move_uids_file(
    file: UploadFile = File(..., description="CSV with study_instance_uid and optionally series_instance_uid columns"),
    job_id: str = Form(...),
    project_id: str = Form(...),
    username: str = Form(...),
    destination: str = Form(..., description="Orthanc modality AE title"),
    level: str = Form("study", description="'study' to move whole study, 'series' to move individual series"),
):
    """
    C-MOVE specific studies or series identified by DICOM UIDs.
    Accepts the CSV produced by the Studies page (already PACS-filtered if requested).
    Deduplicates rows before moving. Enqueues for backend/worker.py to
    execute (docs/worker-queue-design.md) -- has no frontend caller today
    (see CLAUDE.md's known-gaps notes), but is real, tested backend
    functionality, converted the same way as the other two export flows.
    """
    enforcement.require_project_member(project_id, username)
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    items = _build_uid_items(str(tmp_path), level)
    return _enqueue_export_job(
        job_id, items, kind="uid_move", description=f"UID C-MOVE ({level}) to {destination}",
        project_id=project_id, username=username, params={"destination": destination},
    )


@router.post("/cancel/{job_id}")
async def cancel_export(job_id: str):
    status_db.cancel_job(job_id)
    # Also flip any still-queued tasks straight to 'cancelled' -- see the
    # identical comment on retrieve/endpoints.py's cancel_import. A no-op
    # for a job that never went through the queue (e.g. the JSON-bodied
    # dicom_move/proknow_upload, still on the synchronous path).
    tasks_db.cancel_queued(job_id)
    logger.info(f"Cancelling: {job_id}")
    return {"cancelled": True}
