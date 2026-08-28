"""
Proves backend/src/common/errors.py's register_pii_safe_exception_handlers
actually redacts HTTPException.detail before it reaches the HTTP response.

Per docs/plans/pii-boundary-test-suite.md §D: a handful of representative
cases -- one each from retrieve/, export/, studies/, results/,
projects/endpoints.py, and identity/anon.py -- forcing a real-MRN/path/
date-bearing exception through the handler, not all ~34 raise sites
individually. The handler is one mechanism (backend/main.py registers it
once), so proving it works for a representative sample from each module
is sufficient; it isn't specific to any one endpoint.

Each test builds its own single-router FastAPI() app (matching the existing
test_*_anon_boundary.py house style) and calls
register_pii_safe_exception_handlers on it directly -- exercising exactly
what backend/main.py wires up, without needing the full app (every router,
every other module's env vars) constructed for a test that only cares about
one endpoint.
"""
import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:test@localhost:55432/hermes_test")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.common.errors import register_pii_safe_exception_handlers
from backend.src.identity import anon

# A message shaped like what a real underlying exception routinely quotes:
# a real MRN, a server filesystem path, and a calendar date, all in one
# string -- if the handler's generic pii_patterns.redact() floor is wired
# up, none of these three should survive into the HTTP response.
REAL_MRN = "500123"
_LEAK_SHAPED_MESSAGE = (
    f"no RTSTRUCT for patient {REAL_MRN} at /pinnacle/{REAL_MRN}/Plan_1/dose.dcm, "
    "planned 2026-01-15"
)


def _assert_leak_shaped_message_redacted(resp):
    """
    Checks the path and date are gone -- NOT that the bare MRN itself is
    gone. The handler is generic-pattern-only (dates/UIDs/paths/secrets),
    with no real-id-aware substitution (docs/plans/pii-boundary-test-suite.md
    §F, explicitly out of scope: the handler has no request-scoped real id
    to substitute). A 6-digit MRN with no surrounding date/path/UID
    structure doesn't match any of those patterns on its own, so it's an
    accepted residual gap here, not a bug -- callers that already know
    their own real_id/display_id pair (e.g. results/endpoints.py's _scrub)
    still do precise substitution on top of this floor.
    """
    detail = resp.json()["detail"]
    assert "/pinnacle/" not in detail
    assert "2026-01-15" not in detail
    assert "[redacted]" in detail


def test_retrieve_find_patient_exception_is_redacted(active_project, monkeypatch):
    from backend.src.retrieve import endpoints as retrieve_endpoints

    project_id, username = active_project

    # Force passthrough for this test's own anon.resolve_real_id(mrn) call,
    # regardless of whether some other test module already flipped
    # anon.ANON_DB_HOST truthy at import time (identity/anon is a
    # process-wide singleton module, so that leaks across test files that
    # don't scope it back down) -- REAL_MRN below is meant to be treated as
    # a real id reaching Importer, not looked up as an anon id.
    monkeypatch.setattr(anon, "ANON_DB_HOST", None)

    class _ExplodingImporter:
        def __init__(self, import_level):
            pass

        def find_patient(self, real_mrn):
            raise RuntimeError(_LEAK_SHAPED_MESSAGE)

    monkeypatch.setattr(retrieve_endpoints, "Importer", _ExplodingImporter)

    app = FastAPI()
    register_pii_safe_exception_handlers(app)
    app.include_router(retrieve_endpoints.router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/import/find_patient", params={"mrn": REAL_MRN, "username": username})
    assert resp.status_code == 500
    _assert_leak_shaped_message_redacted(resp)


def test_export_get_orthanc_modalities_exception_is_redacted(active_project, monkeypatch):
    from backend.src.export import endpoints as export_endpoints

    project_id, username = active_project

    def _exploding_orthanc(*args, **kwargs):
        raise RuntimeError(_LEAK_SHAPED_MESSAGE)

    monkeypatch.setattr(export_endpoints, "Orthanc", _exploding_orthanc)

    app = FastAPI()
    register_pii_safe_exception_handlers(app)
    app.include_router(export_endpoints.router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/export/get_orthanc_modalities", params={"username": username})
    assert resp.status_code == 502
    _assert_leak_shaped_message_redacted(resp)


def test_studies_list_exception_is_redacted(monkeypatch):
    from backend.src.studies import endpoints as studies_endpoints

    def _exploding_orthanc(method, path, **kwargs):
        raise RuntimeError(_LEAK_SHAPED_MESSAGE)

    monkeypatch.setattr(studies_endpoints, "_orthanc", _exploding_orthanc)

    app = FastAPI()
    register_pii_safe_exception_handlers(app)
    app.include_router(studies_endpoints.router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/studies")
    assert resp.status_code == 502
    _assert_leak_shaped_message_redacted(resp)


def test_results_job_summary_exception_is_redacted(monkeypatch):
    from backend.src.results import endpoints as results_endpoints

    def _exploding_summarize_job(job_id):
        raise RuntimeError(_LEAK_SHAPED_MESSAGE)

    monkeypatch.setattr(results_endpoints.status_db, "summarize_job", _exploding_summarize_job)

    app = FastAPI()
    register_pii_safe_exception_handlers(app)
    app.include_router(results_endpoints.router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(f"/results/job/job-{uuid.uuid4()}")
    assert resp.status_code == 500
    _assert_leak_shaped_message_redacted(resp)


def test_projects_create_exception_is_redacted(monkeypatch):
    from backend.src.projects import endpoints as projects_endpoints

    def _exploding_create_project(*args, **kwargs):
        raise RuntimeError(_LEAK_SHAPED_MESSAGE)

    monkeypatch.setattr(projects_endpoints.projects_db, "create_project", _exploding_create_project)

    app = FastAPI()
    register_pii_safe_exception_handlers(app)
    app.include_router(projects_endpoints.router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/projects", json={"title": "t", "created_by": "alice"})
    assert resp.status_code == 500
    _assert_leak_shaped_message_redacted(resp)


def test_anon_service_error_propagated_through_results_is_redacted(monkeypatch):
    """
    identity/anon.py's own AnonServiceError, raised when the anon-mapping DB
    itself is unreachable, wraps whatever the underlying psycopg2 error says
    -- which routinely includes a host:port (backend/src/common/pii_patterns.py's
    SECRET_LIKE_PATTERNS). results/endpoints.py's patient_plans converts it
    straight into `HTTPException(503, detail=str(e))`, the same as every
    other anon.AnonServiceError call site -- proving the handler catches an
    exception that originates in anon.py, not just ones raised directly in
    an endpoint module.
    """
    from backend.src.results import endpoints as results_endpoints

    # anon.py reads ANON_DB_HOST into a module-level constant at import
    # time, so setting the env var this late wouldn't take effect (the
    # module is already imported, cached in sys.modules, by the time this
    # test runs) -- patching the constant directly is what actually flips
    # is_configured() to True for this test only.
    monkeypatch.setattr(anon, "ANON_DB_HOST", "localhost")

    def _exploding_get_pool():
        raise RuntimeError(
            "could not connect to server: Connection refused at db.internal:5432"
        )

    monkeypatch.setattr(anon, "_get_pool", _exploding_get_pool)

    app = FastAPI()
    register_pii_safe_exception_handlers(app)
    app.include_router(results_endpoints.router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(f"/results/patient/{REAL_MRN}/plans")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "db.internal:5432" not in detail
    assert "[redacted]" in detail
