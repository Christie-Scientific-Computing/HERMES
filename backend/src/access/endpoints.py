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
from backend.src.projects.enforcement import verify_internal_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/access", tags=["access"], dependencies=[Depends(verify_internal_key)])

access_db = AccessDB()

_VALID_DESTINATION_TYPES = ("dicom_modality", "proknow_collection")


class AddDestinationRequest(BaseModel):
    destination_type: str
    destination: str
    added_by: str


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


@router.delete("/{username}/{id}")
async def remove_access(username: str, id: int):
    access_db.remove(username, id)
    return {"username": username, "destinations": access_db.list_for_user(username)}
