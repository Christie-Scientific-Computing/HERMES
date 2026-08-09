"""
View/template tests for the job patient table and the patient detail page.

The backend is stubbed at the hermes_frontend.backend_client boundary -- these
assert the Django half (filtering, row flattening, template rendering, access
control), not the API itself, which has its own tests under backend/tests/.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

JOB_ID = "job-abc"
PROJECT_ID = "proj-1"

PATIENTS = ["1001", "1002", "1003", "1004"]
SUMMARY = [
    # found everywhere, succeeded
    {"mrn": "1001", "in_mosaiq": True, "in_pinnacle": True, "in_proknow": True,
     "status": "imported", "outcome": "success", "error_message": None},
    # found nowhere, failed
    {"mrn": "1002", "in_mosaiq": False, "in_pinnacle": False, "in_proknow": False,
     "status": None, "outcome": "failure", "error_message": "nothing found"},
    # missing from pinnacle only
    {"mrn": "1003", "in_mosaiq": True, "in_pinnacle": False, "in_proknow": True,
     "status": "imported", "outcome": "success", "error_message": None},
    # never checked -- export-only patient
    {"mrn": "1004", "in_mosaiq": None, "in_pinnacle": None, "in_proknow": None,
     "status": None, "outcome": "success", "error_message": None},
]


class _StubbedBackend(TestCase):
    """Shared setUp: a logged-in staff user and a stubbed backend_client."""

    username = "tester"

    def setUp(self):
        self.user = User.objects.create_user(self.username, password="pw", is_staff=True)
        self.client.force_login(self.user)

        patcher = mock.patch("jobs.views.backend_client")
        self.backend = patcher.start()
        self.addCleanup(patcher.stop)

        # base.html's nav banner is fed by a context processor that holds its
        # own reference to backend_client, so it needs stubbing too or every
        # render tries a real HTTP call.
        nav_patcher = mock.patch(
            "hermes_frontend.context_processors.backend_client.list_user_active_projects",
            return_value=[{"project_id": PROJECT_ID, "title": "P"}],
        )
        nav_patcher.start()
        self.addCleanup(nav_patcher.stop)

        # The real BackendError is what views catch; the mock needs a real
        # exception class or `except backend_client.BackendError` blows up.
        self.backend.BackendError = _FakeBackendError
        self.backend.job_summary.return_value = {"summary": [], "project_id": PROJECT_ID}
        self.backend.job_patients.return_value = {"patients": PATIENTS}
        self.backend.job_patients_summary.return_value = {"patients": SUMMARY}
        self.backend.list_projects.return_value = [{"project_id": PROJECT_ID, "title": "P"}]


class _FakeBackendError(Exception):
    def __init__(self, detail=""):
        self.detail = detail
        super().__init__(detail)


class PatientTableTests(_StubbedBackend):
    def _get(self, **params):
        return self.client.get(reverse("jobs:job_detail", args=[JOB_ID]), params)

    def test_unfiltered_shows_every_patient(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["mrn"] for r in resp.context["rows"]], PATIENTS)

    def test_failed_filter(self):
        resp = self._get(filter="failed")
        self.assertEqual([r["mrn"] for r in resp.context["rows"]], ["1002"])

    def test_not_found_filter_requires_all_three_to_be_false(self):
        resp = self._get(filter="not_found")
        self.assertEqual([r["mrn"] for r in resp.context["rows"]], ["1002"])

    def test_missing_pinnacle_filter(self):
        resp = self._get(filter="missing_pinnacle")
        self.assertEqual(sorted(r["mrn"] for r in resp.context["rows"]), ["1002", "1003"])

    def test_unknown_source_is_not_treated_as_missing(self):
        """1004 is None for every source -- never checked, not absent. It must
        not appear under not_found or any missing_* filter."""
        for f in ("not_found", "missing_mosaiq", "missing_pinnacle", "missing_proknow"):
            resp = self._get(filter=f)
            self.assertNotIn("1004", [r["mrn"] for r in resp.context["rows"]], f)

    def test_pill_counts_come_from_unfiltered_rows(self):
        resp = self._get(filter="failed")
        counts = {p["key"]: p["count"] for p in resp.context["pills"]}
        self.assertEqual(counts[""], 4)  # still the full total, not the filtered one
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["missing_pinnacle"], 2)

    def test_patient_mrn_links_to_patient_detail(self):
        resp = self._get()
        self.assertContains(resp, reverse("jobs:patient_detail", args=[JOB_ID, "1001"]))

    def test_job_outside_the_users_projects_is_refused(self):
        self.user.is_staff = False
        self.user.save()
        self.backend.list_projects.return_value = [{"project_id": "someone-else", "title": "X"}]
        resp = self._get()
        self.assertRedirects(resp, reverse("jobs:dashboard"), fetch_redirect_response=False)


class PatientDetailTests(_StubbedBackend):
    username = "tester2"

    def setUp(self):
        super().setUp()
        self.backend.patient_timeline.return_value = {"events": [
            {"ts": "2026-08-01", "stage": "retrieve", "event_type": "failure",
             "attempt": 1, "error_message": "boom", "details": None},
        ]}
        self.backend.patient_plans.return_value = {"available": True, "plans": [
            {"id": 1, "path": "/p/1001/Plan_1", "plan_id": 1, "plan_name": "Prostate",
             "plan_date": "2026-03-01", "primary_image_set": 2, "pinnacle_version": "16.2",
             "comment": None, "status": "exported", "error_message": None},
            {"id": 2, "path": "/p/1001/Plan_2", "plan_id": 2, "plan_name": "Boost",
             "plan_date": None, "primary_image_set": None, "pinnacle_version": None,
             "comment": "retry", "status": "failed", "error_message": "no RTSTRUCT"},
        ]}

    def _get(self, **params):
        return self.client.get(reverse("jobs:patient_detail", args=[JOB_ID, "1001"]), params)

    def test_renders_plans_and_timeline(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Prostate")
        self.assertContains(resp, "no RTSTRUCT")
        self.assertContains(resp, "boom")

    def test_status_filter(self):
        resp = self._get(status="failed")
        self.assertEqual([p["plan_name"] for p in resp.context["plans"]], ["Boost"])
        self.assertNotContains(resp, "Prostate")

    def test_status_pills_derived_from_data_with_stable_counts(self):
        resp = self._get(status="failed")
        counts = {p["key"]: p["count"] for p in resp.context["status_pills"]}
        self.assertEqual(counts, {"": 2, "exported": 1, "failed": 1})

    def test_unavailable_plans_render_a_notice_not_an_empty_table(self):
        self.backend.patient_plans.return_value = {"available": False, "plans": []}
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn't set up for this deployment yet")
        self.assertContains(resp, "boom")  # timeline still rendered

    def test_plans_failure_does_not_blank_the_timeline(self):
        self.backend.patient_plans.side_effect = _FakeBackendError("backend down")
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "boom")
        self.assertContains(resp, "backend down")

    def test_timeline_failure_does_not_blank_the_plans(self):
        self.backend.patient_timeline.side_effect = _FakeBackendError("backend down")
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Prostate")

    def test_job_outside_the_users_projects_is_refused(self):
        self.user.is_staff = False
        self.user.save()
        self.backend.list_projects.return_value = [{"project_id": "someone-else", "title": "X"}]
        resp = self._get()
        self.assertRedirects(resp, reverse("jobs:dashboard"), fetch_redirect_response=False)
