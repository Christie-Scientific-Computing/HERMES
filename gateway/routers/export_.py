"""Documented export endpoints — all forwarded transparently to the Hermes backend."""
from fastapi import APIRouter, Request
from proxy import proxy_request

router = APIRouter(prefix="/export", tags=["export"])


@router.get("/get_orthanc_modalities")
async def get_orthanc_modalities(request: Request):
    """List the DICOM modalities (AE titles) registered in the Orthanc instance."""
    return await proxy_request(request, "export/get_orthanc_modalities")


@router.get("/get_proknow_collections")
async def get_proknow_collections(request: Request):
    """List available ProKnow collections in the configured workspace."""
    return await proxy_request(request, "export/get_proknow_collections")


@router.post("/dicom_move")
async def dicom_move(request: Request):
    """
    Export patients to a DICOM destination via C-MOVE, using a CSV path on the Hermes server.
    Returns an SSE stream of progress events.
    Body: `{"job_id": "...", "path_to_csv": "...", "destination": "<AE title>"}`
    """
    return await proxy_request(request, "export/dicom_move")


@router.post("/dicom_move_file")
async def dicom_move_file(request: Request):
    """
    Export patients to a DICOM destination via C-MOVE, using an uploaded CSV file.
    Accepts `multipart/form-data` with fields: `file` (CSV), `job_id`, `destination`.
    Returns an SSE stream of progress events.
    """
    return await proxy_request(request, "export/dicom_move_file")


@router.post("/proknow_upload")
async def proknow_upload(request: Request):
    """
    Upload patients to a ProKnow collection, using a CSV path on the Hermes server.
    Returns an SSE stream of progress events.
    Body: `{"job_id": "...", "path_to_csv": "...", "collection": "<collection name>"}`
    """
    return await proxy_request(request, "export/proknow_upload")


@router.post("/proknow_upload_file")
async def proknow_upload_file(request: Request):
    """
    Upload patients to a ProKnow collection using an uploaded CSV file.
    Accepts `multipart/form-data` with fields: `file` (CSV), `job_id`, `collection`.
    Returns an SSE stream of progress events.
    """
    return await proxy_request(request, "export/proknow_upload_file")


@router.post("/proknow_upload_patient")
async def proknow_upload_patient(request: Request):
    """
    Upload a single patient to ProKnow.
    Body: `{"job_id": "...", "mrn": "...", "collection": "<collection name>"}`
    """
    return await proxy_request(request, "export/proknow_upload_patient")


@router.post("/dicom_move_uids_file")
async def dicom_move_uids_file(request: Request):
    """
    C-MOVE specific studies or series identified by DICOM UIDs.
    Accepts `multipart/form-data` with fields: `file` (CSV), `job_id`, `destination`, `level`.
    The CSV must have a `study_instance_uid` column and optionally `series_instance_uid`.
    Use the CSV downloaded from `GET /studies` as input — filter it on your side first.
    `level`: `study` (default, deduplicates by study UID) or `series` (moves individual series).
    Returns an SSE stream of progress events.
    """
    return await proxy_request(request, "export/dicom_move_uids_file")


@router.post("/cancel/{job_id}")
async def cancel_export(request: Request, job_id: str):
    """Cancel a running batch export job by its job_id."""
    return await proxy_request(request, f"export/cancel/{job_id}")
