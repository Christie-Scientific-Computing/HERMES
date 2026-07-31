"""
Injects the logged-in user's active (approved, non-expired) projects and
their current project-switcher selection into every template context, so
the nav bar can render it without every view having to fetch it itself.

The *authoritative* re-check -- "is the session's current project still
actually active right now" -- happens at job-creation time in jobs/views.py,
not here; this context processor is just for display/navigation.
"""
from hermes_frontend import backend_client


def active_projects(request):
    if not request.user.is_authenticated:
        return {}
    try:
        projects = backend_client.list_user_active_projects(request.user.username)
    except backend_client.BackendError:
        projects = []
    current_id = request.session.get("current_project_id")
    current = next((p for p in projects if p["project_id"] == current_id), None)
    if current is None and projects:
        current = projects[0]
        request.session["current_project_id"] = current["project_id"]
    return {"nav_active_projects": projects, "nav_current_project": current}
