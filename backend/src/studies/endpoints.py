"""
Study-discovery endpoints.

Queries the linked Orthanc instance and returns study-level metadata.
Used by the HERMES Gateway (and directly accessible on the internal network).
"""
import os
import logging
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/studies", tags=["studies"])

ORTHANC_URL = os.getenv("ORTHANC_URL")
ORTHANC_USER = os.getenv("ORTHANC_USER")
ORTHANC_PASS = os.getenv("ORTHANC_PASS")


def _orthanc(method: str, path: str, **kwargs):
    resp = requests.request(
        method,
        f"{ORTHANC_URL}{path}",
        auth=(ORTHANC_USER, ORTHANC_PASS),
        verify=False,
        timeout=30,
        **kwargs,
    )
    resp.raise_for_status()
    return resp.json()


@router.get("")
async def list_studies(
    patient_id: Optional[str] = Query(None, description="Filter by patient MRN (PatientID)"),
    study_date: Optional[str] = Query(
        None,
        description="Study date: YYYYMMDD for exact date, YYYYMMDD-YYYYMMDD for range",
    ),
    modality: Optional[str] = Query(
        None, description="Require this modality to be present in the study (e.g. RTDOSE)"
    ),
):
    """Return studies available in Orthanc, with optional filters."""
    query: dict = {}
    if patient_id:
        query["PatientID"] = patient_id
    if study_date:
        query["StudyDate"] = study_date
    if modality:
        query["ModalitiesInStudy"] = modality

    try:
        raw = _orthanc("POST", "/tools/find", json={"Level": "Study", "Query": query, "Expand": True})
    except Exception as exc:
        logger.exception("Orthanc /tools/find failed")
        raise HTTPException(status_code=502, detail=f"Orthanc query failed: {exc}")

    studies = [
        {
            "orthanc_id": item["ID"],
            "patient_id": item.get("PatientMainDicomTags", {}).get("PatientID"),
            "patient_name": item.get("PatientMainDicomTags", {}).get("PatientName"),
            "study_date": item.get("MainDicomTags", {}).get("StudyDate"),
            "study_description": item.get("MainDicomTags", {}).get("StudyDescription"),
            "study_instance_uid": item.get("MainDicomTags", {}).get("StudyInstanceUID"),
            "series_count": len(item.get("Series", [])),
        }
        for item in raw
    ]
    return {"studies": studies, "total": len(studies)}


@router.get("/{orthanc_id}")
async def get_study(orthanc_id: str):
    """Return full metadata for one study including per-series details."""
    try:
        data = _orthanc("GET", f"/studies/{orthanc_id}")
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Study not found in Orthanc")
        raise HTTPException(status_code=502, detail=f"Orthanc error: {exc}")
    except Exception as exc:
        logger.exception("Orthanc query failed for study %s", orthanc_id)
        raise HTTPException(status_code=502, detail=f"Orthanc query failed: {exc}")

    series = []
    for series_id in data.get("Series", []):
        try:
            s = _orthanc("GET", f"/series/{series_id}")
            s_tags = s.get("MainDicomTags", {})

            # SeriesInstanceUID is not always indexed in MainDicomTags depending on the
            # Orthanc version and configuration. Read it from the DICOM module directly
            # as a reliable fallback.
            series_uid = s_tags.get("SeriesInstanceUID")
            if not series_uid:
                try:
                    module = _orthanc("GET", f"/series/{series_id}/module?simplify")
                    series_uid = module.get("SeriesInstanceUID")
                except Exception:
                    pass

            series.append({
                "orthanc_id": series_id,
                "modality": s_tags.get("Modality"),
                "series_description": s_tags.get("SeriesDescription"),
                "series_date": s_tags.get("SeriesDate"),
                "series_instance_uid": series_uid,
                "instance_count": len(s.get("Instances", [])),
            })
        except Exception:
            logger.warning("Could not fetch series details for %s", series_id)
            series.append({"orthanc_id": series_id})

    tags = data.get("MainDicomTags", {})
    patient_tags = data.get("PatientMainDicomTags", {})
    return {
        "orthanc_id": orthanc_id,
        "patient_id": patient_tags.get("PatientID"),
        "patient_name": patient_tags.get("PatientName"),
        "study_date": tags.get("StudyDate"),
        "study_description": tags.get("StudyDescription"),
        "study_instance_uid": tags.get("StudyInstanceUID"),
        "series": series,
    }
