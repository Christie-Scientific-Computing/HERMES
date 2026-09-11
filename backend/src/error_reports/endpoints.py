"""
Error report / suggestion endpoints (item 06,
docs/plans/feature-round-implementation-plan.md). Mirrors
notifications/endpoints.py's shape: POST to create (from
frontend_fastapi/routers/error_reports.py's report form), GET to list (the
admin log, gated by require_data_custodian on the frontend side -- see that
router's module docstring for why the actual access control lives there,
not here: HermesDB has no user/role table to check against).

No email dispatch here even for `urgent=True` reports -- frontend_fastapi's
SMTP isn't wired up yet (see the plan doc's item 06), `urgent` is just
recorded for now.
"""
from typing import Literal, Optional

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.src.error_reports.db_client import ErrorReportsDB
from backend.src.projects.enforcement import verify_internal_key

router = APIRouter(prefix="/error_reports", tags=["error_reports"], dependencies=[Depends(verify_internal_key)])

error_reports_db = ErrorReportsDB()


class CreateErrorReportRequest(BaseModel):
    username: str
    category: Literal["feedback", "error"]
    message: str
    urgent: bool = False
    job_id: Optional[str] = None


@router.post("")
async def create_error_report(body: CreateErrorReportRequest):
    try:
        error_reports_db.create(
            body.username, body.category, body.message, urgent=body.urgent, job_id=body.job_id,
        )
    except psycopg2.errors.ForeignKeyViolation:
        # job_id is free-typed by the reporting user (see ErrorReportForm),
        # unlike every other job_id-bearing write in this codebase -- a
        # typo shouldn't surface as an opaque 500.
        raise HTTPException(status_code=422, detail=f"No such job: {body.job_id!r}")
    return {"ok": True}


@router.get("")
async def list_error_reports(limit: int = Query(100), unaddressed_only: bool = Query(False)):
    return {"error_reports": error_reports_db.list_all(limit=limit, unaddressed_only=unaddressed_only)}


@router.post("/{report_id}/resolve")
async def resolve_error_report(report_id: int, username: str = Query(...)):
    if not error_reports_db.mark_addressed(report_id, username):
        raise HTTPException(status_code=404, detail="No such unaddressed error report")
    return {"ok": True}
