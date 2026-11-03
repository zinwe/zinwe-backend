"""Report an issue: owners send a problem with an optional screenshot; staff work it in the admin Issues inbox."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import Issue, User

URL = "/api/auth/issues/"
ADMIN = "/api/admin/issues/"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64


def image(name: str = "shot.png", data: bytes = PNG) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type="image/png")


class IssueTests(APITestCase):
    def setUp(self) -> None:
        cache.clear()  # throttle counters
        self.media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media, True)
        self.owner = self.login_as("owner@acme.in")

    def login_as(self, email: str, staff: bool = False) -> User:
        if staff:
            user = User.objects.create_superuser(email, "pw-12345678")
        else:
            user = User.objects.create_user(email, "pw-12345678", email_verified_at=timezone.now())
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return user

    def report(self, **extra) -> Issue:  # type: ignore[no-untyped-def]
        data = {"description": "The Team page keeps loading", "page": "/app/team", **extra}
        response = self.client.post(URL, data, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return Issue.objects.get(pk=response.json()["id"])

    # --- owner ---------------------------------------------------------------------------------------------

    def test_report_with_screenshot_and_browser_details(self) -> None:
        issue = self.report(
            screenshot=image(),
            context='{"viewport": "1366x768", "browser": "Chrome 131", "password": "nope", "url": "/app/team"}',
        )
        self.assertEqual((issue.owner, issue.status, issue.contact_ok), (self.owner, "new", True))
        self.assertTrue(issue.screenshot.name.startswith("issues/"))
        self.assertNotIn("shot", issue.screenshot.name)  # never the uploaded name
        self.assertEqual(issue.context, {"viewport": "1366x768", "browser": "Chrome 131", "url": "/app/team"})
        body = self.client.get(URL).json()
        self.assertEqual([(r["description"], r["has_screenshot"], r["status"]) for r in body],
                         [("The Team page keeps loading", True, "new")])
        self.assertNotIn("screenshot", body[0])

    def test_json_without_screenshot(self) -> None:
        response = self.client.post(URL, {"description": "  Export is slow ", "context": {"screen": "1920x1080"},
                                          "contact_ok": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        issue = Issue.objects.get()
        self.assertEqual((issue.description, issue.context, issue.contact_ok, bool(issue.screenshot)),
                         ("Export is slow", {"screen": "1920x1080"}, False, False))

    def test_description_is_required(self) -> None:
        response = self.client.post(URL, {"description": "   "}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["description"], ["Tell us what went wrong."])

    def test_only_real_images(self) -> None:
        for name, data in (("shot.png", b"<svg onload=alert(1)>"), ("shot.gif", b"GIF89a" + b"\x00" * 10),
                           ("shot.webp", b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 10), ("shot.pdf", b"%PDF-1.7")):
            with self.subTest(name=name):
                response = self.client.post(URL, {"description": "x", "screenshot": image(name, data)}, format="multipart")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json()["screenshot"], ["Attach a PNG, JPG or WebP image."])
        self.assertFalse(Issue.objects.exists())
        self.report(screenshot=image("shot.webp", WEBP))

    @override_settings(ZINWE_ISSUE_SCREENSHOT_MAX_BYTES=50)
    def test_size_limit(self) -> None:
        response = self.client.post(URL, {"description": "x", "screenshot": image()}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("too large", response.json()["screenshot"][0])

    def test_screenshot_is_owner_or_staff_only(self) -> None:
        issue = self.report(screenshot=image())
        shot = f"{URL}{issue.pk}/screenshot/"
        response = self.client.get(shot)
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/png"))
        self.assertEqual(b"".join(response.streaming_content), PNG)
        self.login_as("other@acme.in")
        self.assertEqual(self.client.get(shot).status_code, 404)
        self.assertEqual(self.client.get(URL).json(), [])
        self.login_as("ops@zinwe.local", staff=True)
        self.assertEqual(self.client.get(shot).status_code, 200)
        self.client.credentials()
        self.assertEqual(self.client.get(shot).status_code, 401)

    def test_deleting_the_account_removes_the_screenshot(self) -> None:
        issue = self.report(screenshot=image())
        path = Path(issue.screenshot.path)
        self.assertTrue(path.exists())
        self.owner.delete()
        self.assertFalse(Issue.objects.exists())
        self.assertFalse(path.exists())

    # --- admin ---------------------------------------------------------------------------------------------

    def test_admin_inbox_reply_and_fixed_email(self) -> None:
        issue = self.report(screenshot=image())
        self.report(description="Second one")
        self.login_as("ops@zinwe.local", staff=True)
        body = self.client.get(ADMIN).json()
        self.assertEqual(body["counts"], {"new": 2, "looking": 0, "fixed": 0, "closed": 0})
        row = next(r for r in body["rows"] if r["id"] == issue.pk)
        self.assertEqual((row["customer"]["email"], row["has_screenshot"], row["page"]), ("owner@acme.in", True, "/app/team"))

        response = self.client.post(f"{ADMIN}{issue.pk}/reply/", {"message": "Thanks, we can see it."}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["status"], response.json()["reply"]), ("looking", "Thanks, we can see it."))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@acme.in"])
        self.assertIn(f"#{issue.pk}", mail.outbox[0].subject)

        response = self.client.patch(f"{ADMIN}{issue.pk}/", {"status": "fixed"}, format="json")
        self.assertEqual((response.json()["status"], response.json()["email"]), ("fixed", "sent"))
        self.assertIsNotNone(Issue.objects.get(pk=issue.pk).resolved_at)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Fixed", mail.outbox[1].subject)
        # Fixed again sends nothing new.
        self.assertEqual(self.client.patch(f"{ADMIN}{issue.pk}/", {"status": "fixed"}, format="json").json()["email"], "")
        self.assertEqual(self.client.get(f"{ADMIN}?status=fixed").json()["rows"][0]["id"], issue.pk)

    def test_no_email_when_they_said_no(self) -> None:
        issue = self.report(contact_ok="false")
        self.login_as("ops@zinwe.local", staff=True)
        response = self.client.post(f"{ADMIN}{issue.pk}/reply/", {"message": "Hi"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.patch(f"{ADMIN}{issue.pk}/", {"status": "fixed"}, format="json").json()["email"], "")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.client.patch(f"{ADMIN}{issue.pk}/", {"status": "done"}, format="json").status_code, 400)

    def test_admin_is_staff_only(self) -> None:
        self.assertEqual(self.client.get(ADMIN).status_code, 403)
