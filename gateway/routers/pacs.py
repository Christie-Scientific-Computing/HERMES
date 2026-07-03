"""
PACS query endpoints — handled directly by the gateway via pynetdicom.

The remote PACS is on the gateway's network, not the Hermes backend's,
so C-FIND / C-ECHO run here rather than being proxied to Hermes.
Config: PACS_HOST, PACS_PORT, PACS_AE_TITLE in gateway .env.
"""
import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import pacs_client

router = APIRouter(prefix="/pacs", tags=["pacs"])
logger = logging.getLogger(__name__)


@router.get("/status")
async def pacs_status():
    """
    Test connectivity to the remote PACS via C-ECHO.
    Returns 503 if PACS_HOST / PACS_AE_TITLE are not set in the gateway .env.
    """
    if not pacs_client.is_configured():
        raise HTTPException(
            status_code=503,
            detail="PACS not configured — set PACS_HOST and PACS_AE_TITLE in the gateway .env",
        )
    reachable = await asyncio.to_thread(pacs_client.echo)
    return {
        "reachable": reachable,
        "ae_title": pacs_client.PACS_AE_TITLE,
        "host": pacs_client.PACS_HOST,
        "port": pacs_client.PACS_PORT,
    }


class SeriesQueryRequest(BaseModel):
    series_uids: list[str]


@router.post("/query_series")
async def query_series(body: SeriesQueryRequest):
    """
    Check which SeriesInstanceUIDs exist on the remote PACS via C-FIND (series level).

    Response: `{"results": {"1.2.3...": true/false/null}, "pacs": {...}}`
    null = query failed for that UID.
    """
    if not pacs_client.is_configured():
        raise HTTPException(status_code=503, detail="PACS not configured")
    try:
        results = await asyncio.to_thread(pacs_client.query_series_batch, body.series_uids)
    except Exception as exc:
        logger.error("PACS series query failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"PACS query failed: {exc}")
    return {
        "results": results,
        "pacs": {"ae_title": pacs_client.PACS_AE_TITLE, "host": pacs_client.PACS_HOST, "port": pacs_client.PACS_PORT},
    }


class StudyQueryRequest(BaseModel):
    study_uids: list[str]


@router.post("/query_studies")
async def query_studies(body: StudyQueryRequest):
    """
    Check which StudyInstanceUIDs exist on the remote PACS via C-FIND (study level).

    Response: `{"results": {"1.2.3...": true/false/null}, "pacs": {...}}`
    """
    if not pacs_client.is_configured():
        raise HTTPException(status_code=503, detail="PACS not configured")
    try:
        results = await asyncio.to_thread(pacs_client.query_studies_batch, body.study_uids)
    except Exception as exc:
        logger.error("PACS study query failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"PACS query failed: {exc}")
    return {
        "results": results,
        "pacs": {"ae_title": pacs_client.PACS_AE_TITLE, "host": pacs_client.PACS_HOST, "port": pacs_client.PACS_PORT},
    }
