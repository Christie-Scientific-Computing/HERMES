"""
View/template tests for the project detail page's review-decision banner
(safety-plan.md §C).

The backend is stubbed at the hermes_frontend.backend_client boundary --
these assert the Django/template half (banner presence, gated correctly by
status and staff-ness), not the API itself, which has its own tests under
backend/tests/. Follows jobs/tests.py's existing pattern.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

PROJECT_ID = "proj-1"

BANNER_TEXT = (
    "Approving this project lets its members export pseudo-anonymised data "
    "for any patient, in any volume, for as long as the approval is active. "
    "There is currently no way to limit exports to a specific cohort — "
    "membership is the only control in place."
)


class _FakeBackendError(Exception):
    def __init__(self, detail=""):
        self.detail = detail
        super().__init__(detail)


def _project(status="submitted"):
    return {
        "project_id": PROJECT_ID,
        "title": "Test Project",
        "description": "A test project.",
        "ethics_reference": "ETH-123",
        "status": status,
        "members": [{"username": "tester", "role": "owner"}],
        "audit_log": [],
    }


class ProjectDetailReviewBannerTests(TestCase):
    def setUp(self):
        patcher = mock.patch("research_projects.views.backend_client")
        self.backend = patcher.start()
        self.addCleanup(patcher.stop)
        self.backend.BackendError = _FakeBackendError
        self.backend.list_project_jobs.return_value = []

        # base.html's nav banner is fed by a context processor holding its
        # own reference to backend_client -- stub it too or every render
        # tries a real HTTP call.
        nav_patcher = mock.patch(
            "hermes_frontend.context_processors.backend_client.list_user_active_projects",
            return_value=[],
        )
        nav_patcher.start()
        self.addCleanup(nav_patcher.stop)

    def _get(self):
        return self.client.get(reverse("research_projects:detail", args=[PROJECT_ID]))

    def test_banner_shown_for_submitted_project_to_staff(self):
        user = User.objects.create_user("staffer", password="pw", is_staff=True)
        self.client.force_login(user)
        self.backend.get_project.return_value = _project(status="submitted")

        resp = self._get()

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, BANNER_TEXT)

    def test_banner_absent_when_project_not_submitted(self):
        user = User.objects.create_user("staffer2", password="pw", is_staff=True)
        self.client.force_login(user)
        self.backend.get_project.return_value = _project(status="approved")

        resp = self._get()

        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, BANNER_TEXT)

    def test_banner_absent_when_viewer_is_not_staff(self):
        user = User.objects.create_user("plain", password="pw", is_staff=False)
        self.client.force_login(user)
        self.backend.get_project.return_value = _project(status="submitted")

        resp = self._get()

        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, BANNER_TEXT)
