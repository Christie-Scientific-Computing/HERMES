"""
Endpoints for the import page
"""
import os
import time
import logging
import numpy as np
from pydantic import BaseModel
from pathlib import Path
from backend.src.retrieve.logic import Importer
from fastapi import APIRouter, Depends, UploadFile, File, Form, Query, HTTPException
from fastapi.responses import StreamingResponse
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB
from backend.src.common.sse import BatchItem, run_batch_job, build_patient_id_batch
from backend.src.identity import anon
from backend.src.projects import enforcement
from backend.src.projects.enforcement import verify_internal_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/import', tags=["import"], dependencies=[Depends(verify_internal_key)])

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


class Request(BaseModel):
    job_id: str
    project_id: str
    username: str
    mrn: str | None = None
    path_to_csv: str | None = None
    import_level: str | None = None

# Note: _import_worker (below) dumps this with exclude_none=True, since its
# output feeds events.details/the SSE payload for every batch endpoint and
# these new fields are still unpopulated (nothing sets them until D1/D3/E
# land) -- exclude_none keeps those payloads from filling up with nulls in
# the meantime. single_import/find_patient, which don't route through
# run_batch_job, don't do this -- not a deliberate inconsistency, just that
# those paths have no live caller today (per D0 review) so it wasn't worth
# reconciling yet. Revisit if/when they grow one.
class Response(BaseModel):
    mrn: str | int
    status: str | None = None
    in_mosaiq: bool | None = None
    in_pinnacle: bool | None = None
    in_proknow: bool | None = None
    mosaiq_reason: str | None = None
    pinnacle_reason: str | None = None
    proknow_reason: str | None = None
    imported: bool | None = None
    study_count: int | None = None
    study_uids: list[str] | None = None


def _build_import_items(path_to_csv: str) -> list[BatchItem]:
    try:
        rows = Importer.read_input_file(path_to_csv)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read CSV: {e}")
    try:
        return build_patient_id_batch(rows, input_path=path_to_csv)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))


def _import_worker(import_level: str):
    def worker(item: BatchItem) -> dict:
        res = Importer(import_level).handle_patient(item.real_id)
        return Response(mrn=item.real_id, **res).model_dump(exclude={"mrn"}, exclude_none=True)
    return worker


### Import page
@router.post("/batch_import")
async def batch_import(body: Request):
    """
    Main method
    """
    req = body.model_dump()
    logger.info(f"Received: {req}")
    enforcement.require_project_member(req['project_id'], req['username'])

    items = _build_import_items(req['path_to_csv'])

    return StreamingResponse(
        run_batch_job(
            req['job_id'], items, stage='retrieve',
            worker=_import_worker(req['import_level']),
            status_db=status_db,
            description=f"Batch import from {req['path_to_csv']}",
            created_by=req['username'],
            project_id=req['project_id'],
        ),
        media_type="text/event-stream",
        headers = {
            "Cache-Control": "no-cache",
        }
    )


@router.post('/single_import')
async def single_import(body: Request):
    req = body.model_dump()
    logger.info("Importing %s", req['mrn'])
    enforcement.require_project_member(req['project_id'], req['username'])
    try:
        real_mrn = anon.resolve_real_id(req['mrn'])
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if status_db:
        try:
            status_db.create_job(
                req['job_id'], description=f"Single import ({req['import_level']})",
                created_by=req['username'], project_id=req['project_id'],
            )
            status_db.add_patient(req['job_id'], real_mrn, input_path=None)
            status_db.add_event(req['job_id'], real_mrn, stage='retrieve', event_type='start')
        except Exception as e:
            logger.warning("Status DB write failed: %s", e)

    start = time.time()
    try:
        imp = Importer(req['import_level'])
        res = imp.handle_patient(real_mrn)
        response = Response(mrn=anon.to_display_id(real_mrn), **res)

        if status_db:
            try:
                status_db.add_event(req['job_id'], real_mrn, stage='retrieve', event_type='success', details=res)
            except Exception as e:
                logger.warning("Status DB write failed: %s", e)

        return {'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()}

    except Exception as e:
        logger.exception("single_import failed for %s", req['mrn'])
        if status_db:
            try:
                status_db.add_event(req['job_id'], real_mrn, stage='retrieve', event_type='failure', error_message=str(e))
            except Exception as ex:
                logger.warning("Status DB write failed: %s", ex)
        try:
            display_mrn = anon.to_display_id(real_mrn)
        except Exception:
            display_mrn = "[unknown]"
        return {'type': 'error', 'execution_time': np.round(time.time() - start, 2), 'mrn': display_mrn, 'error': str(e)}


@router.get('/find_patient')
async def find_patient(
    mrn: str = Query(...),
    username: str = Query(...),
    import_level: str | None = Query(None),
) -> Response:
    logger.info("Searching for %s", mrn)
    enforcement.require_any_active_project(username)
    try:
        real_mrn = anon.resolve_real_id(mrn)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        imp = Importer(import_level)
        res = imp.find_patient(real_mrn)
        return Response(mrn=anon.to_display_id(real_mrn), **res)
    except anon.AnonServiceError as e:
        logger.exception("find_patient failed to translate result for %s", mrn)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("find_patient failed for %s", mrn)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/batch_import_file")
async def batch_import_file(
    file: UploadFile = File(..., description="CSV with a patient_id column"),
    job_id: str = Form(...),
    project_id: str = Form(...),
    username: str = Form(...),
    import_level: str = Form("Planning data"),
):
    """
    Accept a CSV file upload and either stream import progress via SSE
    (today's default) or enqueue it onto the tasks table for
    backend/worker.py to execute (when HERMES_USE_QUEUE=1 -- see
    docs/worker-queue-design.md). CSV parsing and anon resolution
    (_build_import_items) happen synchronously either way, so a bad CSV or
    an unmapped id still fails the request with a clean 400/422/503 rather
    than turning into a silently-failed queued job.
    """
    enforcement.require_project_member(project_id, username)
    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{file.filename}"
    tmp_path.write_bytes(await file.read())

    items = _build_import_items(str(tmp_path))

    if os.getenv("HERMES_USE_QUEUE") == "1":
        status_db.create_job(
            job_id, description=f"Batch import from {tmp_path}",
            created_by=username, project_id=project_id,
        )
        # Registered here, at enqueue time, rather than by the worker as it
        # picks each task up: every submitted patient is already fully known
        # from `items`, and StatusDB.count_imported_patients' "M" (submitted
        # count) denominator should reflect what was actually submitted
        # regardless of whether/when a worker gets around to running it --
        # not silently read as 0 for a job whose tasks haven't been claimed
        # yet.
        for item in items:
            status_db.add_patient(job_id, item.status_mrn, input_path=item.input_path)
        tasks_db.enqueue(
            job_id, items, kind="import", stage="retrieve",
            params={"import_level": import_level, "project_id": project_id, "username": username},
        )
        return {"job_id": job_id, "total": len(items)}

    return StreamingResponse(
        run_batch_job(
            job_id, items, stage='retrieve',
            worker=_import_worker(import_level),
            status_db=status_db,
            description=f"Batch import from {tmp_path}",
            created_by=username,
            project_id=project_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/cancel/{job_id}")
async def cancel_import(job_id: str):
    status_db.cancel_job(job_id)
    logger.info(f"Cancelling: {job_id}")
    return {"cancelled": True}

