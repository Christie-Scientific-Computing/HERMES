"""
Shared PACS connectivity utilities.

Uses Orthanc as a DICOM SCU to perform C-FIND and C-ECHO against a remote PACS.
Requires PACS_AE_TITLE, PACS_HOST, and PACS_PORT in the environment.
"""
import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ORTHANC_URL  = os.getenv("ORTHANC_URL")
ORTHANC_USER = os.getenv("ORTHANC_USER")
ORTHANC_PASS = os.getenv("ORTHANC_PASS")

PACS_AE_TITLE      = os.getenv("PACS_AE_TITLE")
PACS_HOST          = os.getenv("PACS_HOST")
PACS_PORT          = int(os.getenv("PACS_PORT", "104"))
PACS_MODALITY_NAME = "__hermes_pacs__"


def _req(method: str, path: str, **kwargs):
    kwargs.setdefault("timeout", 30)
    resp = requests.request(
        method,
        f"{ORTHANC_URL}{path}",
        auth=(ORTHANC_USER, ORTHANC_PASS),
        verify=False,
        **kwargs,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


def is_configured() -> bool:
    return bool(PACS_AE_TITLE and PACS_HOST)


def ensure_registered():
    """Register (or refresh) the remote PACS as a named Orthanc modality so we can query it."""
    _req("PUT", f"/modalities/{PACS_MODALITY_NAME}", json={
        "AET": PACS_AE_TITLE,
        "Host": PACS_HOST,
        "Port": PACS_PORT,
    })


def echo() -> bool:
    """C-ECHO the remote PACS. Returns True if reachable."""
    try:
        _req("POST", f"/modalities/{PACS_MODALITY_NAME}/echo", timeout=10)
        return True
    except Exception:
        return False


def series_on_pacs(series_uid: str) -> bool:
    """Return True if the SeriesInstanceUID matches anything on the remote PACS."""
    results = _req("POST", f"/modalities/{PACS_MODALITY_NAME}/find", json={
        "Level": "Series",
        "Query": {"SeriesInstanceUID": series_uid},
    })
    return bool(results)


def study_on_pacs(study_uid: str) -> bool:
    """Return True if the StudyInstanceUID matches anything on the remote PACS."""
    results = _req("POST", f"/modalities/{PACS_MODALITY_NAME}/find", json={
        "Level": "Study",
        "Query": {"StudyInstanceUID": study_uid},
    })
    return bool(results)
