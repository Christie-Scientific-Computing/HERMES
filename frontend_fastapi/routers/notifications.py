"""
Notification acknowledgement (Phase 4,
docs/plans/frontend-rewrite-implementation-plan.md §6). Not staff-gated --
require_login is the whole gate, same as any other page: every user manages
their own notifications, unlike routers/admin.py's overview page. The
backend itself scopes mark_read to (notification_id, username) together
(backend/src/notifications/db_client.py's NotificationsDB.mark_read), so one
user marking another's notification read fails there regardless of what
this router does -- this route's own require_login just keeps an entirely
anonymous request from reaching that far.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from frontend_fastapi import backend_client
from frontend_fastapi.deps import require_login
from frontend_fastapi.models import User

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: int, request: Request, user: User = Depends(require_login)):
    try:
        await backend_client.mark_notification_read(notification_id, user.username)
    except backend_client.BackendError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    # Always back to the dashboard, not an attacker-influenceable Referer --
    # the dropdown is ambient (rendered in base.html on every page), so
    # there's no single "come back to this page" to preserve anyway.
    return RedirectResponse(request.url_for("dashboard"), status_code=303)
