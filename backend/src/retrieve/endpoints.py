"""
Endpoints for the import page
"""
import json
import time
import asyncio
import logging
import numpy as np
from pydantic import BaseModel
from pathlib import Path
from backend.src.retrieve.logic import Importer
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/import', tags=["import"])

class Request(BaseModel):
    job_id: str
    path_to_csv: str
    import_level: str

class Response(BaseModel):
    mrn: int 
    status: str | None = None
    in_mosaiq: bool | None = None
    in_pinnacle: bool | None = None
    in_proknow: bool | None = None

cancel_flags: dict[str, bool] = {} ## Holds cancellation status for every job

async def import_event_stream(job_id: str, path_to_csv: str, import_level: str):
    """
    Generator that yields SSE-formatted events, one per patient.
    """
    cancel_flags[job_id] = False
    rows = Importer.read_input_file(path_to_csv)
    total = len(rows)

    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

    for row in rows:
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
                Importer(import_level).handle_patient, patient_id
            )
            response = Response(mrn=patient_id, **res)
            
            yield f"data: {json.dumps({
                'type': 'success', 'execution_time': np.round(time.time() - start, 2), **response.model_dump()})}\n\n"

        except Exception as e:
            logger.error("Failed to import patient %s: %s", patient_id, e)
            yield f"data: {json.dumps({'type': 'error', 'execution_time': np.round(time.time() - start, 2), 'mrn': patient_id, 'error': str(e)})}\n\n"
    
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


@router.get('/single_import')
async def single_import(mrn: int):
    logger.info("Importing %s", mrn)
    imp = Importer()
    res = imp.handle_patient(mrn)


@router.get('/find_patient')
async def find_patient(mrn: int) -> Response:
    logger.info("Searching for %s", mrn)
    imp = Importer()
    res = imp.find_patient(mrn)
    return Response(mrn=mrn, **res)


@router.post("/cancel/{job_id}")
async def cancel_import(job_id: str):
    cancel_flags[job_id] = True
    logger.info(f"Cancelling: {cancel_flags}")
    return {"cancelled": True}