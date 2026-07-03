"""
Direct DICOM PACS connectivity for the gateway.

Uses pynetdicom to perform C-FIND and C-ECHO directly from the gateway host.
The PACS must be reachable from the gateway's network (not the Hermes backend's).
Config: PACS_HOST, PACS_PORT, PACS_AE_TITLE, GATEWAY_AE_TITLE in gateway .env.
"""
import os
import logging
from dotenv import load_dotenv
from pynetdicom import AE
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind, Verification
from pydicom.dataset import Dataset

load_dotenv()
logger = logging.getLogger(__name__)

PACS_HOST        = os.getenv("PACS_HOST")
PACS_PORT        = int(os.getenv("PACS_PORT", "104"))
PACS_AE_TITLE    = os.getenv("PACS_AE_TITLE")
GATEWAY_AE_TITLE = os.getenv("GATEWAY_AE_TITLE", "HERMES_GW")

# C-FIND pending status codes
_PENDING = (0xFF00, 0xFF01)


def is_configured() -> bool:
    return bool(PACS_HOST and PACS_AE_TITLE)


def _associate():
    """Open a DICOM association with the remote PACS."""
    ae = AE(ae_title=GATEWAY_AE_TITLE)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
    ae.add_requested_context(Verification)
    assoc = ae.associate(PACS_HOST, PACS_PORT, ae_title=PACS_AE_TITLE)
    if not assoc.is_established:
        raise ConnectionError(
            f"Cannot connect to PACS {PACS_AE_TITLE}@{PACS_HOST}:{PACS_PORT}"
        )
    return assoc


def echo() -> bool:
    """C-ECHO the remote PACS. Returns True if reachable."""
    try:
        ae = AE(ae_title=GATEWAY_AE_TITLE)
        ae.add_requested_context(Verification)
        assoc = ae.associate(PACS_HOST, PACS_PORT, ae_title=PACS_AE_TITLE)
        if not assoc.is_established:
            return False
        status = assoc.send_c_echo()
        assoc.release()
        return status is not None and status.Status == 0x0000
    except Exception as exc:
        logger.error("C-ECHO failed: %s", exc)
        return False


def query_series_batch(series_uids: list[str]) -> dict[str, bool | None]:
    """
    Check multiple SeriesInstanceUIDs via C-FIND (series level).
    Returns: uid -> True (on PACS) / False (not found) / None (query failed).
    Reuses a single DICOM association for all queries.
    """
    if not series_uids:
        return {}

    results: dict[str, bool | None] = {}
    try:
        assoc = _associate()
    except Exception as exc:
        logger.error("PACS association failed: %s", exc)
        return {uid: None for uid in series_uids}

    try:
        for uid in series_uids:
            try:
                ds = Dataset()
                ds.QueryRetrieveLevel = "SERIES"
                ds.SeriesInstanceUID  = uid
                ds.StudyInstanceUID   = ""
                found = any(
                    s.Status in _PENDING
                    for s, _ in assoc.send_c_find(ds, StudyRootQueryRetrieveInformationModelFind)
                    if s
                )
                results[uid] = found
            except Exception as exc:
                logger.error("C-FIND failed for series %s: %s", uid, exc)
                results[uid] = None
    finally:
        assoc.release()

    return results


def query_studies_batch(study_uids: list[str]) -> dict[str, bool | None]:
    """
    Check multiple StudyInstanceUIDs via C-FIND (study level).
    Returns: uid -> True (on PACS) / False (not found) / None (query failed).
    """
    if not study_uids:
        return {}

    results: dict[str, bool | None] = {}
    try:
        assoc = _associate()
    except Exception as exc:
        logger.error("PACS association failed: %s", exc)
        return {uid: None for uid in study_uids}

    try:
        for uid in study_uids:
            try:
                ds = Dataset()
                ds.QueryRetrieveLevel  = "STUDY"
                ds.StudyInstanceUID    = uid
                found = any(
                    s.Status in _PENDING
                    for s, _ in assoc.send_c_find(ds, StudyRootQueryRetrieveInformationModelFind)
                    if s
                )
                results[uid] = found
            except Exception as exc:
                logger.error("C-FIND failed for study %s: %s", uid, exc)
                results[uid] = None
    finally:
        assoc.release()

    return results
