"""
F005 -- idempotent bootstrap for the sentinel research_projects row every
local-PACS-move audit record is tagged with (project_id="localPACSTransfer").

Mirrors frontend_fastapi/backend_client.py's own
_find_or_create_superuser_bypass_project pattern (see that module), but done
directly against ProjectsDB rather than over HTTP -- this code already runs
inside the backend process, so there's no self-call to make. create_project
adds its creator ("system") as an 'owner' member same as any other project,
but -- unlike the superuser bypass project -- no real user is EVER added as
a member of this one: it exists only to satisfy jobs.project_id's foreign
key. Authorization for a local-PACS move is entirely require_data_custodian,
checked in frontend_fastapi before this endpoint is ever called (plan.md
D005); nothing here ever calls add_member for anyone else.
"""
from datetime import datetime, timezone

from backend.src.projects.db_client import ProjectNotFoundError, ProjectsDB

SENTINEL_PROJECT_ID = "localPACSTransfer"
_SENTINEL_SYSTEM_USER = "system"
# Approving requires a non-null expiry_date -- there's no "never expires"
# option through ProjectsDB, so a far-future sentinel stands in for one,
# same as the superuser bypass project.
_FAR_FUTURE_EXPIRY = datetime(9999, 12, 31, tzinfo=timezone.utc)


def ensure_sentinel_project(projects_db: ProjectsDB) -> str:
    """Create+submit+approve the sentinel project on first-ever use; a
    no-op on every subsequent call. Two processes racing on the very first
    call is a tolerated, harmless duplicate-effort race (each step is
    itself idempotent or ignores "already past this stage"), not something
    that needs a hard lock -- identical reasoning to the superuser bypass
    project's own docstring."""
    if projects_db.is_project_active(SENTINEL_PROJECT_ID):
        return SENTINEL_PROJECT_ID

    # create_project's own INSERT is ON CONFLICT DO NOTHING, so a racing
    # process having already created the row is harmless here.
    projects_db.create_project(
        SENTINEL_PROJECT_ID,
        title="Local PACS Transfer (system)",
        created_by=_SENTINEL_SYSTEM_USER,
        description=(
            "Sentinel project satisfying jobs.project_id for custodian-initiated local-PACS "
            "(Conquest) relays. No real user is ever added as a member, and membership here "
            "authorizes nothing -- the actual authorization is require_data_custodian, checked "
            "in frontend_fastapi before this project is ever referenced."
        ),
        ethics_reference=SENTINEL_PROJECT_ID,
    )
    try:
        projects_db.submit_project(SENTINEL_PROJECT_ID, _SENTINEL_SYSTEM_USER)
    except ProjectNotFoundError:
        pass  # already submitted/approved by a racing process
    try:
        projects_db.review_project(
            SENTINEL_PROJECT_ID, approved=True, reviewer=_SENTINEL_SYSTEM_USER,
            comment="Auto-approved: sentinel project for local-PACS transfer audit records",
            expiry_date=_FAR_FUTURE_EXPIRY,
        )
    except ProjectNotFoundError:
        pass  # already approved by a racing process
    return SENTINEL_PROJECT_ID
