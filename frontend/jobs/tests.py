"""
View/template tests for the job patient table and the patient detail page.

The backend is stubbed at the hermes_frontend.backend_client boundary -- these
assert the Django half (filtering, row flattening, template rendering, access
control), not the API itself, which has its own tests under backend/tests/.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
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
     "status": None, "outcome": "failure", "error_message": "nothing found",
     "mosaiq_reason": "Not found in Mosaiq", "pinnacle_reason": "Not found in Pinnacle export index",
     "proknow_reason": "Patient not found on ProKnow"},
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

    def test_headline_imported_stat_rendered(self):
        """job_summary's new imported_count/submitted_count (§E) surface as
        the "N / M ... imported" headline stat above the summary table."""
        self.backend.job_summary.return_value = {
            "summary": [], "project_id": PROJECT_ID,
            "imported_count": 3, "submitted_count": 4,
        }
        resp = self._get()
        self.assertContains(resp, "3 / 4")

    def test_headline_imported_stat_omitted_when_backend_does_not_supply_it(self):
        """Older/stubbed responses without imported_count/submitted_count
        (e.g. this test module's default job_summary stub) must not blow up
        the template -- the stat block simply doesn't render."""
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "imported</span>")


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

    def test_reason_text_rendered_for_missing_source(self):
        """§E: per-source reason strings (from job_patients_summary) render
        as visible text on the patient detail page, not just a tooltip --
        1002's SUMMARY row carries reasons for all three sources being
        False."""
        resp = self.client.get(reverse("jobs:patient_detail", args=[JOB_ID, "1002"]))
        self.assertContains(resp, "Not found in Mosaiq")
        self.assertContains(resp, "Not found in Pinnacle export index")
        self.assertContains(resp, "Patient not found on ProKnow")

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


class CollectDataQueueTests(_StubbedBackend):
    """
    collect_data posts straight to backend_client.batch_import_file
    (docs/worker-queue-design.md) -- for both the single-patient and batch
    (CSV upload) modes. No more session staging: every submission is fully
    handed off to the backend before this view returns.
    """

    def setUp(self):
        super().setUp()
        self.backend.list_user_active_projects.return_value = [{"project_id": PROJECT_ID, "title": "P"}]
        self.backend.batch_import_file.return_value = {"job_id": "enqueued-job-id", "total": 1}

    def test_single_mode_enqueues_and_stays_on_page(self):
        resp = self.client.post(reverse("jobs:collect_data"), {
            "mode": "single", "mrn": "1001", "import_level": "Planning data", "project_id": PROJECT_ID,
        })
        self.assertEqual(resp.status_code, 200)  # single mode re-renders inline, doesn't redirect

        self.backend.batch_import_file.assert_called_once()
        kwargs = self.backend.batch_import_file.call_args.kwargs
        self.assertEqual(kwargs["filename"], "single_patient.csv")
        self.assertEqual(kwargs["content"], b"patient_id\n1001\n")
        self.assertEqual(kwargs["project_id"], PROJECT_ID)
        self.assertEqual(kwargs["username"], self.username)
        self.assertEqual(kwargs["import_level"], "Planning data")

        # nothing staged -- there's no session dance to check anymore
        self.assertEqual([k for k in self.client.session.keys() if k.startswith("pending_job:")], [])

    def test_batch_mode_enqueues_and_redirects_to_watch(self):
        csv_file = SimpleUploadedFile("patients.csv", b"patient_id\n1001\n1002\n", content_type="text/csv")
        resp = self.client.post(reverse("jobs:collect_data"), {
            "mode": "batch", "file": csv_file, "import_level": "Planning data", "project_id": PROJECT_ID,
        })

        kwargs = self.backend.batch_import_file.call_args.kwargs
        self.assertEqual(kwargs["filename"], "patients.csv")
        self.assertEqual(kwargs["content"], b"patient_id\n1001\n1002\n")
        # _enqueue_batch_job mints job_id client-side and hands it to the
        # backend; the backend's queue path just echoes it back, so the
        # redirect must target whatever job_id was actually sent, not a
        # value read back out of the (mocked) response.
        self.assertRedirects(resp, reverse("jobs:job_watch", args=[kwargs["job_id"]]),
                              fetch_redirect_response=False)

    def test_batch_mode_backend_error_shows_message_not_redirect(self):
        self.backend.batch_import_file.side_effect = _FakeBackendError("Could not read CSV")
        csv_file = SimpleUploadedFile("patients.csv", b"garbage", content_type="text/csv")
        resp = self.client.post(reverse("jobs:collect_data"), {
            "mode": "batch", "file": csv_file, "import_level": "Planning data", "project_id": PROJECT_ID,
        })
        self.assertEqual(resp.status_code, 200)  # re-renders the form, no redirect
        self.assertContains(resp, "Could not read CSV")


class RetrieveDataQueueTests(_StubbedBackend):
    """retrieve_data's DICOM/ProKnow export tabs, converted the same way
    collect_data's import tab was -- see CollectDataQueueTests."""

    def setUp(self):
        super().setUp()
        self.backend.list_user_active_projects.return_value = [{"project_id": PROJECT_ID, "title": "P"}]
        self.backend.get_orthanc_modalities.return_value = ["AE1"]
        self.backend.get_proknow_collections.return_value = ["Collection1"]
        self.backend.dicom_move_file.return_value = {"job_id": "enqueued-dicom-job", "total": 1}
        self.backend.proknow_upload_file.return_value = {"job_id": "enqueued-proknow-job", "total": 1}

    def test_dicom_mode_enqueues_and_redirects_to_watch(self):
        csv_file = SimpleUploadedFile("patients.csv", b"patient_id\n1001\n", content_type="text/csv")
        resp = self.client.post(reverse("jobs:retrieve_data"), {
            "mode": "dicom", "file": csv_file, "destination": "AE1", "project_id": PROJECT_ID,
        })
        kwargs = self.backend.dicom_move_file.call_args.kwargs
        self.assertEqual(kwargs["filename"], "patients.csv")
        self.assertEqual(kwargs["content"], b"patient_id\n1001\n")
        self.assertEqual(kwargs["destination"], "AE1")
        self.assertEqual(kwargs["username"], self.username)
        # Left blank -- must reach backend_client as None, not "" or missing.
        self.assertIsNone(kwargs["message_id"])
        self.assertRedirects(resp, reverse("jobs:job_watch", args=[kwargs["job_id"]]),
                              fetch_redirect_response=False)

    def test_dicom_mode_with_message_id_passes_it_through(self):
        """Clinical-trial export path: a message_id in the form must reach
        backend_client.dicom_move_file as a real int (docs on
        Exporter.dicom_c_move -- forwarded to Orthanc as MoveOriginatorID)."""
        csv_file = SimpleUploadedFile("patients.csv", b"patient_id\n1001\n", content_type="text/csv")
        resp = self.client.post(reverse("jobs:retrieve_data"), {
            "mode": "dicom", "file": csv_file, "destination": "AE1", "project_id": PROJECT_ID,
            "message_id": "51966",
        })
        kwargs = self.backend.dicom_move_file.call_args.kwargs
        self.assertEqual(kwargs["message_id"], 51966)
        self.assertRedirects(resp, reverse("jobs:job_watch", args=[kwargs["job_id"]]),
                              fetch_redirect_response=False)

    def test_dicom_mode_rejects_message_id_outside_dicom_us_range(self):
        csv_file = SimpleUploadedFile("patients.csv", b"patient_id\n1001\n", content_type="text/csv")
        resp = self.client.post(reverse("jobs:retrieve_data"), {
            "mode": "dicom", "file": csv_file, "destination": "AE1", "project_id": PROJECT_ID,
            "message_id": "70000",
        })
        self.assertEqual(resp.status_code, 200)  # re-renders the form, no redirect
        self.backend.dicom_move_file.assert_not_called()

    def test_proknow_mode_enqueues_and_redirects_to_watch(self):
        csv_file = SimpleUploadedFile("patients.csv", b"patient_id\n1001\n", content_type="text/csv")
        resp = self.client.post(reverse("jobs:retrieve_data"), {
            "mode": "proknow", "file": csv_file, "collection": "Collection1", "project_id": PROJECT_ID,
        })
        kwargs = self.backend.proknow_upload_file.call_args.kwargs
        self.assertEqual(kwargs["filename"], "patients.csv")
        self.assertEqual(kwargs["collection"], "Collection1")
        self.assertRedirects(resp, reverse("jobs:job_watch", args=[kwargs["job_id"]]),
                              fetch_redirect_response=False)

    def test_dicom_mode_backend_error_shows_message_not_redirect(self):
        self.backend.dicom_move_file.side_effect = _FakeBackendError("Could not read CSV")
        csv_file = SimpleUploadedFile("patients.csv", b"garbage", content_type="text/csv")
        resp = self.client.post(reverse("jobs:retrieve_data"), {
            "mode": "dicom", "file": csv_file, "destination": "AE1", "project_id": PROJECT_ID,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Could not read CSV")


class ImportExportDataQueueTests(_StubbedBackend):
    """
    jobs:import_export_data -- the combined one-stop-shop job. All four tabs
    post to backend_client.combined_import_export_file (same enqueue-then-
    redirect pattern as CollectDataQueueTests/RetrieveDataQueueTests); this
    view is purely additive, so it shares no code path with collect_data/
    retrieve_data's own views/forms.
    """

    def setUp(self):
        super().setUp()
        self.backend.list_user_active_projects.return_value = [{"project_id": PROJECT_ID, "title": "P"}]
        self.backend.get_orthanc_modalities.return_value = ["AE1"]
        self.backend.get_proknow_collections.return_value = ["Collection1"]
        self.backend.combined_import_export_file.return_value = {"job_id": "enqueued-combined-job", "total": 1}

    def test_single_dicom_mode_enqueues_and_stays_on_page(self):
        resp = self.client.post(reverse("jobs:import_export_data"), {
            "mode": "single_dicom", "mrn": "1001", "import_level": "Planning data",
            "destination": "AE1", "project_id": PROJECT_ID,
        })
        self.assertEqual(resp.status_code, 200)  # single mode re-renders inline, doesn't redirect

        kwargs = self.backend.combined_import_export_file.call_args.kwargs
        self.assertEqual(kwargs["filename"], "single_patient.csv")
        self.assertEqual(kwargs["content"], b"patient_id\n1001\n")
        self.assertEqual(kwargs["import_level"], "Planning data")
        self.assertEqual(kwargs["export_kind"], "dicom_move")
        self.assertEqual(kwargs["destination_or_collection"], "AE1")
        self.assertIsNone(kwargs["message_id"])
        self.assertEqual(kwargs["username"], self.username)

    def test_single_dicom_mode_with_message_id_passes_it_through(self):
        resp = self.client.post(reverse("jobs:import_export_data"), {
            "mode": "single_dicom", "mrn": "1001", "import_level": "Planning data",
            "destination": "AE1", "message_id": "51966", "project_id": PROJECT_ID,
        })
        self.assertEqual(resp.status_code, 200)
        kwargs = self.backend.combined_import_export_file.call_args.kwargs
        self.assertEqual(kwargs["message_id"], 51966)

    def test_single_proknow_mode_enqueues_and_stays_on_page(self):
        resp = self.client.post(reverse("jobs:import_export_data"), {
            "mode": "single_proknow", "mrn": "1001", "import_level": "Planning data",
            "collection": "Collection1", "project_id": PROJECT_ID,
        })
        self.assertEqual(resp.status_code, 200)
        kwargs = self.backend.combined_import_export_file.call_args.kwargs
        self.assertEqual(kwargs["export_kind"], "proknow_upload")
        self.assertEqual(kwargs["destination_or_collection"], "Collection1")

    def test_batch_dicom_mode_enqueues_and_redirects_to_watch(self):
        csv_file = SimpleUploadedFile("patients.csv", b"patient_id\n1001\n1002\n", content_type="text/csv")
        resp = self.client.post(reverse("jobs:import_export_data"), {
            "mode": "batch_dicom", "file": csv_file, "import_level": "Planning data",
            "destination": "AE1", "project_id": PROJECT_ID,
        })
        kwargs = self.backend.combined_import_export_file.call_args.kwargs
        self.assertEqual(kwargs["filename"], "patients.csv")
        self.assertEqual(kwargs["content"], b"patient_id\n1001\n1002\n")
        self.assertEqual(kwargs["export_kind"], "dicom_move")
        self.assertEqual(kwargs["destination_or_collection"], "AE1")
        self.assertRedirects(resp, reverse("jobs:job_watch", args=[kwargs["job_id"]]),
                              fetch_redirect_response=False)

    def test_batch_proknow_mode_enqueues_and_redirects_to_watch(self):
        csv_file = SimpleUploadedFile("patients.csv", b"patient_id\n1001\n", content_type="text/csv")
        resp = self.client.post(reverse("jobs:import_export_data"), {
            "mode": "batch_proknow", "file": csv_file, "import_level": "Planning data",
            "collection": "Collection1", "project_id": PROJECT_ID,
        })
        kwargs = self.backend.combined_import_export_file.call_args.kwargs
        self.assertEqual(kwargs["export_kind"], "proknow_upload")
        self.assertEqual(kwargs["destination_or_collection"], "Collection1")
        self.assertRedirects(resp, reverse("jobs:job_watch", args=[kwargs["job_id"]]),
                              fetch_redirect_response=False)

    def test_batch_dicom_mode_rejects_message_id_outside_dicom_us_range(self):
        csv_file = SimpleUploadedFile("patients.csv", b"patient_id\n1001\n", content_type="text/csv")
        resp = self.client.post(reverse("jobs:import_export_data"), {
            "mode": "batch_dicom", "file": csv_file, "import_level": "Planning data",
            "destination": "AE1", "project_id": PROJECT_ID, "message_id": "70000",
        })
        self.assertEqual(resp.status_code, 200)  # re-renders the form, no redirect
        self.backend.combined_import_export_file.assert_not_called()

    def test_batch_dicom_mode_backend_error_shows_message_not_redirect(self):
        self.backend.combined_import_export_file.side_effect = _FakeBackendError("Could not read CSV")
        csv_file = SimpleUploadedFile("patients.csv", b"garbage", content_type="text/csv")
        resp = self.client.post(reverse("jobs:import_export_data"), {
            "mode": "batch_dicom", "file": csv_file, "import_level": "Planning data",
            "destination": "AE1", "project_id": PROJECT_ID,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Could not read CSV")

    def test_dashboard_links_to_import_export_data(self):
        resp = self.client.get(reverse("jobs:dashboard"))
        self.assertContains(resp, reverse("jobs:import_export_data"))


class JobWatchTests(_StubbedBackend):
    """job_watch's live visibility check (_user_can_watch_job, mirroring
    job_detail's _job_is_visible_to) -- re-checked on every request rather
    than trusting anything from submission time."""

    def test_visible_job_renders(self):
        self.backend.job_summary.return_value = {"summary": [], "project_id": PROJECT_ID}
        resp = self.client.get(reverse("jobs:job_watch", args=["queued-job-id"]))
        self.assertEqual(resp.status_code, 200)

    def test_unknown_job_404s(self):
        self.backend.job_summary.side_effect = _FakeBackendError("not found")
        resp = self.client.get(reverse("jobs:job_watch", args=["nonexistent-job-id"]))
        self.assertEqual(resp.status_code, 404)

    def test_job_outside_the_users_projects_404s(self):
        self.user.is_staff = False
        self.user.save()
        self.backend.job_summary.return_value = {"summary": [], "project_id": "someone-elses-project"}
        self.backend.list_projects.return_value = [{"project_id": PROJECT_ID, "title": "P"}]
        resp = self.client.get(reverse("jobs:job_watch", args=["someone-elses-job-id"]))
        self.assertEqual(resp.status_code, 404)

    def test_combined_job_renders_two_stage_progress_component(self):
        """job_summary's is_combined picks c-combined-job-progress (two
        bars) over the single-stage c-job-progress."""
        self.backend.job_summary.return_value = {
            "summary": [], "project_id": PROJECT_ID, "is_combined": True,
        }
        resp = self.client.get(reverse("jobs:job_watch", args=["combined-job-id"]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="import-bar-combined-job-id"')
        # is_combined is read off the same job_summary call the visibility
        # check already made -- must not be a second round trip.
        self.backend.job_summary.assert_called_once()
        self.assertContains(resp, 'id="export-bar-combined-job-id"')

    def test_plain_job_renders_single_stage_progress_component(self):
        self.backend.job_summary.return_value = {
            "summary": [], "project_id": PROJECT_ID, "is_combined": False,
        }
        resp = self.client.get(reverse("jobs:job_watch", args=["plain-job-id"]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="progress-bar-plain-job-id"')
        self.assertNotContains(resp, 'id="import-bar-plain-job-id"')

    def test_job_summary_missing_is_combined_key_defaults_to_plain(self):
        """Backward compatibility: an older/stubbed job_summary response
        with no is_combined key at all must not crash job_watch."""
        self.backend.job_summary.return_value = {"summary": [], "project_id": PROJECT_ID}
        resp = self.client.get(reverse("jobs:job_watch", args=["queued-job-id"]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="progress-bar-queued-job-id"')


class JobStreamObserverTests(_StubbedBackend):
    """job_stream relays the backend's observer stream (GET
    /results/job/{job_id}/stream)."""

    def setUp(self):
        super().setUp()

        async def _fake_stream_sse(path):
            self.stream_sse_call = {"path": path}
            yield b'data: {"type": "start", "total": 1}\n\n'
            yield b'data: {"type": "done"}\n\n'

        self.backend.stream_sse = _fake_stream_sse
        self.stream_sse_call = None

    def test_relays_observer_stream(self):
        from asgiref.sync import async_to_sync

        self.backend.job_summary.return_value = {"summary": [], "project_id": PROJECT_ID}
        resp = self.client.get(reverse("jobs:job_stream", args=["queued-job-id"]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/event-stream")

        async def _collect(aiter):
            return b"".join([chunk async for chunk in aiter])

        body = async_to_sync(_collect)(resp.streaming_content).decode()

        self.assertEqual(self.stream_sse_call["path"], "/results/job/queued-job-id/stream")
        self.assertIn("event: start", body)
        self.assertIn("event: done", body)

    def test_unknown_job_404s(self):
        self.backend.job_summary.side_effect = _FakeBackendError("not found")
        resp = self.client.get(reverse("jobs:job_stream", args=["nonexistent-job-id"]))
        self.assertEqual(resp.status_code, 404)
