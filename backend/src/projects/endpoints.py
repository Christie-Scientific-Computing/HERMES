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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.src.projects.db_client import ProjectsDB, ProjectNotFoundError
from backend.src.projects.enforcement import verify_internal_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(verify_internal_key)])

projects_db = ProjectsDB()


class CreateProjectRequest(BaseModel):
    title: str
    created_by: str
    description: Optional[str] = None
    ethics_reference: Optional[str] = None


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
    try:
        projects_db.create_project(
            project_id, body.title, body.created_by,
            description=body.description, ethics_reference=body.ethics_reference,
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
    return projects_db.get_project(project_id)


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
