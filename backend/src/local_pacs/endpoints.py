"""
F005 -- records a local-PACS (Conquest) move, performed entirely inside
frontend_fastapi (F004), into HermesDB's normal jobs/patients/events audit
trail. Mirrors error_reports/endpoints.py's shape: verify_internal_key only,
no per-user role check, because HermesDB has no user/role table to check
against -- the real authorization (require_data_custodian) already happened
in frontend_fastapi before this endpoint is ever called (plan.md D005).

This is the only place in the local-PACS plan that touches HermesDB
directly -- F001-F004 have no direct HermesDB access, only this endpoint
(called over HTTP via frontend_fastapi's backend_client.py).
"""
import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.src.identity import anon
from backend.src.local_pacs.db_client import ensure_sentinel_project
from backend.src.projects.db_client import ProjectsDB
from backend.src.projects.enforcement import verify_internal_key
from backend.src.status.db_client import StatusDB

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/local_pacs", tags=["local_pacs"], dependencies=[Depends(verify_internal_key)])

projects_db = ProjectsDB()
status_db = StatusDB()


class RecordMoveRequest(BaseModel):
    anon_patient_id: str
    study_instance_uid: str
    destination_ae_title: str
    destination_display_name: str
    performed_by: str
    outcome: Literal["success", "failure"]
    detail: Optional[str] = None


@router.post("/audit_move")
async def audit_move(body: RecordMoveRequest):
    try:
        real_mrn = anon.resolve_real_id(body.anon_patient_id)
    except anon.AnonLookupError as e:
        raise HTTPException(status_code=422, detail=str(e))

    project_id = ensure_sentinel_project(projects_db)

    job_id = str(uuid.uuid4())
    status_db.create_job(
        job_id, description=f"Local PACS move to {body.destination_display_name}",
        created_by=body.performed_by, project_id=project_id,
    )
    status_db.add_patient(job_id, real_mrn)
    status_db.add_event(
        job_id, real_mrn, stage="export", event_type=body.outcome,
        error_message=body.detail if body.outcome == "failure" else None,
        details={
            "destination": body.destination_ae_title,
            "destination_display_name": body.destination_display_name,
            "destination_type": "local_pacs_relay",
            "study_instance_uid": body.study_instance_uid,
            "detail": body.detail,
        },
    )
    return {"ok": True, "job_id": job_id}
