"""v1.5 file sharing in chat: upload limits and the off switch, attaching on all four routes, the access matrix,
expiry, clean-up, the cascade, edit / delete, and the old-client shape (body still present)."""
from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from django.contrib.admin.sites import site
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from z_auth._testing import give_plan
from z_auth.models import User
from z_devices.models import Member, Message

from . import files
from .models import ChatFile, ChatMessage, ChatReport, ConversationMember
from .retention import prune_chat
from .tests import DEVICE_CHAT, GROUPS, ChatCase

OWNER_FILES = "/api/chat/files/"
DEVICE_FILES = "/api/devices/chat/files/"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.7\n" + b"x" * 200
CLIP = "\U0001F4CE"
FILE_KEYS = {"id", "name", "size", "mime", "kind", "caption", "risky", "expires_at", "expired", "url"}


class FileCase(ChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="zinwe-chatfiles-")
        override = override_settings(CHAT_FILES_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.root, True)

    # --- helpers ---

    def upload(self, name: str = "Quote-Lumen.pdf", content: bytes = PDF, token: str | None = None,
               content_type: str = "application/pdf") -> Any:
        if token is None:
            self.as_owner()
            url = OWNER_FILES
        else:
            self.device(token)
            url = DEVICE_FILES
        return self.client.post(url, {"file": SimpleUploadedFile(name, content, content_type=content_type)},
                                format="multipart")

    def uploaded(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        r = self.upload(*args, **kwargs)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def disk(self, file_id: str) -> Path:
        return ChatFile.objects.get(pk=file_id).path

    def fetch(self, url: str) -> Any:
        return self.client.get(url)

    @staticmethod
    def content(response: Any) -> bytes:
        return b"".join(response.streaming_content)

    def group_file_line(self, g: dict[str, Any], token: str | None = None, body: str = "Final version",
                        name: str = "Quote-Lumen.pdf", content: bytes = PDF) -> dict[str, Any]:
        f = self.uploaded(name, content, token=token)
        url = f"{GROUPS}{g['id']}/messages/" if token is None else f"{DEVICE_CHAT}{g['id']}/messages/"
        r = self.client.post(url, {"body": body, "file_id": f["id"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def direct_with_emily(self) -> dict[str, Any]:
        self.make_group("Ops", ids=[self.karan.id, self.emily.id])
        self.device(self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        return r.json()


class UploadTests(FileCase):
    def test_owner_upload_shape_and_disk(self) -> None:
        r = self.upload()
        self.assertEqual(r.status_code, 201, r.content)
        f = r.json()
        self.assertEqual(set(f), FILE_KEYS)
        self.assertEqual((f["name"], f["size"], f["mime"], f["kind"], f["caption"], f["risky"]),
                         ("Quote-Lumen.pdf", len(PDF), "application/pdf", "file", "", False))
        self.assertEqual((f["expires_at"], f["expired"]), (None, False))  # not attached yet
        self.assertEqual(f["url"], f"http://testserver/api/chat/files/{f['id']}/")
        row = ChatFile.objects.get(pk=f["id"])
        self.assertEqual((row.owner_id, row.by_member_id, row.chat_line_id, row.member_message_id),
                         (self.owner.id, None, None, None))
        self.assertEqual(row.path, Path(self.root) / str(self.owner.id) / f["id"])
        self.assertEqual(row.path.read_bytes(), PDF)
        self.assertEqual(row.sha256, hashlib.sha256(PDF).hexdigest())

    def test_device_upload_gets_the_device_route(self) -> None:
        f = self.uploaded(token=self.karan_token)
        self.assertEqual(f["url"], f"http://testserver/api/devices/chat/files/{f['id']}/")
        self.assertEqual(ChatFile.objects.get(pk=f["id"]).by_member_id, self.karan.id)

    def test_kind_by_magic_bytes_risky_and_safe_names(self) -> None:
        self.assertEqual(self.uploaded("shot.png", PNG, content_type="image/png")["kind"], "image")
        self.assertEqual(self.uploaded("shot.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\0" * 20)["kind"], "image")
        self.assertEqual(self.uploaded("fake.png", b"not a picture")["kind"], "file")  # extension alone isn't enough
        self.assertEqual(self.uploaded("drawing.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")["kind"], "file")
        self.assertEqual(self.uploaded("data.bin", PNG)["kind"], "file")  # picture bytes, but not an image name
        self.assertTrue(self.uploaded("setup.EXE", b"MZ\x90\x00")["risky"])
        self.assertTrue(self.uploaded("run.ps1", b"Write-Host hi")["risky"])
        self.assertFalse(self.uploaded("notes.txt", b"hello")["risky"])
        self.assertEqual(files.safe_name("..\\..\\windows\\evil.pdf"), "evil.pdf")
        self.assertEqual(files.safe_name("a‮gpj.exe"), "agpj.exe")  # direction override stripped
        self.assertEqual(files.safe_name('what?:"*.txt'), "what____.txt")
        self.assertEqual(files.safe_name(""), "file")
        long = files.safe_name("x" * 300 + ".pdf")
        self.assertEqual((len(long), long[-4:]), (200, ".pdf"))

    def test_size_limit_empty_and_missing(self) -> None:
        exact = self.upload("big.bin", b"\0" * files.FILE_MAX_BYTES)
        self.assertEqual(exact.status_code, 201, exact.content)
        self.assertEqual(exact.json()["size"], 26214400)
        over = self.upload("bigger.bin", b"\0" * (files.FILE_MAX_BYTES + 1))
        self.assertEqual((over.status_code, over.json()), (400, {"file": ["Files can be up to 25 MB."]}))
        empty = self.upload("empty.txt", b"")
        self.assertEqual((empty.status_code, empty.json()), (400, {"file": ["That file is empty."]}))
        missing = self.client.post(OWNER_FILES, {}, format="multipart")
        self.assertEqual((missing.status_code, missing.json()), (400, {"file": ["Pick a file."]}))
        # A body far over the limit is refused from its Content-Length, before it is read.
        self.as_owner()
        r = self.client.post(OWNER_FILES, {"file": SimpleUploadedFile("x.bin", b"1")}, format="multipart",
                             CONTENT_LENGTH=str(40 * 1024 * 1024))
        self.assertEqual((r.status_code, r.json()), (400, {"file": ["Files can be up to 25 MB."]}))
        # Nothing refused was left on disk.
        on_disk = [p for p in Path(self.root).rglob("*") if p.is_file()]
        self.assertEqual(len(on_disk), 1)

    def test_off_switch_stops_every_upload_but_sent_files_stay(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g)
        self.as_owner()
        r = self.client.patch("/api/chat/settings/", {"allow_files": False}, format="json")
        self.assertEqual(r.json(), {"allow_member_dm": True, "allow_files": False, "file_max_bytes": 26214400,
                                    "file_keep_days": 7})
        off = {"detail": "Your admin has turned off file sharing."}
        r = self.upload()
        self.assertEqual((r.status_code, r.json()), (403, off))
        r = self.upload(token=self.karan_token)
        self.assertEqual((r.status_code, r.json()), (403, off))
        self.assertEqual(self.client.get(DEVICE_FILES).json(), {"allowed": False, "max_bytes": 26214400, "keep_days": 7})
        self.device(self.karan_token)
        self.assertEqual(self.client.get(line["file"]["url"].replace("/api/chat/", "/api/devices/chat/")).status_code, 200)
        self.as_owner()
        self.assertEqual(self.client.get(line["file"]["url"]).status_code, 200)
        self.client.patch("/api/chat/settings/", {"allow_files": True}, format="json")
        self.assertEqual(self.upload().status_code, 201)

    def test_settings_shape_and_validation(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get("/api/chat/settings/").json(),
                         {"allow_member_dm": True, "allow_files": True, "file_max_bytes": 26214400, "file_keep_days": 7})
        r = self.client.patch("/api/chat/settings/", {"allow_files": "no"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"allow_files": ["true or false."]}))
        r = self.client.patch("/api/chat/settings/", {"file_max_bytes": 1}, format="json")  # read-only: not a switch
        self.assertEqual(r.status_code, 400)
        r = self.client.patch("/api/chat/settings/", {"allow_member_dm": False, "allow_files": False}, format="json")
        self.assertEqual((r.json()["allow_member_dm"], r.json()["allow_files"]), (False, False))

    def test_device_files_endpoint(self) -> None:
        self.device(self.karan_token)
        self.assertEqual(self.client.get(DEVICE_FILES).json(), {"allowed": True, "max_bytes": 26214400, "keep_days": 7})

    def test_too_many_waiting_per_sender(self) -> None:
        for _ in range(files.MAX_WAITING):
            ChatFile.objects.create(owner=self.owner, by_member=self.karan, name="a.txt", size=1, sha256="0")
        r = self.upload(token=self.karan_token)
        self.assertEqual((r.status_code, r.json()), (400, {"file": ["Too many files waiting. Send or remove some first."]}))
        self.assertEqual(self.upload(token=self.emily_token).status_code, 201)  # per sender
        self.assertEqual(self.upload().status_code, 201)  # the owner too
        ChatFile.objects.filter(by_member=self.karan).update(created_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(self.upload(token=self.karan_token).status_code, 201)  # stale ones don't count

    def test_plan_without_team_chat(self) -> None:
        from z_auth.models import Plan

        Plan.objects.filter(code="businesses").update(modules=["usb_alerts"])
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(self.upload(token=self.karan_token).status_code, 403)
        self.assertFalse(self.client.get(DEVICE_FILES).json()["allowed"])


class AttachTests(FileCase):
    def test_owner_group_line_old_and_new_shape(self) -> None:
        g = self.make_group()
        content = b"\0" * 2202009
        line = self.group_file_line(g, name="Quote-Lumen.pdf", content=content)
        # Old clients: the body is the fallback text. New clients: the file card + caption.
        self.assertEqual(line["body"], f"{CLIP} Quote-Lumen.pdf (2.1 MB)\nFinal version")
        f = line["file"]
        self.assertEqual(set(f), FILE_KEYS)
        self.assertEqual((f["caption"], f["size"], f["expired"]), ("Final version", 2202009, False))
        self.assertEqual(f["url"], f"http://testserver/api/chat/files/{f['id']}/")
        row = ChatFile.objects.get(pk=f["id"])
        posted = ChatMessage.objects.get(pk=line["id"]).created_at
        self.assertEqual((row.attached_at, row.expires_at), (posted, posted + timedelta(days=7)))
        self.assertEqual(f["expires_at"], (posted + timedelta(days=7)).isoformat().replace("+00:00", "Z"))
        # The member's view of the same line: the device route.
        self.device(self.karan_token)
        seen = self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").json()[-1]
        self.assertEqual(seen["body"], line["body"])
        self.assertEqual(seen["file"]["url"], f"http://testserver/api/devices/chat/files/{f['id']}/")
        # Plain lines say file: null.
        self.as_owner()
        self.assertIsNone(self.owner_lines(g["id"])[0]["file"])
        # The group list preview shows the fallback text.
        self.assertTrue(self.client.get(GROUPS).json()[0]["last"]["text"].startswith(f"{CLIP} Quote-Lumen.pdf"))

    def test_member_group_line_without_text_and_in_a_direct_chat(self) -> None:
        g = self.make_group()
        f = self.uploaded("photo.png", PNG, token=self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"file_id": f["id"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["body"], f"{CLIP} photo.png (1 KB)")
        self.assertEqual((r.json()["file"]["caption"], r.json()["file"]["kind"]), ("", "image"))
        direct = self.direct_with_emily()
        f = self.uploaded("plan.docx", b"PK\x03\x04", token=self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"body": "", "file_id": f["id"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.device(self.emily_token)
        got = self.client.get(f"{DEVICE_CHAT}{direct['id']}/messages/").json()[-1]["file"]
        self.assertEqual(got["name"], "plan.docx")
        self.assertEqual(self.client.get(got["url"]).status_code, 200)

    def test_owner_to_member_admin_chat(self) -> None:
        f = self.uploaded("Brief.pdf")
        r = self.client.post(f"/api/devices/members/{self.member.id}/message/", {"file_id": f["id"], "body": "Read this"},
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        line = r.json()
        self.assertEqual(line["title"], f"{CLIP} Brief.pdf")  # title defaults for a file line
        self.assertEqual(line["body"], f"{CLIP} Brief.pdf (1 KB)\nRead this")
        self.assertEqual((line["file"]["caption"], line["file"]["url"]),
                         ("Read this", f"http://testserver/api/chat/files/{f['id']}/"))
        msg = Message.objects.get(pk=line["id"])
        self.assertEqual(ChatFile.objects.get(pk=f["id"]).member_message, msg)
        # A title the owner typed is kept.
        f2 = self.uploaded("Two.pdf")
        r = self.client.post(f"/api/devices/members/{self.member.id}/message/",
                             {"file_id": f2["id"], "title": "Contract"}, format="json")
        self.assertEqual((r.json()["title"], r.json()["file"]["caption"]), ("Contract", ""))
        # The member's PC: the device route, and the download works.
        self.device(self.asha_token)
        feed = self.client.get("/api/devices/messages/").json()
        mine = next(m for m in feed if m["id"] == line["id"])
        self.assertEqual(mine["file"]["url"], f"http://testserver/api/devices/chat/files/{f['id']}/")
        self.assertEqual(self.content(self.client.get(mine["file"]["url"])), PDF)
        # Another member can't.
        self.device(self.karan_token)
        self.assertEqual(self.client.get(mine["file"]["url"]).status_code, 404)

    def test_member_to_owner_admin_chat(self) -> None:
        f = self.uploaded("Timesheet.xlsx", b"PK\x03\x04data", token=self.asha_token)
        r = self.client.post("/api/devices/messages/", {"file_id": f["id"], "body": "For October"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        line = r.json()
        self.assertEqual((line["sender"], line["body"]), ("member", f"{CLIP} Timesheet.xlsx (1 KB)\nFor October"))
        self.assertEqual(line["file"]["url"], f"http://testserver/api/devices/chat/files/{f['id']}/")
        self.as_owner()
        thread = self.client.get(f"/api/devices/members/{self.member.id}/messages/").json()
        got = next(m for m in thread if m["id"] == line["id"])["file"]
        self.assertEqual((got["caption"], got["url"]), ("For October", f"http://testserver/api/chat/files/{f['id']}/"))
        self.assertEqual(self.content(self.client.get(got["url"])), b"PK\x03\x04data")
        # Plain lines still work as before, and say file: null.
        self.device(self.asha_token)
        r = self.client.post("/api/devices/messages/", {"body": "thanks"}, format="json")
        self.assertEqual((r.status_code, r.json()["body"], r.json()["file"]), (201, "thanks", None))

    def test_file_id_must_be_the_senders_own_fresh_unattached_upload(self) -> None:
        g = self.make_group(ids=[self.member.id, self.karan.id])
        refused = {"file_id": ["That file wasn't found. Attach it again."]}
        url = f"{DEVICE_CHAT}{g['id']}/messages/"
        karans = self.uploaded(token=self.karan_token)
        # Someone else's upload.
        self.device(self.asha_token)
        r = self.client.post(url, {"file_id": karans["id"]}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, refused))
        # The owner's upload, used by a member; and a member's upload, used by the owner.
        owners = self.uploaded()
        self.device(self.karan_token)
        self.assertEqual(self.client.post(url, {"file_id": owners["id"]}, format="json").json(), refused)
        self.as_owner()
        self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"file_id": karans["id"]}, format="json").json(),
                         refused)
        self.assertEqual(self.client.post(f"/api/devices/members/{self.member.id}/message/", {"file_id": karans["id"]},
                                          format="json").json(), refused)
        # Another company's.
        other = User.objects.create_user("other@corp.in", "pw-12345678")
        theirs = ChatFile.objects.create(owner=other, name="x.pdf", size=1, sha256="0")
        self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"file_id": str(theirs.pk)},
                                          format="json").json(), refused)
        # Unknown, not a UUID, and older than an hour.
        for bad in (str(uuid.uuid4()), "nope"):
            self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"file_id": bad}, format="json").json(),
                             refused)
        ChatFile.objects.filter(pk=owners["id"]).update(created_at=timezone.now() - timedelta(minutes=61))
        self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"file_id": owners["id"]}, format="json").json(),
                         refused)
        # Attached once: never again (on any route).
        self.device(self.karan_token)
        self.assertEqual(self.client.post(url, {"file_id": karans["id"]}, format="json").status_code, 201)
        self.assertEqual(self.client.post(url, {"file_id": karans["id"]}, format="json").json(), refused)
        self.assertEqual(self.client.post("/api/devices/messages/", {"file_id": karans["id"]}, format="json").json(),
                         refused)
        # A refused attach posts nothing.
        self.assertEqual(Message.objects.count(), 0)

    def test_without_a_file_the_old_rules_and_errors_stand(self) -> None:
        g = self.make_group()
        self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": ""}, format="json").json(),
                         {"body": ["This field may not be blank."]})
        self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {}, format="json").json(),
                         {"body": ["This field is required."]})
        self.assertEqual(self.client.post(f"/api/devices/members/{self.member.id}/message/", {"body": "x"},
                                          format="json").json(), {"title": ["This field is required."]})
        self.device(self.asha_token)
        self.assertEqual(self.client.post("/api/devices/messages/", {}, format="json").json(),
                         {"body": ["This field is required."]})

    def test_announce_group_and_left_member_still_cant_post_a_file(self) -> None:
        g = self.make_group(announce=True)
        f = self.uploaded(token=self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"file_id": f["id"]}, format="json")
        self.assertEqual((r.status_code, r.json()), (403, {"detail": "Only the admin can post in this group."}))
        self.assertIsNone(ChatFile.objects.get(pk=f["id"]).attached_at)  # still attachable elsewhere


class AccessTests(FileCase):
    def test_owner_never_reaches_a_direct_chat_file(self) -> None:
        direct = self.direct_with_emily()
        f = self.uploaded("secret.pdf", token=self.karan_token)
        self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"file_id": f["id"]}, format="json")
        self.as_owner()
        self.assertEqual(self.client.get(f"{OWNER_FILES}{f['id']}/").status_code, 404)
        self.assertEqual(self.client.get(f"{OWNER_FILES}{f['id']}/?inline=1").status_code, 404)
        # Even past its 7 days: not found (not "no longer available"), so nothing about it leaks.
        ChatFile.objects.filter(pk=f["id"]).update(expires_at=timezone.now() - timedelta(days=1))
        self.assertEqual(self.client.get(f"{OWNER_FILES}{f['id']}/").status_code, 404)
        # Nor the Django admin: ChatFile isn't registered at all.
        self.assertNotIn(ChatFile, site._registry)
        # A third member isn't in it either.
        self.device(self.asha_token)
        self.assertEqual(self.client.get(f"{DEVICE_FILES}{f['id']}/").status_code, 404)

    def test_other_company_and_non_members(self) -> None:
        g = self.make_group(ids=[self.member.id, self.karan.id])
        line = self.group_file_line(g)
        fid = line["file"]["id"]
        # Jake isn't in the group.
        self.device(self.jake_token)
        self.assertEqual(self.client.get(f"{DEVICE_FILES}{fid}/").status_code, 404)
        # Another company's owner and member.
        other = User.objects.create_user("other@corp.in", "pw-12345678")
        give_plan(other, "businesses")
        stranger = Member.objects.create(owner=other, name="Sam Stone", email="sam@corp.in")
        token = self._enroll(stranger, "SAM-PC")
        self.device(token)
        self.assertEqual(self.client.get(f"{DEVICE_FILES}{fid}/").status_code, 404)
        self.client.force_authenticate(user=other)
        self.assertEqual(self.client.get(f"{OWNER_FILES}{fid}/").status_code, 404)
        self.client.force_authenticate(user=None)
        # An unattached upload: only its sender.
        mine = self.uploaded(token=self.karan_token)
        self.assertEqual(self.client.get(mine["url"]).status_code, 200)
        self.device(self.asha_token)
        self.assertEqual(self.client.get(mine["url"]).status_code, 404)
        self.as_owner()
        self.assertEqual(self.client.get(f"{OWNER_FILES}{mine['id']}/").status_code, 404)
        own = self.uploaded()
        self.assertEqual(self.client.get(own["url"]).status_code, 200)
        self.assertEqual(self.client.get(f"{OWNER_FILES}{uuid.uuid4()}/").status_code, 404)

    def test_member_who_left_keeps_older_files_only(self) -> None:
        g = self.make_group(ids=[self.member.id, self.karan.id])
        before = self.group_file_line(g)["file"]["id"]
        self.as_owner()
        self.client.patch(f"{GROUPS}{g['id']}/", {"member_ids": [self.karan.id]}, format="json")
        after = self.group_file_line(g)["file"]["id"]
        self.device(self.asha_token)
        self.assertEqual(self.client.get(f"{DEVICE_FILES}{before}/").status_code, 200)  # read-only history
        self.assertEqual(self.client.get(f"{DEVICE_FILES}{after}/").status_code, 404)
        self.device(self.karan_token)
        self.assertEqual(self.client.get(f"{DEVICE_FILES}{after}/").status_code, 200)

    def test_download_headers_and_inline_images(self) -> None:
        g = self.make_group()
        doc = self.group_file_line(g, name="Résumé 2026 ✓.pdf")["file"]
        r = self.client.get(doc["url"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Disposition"], "attachment; filename*=UTF-8''R%C3%A9sum%C3%A9%202026%20%E2%9C%93.pdf")
        self.assertEqual(r["Content-Type"], "application/octet-stream")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r["Cache-Control"], "private, no-store")
        self.assertEqual(self.content(r), PDF)
        self.assertEqual(self.client.get(doc["url"] + "?inline=1")["Content-Disposition"][:10], "attachment")
        pic = self.group_file_line(g, name="shot.png", content=PNG)["file"]
        r = self.client.get(pic["url"] + "?inline=1")
        self.assertEqual((r["Content-Type"], r["Content-Disposition"]), ("image/png", "inline; filename*=UTF-8''shot.png"))
        self.assertEqual(self.content(r), PNG)
        self.assertEqual(self.client.get(pic["url"])["Content-Type"], "application/octet-stream")


class LifecycleTests(FileCase):
    def test_expired_is_410_and_clean_up_keeps_the_row(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g)
        fid = line["file"]["id"]
        path = self.disk(fid)
        ChatFile.objects.filter(pk=fid).update(expires_at=timezone.now() - timedelta(seconds=1))
        gone = {"detail": "This file is no longer available. Files are kept for 7 days."}
        r = self.client.get(f"{OWNER_FILES}{fid}/")
        self.assertEqual((r.status_code, r.json()), (410, gone))
        self.device(self.karan_token)
        self.assertEqual(self.client.get(f"{DEVICE_FILES}{fid}/").status_code, 410)
        shown = self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").json()[-1]
        self.assertEqual((shown["file"]["expired"], shown["file"]["url"]), (True, None))
        self.assertTrue(path.exists())  # until the clean-up runs
        result = files.prune_chat_files()
        self.assertEqual((result.expired, result.unattached, result.failed), (1, 0, []))
        self.assertFalse(path.exists())
        row = ChatFile.objects.get(pk=fid)
        self.assertIsNotNone(row.deleted_at)
        self.assertEqual(files.prune_chat_files().expired, 0)  # once
        self.as_owner()
        shown = self.owner_lines(g["id"])[-1]
        self.assertEqual((shown["file"]["name"], shown["file"]["expired"], shown["file"]["url"]),
                         ("Quote-Lumen.pdf", True, None))
        self.assertEqual(self.client.get(f"{OWNER_FILES}{fid}/").status_code, 410)

    def test_clean_up_deletes_unsent_uploads_after_an_hour(self) -> None:
        fresh = self.uploaded("fresh.txt", b"a")
        stale = self.uploaded("stale.txt", b"b")
        stale_path = self.disk(stale["id"])
        ChatFile.objects.filter(pk=stale["id"]).update(created_at=timezone.now() - timedelta(minutes=61))
        with self.captureOnCommitCallbacks(execute=True):
            result = files.prune_chat_files()
        self.assertEqual((result.expired, result.unattached), (0, 1))
        self.assertFalse(ChatFile.objects.filter(pk=stale["id"]).exists())
        self.assertFalse(stale_path.exists())
        self.assertTrue(self.disk(fresh["id"]).exists())
        self.assertIn("unsent upload(s) deleted", result.summary())

    def test_run_jobs_runs_the_clean_up(self) -> None:
        from z_main.management.commands.run_jobs import JOBS

        self.assertIn(files.prune_chat_files, [job for _, job in JOBS])

    def test_cascade_removes_the_disk_file(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g)
        path = self.disk(line["file"]["id"])
        # Chat history clean-up deletes old lines for good: the file row and the disk file go with them.
        ChatMessage.objects.filter(pk=line["id"]).update(created_at=timezone.now() - timedelta(days=4000))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(prune_chat().lines, 1)
        self.assertFalse(ChatFile.objects.exists())
        self.assertFalse(path.exists())
        # Deleting a group (owner) does the same.
        line = self.group_file_line(g)
        path = self.disk(line["file"]["id"])
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"{GROUPS}{g['id']}/").status_code, 204)
        self.assertFalse(path.exists())
        # Already gone from disk: the delete still works.
        f = self.uploaded()
        self.disk(f["id"]).unlink()
        with self.captureOnCommitCallbacks(execute=True):
            ChatFile.objects.filter(pk=f["id"]).delete()

    def test_a_rolled_back_delete_keeps_the_disk_file(self) -> None:
        from django.db import transaction

        f = self.uploaded()
        path = self.disk(f["id"])
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    ChatFile.objects.filter(pk=f["id"]).delete()
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertTrue(path.exists())

    def test_file_lines_cant_be_edited_and_delete_takes_the_file(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g)
        url = f"{GROUPS}{g['id']}/messages/{line['id']}/"
        r = self.client.patch(url, {"body": "changed"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"body": ["A file can't be edited. Delete it and send it again."]}))
        path = self.disk(line["file"]["id"])
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.delete(url)
        self.assertEqual((r.status_code, r.json()["deleted"], r.json()["body"], r.json()["file"]), (200, True, "", None))
        self.assertFalse(ChatFile.objects.exists())
        self.assertFalse(path.exists())
        self.assertIsNone(self.owner_lines(g["id"])[-1]["file"])
        self.assertEqual(self.client.get(line["file"]["url"]).status_code, 404)
        # Past 15 minutes: the line (and its file) stay.
        mine = self.group_file_line(g, token=self.karan_token)
        ChatMessage.objects.filter(pk=mine["id"]).update(created_at=timezone.now() - timedelta(minutes=16))
        ConversationMember.objects.filter(member=self.karan).update(joined_at=timezone.now() - timedelta(hours=1))
        r = self.client.delete(f"{DEVICE_CHAT}{g['id']}/messages/{mine['id']}/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(ChatFile.objects.filter(pk=mine["file"]["id"]).exists())
        self.assertEqual(self.client.patch(f"{DEVICE_CHAT}{g['id']}/messages/{mine['id']}/", {"body": "x"},
                                           format="json").status_code, 400)

    def test_a_report_keeps_the_fallback_text(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g, token=self.karan_token)
        self.device(self.asha_token)
        r = self.client.post(f"{DEVICE_CHAT}{g['id']}/report/", {"message_id": line["id"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(ChatReport.objects.get().message_body, f"{CLIP} Quote-Lumen.pdf (1 KB)\nFinal version")

    def test_human_size(self) -> None:
        self.assertEqual([files.human_size(n) for n in (1, 348160, 1048576, 2202009, 26214400)],
                         ["1 KB", "340 KB", "1 MB", "2.1 MB", "25 MB"])

    def test_disk_write_failure_leaves_nothing(self) -> None:
        with mock.patch.object(ChatFile, "save", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                files.store_upload(SimpleUploadedFile("a.pdf", PDF), self.owner.id, None)
        self.assertEqual([p for p in Path(self.root).rglob("*") if p.is_file()], [])
