"""
Study-discovery endpoints.

Queries the linked Orthanc instance and returns study-level metadata.
Inbound `patient_id` filters are anon ids, resolved to the real PatientID
before querying Orthanc; outbound `patient_id` fields are translated back
to anon ids. `patient_name` has no anonymisation mapping at all (the
key_value table only maps numeric patient ids) so it's redacted whenever
anonymisation is configured, rather than leaking a real name.

`study_date`/`series_date` are shifted (backend/src/identity/anon.py's
shift_date, docs/plans/pii-boundary-test-suite.md §B) rather than redacted
outright, preserving the relative interval between a patient's scans while
breaking the link to the real calendar date -- called unconditionally
(shift_date itself degrades to a 0-day no-op when anonymisation isn't
configured, so there's no separate passthrough branch to maintain here).
`study_instance_uid`/`series_instance_uid`/`study_description`/
`series_description` have no equivalent "shifted" form (there's no
legitimate partial UID/description the way a shifted date is still a real
date), so they're redacted to None whenever anonymisation is configured,
the same pattern `patient_name` already used.
"""
import os
import logging
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from dotenv import load_dotenv

from backend.src.identity import anon

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
    patient_id: Optional[str] = Query(None, description="Filter by patient MRN (PatientID) -- anon id"),
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
        try:
            query["PatientID"] = anon.resolve_real_id(patient_id)
        except anon.AnonLookupError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except anon.AnonServiceError as e:
            raise HTTPException(status_code=503, detail=str(e))
    if study_date:
        query["StudyDate"] = study_date
    if modality:
        query["ModalitiesInStudy"] = modality

    try:
        raw = _orthanc("POST", "/tools/find", json={"Level": "Study", "Query": query, "Expand": True})
    except Exception as exc:
        logger.exception("Orthanc /tools/find failed")
        raise HTTPException(status_code=502, detail=f"Orthanc query failed: {exc}")

    real_patient_ids = [
        item.get("PatientMainDicomTags", {}).get("PatientID")
        for item in raw
        if item.get("PatientMainDicomTags", {}).get("PatientID")
    ]
    try:
        display_map = anon.to_display_ids(real_patient_ids)
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    redact_identifying_fields = anon.is_configured()

    def _to_study(item: dict) -> dict:
        real_patient_id = item.get("PatientMainDicomTags", {}).get("PatientID")
        main_tags = item.get("MainDicomTags", {})
        return {
            "orthanc_id": item["ID"],
            "patient_id": display_map.get(real_patient_id),
            "patient_name": None if redact_identifying_fields else item.get("PatientMainDicomTags", {}).get("PatientName"),
            "study_date": anon.shift_date(real_patient_id, main_tags.get("StudyDate")),
            "study_description": None if redact_identifying_fields else main_tags.get("StudyDescription"),
            "study_instance_uid": None if redact_identifying_fields else main_tags.get("StudyInstanceUID"),
            "series_count": len(item.get("Series", [])),
        }

    studies = [_to_study(item) for item in raw]
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

    tags = data.get("MainDicomTags", {})
    patient_tags = data.get("PatientMainDicomTags", {})
    real_patient_id = patient_tags.get("PatientID")
    redact_identifying_fields = anon.is_configured()

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
                "series_description": None if redact_identifying_fields else s_tags.get("SeriesDescription"),
                "series_date": anon.shift_date(real_patient_id, s_tags.get("SeriesDate")),
                "series_instance_uid": None if redact_identifying_fields else series_uid,
                "instance_count": len(s.get("Instances", [])),
            })
        except Exception:
            logger.warning("Could not fetch series details for %s", series_id)
            series.append({"orthanc_id": series_id})

    try:
        display_patient_id = anon.to_display_id(real_patient_id) if real_patient_id else None
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {
        "orthanc_id": orthanc_id,
        "patient_id": display_patient_id,
        "patient_name": None if redact_identifying_fields else patient_tags.get("PatientName"),
        "study_date": anon.shift_date(real_patient_id, tags.get("StudyDate")),
        "study_description": None if redact_identifying_fields else tags.get("StudyDescription"),
        "study_instance_uid": None if redact_identifying_fields else tags.get("StudyInstanceUID"),
        "series": series,
    }
