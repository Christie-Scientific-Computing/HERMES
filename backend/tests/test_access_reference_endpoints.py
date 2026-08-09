"""
Confirms backend/src/access/endpoints.py's admin/reference lookup routes
(/access/reference/orthanc_modalities, /access/reference/proknow_collections)
are NOT gated by project membership -- unlike export/endpoints.py's
get_orthanc_modalities/get_proknow_collections, which 403 for a caller who
isn't an active member of any project.

This is a regression test for a real bug found by review: accounts/views.py's
user_access page (a staff-only admin page for managing someone ELSE's export
allow-list) originally populated its "add destination" dropdown via the
export-facing, membership-gated endpoints, called with the *viewing admin's*
own username. A data-custodian admin who reviews/administers other users'
access is a distinct role from "researcher who is an active project member"
in this app, and routinely isn't one -- so the dropdown silently came back
empty for exactly the admins most likely to use the page. The fix moved this
lookup to its own admin/reference routes, authorized by verify_internal_key
(this router's dependency) plus Django's own is_staff gate on the call site,
not project membership.

Builds a real FastAPI TestClient against both the access and export routers
(no mocked backend_client -- see frontend/accounts/tests.py for that side),
following test_export_anon_boundary.py's pattern of mocking only the
external Orthanc/ProKnow clients.
"""
import os

# backend.src.access.endpoints imports backend.src.export.endpoints, which
# imports backend.src.identity.anon -- and anon.py freezes ANON_DB_* as
# module-level constants at *import* time (see its own docstring/header).
# This file happens to alphabetically collect before test_anon.py/
# test_export_anon_boundary.py, so without setting these first, anon.py
# would get imported (transitively, via the access router import below)
# with ANON_DB_HOST unset and "is_configured() -> False" frozen for the rest
# of the test session, breaking every other anon-boundary test that runs
# after this file regardless of what they set later. Match the same
# bootstrap those files already do, even though this file itself never
# exercises anon translation.
os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.access import endpoints as access_endpoints
from backend.src.export import endpoints as export_endpoints


class _FakeCollection:
    def __init__(self, name):
        self.name = name


class _FakeCollections:
    def query(self, workspace):
        return [_FakeCollection("CollectionA"), _FakeCollection("CollectionB")]


class _FakeProKnow:
    def __init__(self, *args, **kwargs):
        self.collections = _FakeCollections()


class _FakeOrthanc:
    def __init__(self, *args, **kwargs):
        pass

    def get_modalities(self):
        return ["AE_ONE", "AE_TWO"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(export_endpoints, "Orthanc", _FakeOrthanc)
    monkeypatch.setattr(export_endpoints, "ProKnow", _FakeProKnow)

    app = FastAPI()
    app.include_router(access_endpoints.router)
    app.include_router(export_endpoints.router)
    return TestClient(app)


def test_reference_orthanc_modalities_does_not_require_project_membership(client):
    # Deliberately not the active_project fixture -- this user has no
    # project membership at all, which is exactly the case that used to 403.
    resp = client.get("/access/reference/orthanc_modalities")
    assert resp.status_code == 200
    assert resp.json() == ["AE_ONE", "AE_TWO"]


def test_reference_proknow_collections_does_not_require_project_membership(client):
    resp = client.get("/access/reference/proknow_collections")
    assert resp.status_code == 200
    assert resp.json() == ["CollectionA", "CollectionB"]


def test_export_get_orthanc_modalities_still_requires_membership_for_contrast(client):
    """
    Regression guard the other direction: the export-facing endpoint (used
    to populate the actual export form, for a user about to export data
    themselves) must still require active project membership -- only the
    new /access/reference/* admin routes bypass it.
    """
    non_member = f"nonmember-{uuid.uuid4()}"
    resp = client.get("/export/get_orthanc_modalities", params={"username": non_member})
    assert resp.status_code == 403


def test_export_get_proknow_collections_still_requires_membership_for_contrast(client):
    non_member = f"nonmember-{uuid.uuid4()}"
    resp = client.get("/export/get_proknow_collections", params={"username": non_member})
    assert resp.status_code == 403
