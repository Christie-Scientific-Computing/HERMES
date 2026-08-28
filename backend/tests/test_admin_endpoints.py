"""
Integration tests for the admin overview endpoint (backend/src/admin/endpoints.py,
Phase 4). Doesn't need the PinnacleExport submodule: this router never
imports retrieve/logic.py.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.admin import endpoints as admin_endpoints
from backend.src.status.audit_chain_db import AuditChainDB
from backend.src.status.db_client import StatusDB


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(admin_endpoints.router)
    return TestClient(app)


def test_admin_overview_returns_all_three_sections(client, active_project):
    project_id, username = active_project
    job_id = f"admin-overview-test-{uuid.uuid4()}"
    StatusDB().create_job(job_id, created_by=username, project_id=project_id)
    AuditChainDB().record_check(ok=True)

    resp = client.get("/admin/overview")

    assert resp.status_code == 200
    body = resp.json()
    assert "expiring_projects" in body
    assert "recent_jobs" in body
    assert "audit_chain_check" in body
    assert any(j["job_id"] == job_id for j in body["recent_jobs"])
    assert body["audit_chain_check"]["ok"] is True


def test_admin_overview_expiring_projects_respects_within_days_param(client):
    from backend.src.projects.db_client import ProjectsDB

    projects_db = ProjectsDB()
    project_id = str(uuid.uuid4())
    owner = f"owner-{uuid.uuid4()}"
    projects_db.create_project(project_id, "Expiring soon", owner)
    projects_db.submit_project(project_id, owner)
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    projects_db.review_project(project_id, approved=True, reviewer="admin", expiry_date=soon)

    resp = client.get("/admin/overview?within_days=10")
    assert any(p["project_id"] == project_id for p in resp.json()["expiring_projects"])

    resp = client.get("/admin/overview?within_days=1")
    assert not any(p["project_id"] == project_id for p in resp.json()["expiring_projects"])
