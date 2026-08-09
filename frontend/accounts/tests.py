"""
Tests for accounts/views.py's user_access page (safety-plan §A): a
staff-only view for managing a single user's export destination allow-list.
The backend is stubbed at the hermes_frontend.backend_client boundary --
these assert the Django half (access control, form handling, template
rendering), not the API itself, which has its own tests under
backend/tests/. Follows frontend/jobs/tests.py's pattern for stubbing
backend_client and the nav's context-processor call.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

TARGET_USERNAME = "restricted_user"

DESTINATIONS = [
    {"id": 1, "username": TARGET_USERNAME, "destination_type": "dicom_modality",
     "destination": "AE_ONE", "added_by": "admin", "added_at": "2026-08-01T00:00:00Z"},
    {"id": 2, "username": TARGET_USERNAME, "destination_type": "proknow_collection",
     "destination": "SomeCollection", "added_by": "admin", "added_at": "2026-08-02T00:00:00Z"},
]


class _FakeBackendError(Exception):
    def __init__(self, detail=""):
        self.detail = detail
        super().__init__(detail)


class _StubbedBackend(TestCase):
    """Shared setUp: a logged-in staff user, a target user to manage access
    for, and a stubbed backend_client."""

    def setUp(self):
        self.staff_user = User.objects.create_user("staffer", password="pw", is_staff=True)
        self.target_user = User.objects.create_user(TARGET_USERNAME, password="pw", is_staff=False)

        patcher = mock.patch("accounts.views.backend_client")
        self.backend = patcher.start()
        self.addCleanup(patcher.stop)
        self.backend.BackendError = _FakeBackendError

        # base.html's nav banner is fed by a context processor that holds its
        # own reference to backend_client -- stub it too, same as jobs/tests.py.
        nav_patcher = mock.patch(
            "hermes_frontend.context_processors.backend_client.list_user_active_projects",
            return_value=[],
        )
        nav_patcher.start()
        self.addCleanup(nav_patcher.stop)

        self.backend.get_orthanc_modalities.return_value = ["AE_ONE", "AE_TWO"]
        self.backend.get_proknow_collections.return_value = ["SomeCollection", "OtherCollection"]
        self.backend.list_access.return_value = DESTINATIONS
        self.backend.add_access.return_value = DESTINATIONS
        self.backend.remove_access.return_value = []


class UserAccessAccessControlTests(_StubbedBackend):
    def test_non_staff_user_is_redirected_not_shown_the_page(self):
        non_staff = User.objects.create_user("plain_user", password="pw", is_staff=False)
        self.client.force_login(non_staff)
        resp = self.client.get(reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("accounts:login"), resp.url)

    def test_anonymous_user_is_redirected(self):
        resp = self.client.get(reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("accounts:login"), resp.url)

    def test_staff_user_can_view_the_page(self):
        self.client.force_login(self.staff_user)
        resp = self.client.get(reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.assertEqual(resp.status_code, 200)


class UserAccessPageRenderingTests(_StubbedBackend):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff_user)

    def test_renders_current_destinations(self):
        resp = self.client.get(reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.assertContains(resp, "AE_ONE")
        self.assertContains(resp, "SomeCollection")

    def test_renders_unrestricted_notice_when_no_destinations(self):
        self.backend.list_access.return_value = []
        resp = self.client.get(reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.assertContains(resp, "may export to any registered destination")

    def test_destination_dropdown_includes_live_modalities_and_collections(self):
        resp = self.client.get(reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.assertContains(resp, "AE_TWO")
        self.assertContains(resp, "OtherCollection")

    def test_modalities_error_is_shown_but_page_still_renders(self):
        self.backend.get_orthanc_modalities.side_effect = _FakeBackendError("orthanc down")
        resp = self.client.get(reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "orthanc down")
        # ProKnow collections still render even though Orthanc failed.
        self.assertContains(resp, "SomeCollection")


class UserAccessAddFormTests(_StubbedBackend):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff_user)

    def test_posting_a_dicom_destination_calls_add_access_with_split_type_and_value(self):
        resp = self.client.post(
            reverse("accounts:user_access", args=[TARGET_USERNAME]),
            {"destination": "dicom_modality:AE_TWO"},
        )
        self.assertRedirects(resp, reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.backend.add_access.assert_called_once_with(
            TARGET_USERNAME, "dicom_modality", "AE_TWO", added_by="staffer",
        )

    def test_posting_a_proknow_destination_calls_add_access_with_split_type_and_value(self):
        resp = self.client.post(
            reverse("accounts:user_access", args=[TARGET_USERNAME]),
            {"destination": "proknow_collection:OtherCollection"},
        )
        self.assertRedirects(resp, reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.backend.add_access.assert_called_once_with(
            TARGET_USERNAME, "proknow_collection", "OtherCollection", added_by="staffer",
        )

    def test_posting_an_invalid_destination_value_does_not_call_the_backend(self):
        resp = self.client.post(
            reverse("accounts:user_access", args=[TARGET_USERNAME]),
            {"destination": "not-a-valid-choice"},
        )
        self.assertEqual(resp.status_code, 200)  # re-renders with form errors, no redirect
        self.backend.add_access.assert_not_called()

    def test_backend_error_on_add_is_surfaced_as_a_message(self):
        self.backend.add_access.side_effect = _FakeBackendError("duplicate")
        resp = self.client.post(
            reverse("accounts:user_access", args=[TARGET_USERNAME]),
            {"destination": "dicom_modality:AE_TWO"},
            follow=True,
        )
        self.assertContains(resp, "duplicate")


class UserAccessRemoveFormTests(_StubbedBackend):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff_user)

    def test_posting_remove_calls_remove_access_and_redirects(self):
        resp = self.client.post(
            reverse("accounts:user_access_remove", args=[TARGET_USERNAME, 1]),
        )
        self.assertRedirects(resp, reverse("accounts:user_access", args=[TARGET_USERNAME]))
        self.backend.remove_access.assert_called_once_with(TARGET_USERNAME, 1)

    def test_remove_denied_for_non_staff(self):
        non_staff = User.objects.create_user("plain_user2", password="pw", is_staff=False)
        self.client.force_login(non_staff)
        resp = self.client.post(
            reverse("accounts:user_access_remove", args=[TARGET_USERNAME, 1]),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("accounts:login"), resp.url)
        self.backend.remove_access.assert_not_called()

    def test_backend_error_on_remove_is_surfaced_as_a_message(self):
        self.backend.remove_access.side_effect = _FakeBackendError("not found")
        resp = self.client.post(
            reverse("accounts:user_access_remove", args=[TARGET_USERNAME, 1]),
            follow=True,
        )
        self.assertContains(resp, "not found")
