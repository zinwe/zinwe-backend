"""An optional picture on an Announcement: staff upload or remove it; every feed item carries its `image_url`."""
from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import Announcement, User

ADMIN = "/api/admin/announcements/"
FEED = "/api/auth/announcements/"
PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
JPG = bytes.fromhex("ffd8ffe0") + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64


def upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data)


class AnnouncementImageTests(APITestCase):
    def setUp(self) -> None:
        media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, media, True)
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        self.client.force_authenticate(self.staff)

    def create(self, **fields):  # type: ignore[no-untyped-def]
        data = {
            "title": "Diwali offer", "body": "Half off this week", "kind": "news", "placement": "popup",
            "audience": "everyone", "plan_codes": "[]", "owner_emails": "[]",
            "starts_at": (timezone.now() - timedelta(hours=1)).isoformat(), **fields,
        }
        return self.client.post(ADMIN, data, format="multipart")

    def owner_feed(self) -> dict:
        owner = User.objects.create_user(
            f"owner{User.objects.count()}@acme.in", "pw-12345678", email_verified_at=timezone.now()
        )
        token, _ = Token.objects.get_or_create(user=owner)
        self.client.force_authenticate(None)
        response = self.client.get(FEED, HTTP_AUTHORIZATION=f"Token {token.key}").json()
        self.client.force_authenticate(self.staff)
        return response

    def test_upload_shows_in_the_feed_and_the_file_is_public(self) -> None:
        created = self.create(image=upload("diwali.png", PNG))
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        row = created.json()
        a = Announcement.objects.get()
        expected = f"/api/auth/announcements/{a.pk}/image/?v={int(a.updated_at.timestamp())}"
        self.assertEqual(row["image_url"], expected)
        self.assertNotIn("image", row)
        # Left out of the form, these stay on (a multipart form has no "missing = true").
        self.assertEqual((row["is_active"], row["dismissible"]), (True, True))

        popups = self.owner_feed()["popups"]
        self.assertEqual([(p["title"], p["image_url"]) for p in popups], [("Diwali offer", expected)])

        self.client.force_authenticate(None)
        image = self.client.get(expected)
        self.assertEqual((image.status_code, image["Content-Type"]), (200, "image/png"))
        self.assertEqual(image["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(b"".join(image.streaming_content), PNG)

    def test_items_without_a_picture_say_null(self) -> None:
        response = self.client.post(ADMIN, {
            "title": "New version", "kind": "news", "placement": "both", "audience": "everyone",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertIsNone(response.json()["image_url"])
        feed = self.owner_feed()["announcements"]
        self.assertTrue(feed)
        self.assertTrue(all("image_url" in item for item in feed))
        self.assertIsNone(next(i for i in feed if i["title"] == "New version")["image_url"])
        self.client.force_authenticate(None)
        public = self.client.get(f"{FEED}public/").json()["announcements"]
        self.assertEqual([(i["title"], i["image_url"]) for i in public], [("New version", None)])
        pk = response.json()["id"]
        self.assertEqual(self.client.get(f"{FEED}{pk}/image/").status_code, status.HTTP_404_NOT_FOUND)

    def test_only_png_jpg_webp_up_to_2_mb(self) -> None:
        self.assertEqual(self.create(image=upload("a.jpg", JPG)).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.create(image=upload("a.webp", WEBP)).status_code, status.HTTP_201_CREATED)
        a = Announcement.objects.get(image__endswith=".jpg")
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"{FEED}{a.pk}/image/")["Content-Type"], "image/jpeg")
        self.client.force_authenticate(self.staff)
        for name, data in (("a.gif", GIF), ("a.png", JPG), ("a.webp", b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 64),
                           ("a.png", PNG + b"\x00" * (2 * 1024 * 1024))):
            response = self.create(image=upload(name, data))
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, name)
            self.assertIn("image", response.json())

    def test_replace_and_remove(self) -> None:
        pk = self.create(image=upload("a.png", PNG)).json()["id"]
        first = Announcement.objects.get().image
        storage, old = first.storage, first.name
        replaced = self.client.patch(f"{ADMIN}{pk}/", {"image": upload("b.webp", WEBP)}, format="multipart")
        self.assertEqual(replaced.status_code, status.HTTP_200_OK, replaced.content)
        self.assertIn(f"/api/auth/announcements/{pk}/image/?v=", replaced.json()["image_url"])
        self.assertFalse(storage.exists(old))
        new = Announcement.objects.get().image.name
        self.assertTrue(new.endswith(".webp"))

        removed = self.client.patch(f"{ADMIN}{pk}/", {"remove_image": "true"}, format="multipart")
        self.assertEqual(removed.status_code, status.HTTP_200_OK, removed.content)
        self.assertIsNone(removed.json()["image_url"])
        self.assertFalse(Announcement.objects.get().image)
        self.assertFalse(storage.exists(new))
        self.assertIsNone(self.owner_feed()["popups"][0]["image_url"])

        # JSON edits keep working and leave the picture alone.
        self.client.patch(f"{ADMIN}{pk}/", {"image": upload("c.png", PNG)}, format="multipart")
        edited = self.client.patch(f"{ADMIN}{pk}/", {"title": "Diwali, last days"}, format="json")
        self.assertEqual(edited.status_code, status.HTTP_200_OK, edited.content)
        self.assertIsNotNone(edited.json()["image_url"])
        both = self.client.patch(f"{ADMIN}{pk}/", {"image": upload("d.png", PNG), "remove_image": "true"},
                                 format="multipart")
        self.assertEqual(both.status_code, status.HTTP_400_BAD_REQUEST)

        # Deleting the message deletes its picture.
        name = Announcement.objects.get().image.name
        self.assertEqual(self.client.delete(f"{ADMIN}{pk}/").status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(storage.exists(name))
