"""
Endpoints for the import page
"""
import logging
from pydantic import BaseModel
from pathlib import Path
from backend.src.import_.logic import Importer
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/import', tags=["import"])

class Request(BaseModel):
    path_to_csv: str
    import_level: str

class Response(BaseModel):
    mrn: int 
    status: str | None = None
    in_mosaiq: bool | None = None
    in_pinnacle: bool | None = None
    in_proknow: bool | None = None

### Import page
@router.post("/batch_import")
async def batch_import(request: Request):
    """
    Main method
    """
    req = request.model_dump()
    logger.info(f"Received: {req}")

    responses = []
    for row in Importer.read_input_file(req['path_to_csv']):
        res = Importer(req['import_level']).handle_patient(row['patient_id'])
        responses.append(Response(mrn=row['patient_id'], **res))
    return responses


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
