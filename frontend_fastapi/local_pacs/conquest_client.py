"""
F001 -- pynetdicom-backed connection to the trust's local PACS (Conquest).

Conquest is reachable only from frontend_fastapi, not from backend/Orthanc
(firewalled) -- see docs/plans (local-pacs-query) D001. So, uniquely among
every other DICOM integration in this codebase (which always goes through
Orthanc's REST API via pyorthanc), this module speaks raw DICOM directly via
pynetdicom. Three primitives: echo (C-ECHO, connectivity check), find
(C-FIND, patient/study/series query), move (C-MOVE with an explicit Move
Destination AE Title -- a direct relay, Conquest -> destination, that never
passes through this process).

Every public function is synchronous (pynetdicom itself is blocking/
threaded, not asyncio) -- callers running under FastAPI's event loop must
wrap calls in asyncio.to_thread(), per CLAUDE.md's "Async threading" pattern.

No UI or HermesDB concerns live here (per F001's own scope) -- just DICOM in,
plain data out, or a specific exception.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from pydicom.dataset import Dataset
from pynetdicom import AE
from pynetdicom.association import Association
from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelMove,
    Verification,
)

from frontend_fastapi import settings

logger = logging.getLogger(__name__)

# Caps a single C-FIND response so an overly broad search (e.g. a bare date
# range) can't return an unbounded result set -- Conquest's actual behaviour
# under a huge query can't be verified from this repo (see plan.md's Open
# issues), so this is a defensive client-side cap, not a tuned limit.
MAX_FIND_RESULTS = 200


class ConquestError(Exception):
    """Base for every distinguishable local-PACS failure mode."""


class ConquestNotConfigured(ConquestError):
    """LOCAL_PACS_* / HERMES_FRONTEND_AE_TITLE env vars are unset or incomplete."""


class ConquestUnreachable(ConquestError):
    """Could not even establish a TCP/DICOM association -- host down, wrong
    port, network partition, or the peer aborted before responding."""


class ConquestAssociationRejected(ConquestError):
    """Conquest actively rejected the association (e.g. unknown calling AE
    title, no matching presentation context)."""


class ConquestTimeout(ConquestError):
    """The association was established but a DIMSE operation didn't
    complete within the configured timeout."""


class ConquestOperationFailed(ConquestError):
    """The operation completed but Conquest returned a DICOM failure/warning
    status (not a connectivity problem)."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ConquestConfig:
    host: str
    port: int
    ae_title: str
    calling_ae_title: str
    timeout_seconds: int


def _get_config() -> ConquestConfig:
    host = settings.LOCAL_PACS_HOST
    port = settings.LOCAL_PACS_PORT
    ae_title = settings.LOCAL_PACS_AE_TITLE
    calling_ae_title = settings.HERMES_FRONTEND_AE_TITLE
    if not host or not port or not ae_title or not calling_ae_title:
        raise ConquestNotConfigured(
            "Local PACS (Conquest) is not fully configured -- LOCAL_PACS_HOST, LOCAL_PACS_PORT, "
            "LOCAL_PACS_AE_TITLE and HERMES_FRONTEND_AE_TITLE must all be set."
        )
    try:
        port_number = int(port)
    except ValueError:
        raise ConquestNotConfigured(f"LOCAL_PACS_PORT must be a number, got {port!r}.")
    return ConquestConfig(
        host=host, port=port_number, ae_title=ae_title, calling_ae_title=calling_ae_title,
        timeout_seconds=settings.LOCAL_PACS_TIMEOUT_SECONDS,
    )


def is_configured() -> bool:
    try:
        _get_config()
        return True
    except ConquestNotConfigured:
        return False


def _associate(cfg: ConquestConfig, contexts: list) -> Association:
    ae = AE(ae_title=cfg.calling_ae_title)
    for context in contexts:
        ae.add_requested_context(context)
    ae.connection_timeout = cfg.timeout_seconds
    ae.acse_timeout = cfg.timeout_seconds
    ae.dimse_timeout = cfg.timeout_seconds
    try:
        assoc = ae.associate(cfg.host, cfg.port, ae_title=cfg.ae_title)
    except Exception as e:
        raise ConquestUnreachable(f"Could not connect to local PACS at {cfg.host}:{cfg.port}: {e}") from e

    if assoc.is_established:
        return assoc
    if assoc.is_rejected:
        raise ConquestAssociationRejected(
            f"Local PACS at {cfg.host}:{cfg.port} rejected the association (calling AE {cfg.calling_ae_title!r})."
        )
    raise ConquestUnreachable(f"Could not connect to local PACS at {cfg.host}:{cfg.port} (no association established).")


def echo() -> None:
    """C-ECHO connectivity check. Raises on any failure; returns None on success."""
    cfg = _get_config()
    assoc = _associate(cfg, [Verification])
    try:
        status = assoc.send_c_echo()
        if status is None:
            raise ConquestTimeout(f"Local PACS at {cfg.host}:{cfg.port} did not respond to C-ECHO in time.")
        if status.Status != 0x0000:
            raise ConquestOperationFailed(
                f"Local PACS rejected the C-ECHO (status 0x{status.Status:04X}).", status_code=status.Status,
            )
    except ConquestError:
        raise
    except Exception as e:
        # A malformed/unexpected response from the DICOM peer mid-operation
        # (not just at association time, already handled by _associate)
        # must still come out as a ConquestError -- every caller (F002/F004)
        # only catches cc.ConquestError, and F004 specifically needs this to
        # reach it so a failed move still gets audited (F005), not silently
        # dropped by an unclassified exception.
        raise ConquestOperationFailed(f"Local PACS C-ECHO failed unexpectedly: {e}") from e
    finally:
        assoc.release()


def _date_range(date_from, date_to) -> str:
    fmt = "%Y%m%d"
    lo = date_from.strftime(fmt) if date_from else ""
    hi = date_to.strftime(fmt) if date_to else ""
    if lo and hi:
        return f"{lo}-{hi}"
    if lo:
        return f"{lo}-"
    if hi:
        return f"-{hi}"
    return ""


@dataclass(frozen=True)
class FindResult:
    matches: list[dict]
    truncated: bool


def find_studies(
    *, patient_id: str = "", study_date_from=None, study_date_to=None,
    study_description: str = "", modalities_in_study: str = "",
) -> FindResult:
    """Study-level C-FIND. Every argument is an optional search key -- callers
    (F002) are responsible for requiring at least one to be non-empty before
    calling this, since an unconstrained query isn't the goal here."""
    cfg = _get_config()
    ds = Dataset()
    ds.QueryRetrieveLevel = "STUDY"
    ds.PatientID = patient_id
    ds.StudyDate = _date_range(study_date_from, study_date_to)
    ds.StudyDescription = study_description
    ds.ModalitiesInStudy = modalities_in_study
    for tag in ("StudyInstanceUID", "StudyTime", "AccessionNumber", "NumberOfStudyRelatedSeries", "PatientName"):
        setattr(ds, tag, "")

    results = []
    truncated = False
    assoc = _associate(cfg, [PatientRootQueryRetrieveInformationModelFind])
    try:
        for status, identifier in assoc.send_c_find(ds, PatientRootQueryRetrieveInformationModelFind):
            if status is None:
                raise ConquestTimeout(f"Local PACS at {cfg.host}:{cfg.port} did not respond to C-FIND in time.")
            if status.Status in (0xFF00, 0xFF01):
                if identifier is not None:
                    if len(results) < MAX_FIND_RESULTS:
                        results.append(_study_to_dict(identifier))
                    else:
                        truncated = True
                        break  # pynetdicom cancels the remaining C-FIND on early generator exit
                continue
            if status.Status != 0x0000:
                raise ConquestOperationFailed(
                    f"Local PACS C-FIND failed (status 0x{status.Status:04X}).", status_code=status.Status,
                )
    except ConquestError:
        raise
    except Exception as e:
        raise ConquestOperationFailed(f"Local PACS C-FIND failed unexpectedly: {e}") from e
    finally:
        assoc.release()
    return FindResult(matches=results, truncated=truncated)


def find_series(*, study_instance_uid: str) -> list[dict]:
    """Series-level C-FIND for a single study (F002's drill-down)."""
    cfg = _get_config()
    ds = Dataset()
    ds.QueryRetrieveLevel = "SERIES"
    ds.StudyInstanceUID = study_instance_uid
    for tag in ("SeriesInstanceUID", "Modality", "SeriesDescription", "SeriesNumber", "NumberOfSeriesRelatedInstances"):
        setattr(ds, tag, "")

    results = []
    assoc = _associate(cfg, [PatientRootQueryRetrieveInformationModelFind])
    try:
        for status, identifier in assoc.send_c_find(ds, PatientRootQueryRetrieveInformationModelFind):
            if status is None:
                raise ConquestTimeout(f"Local PACS at {cfg.host}:{cfg.port} did not respond to C-FIND in time.")
            if status.Status in (0xFF00, 0xFF01):
                if identifier is not None and len(results) < MAX_FIND_RESULTS:
                    results.append(_series_to_dict(identifier))
                continue
            if status.Status != 0x0000:
                raise ConquestOperationFailed(
                    f"Local PACS C-FIND failed (status 0x{status.Status:04X}).", status_code=status.Status,
                )
    except ConquestError:
        raise
    except Exception as e:
        raise ConquestOperationFailed(f"Local PACS C-FIND failed unexpectedly: {e}") from e
    finally:
        assoc.release()
    return results


def _get(identifier: Dataset, tag: str, default=""):
    return getattr(identifier, tag, default) or default


def _study_to_dict(identifier: Dataset) -> dict:
    return {
        "patient_id": _get(identifier, "PatientID"),
        "study_instance_uid": _get(identifier, "StudyInstanceUID"),
        "study_date": _get(identifier, "StudyDate"),
        "study_description": _get(identifier, "StudyDescription"),
        "modalities_in_study": _get(identifier, "ModalitiesInStudy"),
        "accession_number": _get(identifier, "AccessionNumber"),
        "series_count": _get(identifier, "NumberOfStudyRelatedSeries", None),
    }


def _series_to_dict(identifier: Dataset) -> dict:
    return {
        "series_instance_uid": _get(identifier, "SeriesInstanceUID"),
        "modality": _get(identifier, "Modality"),
        "series_description": _get(identifier, "SeriesDescription"),
        "series_number": _get(identifier, "SeriesNumber"),
        "instance_count": _get(identifier, "NumberOfSeriesRelatedInstances", None),
    }


@dataclass(frozen=True)
class MoveResult:
    completed: int = 0
    failed: int = 0
    warning: int = 0
    status_code: Optional[int] = None

    @property
    def success(self) -> bool:
        return self.status_code == 0x0000 and self.failed == 0


def move_study(*, study_instance_uid: str, destination_ae: str) -> MoveResult:
    """
    Direct one-hop C-MOVE relay: Conquest performs the transfer straight to
    `destination_ae` (the Move Destination AE Title), never to this process
    or through Orthanc -- see plan.md D002. Raises ConquestOperationFailed
    only for a connectivity-level problem with the move request itself;
    a completed-with-failed-suboperations outcome is reported back via
    MoveResult (not raised), since that's the actual DICOM semantics of a
    C-MOVE response and F004 needs to show it as a specific outcome, not a
    generic error.
    """
    cfg = _get_config()
    ds = Dataset()
    ds.QueryRetrieveLevel = "STUDY"
    ds.StudyInstanceUID = study_instance_uid

    final = MoveResult()
    assoc = _associate(cfg, [PatientRootQueryRetrieveInformationModelMove])
    try:
        for status, _identifier in assoc.send_c_move(ds, destination_ae, PatientRootQueryRetrieveInformationModelMove):
            if status is None:
                raise ConquestTimeout(f"Local PACS at {cfg.host}:{cfg.port} did not respond to C-MOVE in time.")
            if status.Status in (0xFF00, 0xFF01):
                continue
            final = MoveResult(
                completed=getattr(status, "NumberOfCompletedSuboperations", 0) or 0,
                failed=getattr(status, "NumberOfFailedSuboperations", 0) or 0,
                warning=getattr(status, "NumberOfWarningSuboperations", 0) or 0,
                status_code=status.Status,
            )
    except ConquestError:
        raise
    except Exception as e:
        raise ConquestOperationFailed(f"Local PACS C-MOVE failed unexpectedly: {e}") from e
    finally:
        assoc.release()
    return final
