"""Documented import endpoints — all forwarded transparently to the Hermes backend."""
from fastapi import APIRouter, Request
from proxy import proxy_request

router = APIRouter(prefix="/import", tags=["import"])


@router.post("/batch_import")
async def batch_import(request: Request):
    """
    Batch import patients from a CSV path that exists on the Hermes server filesystem.
    Returns an SSE stream of progress events (start, progress, success, error, done).
    Body: `{"job_id": "...", "path_to_csv": "...", "import_level": "Planning data"}`
    """
    return await proxy_request(request, "import/batch_import")


@router.post("/batch_import_file")
async def batch_import_file(request: Request):
    """
    Batch import patients from an uploaded CSV file.
    Accepts `multipart/form-data` with fields: `file` (CSV), `job_id`, `import_level`.
    Returns an SSE stream of progress events. Import levels: `Planning data`, `Images only`, `Everything`.
    """
    return await proxy_request(request, "import/batch_import_file")


@router.post("/single_import")
async def single_import(request: Request):
    """
    Import a single patient by MRN.
    Body: `{"job_id": "...", "mrn": "...", "import_level": "Planning data"}`
    """
    return await proxy_request(request, "import/single_import")


@router.get("/find_patient")
async def find_patient(request: Request):
    """
    Search for a patient across all connected sources (Mosaiq, Pinnacle, ProKnow)
    without importing anything.
    Query params: `mrn`, `import_level`
    """
    return await proxy_request(request, "import/find_patient")


@router.post("/cancel/{job_id}")
async def cancel_import(request: Request, job_id: str):
    """Cancel a running batch import job by its job_id."""
    return await proxy_request(request, f"import/cancel/{job_id}")
