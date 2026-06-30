"""Documented results endpoints — all forwarded transparently to the Hermes backend."""
from fastapi import APIRouter, Request
from proxy import proxy_request

router = APIRouter(prefix="/results", tags=["results"])


@router.get("/job/{job_id}")
async def job_summary(request: Request, job_id: str):
    """Return aggregated success/failure counts by stage for a job."""
    return await proxy_request(request, f"results/job/{job_id}")


@router.get("/job/{job_id}/patients")
async def job_patients(request: Request, job_id: str):
    """Return the list of patient MRNs that have events recorded for a job."""
    return await proxy_request(request, f"results/job/{job_id}/patients")


@router.get("/patient/{job_id}/{mrn}")
async def patient_timeline(request: Request, job_id: str, mrn: str):
    """Return the chronological event timeline for a patient within a specific job."""
    return await proxy_request(request, f"results/patient/{job_id}/{mrn}")


@router.get("/patient/timeline/{mrn}/all")
async def patient_timeline_all(request: Request, mrn: str):
    """Return all events for a patient across every job."""
    return await proxy_request(request, f"results/patient/timeline/{mrn}/all")
