"""
Endpoints for the per-user export destination allow-list
(docs/safety-plan.md §A). An admin restricts which Orthanc modalities /
ProKnow collections a given user may export to, independent of project
membership (which governs *whether* someone may export at all, not
*where* -- see backend/src/projects/enforcement.py's
`require_allowed_destination`, the dependency actually enforcing this on
the export endpoints).

No staff/admin check in the backend itself -- same posture as the
research_projects review endpoints (backend/src/projects/endpoints.py),
which trust Django to have already gated on `is_staff` before calling in.
This backend has no auth of its own; `verify_internal_key` is what makes
"only Django calls this" enforced rather than just topological.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.src.access.db_client import AccessDB
from backend.src.export.endpoints import fetch_orthanc_modalities, fetch_proknow_collections
from backend.src.projects.enforcement import verify_internal_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/access", tags=["access"], dependencies=[Depends(verify_internal_key)])

access_db = AccessDB()

_VALID_DESTINATION_TYPES = ("dicom_modality", "proknow_collection")


class AddDestinationRequest(BaseModel):
    destination_type: str
    destination: str
    added_by: str


@router.get("/reference/orthanc_modalities")
async def reference_orthanc_modalities():
    """
    Every registered Orthanc modality -- populates the "add destination"
    dropdown on accounts/views.py's user_access page. Deliberately NOT
    gated by require_any_active_project the way export/endpoints.py's
    get_orthanc_modalities is: the caller here is a staff admin managing
    someone else's allow-list, not exporting themselves, and routinely
    isn't a project member at all (a data-custodian/admin role is distinct
    from "researcher who exports data" in this app). verify_internal_key
    (this router's dependency) plus Django's own is_staff gate
    (_is_data_custodian, enforced before this endpoint is ever called) is
    the intended authorization here -- same posture as list_access/
    add_access/remove_access above.
    """
    return fetch_orthanc_modalities()


@router.get("/reference/proknow_collections")
async def reference_proknow_collections():
    """ProKnow counterpart to reference_orthanc_modalities -- see its docstring."""
    return fetch_proknow_collections()


@router.get("/{username}")
async def list_access(username: str):
    return {"username": username, "destinations": access_db.list_for_user(username)}


@router.post("/{username}")
async def add_access(username: str, body: AddDestinationRequest):
    if body.destination_type not in _VALID_DESTINATION_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"destination_type must be one of {_VALID_DESTINATION_TYPES}",
        )
    access_db.add(username, body.destination_type, body.destination, added_by=body.added_by)
    return {"username": username, "destinations": access_db.list_for_user(username)}


@router.delete("/{username}/{destination_id}")
async def remove_access(username: str, destination_id: int):
    access_db.remove(username, destination_id)
    return {"username": username, "destinations": access_db.list_for_user(username)}
