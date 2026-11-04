"""F-13: Report an issue from a member's Zinwe window and from the staff admin; owners see only their own reports."""
from __future__ import annotations

import base64
import shutil
import tempfile

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status

from z_auth.models import Issue, User

from .tests import DeviceTestCase

URL = "/api/devices/issues/"
ADMIN = "/api/admin/issues/"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class IssueSourceTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()  # throttle counters
        media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, media, True)
        self.as_device(self.enroll().json()["device_token"])

    def as_staff(self) -> User:
        staff = User.objects.filter(email="ops@zinwe.local").first() or User.objects.create_superuser(
            "ops@zinwe.local", "ops-12345678")
        self.client.credentials()
        self.client.force_authenticate(staff)
        return staff

    def test_member_report_with_a_screenshot(self) -> None:
        response = self.client.post(URL, {
            "description": "  Tasks list is empty  ", "page": "Tasks", "context": {"viewport": "380x640", "secret": "x"},
            "screenshot_b64": base64.b64encode(PNG).decode(),
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        issue = Issue.objects.get(pk=response.json()["id"])
        self.assertEqual(list(response.json()), ["id"])
        self.assertEqual((issue.owner, issue.source, issue.member, issue.device.hostname, issue.contact_ok),
                         (self.owner, "member", self.member, "ACCOUNTS-PC-01", False))
        self.assertEqual((issue.description, issue.page), ("Tasks list is empty", "Tasks"))
        self.assertEqual(issue.context["viewport"], "380x640")
        self.assertNotIn("secret", issue.context)
        self.assertEqual(issue.context["app"], "Zinwe window")
        self.assertTrue(issue.screenshot.name.endswith(".png"))
        self.assertEqual(issue.screenshot.read(), PNG)

    def test_member_report_checks(self) -> None:
        for body in ({}, {"description": " "}, {"description": "x", "screenshot_b64": "not base64!"},
                     {"description": "x", "screenshot_b64": base64.b64encode(b"GIF89a....").decode()}):
            self.assertEqual(self.client.post(URL, body, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        with override_settings(ZINWE_ISSUE_SCREENSHOT_MAX_BYTES=32):
            big = base64.b64encode(PNG).decode()
            self.assertEqual(self.client.post(URL, {"description": "x", "screenshot_b64": big}, format="json").status_code,
                             status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Issue.objects.exists())
        self.client.credentials()
        self.assertEqual(self.client.post(URL, {"description": "x"}, format="json").status_code,
                         status.HTTP_401_UNAUTHORIZED)

    def test_staff_report_from_the_admin(self) -> None:
        staff = self.as_staff()
        response = self.client.post(f"{ADMIN}report/", {
            "description": "Pricing page shows the old price", "page": "/pricing",
            "screenshot": SimpleUploadedFile("shot.png", PNG, content_type="image/png"),
        }, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        self.assertEqual((body["source"], body["reporter_email"], body["customer"], body["member_name"]),
                         ("staff", "ops@zinwe.local", None, None))
        issue = Issue.objects.get()
        self.assertEqual((issue.owner, issue.reporter, issue.contact_ok), (None, staff, False))
        self.assertTrue(issue.screenshot)
        # Nobody to email: a reply is refused, Fixed sends nothing.
        self.assertEqual(self.client.post(f"{ADMIN}{issue.pk}/reply/", {"message": "On it"}, format="json").status_code,
                         status.HTTP_400_BAD_REQUEST)
        fixed = self.client.patch(f"{ADMIN}{issue.pk}/", {"status": "fixed"}, format="json").json()
        self.assertEqual((fixed["status"], fixed["email"]), ("fixed", ""))
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_only(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.post(f"{ADMIN}report/", {"description": "x"}).status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_inbox_filters_by_source_and_owner_sees_only_their_own(self) -> None:
        self.client.post(URL, {"description": "From the window"}, format="json")
        self.as_owner()
        self.client.post("/api/auth/issues/", {"description": "From the dashboard"}, format="json")
        mine = self.client.get("/api/auth/issues/").json()
        self.assertEqual([r["description"] for r in mine], ["From the dashboard"])
        member_issue = Issue.objects.get(source="member")
        Issue.objects.filter(pk=member_issue.pk).update(screenshot="issues/x.png")
        self.assertEqual(self.client.get(f"/api/auth/issues/{member_issue.pk}/screenshot/").status_code,
                         status.HTTP_404_NOT_FOUND)

        self.as_staff()
        self.client.post(f"{ADMIN}report/", {"description": "From staff"}, format="multipart")
        everything = self.client.get(ADMIN).json()
        self.assertEqual(sorted(r["source"] for r in everything["rows"]), ["member", "owner", "staff"])
        self.assertEqual(everything["counts"]["new"], 3)
        members = self.client.get(ADMIN, {"source": "member"}).json()
        self.assertEqual([(r["member_name"], r["customer"]["email"]) for r in members["rows"]],
                         [("Asha Rao", "owner@acme.in")])
        self.assertEqual(members["counts"]["new"], 1)
        self.assertEqual(self.client.get(ADMIN, {"source": "robot"}).status_code, status.HTTP_400_BAD_REQUEST)
