"""
Endpoints for the ethics/research-project approval workflow.

This is HermesDB-owned state (see backend/alembic/versions/8aa3a51c978c_*),
not Django-local -- Django (the sole caller, per CLAUDE.md's frontend
architecture) is a thin client of this router. `username` fields throughout
are plain Django usernames, trusted from the caller the same way `mrn`/
`created_by` already are elsewhere in this backend -- hardened by
`verify_internal_key` (backend/src/projects/enforcement.py) rather than by
any auth of its own, since this backend has none.
"""
import logging
import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.src.identity import anon
from backend.src.notifications.db_client import NotificationsDB
from backend.src.projects.db_client import ProjectsDB, ProjectNotFoundError
from backend.src.projects.enforcement import verify_internal_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(verify_internal_key)])

projects_db = ProjectsDB()
notifications_db = NotificationsDB()


class DestinationIn(BaseModel):
    destination_type: Literal["dicom", "proknow"]
    destination_value: str


class CreateProjectRequest(BaseModel):
    title: str
    created_by: str
    description: Optional[str] = None
    ethics_reference: Optional[str] = None
    destinations: Optional[list[DestinationIn]] = None
    message_id: Optional[int] = None


class RequestedPatientsRequest(BaseModel):
    added_by: str
    mrns: list[str]


class ProposeAmendmentRequest(BaseModel):
    proposed_by: str
    destinations: Optional[list[DestinationIn]] = None
    message_id: Optional[int] = None


class AmendmentDecisionRequest(BaseModel):
    reviewed_by: str
    comment: Optional[str] = None


class SubmitProjectRequest(BaseModel):
    username: str


class ReviewProjectRequest(BaseModel):
    reviewer: str
    approved: bool
    comment: Optional[str] = None
    expiry_date: Optional[datetime] = None


class AddMemberRequest(BaseModel):
    username: str
    role: str = "member"
    added_by: str


class RevokeProjectRequest(BaseModel):
    revoked_by: str
    comment: Optional[str] = None


@router.post("")
async def create_project(body: CreateProjectRequest):
    project_id = str(uuid.uuid4())
    destinations = [d.model_dump() for d in body.destinations] if body.destinations else None
    try:
        projects_db.create_project(
            project_id, body.title, body.created_by,
            description=body.description, ethics_reference=body.ethics_reference,
            destinations=destinations, message_id=body.message_id,
        )
    except Exception as e:
        logger.exception("Failed to create project")
        raise HTTPException(status_code=500, detail=str(e))
    return projects_db.get_project(project_id)


@router.post("/{project_id}/submit")
async def submit_project(project_id: str, body: SubmitProjectRequest):
    if not projects_db.is_member(project_id, body.username):
        raise HTTPException(status_code=403, detail="Only a project member may submit it for review")
    try:
        projects_db.submit_project(project_id, body.username)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return projects_db.get_project(project_id)


@router.post("/{project_id}/review")
async def review_project(project_id: str, body: ReviewProjectRequest):
    if body.approved and body.expiry_date is None:
        raise HTTPException(status_code=422, detail="expiry_date is required when approving a project")
    try:
        projects_db.review_project(
            project_id, body.approved, body.reviewer,
            comment=body.comment, expiry_date=body.expiry_date,
        )
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    project = projects_db.get_project(project_id)
    # Notify every CURRENT member (not just whoever created/submitted it) --
    # membership can change between submission and review, and anyone
    # currently on the project needs to know its access just changed.
    # Best-effort: a notification-write failure must not undo or mask an
    # already-committed review decision.
    decision = "approved" if body.approved else "rejected"
    for member in projects_db.list_members(project_id):
        try:
            notifications_db.create(
                member["username"], kind="project_reviewed",
                message=f"Project {project.get('title', project_id)!r} was {decision}.",
                project_id=project_id,
            )
        except Exception:
            logger.exception("Failed to notify %r of project %r review decision", member["username"], project_id)
    return project


@router.post("/{project_id}/revoke")
async def revoke_project(project_id: str, body: RevokeProjectRequest):
    try:
        projects_db.revoke_project(project_id, body.revoked_by, comment=body.comment)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return projects_db.get_project(project_id)


@router.get("")
async def list_projects(username: Optional[str] = Query(None), status: Optional[str] = Query(None)):
    return {"projects": projects_db.list_projects(username=username, status=status)}


@router.get("/pending_amendments")
async def pending_amendments():
    # Must stay registered before GET /{project_id} below -- FastAPI/
    # Starlette matches routes in registration order, and /{project_id}
    # would otherwise swallow this path, treating "pending_amendments" as
    # a project_id.
    return {"projects": projects_db.list_pending_amendments()}


@router.get("/{project_id}")
async def get_project(project_id: str):
    try:
        project = projects_db.get_project(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        **project,
        "members": projects_db.list_members(project_id),
        "audit_log": projects_db.list_audit_log(project_id),
        # Both active and proposed rows -- the frontend tells them apart by
        # `status` (a proposed row's mere presence IS the pending-amendment
        # signal, so no separate "has_pending_amendment" field is needed here).
        "destinations": projects_db.list_destinations(project_id),
    }


@router.post("/{project_id}/members")
async def add_member(project_id: str, body: AddMemberRequest):
    if not projects_db.is_member(project_id, body.added_by):
        raise HTTPException(status_code=403, detail="Only an existing project member may add members")
    projects_db.add_member(project_id, body.username, role=body.role, added_by=body.added_by)
    return {"members": projects_db.list_members(project_id)}


@router.delete("/{project_id}/members/{username}")
async def remove_member(project_id: str, username: str, removed_by: str = Query(...)):
    if not projects_db.is_member(project_id, removed_by):
        raise HTTPException(status_code=403, detail="Only an existing project member may remove members")
    projects_db.remove_member(project_id, username, removed_by=removed_by)
    return {"members": projects_db.list_members(project_id)}


@router.get("/{project_id}/jobs")
async def project_jobs(project_id: str):
    return {"project_id": project_id, "jobs": projects_db.list_project_jobs(project_id)}


@router.post("/{project_id}/requested_patients")
async def add_requested_patients(project_id: str, body: RequestedPatientsRequest):
    if not projects_db.is_member(project_id, body.added_by):
        raise HTTPException(status_code=403, detail="Only an existing project member may add requested patients")
    # Every other mrn-accepting endpoint resolves inbound anon ids to real
    # ones before writing to HermesDB (see identity/anon.py's module
    # docstring) -- project_requested_patients.mrn must hold the same real
    # id every other mrn column does, or item 04's "requested vs imported"
    # stat could never join against events.mrn.
    try:
        real_id_map = anon.resolve_real_ids(body.mrns)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except anon.AnonServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    count = projects_db.add_requested_patients(project_id, [real_id_map[m] for m in body.mrns])
    return {"added": count}


@router.post("/{project_id}/amendments")
async def propose_amendment(project_id: str, body: ProposeAmendmentRequest):
    if not projects_db.is_member(project_id, body.proposed_by):
        raise HTTPException(status_code=403, detail="Only an existing project member may propose an amendment")
    if projects_db.has_pending_amendment(project_id):
        raise HTTPException(status_code=409, detail="An amendment is already pending review for this project")
    destinations = [d.model_dump() for d in body.destinations] if body.destinations else None
    try:
        projects_db.propose_amendment(
            project_id, body.proposed_by, destinations=destinations, message_id=body.message_id,
        )
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to propose amendment")
        raise HTTPException(status_code=500, detail=str(e))
    return projects_db.get_project(project_id)


@router.post("/{project_id}/amendments/approve")
async def approve_amendment(project_id: str, body: AmendmentDecisionRequest):
    if not projects_db.has_pending_amendment(project_id):
        raise HTTPException(status_code=404, detail="No amendment pending review for this project")
    projects_db.approve_amendment(project_id, body.reviewed_by, comment=body.comment)
    project = projects_db.get_project(project_id)
    # Same "notify every current member" reasoning as review_project above.
    for member in projects_db.list_members(project_id):
        try:
            notifications_db.create(
                member["username"], kind="amendment_reviewed",
                message=f"An amendment to {project.get('title', project_id)!r} was approved.",
                project_id=project_id,
            )
        except Exception:
            logger.exception("Failed to notify %r of project %r amendment approval", member["username"], project_id)
    return project


@router.post("/{project_id}/amendments/reject")
async def reject_amendment(project_id: str, body: AmendmentDecisionRequest):
    if not projects_db.has_pending_amendment(project_id):
        raise HTTPException(status_code=404, detail="No amendment pending review for this project")
    projects_db.reject_amendment(project_id, body.reviewed_by, comment=body.comment)
    project = projects_db.get_project(project_id)
    for member in projects_db.list_members(project_id):
        try:
            notifications_db.create(
                member["username"], kind="amendment_reviewed",
                message=f"An amendment to {project.get('title', project_id)!r} was rejected.",
                project_id=project_id,
            )
        except Exception:
            logger.exception("Failed to notify %r of project %r amendment rejection", member["username"], project_id)
    return project
