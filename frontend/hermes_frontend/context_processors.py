"""
Injects the logged-in user's active (approved, non-expired) projects into
every template context, so the nav's "no active projects" banner can render
without every view having to fetch it itself.

There is no "current project" concept -- project selection happens
explicitly, per submission, on the form that actually starts a job (see
jobs/forms.py's project_id fields and jobs/views.py's per-request choice
population). This context processor is purely for the banner.
"""
from hermes_frontend import backend_client


def active_projects(request):
    if not request.user.is_authenticated:
        return {}
    try:
        projects = backend_client.list_user_active_projects(request.user.username)
    except backend_client.BackendError:
        projects = []
    return {"nav_active_projects": projects}
