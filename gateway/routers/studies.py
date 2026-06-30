"""
Study-discovery endpoints.

These are explicitly documented in the gateway's OpenAPI schema.
Internally they forward to the /studies endpoints on the Hermes backend,
which in turn queries the linked Orthanc instance.
"""
from typing import Optional
from fastapi import APIRouter, Request, Query
from proxy import proxy_request

router = APIRouter(prefix="/studies", tags=["studies"])


@router.get("")
async def list_studies(
    request: Request,
    patient_id: Optional[str] = Query(None, description="Filter by patient MRN (PatientID)"),
    study_date: Optional[str] = Query(
        None,
        description="Study date filter: YYYYMMDD for exact date, YYYYMMDD-YYYYMMDD for range",
    ),
    modality: Optional[str] = Query(
        None, description="Filter by modality present in study (e.g. CT, RTPLAN, RTDOSE)"
    ),
):
    """
    List studies available in the Orthanc instance linked to Hermes.
    All parameters are optional; omitting them returns all studies.
    """
    return await proxy_request(request, "studies")


@router.get("/{orthanc_id}")
async def get_study(request: Request, orthanc_id: str):
    """
    Return full details for a single study, including per-series metadata.
    Use the `orthanc_id` returned by `GET /studies`.
    """
    return await proxy_request(request, f"studies/{orthanc_id}")
