"""
Per-user notification endpoints (Phase 4,
docs/plans/frontend-rewrite-implementation-plan.md §6). Population is
entirely internal (backend/worker.py's job-completion hook,
backend/src/projects/endpoints.py's review_project) -- this router is
read/acknowledge only, no POST to create one directly.

Not staff-gated: every user reads and acknowledges their own notifications,
unlike the admin overview router. `username` is trusted from the caller the
same way it is everywhere else in this backend (verify_internal_key is what
makes "only the frontend calls this" enforced, not the caller's identity
itself) -- there is no user table/auth here to check it against.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.src.notifications.db_client import NotificationsDB
from backend.src.projects.enforcement import verify_internal_key

router = APIRouter(prefix="/notifications", tags=["notifications"], dependencies=[Depends(verify_internal_key)])

notifications_db = NotificationsDB()


@router.get("")
async def list_notifications(username: str = Query(...), unread_only: bool = Query(False), limit: int = Query(20)):
    return {"notifications": notifications_db.list_for_user(username, unread_only=unread_only, limit=limit)}


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: int, username: str = Query(...)):
    if not notifications_db.mark_read(notification_id, username):
        raise HTTPException(status_code=404, detail="No such unread notification for this user")
    return {"ok": True}
