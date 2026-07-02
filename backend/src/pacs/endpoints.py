"""
PACS query endpoints.

Lets clients check whether series/studies exist on the remote PACS configured
via PACS_AE_TITLE / PACS_HOST / PACS_PORT environment variables.
All DICOM communication is handled by Orthanc (C-FIND / C-ECHO via its REST API).
"""
import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.src.pacs.client import (
    is_configured,
    ensure_registered,
    echo,
    series_on_pacs,
    study_on_pacs,
    PACS_AE_TITLE,
    PACS_HOST,
    PACS_PORT,
)

router = APIRouter(prefix="/pacs", tags=["pacs"])
logger = logging.getLogger(__name__)


@router.get("/status")
async def pacs_status():
    """
    Test connectivity to the configured remote PACS via C-ECHO.
    Returns the PACS coordinates and whether it responded.
    """
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="PACS not configured — set PACS_AE_TITLE and PACS_HOST in the Hermes .env",
        )
    try:
        ensure_registered()
        reachable = await asyncio.to_thread(echo)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"PACS check failed: {exc}")
    return {
        "reachable": reachable,
        "ae_title": PACS_AE_TITLE,
        "host": PACS_HOST,
        "port": PACS_PORT,
    }


class SeriesQueryRequest(BaseModel):
    series_uids: list[str]


@router.post("/query_series")
async def query_series(body: SeriesQueryRequest):
    """
    Check which SeriesInstanceUIDs exist on the remote PACS via DICOM C-FIND.

    Returns a `results` dict mapping each UID to:
    - `true`  — found on PACS
    - `false` — not found
    - `null`  — query failed for this UID (PACS timeout, etc.)
    """
    if not is_configured():
        raise HTTPException(status_code=503, detail="PACS not configured")
    try:
        ensure_registered()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not register PACS modality in Orthanc: {exc}")

    results: dict[str, bool | None] = {}
    for uid in body.series_uids:
        try:
            results[uid] = await asyncio.to_thread(series_on_pacs, uid)
        except Exception as exc:
            logger.error("C-FIND failed for series %s: %s", uid, exc)
            results[uid] = None

    return {
        "results": results,
        "pacs": {"ae_title": PACS_AE_TITLE, "host": PACS_HOST, "port": PACS_PORT},
    }
