"""v1.5 file sharing in chat, round 2 (files stay on the sender's device): plan limits, registration (no bytes, no
disk), attaching on all four routes, holder / sender checks for PATCH and stop, the ticket access matrix, signed and
scoped tickets, previews, "shared by me", the clean-up and the old-client shape (body still present)."""
from __future__ import annotations

import base64
import importlib
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.admin.sites import site
from django.test import override_settings
from django.utils import timezone

from z_auth._testing import give_plan
from z_auth.models import Plan, User
from z_devices.models import Device, Member, Message
from z_relay import tickets

from . import files
from .models import ChatFile, ChatMessage, ChatReport, ConversationMember
from .retention import prune_chat
from .tests import DEVICE_CHAT, GROUPS, ChatCase

OWNER_FILES = "/api/chat/files/"
DEVICE_FILES = "/api/devices/chat/files/"
CLIP = "\U0001F4CE"
BROWSER = "br-7f3a9c21e5"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 120
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
GB = 1024 ** 3
FILE_KEYS = {
    "id", "name", "size", "mime", "kind", "caption", "risky", "batch_id", "width", "height", "duration_s", "thumb_url",
    "sender", "mine", "available", "state", "created_at", "expires_at", "expired",
}
NOT_SHARED = {"detail": "This file isn't shared any more."}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class FileCase(ChatCase):
    # --- helpers ---

    def register(self, name: str = "Quote-Lumen.pdf", size: int = 2202009, token: str | None = None,
                 **extra: Any) -> Any:
        body: dict[str, Any] = {"name": name, "size": size, "mime": extra.pop("mime", "application/pdf"),
                                "local_ref": extra.pop("local_ref", f"C:\\Users\\me\\Documents\\{name}")}
        if token is None:
            self.as_owner()
            url = OWNER_FILES
            body["browser_id"] = extra.pop("browser_id", BROWSER)
        else:
            self.device(token)
            url = DEVICE_FILES
        body.update(extra)
        return self.client.post(url, body, format="json")

    def registered(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        r = self.register(*args, **kwargs)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def group_file_line(self, g: dict[str, Any], token: str | None = None, body: str = "Final version",
                        name: str = "Quote-Lumen.pdf", size: int = 2202009, **extra: Any) -> dict[str, Any]:
        f = self.registered(name, size, token=token, **extra)
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

    def device_of(self, member: Member) -> Device:
        return Device.objects.get(member=member)

    def ticket(self, route: str, file_id: str) -> Any:
        return self.client.post(f"{route}{file_id}/ticket/", format="json")


class PlanLimitTests(FileCase):
    def test_limit_comes_from_the_plan(self) -> None:
        # Businesses (the test owner's plan): 5 GB.
        self.assertEqual(self.register("big.mp4", 5 * GB, mime="video/mp4").status_code, 201)
        r = self.register("bigger.mp4", 5 * GB + 1)
        self.assertEqual((r.status_code, r.json()), (400, {"size": ["Files can be up to 5 GB on your plan."]}))
        r = self.register("bigger.mp4", 5 * GB + 1, token=self.karan_token)
        self.assertEqual(r.json(), {"size": ["Files can be up to 5 GB on your plan."]})
        # The plan's own number, whatever the admin set.
        for mb, text in ((2048, "2 GB"), (1536, "1.5 GB"), (1024, "1 GB"), (500, "500 MB")):
            Plan.objects.filter(code="businesses").update(file_max_mb=mb)
            r = self.register("x.zip", mb * 1024 * 1024 + 1)
            self.assertEqual(r.json(), {"size": [f"Files can be up to {text} on your plan."]})
            self.assertEqual(self.register("x.zip", mb * 1024 * 1024).status_code, 201)
            self.device(self.karan_token)
            self.assertEqual(self.client.get(DEVICE_FILES).json(),
                             {"allowed": True, "max_bytes": mb * 1024 * 1024, "keep_days": 30})

    def test_seeded_plan_sizes_and_entitlements(self) -> None:
        self.assertEqual(dict(Plan.objects.filter(is_active=True).values_list("code", "file_max_mb")),
                         {"free": 1024, "startups": 2048, "teams": 3072, "businesses": 5120})
        self.as_owner()
        self.assertEqual(self.client.get("/api/auth/me/").json()["entitlements"]["file_max_mb"], 5120)
        plans = {p["code"]: p for p in self.client.get("/api/auth/plans/").json()}
        self.assertEqual(plans["startups"]["file_max_mb"], 2048)
        # Another company on Startups gets 2 GB.
        other = User.objects.create_user("startup@corp.in", "pw-12345678")
        give_plan(other, "startups")
        from z_auth.entitlements import entitlements_for

        self.assertEqual((entitlements_for(other).file_max_mb, entitlements_for(other).file_max_bytes),
                         (2048, 2 * GB))

    def test_settings_show_the_plan_limit(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get("/api/chat/settings/").json(), {
            "allow_member_dm": True, "allow_files": True, "file_max_bytes": 5 * GB, "file_keep_days": 30,
            "allowed": True, "max_bytes": 5 * GB, "keep_days": 30,
        })
        self.assertEqual(self.client.get(OWNER_FILES).json(), {"allowed": True, "max_bytes": 5 * GB, "keep_days": 30})
        r = self.client.patch("/api/chat/settings/", {"allow_files": False}, format="json")
        self.assertEqual((r.json()["allow_files"], r.json()["allowed"]), (False, False))
        r = self.client.patch("/api/chat/settings/", {"allow_files": "no"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"allow_files": ["true or false."]}))


class RegisterTests(FileCase):
    def test_owner_registration_is_only_a_record(self) -> None:
        f = self.registered(batch_id="0b8e4c1e-0d36-4a3c-a7d2-3c1f8f0f5a11")
        self.assertEqual(set(f), FILE_KEYS)
        self.assertEqual((f["name"], f["size"], f["mime"], f["kind"], f["caption"], f["risky"]),
                         ("Quote-Lumen.pdf", 2202009, "application/pdf", "document", "", False))
        self.assertEqual((f["mine"], f["available"], f["state"], f["expired"], f["thumb_url"]),
                         (True, True, "ok", False, None))
        self.assertEqual(f["sender"], {"name": "Ananya Rao", "first_name": "Ananya"})
        self.assertEqual(f["batch_id"], "0b8e4c1e-0d36-4a3c-a7d2-3c1f8f0f5a11")
        self.assertNotIn("url", f)  # clients ask for a ticket
        row = ChatFile.objects.get(pk=f["id"])
        self.assertEqual((row.holder, row.local_ref, row.sha256, row.by_member_id),
                         (f"b{self.owner.id}.{BROWSER}", "C:\\Users\\me\\Documents\\Quote-Lumen.pdf", "", None))
        self.assertEqual(row.expires_at, row.created_at + timedelta(days=30))
        self.assertEqual(f["expires_at"], row.expires_at.isoformat().replace("+00:00", "Z"))
        self.assertFalse(hasattr(settings, "CHAT_FILES_ROOT"))  # nothing is ever stored on the server

    def test_device_registration_with_preview(self) -> None:
        f = self.registered("clip.mp4", 734003200, token=self.karan_token, mime="video/mp4", sha256="A" * 64,
                            thumb_b64=b64(JPEG), width=1920, height=1080, duration_s=83.6)
        self.assertEqual((f["kind"], f["width"], f["height"], f["duration_s"]), ("video", 1920, 1080, 84))
        self.assertEqual(f["thumb_url"], f"http://testserver/api/devices/chat/files/{f['id']}/thumb/")
        self.assertEqual(f["sender"], {"name": "Karan Malhotra", "first_name": "Karan"})
        row = ChatFile.objects.get(pk=f["id"])
        self.assertEqual((row.holder, row.sha256, bytes(row.thumb)),
                         (f"d{self.device_of(self.karan).id}", "a" * 64, JPEG))
        r = self.client.get(f["thumb_url"])
        self.assertEqual((r.status_code, r["Content-Type"], r["Cache-Control"], r["X-Content-Type-Options"]),
                         (200, "image/jpeg", "private, max-age=86400", "nosniff"))
        self.assertEqual(r.content, JPEG)
        # A data: URL (canvas.toDataURL) works too, and WebP.
        w = self.registered("a.webp", 10, thumb_b64="data:image/webp;base64," + b64(WEBP), mime="image/webp")
        self.assertEqual(self.client.get(w["thumb_url"])["Content-Type"], "image/webp")

    def test_bad_previews_and_fields(self) -> None:
        bad_thumb = {"thumb_b64": ["The preview must be a JPEG or WebP picture of up to 40 KB."]}
        for thumb in (b64(PNG), b64(b"\xff\xd8\xff" + b"\x00" * (40 * 1024)), "not base64!", b64(b"")):
            r = self.register(thumb_b64=thumb)
            if thumb == "":
                self.assertEqual(r.status_code, 201)
                continue
            self.assertEqual((r.status_code, r.json()), (400, bad_thumb), thumb[:20])
        self.assertEqual(self.register(thumb_b64=b64(b"\xff\xd8\xff" + b"\x00" * (40 * 1024 - 3))).status_code, 201)
        self.assertEqual(self.register(size=0).json(), {"size": ["That file is empty."]})
        self.assertEqual(self.register(sha256="xyz").json(), {"sha256": ["A SHA-256 is 64 hex characters."]})
        self.assertEqual(self.register(browser_id="").json(), {"browser_id": ["This field is required."]})
        self.assertEqual(self.register(browser_id="a b").json(), {"browser_id": ["Not a valid browser id."]})
        self.assertEqual(self.register(local_ref="").status_code, 400)
        self.assertEqual(self.register(local_ref="x" * 501).status_code, 400)
        self.assertEqual(self.register(batch_id="nope").status_code, 400)
        self.assertEqual(self.register(width=0).status_code, 400)
        # No bytes, ever: a multipart upload (round 1) isn't accepted.
        self.as_owner()
        r = self.client.post(OWNER_FILES, {"file": "x"}, format="multipart")
        self.assertEqual(r.status_code, 415)
        # Nothing refused left a row.
        self.assertEqual(ChatFile.objects.count(), 2)

    def test_kinds_risky_and_safe_names(self) -> None:
        cases = {
            "shot.PNG": "image", "pic.heic": "image", "clip.mov": "video", "song.mp3": "audio", "notes.txt": "document",
            "deck.pptx": "document", "data.csv": "document", "drawing.svg": "file", "archive.zip": "file",
            "setup.exe": "file", "noext": "file",
        }
        for name, kind in cases.items():
            self.assertEqual(self.registered(name, 10, mime="")["kind"], kind, name)
        self.assertEqual(self.registered("blob", 10, mime="video/webm")["kind"], "video")  # by type when no extension
        self.assertEqual(self.registered("pic", 10, mime="image/svg+xml")["kind"], "file")
        self.assertTrue(self.registered("setup.EXE", 10)["risky"])
        self.assertTrue(self.registered("run.ps1", 10)["risky"])
        self.assertEqual(self.registered("..\\..\\windows\\evil.pdf", 10)["name"], "evil.pdf")
        self.assertEqual(files.safe_name("a\u202egpj.exe"), "agpj.exe")
        self.assertEqual(files.safe_name('what?:"*.txt'), "what____.txt")

    def test_off_switch_and_plan_without_team_chat(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g)
        self.as_owner()
        self.client.patch("/api/chat/settings/", {"allow_files": False}, format="json")
        off = {"detail": "Your admin has turned off file sharing."}
        self.assertEqual((self.register().status_code, self.register().json()), (403, off))
        self.assertEqual(self.register(token=self.karan_token).json(), off)
        self.assertEqual(self.client.get(DEVICE_FILES).json(), {"allowed": False, "max_bytes": 5 * GB, "keep_days": 30})
        # Files already sent can still be fetched.
        self.as_owner()
        self.assertEqual(self.ticket(OWNER_FILES, line["file"]["id"]).status_code, 200)
        self.client.patch("/api/chat/settings/", {"allow_files": True}, format="json")
        Plan.objects.filter(code="businesses").update(modules=["usb_alerts"])
        self.assertEqual(self.register().status_code, 403)
        self.assertEqual(self.register(token=self.karan_token).status_code, 403)
        self.assertFalse(self.client.get(DEVICE_FILES).json()["allowed"])
        self.assertEqual(self.client.get(f"{DEVICE_FILES}serve-ticket/").status_code, 403)

    def test_too_many_waiting_per_sender(self) -> None:
        for _ in range(files.MAX_WAITING):
            ChatFile.objects.create(owner=self.owner, by_member=self.karan, name="a.txt", size=1, holder="d1")
        r = self.register(token=self.karan_token)
        self.assertEqual((r.status_code, r.json()), (400, {"file": ["Too many files waiting. Send or remove some first."]}))
        self.assertEqual(self.register(token=self.emily_token).status_code, 201)
        self.assertEqual(self.register().status_code, 201)
        ChatFile.objects.filter(by_member=self.karan).update(created_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(self.register(token=self.karan_token).status_code, 201)

    def test_pc_without_a_member(self) -> None:
        Device.objects.filter(member=self.karan).update(member=None)
        self.assertEqual(self.register(token=self.karan_token).status_code, 404)


class AttachTests(FileCase):
    def test_owner_group_line_old_and_new_shape(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g)
        self.assertEqual(line["body"], f"{CLIP} Quote-Lumen.pdf (2.1 MB)\nFinal version")
        f = line["file"]
        self.assertEqual(set(f), FILE_KEYS)
        self.assertEqual((f["caption"], f["mine"], f["state"]), ("Final version", True, "ok"))
        # The member's view of the same line.
        self.device(self.karan_token)
        seen = self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").json()[-1]
        self.assertEqual((seen["body"], seen["file"]["mine"], seen["file"]["sender"]["first_name"]),
                         (line["body"], False, "Ananya"))
        self.as_owner()
        self.assertIsNone(self.owner_lines(g["id"])[0]["file"])
        self.assertTrue(self.client.get(GROUPS).json()[0]["last"]["text"].startswith(f"{CLIP} Quote-Lumen.pdf"))

    def test_big_files_show_gb_in_the_fallback(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g, name="Shoot.mov", size=int(1.4 * GB), body="")
        self.assertEqual(line["body"], f"{CLIP} Shoot.mov (1.4 GB)")
        self.assertEqual([files.human_size(n) for n in (1, 348160, 2202009, 2 * GB, int(4.25 * GB))],
                         ["1 KB", "340 KB", "2.1 MB", "2 GB", "4.2 GB"])

    def test_a_batch_is_several_lines_with_one_batch_id(self) -> None:
        g = self.make_group()
        batch = str(uuid.uuid4())
        self.device(self.karan_token)
        ids = [self.registered(f"p{i}.jpg", 1000, token=self.karan_token, batch_id=batch, thumb_b64=b64(JPEG))["id"]
               for i in range(3)]
        for i, fid in enumerate(ids):
            body = "From the site visit" if i == len(ids) - 1 else ""
            r = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": body, "file_id": fid}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
        got = [line["file"] for line in self.owner_lines(g["id"]) if line["file"]]
        self.assertEqual([x["batch_id"] for x in got], [batch] * 3)
        self.assertEqual([x["caption"] for x in got], ["", "", "From the site visit"])
        self.assertTrue(all(x["thumb_url"].startswith("http://testserver/api/chat/files/") for x in got))

    def test_member_line_in_a_direct_chat(self) -> None:
        direct = self.direct_with_emily()
        f = self.registered("plan.docx", 4000, token=self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"file_id": f["id"]}, format="json")
        self.assertEqual((r.status_code, r.json()["body"]), (201, f"{CLIP} plan.docx (4 KB)"))
        self.device(self.emily_token)
        got = self.client.get(f"{DEVICE_CHAT}{direct['id']}/messages/").json()[-1]["file"]
        self.assertEqual((got["name"], got["mine"]), ("plan.docx", False))
        self.assertEqual(self.ticket(DEVICE_FILES, got["id"]).status_code, 200)

    def test_admin_chat_both_ways(self) -> None:
        f = self.registered("Brief.pdf", 209)
        r = self.client.post(f"/api/devices/members/{self.member.id}/message/", {"file_id": f["id"], "body": "Read this"},
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        line = r.json()
        self.assertEqual((line["title"], line["body"]), (f"{CLIP} Brief.pdf", f"{CLIP} Brief.pdf (1 KB)\nRead this"))
        self.assertEqual((line["file"]["caption"], line["file"]["mine"]), ("Read this", True))
        self.device(self.asha_token)
        mine = next(m for m in self.client.get("/api/devices/messages/").json() if m["id"] == line["id"])
        self.assertEqual((mine["file"]["mine"], mine["file"]["sender"]["name"]), (False, "Ananya Rao"))
        self.assertEqual(self.ticket(DEVICE_FILES, f["id"]).status_code, 200)
        self.device(self.karan_token)
        self.assertEqual(self.ticket(DEVICE_FILES, f["id"]).status_code, 404)
        # Member -> owner.
        f2 = self.registered("Timesheet.xlsx", 8, token=self.asha_token)
        r = self.client.post("/api/devices/messages/", {"file_id": f2["id"], "body": "For October"}, format="json")
        self.assertEqual((r.status_code, r.json()["file"]["mine"]), (201, True))
        self.as_owner()
        thread = self.client.get(f"/api/devices/members/{self.member.id}/messages/").json()
        got = next(m for m in thread if m["id"] == r.json()["id"])["file"]
        self.assertEqual((got["caption"], got["mine"], got["kind"]), ("For October", False, "document"))
        self.assertEqual(self.ticket(OWNER_FILES, f2["id"]).status_code, 200)

    def test_file_id_must_be_the_senders_own_fresh_shared_registration(self) -> None:
        g = self.make_group(ids=[self.member.id, self.karan.id])
        refused = {"file_id": ["That file wasn't found. Attach it again."]}
        url = f"{DEVICE_CHAT}{g['id']}/messages/"
        karans = self.registered(token=self.karan_token)
        self.device(self.asha_token)
        self.assertEqual(self.client.post(url, {"file_id": karans["id"]}, format="json").json(), refused)
        owners = self.registered()
        self.device(self.karan_token)
        self.assertEqual(self.client.post(url, {"file_id": owners["id"]}, format="json").json(), refused)
        self.as_owner()
        self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"file_id": karans["id"]}, format="json").json(),
                         refused)
        for bad in (str(uuid.uuid4()), "nope"):
            self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"file_id": bad}, format="json").json(),
                             refused)
        # Stopped or gone before it was sent: can't be attached.
        for change in ({"stopped_at": timezone.now()}, {"gone_at": timezone.now()},
                       {"created_at": timezone.now() - timedelta(minutes=61)}):
            f = self.registered()
            ChatFile.objects.filter(pk=f["id"]).update(**change)
            self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"file_id": f["id"]},
                                              format="json").json(), refused)
        self.device(self.karan_token)
        self.assertEqual(self.client.post(url, {"file_id": karans["id"]}, format="json").status_code, 201)
        self.assertEqual(self.client.post(url, {"file_id": karans["id"]}, format="json").json(), refused)
        self.assertEqual(Message.objects.count(), 0)

    def test_without_a_file_the_old_rules_and_errors_stand(self) -> None:
        g = self.make_group()
        self.assertEqual(self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": ""}, format="json").json(),
                         {"body": ["This field may not be blank."]})
        self.assertEqual(self.client.post(f"/api/devices/members/{self.member.id}/message/", {"body": "x"},
                                          format="json").json(), {"title": ["This field is required."]})

    def test_announce_group_refuses_a_members_file(self) -> None:
        g = self.make_group(announce=True)
        f = self.registered(token=self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"file_id": f["id"]}, format="json")
        self.assertEqual((r.status_code, r.json()), (403, {"detail": "Only the admin can post in this group."}))
        self.assertIsNone(ChatFile.objects.get(pk=f["id"]).attached_at)


class TicketTests(FileCase):
    def test_ticket_shape_and_signed_payload(self) -> None:
        g = self.make_group()
        f = self.group_file_line(g, token=self.karan_token, name="Site plan.pdf", size=5000)["file"]
        self.as_owner()
        before = timezone.now()
        r = self.ticket(OWNER_FILES, f["id"])
        self.assertEqual(r.status_code, 200, r.content)
        t = r.json()
        self.assertEqual(set(t), {"url", "inline_url", "status_url", "expires_at"})
        parts = urlsplit(t["url"])
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("http", "testserver", f"/relay/file/{f['id']}"))
        self.assertEqual(t["inline_url"], t["url"] + "&inline=1")
        token = parse_qs(parts.query)["t"][0]
        self.assertEqual(t["status_url"], f"http://testserver/relay/status/{f['id']}?t={t['url'].split('?t=')[1]}")
        self.assertEqual(tickets.read(token, tickets.GET), {
            "k": "get", "f": f["id"], "s": 5000, "n": "Site plan.pdf", "h": f"d{self.device_of(self.karan).id}",
            "fn": "Karan", "i": "application/pdf",
        })
        self.assertIsNone(tickets.read(token, tickets.SERVE))  # scoped: a get ticket can't wait or push
        expires = timezone.datetime.fromisoformat(t["expires_at"].replace("Z", "+00:00"))
        self.assertAlmostEqual((expires - before).total_seconds(), 900, delta=5)

    def test_inline_types(self) -> None:
        for name, inline in (("a.png", "image/png"), ("b.mp4", "video/mp4"), ("c.pdf", "application/pdf"),
                             ("d.docx", ""), ("e.svg", ""), ("f.mkv", ""), ("g.heic", ""), ("h.exe", "")):
            f = self.registered(name, 10)
            token = parse_qs(urlsplit(self.ticket(OWNER_FILES, f["id"]).json()["url"]).query)["t"][0]
            self.assertEqual(tickets.read(token, tickets.GET)["i"], inline, name)

    @override_settings(ZINWE_RELAY_BASE="http://127.0.0.1:8100")
    def test_relay_base_setting(self) -> None:
        f = self.registered()
        self.assertTrue(self.ticket(OWNER_FILES, f["id"]).json()["url"].startswith(
            f"http://127.0.0.1:8100/relay/file/{f['id']}?t="))
        self.assertEqual(self.client.get(f"{OWNER_FILES}serve-ticket/?browser_id={BROWSER}").json()["push_base"],
                         "http://127.0.0.1:8100/relay/push/")

    def test_owner_never_reaches_a_direct_chat_file(self) -> None:
        direct = self.direct_with_emily()
        f = self.registered("secret.pdf", token=self.karan_token, thumb_b64=b64(JPEG))
        self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"file_id": f["id"]}, format="json")
        self.as_owner()
        for path in ("ticket/", "thumb/", "", "stop/"):
            call = self.client.get if path in ("thumb/", "") else self.client.post
            self.assertEqual(call(f"{OWNER_FILES}{f['id']}/{path}").status_code, 404, path)
        self.assertEqual(self.client.patch(f"{OWNER_FILES}{f['id']}/", {"gone": True}, format="json").status_code, 404)
        # Even once it isn't shared any more: 404, never 410, so nothing about it leaks.
        ChatFile.objects.filter(pk=f["id"]).update(stopped_at=timezone.now())
        self.assertEqual(self.ticket(OWNER_FILES, f["id"]).status_code, 404)
        self.assertNotIn(ChatFile, site._registry)
        self.device(self.asha_token)  # not in that chat
        self.assertEqual(self.ticket(DEVICE_FILES, f["id"]).status_code, 404)
        self.device(self.emily_token)
        self.assertEqual(self.ticket(DEVICE_FILES, f["id"]).status_code, 410)

    def test_other_company_non_members_and_unattached(self) -> None:
        g = self.make_group(ids=[self.member.id, self.karan.id])
        fid = self.group_file_line(g)["file"]["id"]
        self.device(self.jake_token)
        self.assertEqual(self.ticket(DEVICE_FILES, fid).status_code, 404)
        other = User.objects.create_user("other@corp.in", "pw-12345678")
        give_plan(other, "businesses")
        stranger = Member.objects.create(owner=other, name="Sam Stone", email="sam@corp.in")
        self.device(self._enroll(stranger, "SAM-PC"))
        self.assertEqual(self.ticket(DEVICE_FILES, fid).status_code, 404)
        self.assertEqual(self.client.get(f"{DEVICE_FILES}{fid}/thumb/").status_code, 404)
        self.client.credentials()
        self.client.force_authenticate(user=other)
        self.assertEqual(self.ticket(OWNER_FILES, fid).status_code, 404)
        self.client.force_authenticate(user=None)
        # Not sent yet: only its sender.
        mine = self.registered(token=self.karan_token)
        self.assertEqual(self.ticket(DEVICE_FILES, mine["id"]).status_code, 200)
        self.device(self.asha_token)
        self.assertEqual(self.ticket(DEVICE_FILES, mine["id"]).status_code, 404)
        self.as_owner()
        self.assertEqual(self.ticket(OWNER_FILES, mine["id"]).status_code, 404)
        self.assertEqual(self.ticket(OWNER_FILES, str(uuid.uuid4())).status_code, 404)

    def test_member_who_left_keeps_older_files_only(self) -> None:
        g = self.make_group(ids=[self.member.id, self.karan.id])
        before = self.group_file_line(g)["file"]["id"]
        self.as_owner()
        self.client.patch(f"{GROUPS}{g['id']}/", {"member_ids": [self.karan.id]}, format="json")
        after = self.group_file_line(g)["file"]["id"]
        self.device(self.asha_token)
        self.assertEqual(self.ticket(DEVICE_FILES, before).status_code, 200)
        self.assertEqual(self.ticket(DEVICE_FILES, after).status_code, 404)

    def test_not_shared_any_more_is_410(self) -> None:
        g = self.make_group()
        for change, state in (({"stopped_at": timezone.now()}, "stopped"), ({"gone_at": timezone.now()}, "gone"),
                              ({"expires_at": timezone.now() - timedelta(seconds=1)}, "old")):
            line = self.group_file_line(g, thumb_b64=b64(JPEG))
            fid = line["file"]["id"]
            ChatFile.objects.filter(pk=fid).update(**change)
            self.as_owner()
            r = self.ticket(OWNER_FILES, fid)
            self.assertEqual((r.status_code, r.json()), (410, NOT_SHARED))
            self.assertEqual(self.client.get(f"{OWNER_FILES}{fid}/thumb/").status_code, 404)
            self.device(self.karan_token)
            self.assertEqual(self.ticket(DEVICE_FILES, fid).status_code, 410)
            shown = self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").json()[-1]["file"]
            self.assertEqual((shown["state"], shown["available"], shown["expired"], shown["thumb_url"]),
                             (state, False, True, None))

    def test_serve_tickets(self) -> None:
        self.device(self.karan_token)
        r = self.client.get(f"{DEVICE_FILES}serve-ticket/")
        self.assertEqual(r.status_code, 200, r.content)
        s = r.json()
        self.assertEqual(set(s), {"ticket", "wait_url", "push_base", "expires_at"})
        holder = f"d{self.device_of(self.karan).id}"
        self.assertEqual(tickets.read(s["ticket"], tickets.SERVE), {"k": "serve", "h": holder})
        self.assertIsNone(tickets.read(s["ticket"], tickets.GET))  # a serve ticket can't download
        self.assertEqual(unquote(s["wait_url"]), f"http://testserver/relay/wait?t={s['ticket']}")
        self.assertEqual(s["push_base"], "http://testserver/relay/push/")
        self.as_owner()
        r = self.client.get(f"{OWNER_FILES}serve-ticket/")
        self.assertEqual((r.status_code, r.json()), (400, {"browser_id": ["This field is required."]}))
        s = self.client.get(f"{OWNER_FILES}serve-ticket/?browser_id={BROWSER}").json()
        self.assertEqual(tickets.read(s["ticket"], tickets.SERVE)["h"], f"b{self.owner.id}.{BROWSER}")


class HolderTests(FileCase):
    def test_patch_sha_and_gone_by_the_holder_only(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g, token=self.karan_token)
        fid = line["file"]["id"]
        url = f"{DEVICE_FILES}{fid}/"
        # Someone who can see the file but doesn't hold it.
        self.device(self.asha_token)
        self.assertEqual(self.client.patch(url, {"sha256": "b" * 64}, format="json").status_code, 404)
        self.as_owner()
        self.assertEqual(self.client.patch(f"{OWNER_FILES}{fid}/", {"gone": True}, format="json").status_code, 404)
        # The holder.
        self.device(self.karan_token)
        r = self.client.patch(url, {"sha256": "B" * 64}, format="json")
        self.assertEqual((r.status_code, r.json()["state"]), (200, "ok"))
        self.assertEqual(ChatFile.objects.get(pk=fid).sha256, "b" * 64)
        self.assertEqual(self.client.patch(url, {"sha256": "b" * 64}, format="json").status_code, 200)  # same: fine
        r = self.client.patch(url, {"sha256": "c" * 64}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"sha256": ["The file changed. Share it again."]}))
        self.assertEqual(self.client.patch(url, {"sha256": "zz"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {}, format="json").status_code, 400)
        r = self.client.patch(url, {"gone": True}, format="json")
        self.assertEqual((r.status_code, r.json()["state"], r.json()["available"]), (200, "gone", False))
        self.assertEqual(self.ticket(DEVICE_FILES, fid).status_code, 410)
        # The same member on another PC isn't the holder.
        ChatFile.objects.filter(pk=fid).update(holder="d999999", gone_at=None)
        self.assertEqual(self.client.patch(url, {"gone": True}, format="json").status_code, 404)

    def test_owner_patches_own_browser_files(self) -> None:
        f = self.registered()
        r = self.client.patch(f"{OWNER_FILES}{f['id']}/", {"sha256": "d" * 64}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.client.get(f"{OWNER_FILES}{f['id']}/").json()["id"], f["id"])

    def test_stop_by_the_sender_only(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g, token=self.karan_token, thumb_b64=b64(JPEG))
        fid = line["file"]["id"]
        self.device(self.asha_token)
        self.assertEqual(self.client.post(f"{DEVICE_FILES}{fid}/stop/").status_code, 404)
        self.as_owner()
        self.assertEqual(self.client.post(f"{OWNER_FILES}{fid}/stop/").status_code, 404)
        # The sender, even from a PC that doesn't hold it.
        ChatFile.objects.filter(pk=fid).update(holder="d999999")
        self.device(self.karan_token)
        r = self.client.post(f"{DEVICE_FILES}{fid}/stop/")
        self.assertEqual((r.status_code, r.json()["state"], r.json()["thumb_url"]), (200, "stopped", None))
        self.assertIsNone(ChatFile.objects.get(pk=fid).thumb)
        self.assertEqual(self.client.post(f"{DEVICE_FILES}{fid}/stop/").status_code, 200)  # again: fine
        self.assertEqual(self.ticket(DEVICE_FILES, fid).json(), NOT_SHARED)
        # The owner stops their own.
        own = self.group_file_line(g)["file"]["id"]
        self.assertEqual(self.client.post(f"{OWNER_FILES}{own}/stop/").json()["state"], "stopped")

    def test_shared_by_me(self) -> None:
        g = self.make_group()
        direct = self.direct_with_emily()
        a = self.group_file_line(g, token=self.karan_token, name="a.pdf")["file"]["id"]
        f = self.registered("b.jpg", 10, token=self.karan_token)
        self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"file_id": f["id"]}, format="json")
        stopped = self.group_file_line(g, token=self.karan_token, name="c.pdf")["file"]["id"]
        self.client.post(f"{DEVICE_FILES}{stopped}/stop/")
        self.registered("not-sent.pdf", 10, token=self.karan_token)
        self.group_file_line(g, name="owners.pdf")
        self.device(self.karan_token)
        listed = self.client.get(f"{DEVICE_FILES}shared/").json()
        self.assertEqual([x["name"] for x in listed], ["b.jpg", "a.pdf"])
        first = listed[0]
        self.assertEqual(set(first), FILE_KEYS | {"holder", "local_ref", "sha256", "chat"})
        self.assertEqual(first["chat"], {"kind": "direct", "id": direct["id"], "name": "Emily Carter"})
        self.assertEqual(listed[1]["chat"], {"kind": "group", "id": g["id"], "name": "Design"})
        self.assertEqual((first["holder"], first["local_ref"]),
                         (f"d{self.device_of(self.karan).id}", "C:\\Users\\me\\Documents\\b.jpg"))
        self.assertTrue(all(x["mine"] for x in listed))
        self.as_owner()
        f = self.registered("Brief.pdf", 10)
        self.client.post(f"/api/devices/members/{self.member.id}/message/", {"file_id": f["id"]}, format="json")
        listed = self.client.get(f"{OWNER_FILES}shared/").json()
        self.assertEqual([(x["name"], x["chat"]["kind"]) for x in listed], [("Brief.pdf", "admin"), ("owners.pdf", "group")])
        self.assertEqual(listed[0]["chat"]["name"], self.member.name)
        self.assertEqual(listed[0]["holder"], f"b{self.owner.id}.{BROWSER}")


class LifecycleTests(FileCase):
    def test_clean_up_marks_old_files_and_deletes_unsent_ones(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g, thumb_b64=b64(JPEG))
        fid = line["file"]["id"]
        fresh = self.registered("fresh.txt", 1)
        stale = self.registered("stale.txt", 1)
        ChatFile.objects.filter(pk=stale["id"]).update(created_at=timezone.now() - timedelta(minutes=61))
        ChatFile.objects.filter(pk=fid).update(expires_at=timezone.now() - timedelta(seconds=1))
        result = files.prune_chat_files()
        self.assertEqual((result.expired, result.unattached, result.failed), (1, 1, []))
        self.assertIn("marked too old", result.summary())
        row = ChatFile.objects.get(pk=fid)
        self.assertIsNotNone(row.deleted_at)
        self.assertIsNone(row.thumb)
        self.assertFalse(ChatFile.objects.filter(pk=stale["id"]).exists())
        self.assertTrue(ChatFile.objects.filter(pk=fresh["id"]).exists())
        self.assertEqual(files.prune_chat_files().expired, 0)  # once
        shown = self.owner_lines(g["id"])[-1]["file"]
        self.assertEqual((shown["name"], shown["state"], shown["available"]), ("Quote-Lumen.pdf", "old", False))
        # Kept 30 days from when it was shared.
        self.assertEqual(files.FILE_KEEP_DAYS, 30)

    def test_run_jobs_runs_the_clean_up(self) -> None:
        from z_main.management.commands.run_jobs import JOBS

        self.assertIn(files.prune_chat_files, [job for _, job in JOBS])

    def test_cascade_and_delete(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g)
        ChatMessage.objects.filter(pk=line["id"]).update(created_at=timezone.now() - timedelta(days=4000))
        self.assertEqual(prune_chat().lines, 1)
        self.assertFalse(ChatFile.objects.exists())
        line = self.group_file_line(g)
        self.assertEqual(self.client.delete(f"{GROUPS}{g['id']}/").status_code, 204)
        self.assertFalse(ChatFile.objects.exists())

    def test_file_lines_cant_be_edited_and_delete_takes_the_file(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g)
        url = f"{GROUPS}{g['id']}/messages/{line['id']}/"
        r = self.client.patch(url, {"body": "changed"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"body": ["A file can't be edited. Delete it and send it again."]}))
        r = self.client.delete(url)
        self.assertEqual((r.status_code, r.json()["deleted"], r.json()["file"]), (200, True, None))
        self.assertFalse(ChatFile.objects.exists())
        self.assertEqual(self.ticket(OWNER_FILES, line["file"]["id"]).status_code, 404)
        mine = self.group_file_line(g, token=self.karan_token)
        ChatMessage.objects.filter(pk=mine["id"]).update(created_at=timezone.now() - timedelta(minutes=16))
        ConversationMember.objects.filter(member=self.karan).update(joined_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.client.delete(f"{DEVICE_CHAT}{g['id']}/messages/{mine['id']}/").status_code, 400)
        self.assertTrue(ChatFile.objects.filter(pk=mine["file"]["id"]).exists())

    def test_a_report_keeps_the_fallback_text(self) -> None:
        g = self.make_group()
        line = self.group_file_line(g, token=self.karan_token, size=900)
        self.device(self.asha_token)
        r = self.client.post(f"{DEVICE_CHAT}{g['id']}/report/", {"message_id": line["id"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(ChatReport.objects.get().message_body, f"{CLIP} Quote-Lumen.pdf (1 KB)\nFinal version")

    def test_round_one_files_are_stopped_by_the_migration(self) -> None:
        old = ChatFile.objects.create(owner=self.owner, name="r1.pdf", size=10, sha256="0" * 64)
        new = ChatFile.objects.create(owner=self.owner, name="r2.pdf", size=10, holder="d1")
        migration = importlib.import_module("z_chat.migrations.0004_files_stay_on_device")
        migration.stop_round_one_files(django_apps, None)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual((files.state(old), files.state(new)), ("stopped", "ok"))
