"""F-15: Zingles, the animated logo for special days: staff upload and schedule them; the live one is public."""
from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import User, Zingle

PUBLIC = "/api/auth/zingle/"
ADMIN = "/api/admin/zingles/"
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data)


class ZingleTests(APITestCase):
    def setUp(self) -> None:
        cache.clear()
        media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, media, True)
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        self.client.force_authenticate(self.staff)

    def create(self, **fields):  # type: ignore[no-untyped-def]
        data = {
            "name": "Diwali 2026", "alt": "Diyas light up around the Zinwe mark", "places": "both",
            "animated": upload("diwali.gif", GIF), "still": upload("diwali.png", PNG),
            "starts_at": (timezone.now() - timedelta(hours=1)).isoformat(), **fields,
        }
        return self.client.post(ADMIN, data, format="multipart")

    def public(self):  # type: ignore[no-untyped-def]
        self.client.force_authenticate(None)
        response = self.client.get(PUBLIC)
        self.client.force_authenticate(self.staff)
        return response

    def test_upload_schedule_and_the_public_answer(self) -> None:
        self.assertIsNone(self.public().json())
        created = self.create()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        row = created.json()
        self.assertEqual((row["status"], row["places"]), ("live", "both"))
        self.assertNotIn("animated", row)

        response = self.public()
        self.assertEqual(response["Cache-Control"], "public, max-age=300")
        body = response.json()
        self.assertEqual(set(body), {"id", "animated_url", "still_url", "alt", "places"})
        self.assertEqual((body["id"], body["alt"], body["places"]), (row["id"], "Diyas light up around the Zinwe mark", "both"))
        self.client.force_authenticate(None)
        image = self.client.get(body["animated_url"])
        self.assertEqual((image.status_code, image["Content-Type"]), (200, "image/gif"))
        self.assertEqual(b"".join(image.streaming_content), GIF)
        self.assertEqual(self.client.get(body["still_url"])["Content-Type"], "image/png")
        self.assertEqual(self.client.get(f"{PUBLIC}{row['id']}/other/").status_code, status.HTTP_404_NOT_FOUND)

    def test_the_latest_start_is_live_and_the_window_counts(self) -> None:
        now = timezone.now()
        self.create(name="Older", starts_at=(now - timedelta(days=2)).isoformat())
        newer = self.create(name="Newer", starts_at=(now - timedelta(hours=2)).isoformat()).json()
        self.create(name="Later", starts_at=(now + timedelta(days=1)).isoformat())
        self.create(name="Over", starts_at=(now - timedelta(days=3)).isoformat(),
                    ends_at=(now - timedelta(hours=1)).isoformat())
        self.assertEqual(self.public().json()["id"], newer["id"])
        statuses = {r["name"]: r["status"] for r in self.client.get(ADMIN).json()}
        self.assertEqual(statuses, {"Later": "scheduled", "Newer": "live", "Older": "waiting", "Over": "ended"})

        # Switching it off shows the next one at once (the cache is cleared on a change).
        self.client.patch(f"{ADMIN}{newer['id']}/", {"is_active": False}, format="multipart")
        self.assertEqual(Zingle.objects.get(pk=self.public().json()["id"]).name, "Older")

    def test_webp_and_file_checks(self) -> None:
        self.assertEqual(self.create(animated=upload("d.webp", WEBP)).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.create(animated=upload("d.png", PNG)).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(still=upload("d.gif", GIF)).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(animated=upload("d.gif", b"GIF89a" + b"\x00" * (2 * 1024 * 1024))).status_code,
                         status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(alt=" ").status_code, status.HTTP_400_BAD_REQUEST)
        now = timezone.now()
        backwards = self.create(starts_at=now.isoformat(), ends_at=(now - timedelta(days=1)).isoformat())
        self.assertIn("ends_at", backwards.json())

    def test_replace_a_file_and_delete(self) -> None:
        row = self.create().json()
        old = Zingle.objects.get().animated.name
        changed = self.client.patch(f"{ADMIN}{row['id']}/", {"animated": upload("new.webp", WEBP)}, format="multipart")
        self.assertEqual(changed.status_code, status.HTTP_200_OK, changed.content)
        zingle = Zingle.objects.get()
        self.assertTrue(zingle.animated.name.endswith(".webp"))
        self.assertFalse(zingle.animated.storage.exists(old))
        self.assertEqual(self.client.delete(f"{ADMIN}{row['id']}/").status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(zingle.animated.storage.exists(zingle.animated.name))
        self.assertIsNone(self.public().json())

    def test_staff_only(self) -> None:
        owner = User.objects.create_user("owner@acme.in", "pw-12345678")
        self.client.force_authenticate(owner)
        self.assertEqual(self.client.get(ADMIN).status_code, status.HTTP_403_FORBIDDEN)
