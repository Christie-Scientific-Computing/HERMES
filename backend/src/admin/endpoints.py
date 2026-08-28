"""
Admin compliance dashboard endpoint (Phase 4,
docs/plans/frontend-rewrite-implementation-plan.md §6).

Gated by verify_internal_key only, same as every other project-gated
router -- the backend has no role/staff concept of its own (HermesDB has no
user table, see backend/src/projects/db_client.py's own docstring). Staff
gating happens in frontend_fastapi/routers/admin.py's require_data_custodian
dependency; this endpoint deliberately does not try to re-derive it, since
there's nothing here to derive it FROM.
"""
from fastapi import APIRouter, Depends, Query

from backend.src.projects.db_client import ProjectsDB
from backend.src.projects.enforcement import verify_internal_key
from backend.src.status.audit_chain_db import AuditChainDB
from backend.src.status.db_client import StatusDB

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(verify_internal_key)])

projects_db = ProjectsDB()
status_db = StatusDB()
audit_chain_db = AuditChainDB()


@router.get("/overview")
async def admin_overview(within_days: int = Query(30), limit: int = Query(50)):
    return {
        "expiring_projects": projects_db.list_expiring_projects(within_days=within_days),
        "recent_jobs": status_db.list_recent_jobs_with_counts(limit=limit),
        "audit_chain_check": audit_chain_db.latest_check(),
    }
