"""
Enforcement for the ethics/project coarse-access-control model, plus the
internal shared-secret check that makes "only Django calls this backend"
an enforced invariant rather than just a network-topology assumption.

Fail-closed discipline: unlike StatusDB bookkeeping elsewhere in this
codebase (backend/src/common/sse.py's run_batch_job logs-and-continues on
DB errors, since that's best-effort event recording), every check here
DENIES on any error. An authorization gate that silently allows on a DB
hiccup isn't a gate.

Callers must invoke these as the first thing in an endpoint -- before any
CSV parsing, anon-id resolution, or StatusDB/ProjectsDB writes -- so a
rejected request never does partial work first.
"""
import os
import logging

from fastapi import Header, HTTPException

from backend.src.projects.db_client import ProjectsDB

logger = logging.getLogger(__name__)

projects_db = ProjectsDB()

_INTERNAL_KEY = os.getenv("HERMES_INTERNAL_KEY")


def verify_internal_key(x_hermes_internal_key: str | None = Header(default=None)) -> None:
    """
    FastAPI dependency: require a shared secret on every project-gated
    endpoint (import/export/projects routers). If HERMES_INTERNAL_KEY isn't
    configured this is a no-op, matching the rest of the codebase's
    env-var-driven opt-in style (e.g. ANON_DB_HOST) -- but any deployment
    where the frontend is reachable from outside the secured network should
    set it, since without it "only Django calls the backend" is topology,
    not something the backend itself verifies.
    """
    if _INTERNAL_KEY is None:
        return
    if x_hermes_internal_key != _INTERNAL_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid internal service key")


def require_any_active_project(username: str) -> None:
    """
    Gate for read-only/reference endpoints (find_patient, get_orthanc_modalities,
    get_proknow_collections): caller must have *some* active approved project
    membership, not a specific one.
    """
    try:
        ok = projects_db.has_any_active_project(username)
    except Exception:
        logger.exception("Could not check project membership for %r; denying", username)
        raise HTTPException(status_code=503, detail="Could not verify project membership")
    if not ok:
        raise HTTPException(status_code=403, detail="No active approved project membership")


def require_project_member(project_id: str, username: str) -> None:
    """
    Gate for data-moving endpoints (import/export): caller must be an active
    member of THIS specific approved, non-expired project.
    """
    try:
        ok = projects_db.is_active_member(project_id, username)
    except Exception:
        logger.exception("Could not check membership of %r in %r; denying", username, project_id)
        raise HTTPException(status_code=503, detail="Could not verify project membership")
    if not ok:
        raise HTTPException(status_code=403, detail="Not an active member of an approved project")
