"""Documented PACS query endpoints — forwarded to the Hermes backend."""
from fastapi import APIRouter, Request
from proxy import proxy_request

router = APIRouter(prefix="/pacs", tags=["pacs"])


@router.get("/status")
async def pacs_status(request: Request):
    """
    Test connectivity to the remote PACS configured in the Hermes backend.
    Returns `{"reachable": true/false, "ae_title": "...", "host": "...", "port": 104}`.
    503 if PACS_AE_TITLE / PACS_HOST are not set in the Hermes environment.
    """
    return await proxy_request(request, "pacs/status")


@router.post("/query_series")
async def query_series(request: Request):
    """
    Check which SeriesInstanceUIDs exist on the remote PACS via DICOM C-FIND.

    Body (JSON):
    ```json
    { "series_uids": ["1.2.3...", "1.2.4..."] }
    ```

    Response:
    ```json
    {
      "results": { "1.2.3...": true, "1.2.4...": false },
      "pacs": { "ae_title": "PACS", "host": "192.168.x.x", "port": 104 }
    }
    ```

    Values: `true` = on PACS, `false` = not found, `null` = query failed for that UID.
    """
    return await proxy_request(request, "pacs/query_series")
