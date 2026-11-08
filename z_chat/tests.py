"""v1.4 team chat: groups (owner + members), system lines, announce, directs and their privacy, reports, settings."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.contrib.admin.sites import site
from django.test import RequestFactory
from django.utils import timezone

from z_auth.models import User
from z_devices.models import Member
from z_devices.tests import DeviceTestCase

from .models import ChatMessage, ChatReport, Conversation, ConversationMember
from .retention import prune_chat

GROUPS = "/api/chat/groups/"
DEVICE_CHAT = "/api/devices/chat/"


class ChatCase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner.full_name = "Ananya Rao"
        self.owner.save(update_fields=["full_name"])
        self.asha_token = self._enroll(self.member, "ASHA-PC")
        self.karan = Member.objects.create(owner=self.owner, name="Karan Malhotra", email="karan@acme.in")
        self.emily = Member.objects.create(owner=self.owner, name="Emily Carter", email="emily@acme.in")
        self.jake = Member.objects.create(owner=self.owner, name="Jake Thompson", email="jake@acme.in")
        self.karan_token = self._enroll(self.karan, "KARAN-PC")
        self.emily_token = self._enroll(self.emily, "EMILY-PC")
        self.jake_token = self._enroll(self.jake, "JAKE-PC")

    def _enroll(self, member: Member, host: str) -> str:
        response = self.enroll(email=member.email, member_id=member.member_code, machine_id=f"MID-{host}", hostname=host)
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()["device_token"]

    def make_group(self, name: str = "Design", ids: list[int] | None = None, **extra: Any) -> dict[str, Any]:
        self.as_owner()
        ids = ids if ids is not None else [self.member.id, self.karan.id]
        r = self.client.post(GROUPS, {"name": name, "member_ids": ids, **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def owner_lines(self, gid: int) -> list[dict[str, Any]]:
        self.as_owner()
        return self.client.get(f"{GROUPS}{gid}/messages/").json()

    def device(self, token: str) -> None:
        self.as_device(token)


class GroupOwnerTests(ChatCase):
    def test_create_group_shape_and_system_line(self) -> None:
        g = self.make_group(icon="M1 1")
        self.assertEqual(g["name"], "Design")
        self.assertEqual(g["member_ids"], [self.member.id, self.karan.id])
        self.assertEqual(set(g), {"id", "kind", "name", "icon", "announce", "members", "member_ids", "unread", "last",
                                  "last_message_at", "created_at", "pinned", "muted"})
        lines = self.owner_lines(g["id"])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0]["system"])
        self.assertEqual(lines[0]["body"], "You made the group and added Asha and Karan.")
        self.assertEqual(lines[0]["by"]["kind"], "system")

    def test_validation(self) -> None:
        self.make_group()
        self.as_owner()
        r = self.client.post(GROUPS, {"name": "design", "member_ids": [self.member.id]}, format="json")
        self.assertEqual(r.json(), {"name": ["There is already a group called Design."]})
        r = self.client.post(GROUPS, {"name": "X", "member_ids": []}, format="json")
        self.assertEqual(r.json(), {"member_ids": ["Pick at least one person."]})
        r = self.client.post(GROUPS, {"name": "", "member_ids": [self.member.id]}, format="json")
        self.assertEqual(r.json(), {"name": ["Give the group a name."]})
        other = User.objects.create_user("o@o.in", "pw-12345678")
        stranger = Member.objects.create(owner=other, name="S", email="s@o.in")
        r = self.client.post(GROUPS, {"name": "Y", "member_ids": [stranger.id]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_edit_writes_the_exact_system_lines(self) -> None:
        g = self.make_group()
        self.as_owner()
        r = self.client.patch(f"{GROUPS}{g['id']}/", {
            "name": "Design team", "member_ids": [self.karan.id, self.emily.id, self.jake.id], "announce": True,
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        bodies = [x["body"] for x in self.owner_lines(g["id"])][1:]
        self.assertEqual(bodies, [
            "You renamed the group to Design team.",
            "You added Emily and Jake.",
            "You removed Asha. They keep what was said before they left.",
            "Only you can post here now.",
        ])
        self.client.patch(f"{GROUPS}{g['id']}/", {"announce": False}, format="json")
        self.assertEqual(self.owner_lines(g["id"])[-1]["body"], "Everyone can post here again.")

    def test_messages_edit_delete_window_and_seen_by(self) -> None:
        g = self.make_group()
        r = self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": "Hello team"}, format="json")
        self.assertEqual(r.status_code, 201)
        line = r.json()
        self.assertEqual(line["by"], {"kind": "owner", "member_id": None, "name": "Ananya Rao"})
        self.device(self.karan_token)
        self.client.post(f"{DEVICE_CHAT}{g['id']}/read/")
        seen = self.owner_lines(g["id"])[-1]["seen_by"]
        self.assertEqual(seen, [{"member_id": self.karan.id, "name": "Karan Malhotra"}])
        r = self.client.patch(f"{GROUPS}{g['id']}/messages/{line['id']}/", {"body": "Hello all"}, format="json")
        self.assertEqual((r.json()["body"], r.json()["edited"]), ("Hello all", True))
        ChatMessage.objects.filter(pk=line["id"]).update(created_at=timezone.now() - timedelta(minutes=16))
        r = self.client.delete(f"{GROUPS}{g['id']}/messages/{line['id']}/")
        self.assertEqual(r.json(), {"detail": ["You can change a message for 15 minutes after sending it."]})
        r = self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": "Second"}, format="json")
        r = self.client.delete(f"{GROUPS}{g['id']}/messages/{r.json()['id']}/")
        self.assertEqual((r.json()["deleted"], r.json()["body"]), (True, ""))
        self.assertEqual(ChatMessage.objects.get(pk=r.json()["id"]).body, "")

    def test_owner_cannot_edit_a_members_line(self) -> None:
        g = self.make_group()
        self.device(self.karan_token)
        mid = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "hi"}, format="json").json()["id"]
        self.as_owner()
        self.assertEqual(self.client.patch(f"{GROUPS}{g['id']}/messages/{mid}/", {"body": "x"}, format="json").status_code, 403)

    def test_unread_read_and_read_all(self) -> None:
        g = self.make_group()
        self.device(self.karan_token)
        self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "One"}, format="json")
        self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "Two"}, format="json")
        self.as_owner()
        self.assertEqual(self.client.get(GROUPS).json()[0]["unread"], 2)
        self.assertEqual(self.client.get(GROUPS).json()[0]["last"]["text"], "Two")
        self.assertEqual(self.client.post("/api/chat/read-all/").json(), {"ok": True, "groups": 1})
        self.assertEqual(self.client.get(GROUPS).json()[0]["unread"], 0)

    def test_before_paging(self) -> None:
        g = self.make_group()
        ids = [self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": f"m{i}"}, format="json").json()["id"]
               for i in range(5)]
        page = self.client.get(f"{GROUPS}{g['id']}/messages/?before={ids[3]}&limit=2").json()
        self.assertEqual([x["body"] for x in page], ["m1", "m2"])


class DevicePrivacyTests(ChatCase):
    def test_device_list_shows_groups_and_directs_with_member_wording(self) -> None:
        g = self.make_group(ids=[self.member.id, self.karan.id, self.emily.id])
        self.device(self.karan_token)
        convs = self.client.get(DEVICE_CHAT).json()
        self.assertEqual([c["id"] for c in convs], [g["id"]])
        self.assertTrue(convs[0]["can_post"])
        lines = self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").json()
        self.assertEqual(lines[0]["body"], "Ananya made the group and added Asha, Karan and Emily.")
        # People: teammates sharing a group.
        people = self.client.get(f"{DEVICE_CHAT}people/").json()
        self.assertEqual({p["member_id"] for p in people}, {self.member.id, self.emily.id})
        # Direct chat, then the owner can never reach it.
        r = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        direct = r.json()
        self.assertEqual((direct["kind"], direct["name"], direct["member_id"]), ("direct", "Emily Carter", self.emily.id))
        self.assertEqual(self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json").status_code, 200)
        self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"body": "secret plan"}, format="json")
        self.device(self.emily_token)
        emily_lines = self.client.get(f"{DEVICE_CHAT}{direct['id']}/messages/").json()
        self.assertEqual([x["body"] for x in emily_lines], ["secret plan"])
        self.assertEqual(next(c for c in self.client.get(DEVICE_CHAT).json() if c["id"] == direct["id"])["unread"], 1)
        # Owner: every route treats the direct as not found; the group list never shows it.
        self.as_owner()
        for url in (f"{GROUPS}{direct['id']}/", f"{GROUPS}{direct['id']}/messages/"):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(f"{GROUPS}{direct['id']}/messages/", {"body": "x"}, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"{GROUPS}{direct['id']}/read/").status_code, 404)
        self.assertNotIn(direct["id"], [x["id"] for x in self.client.get(GROUPS).json()])
        self.assertNotIn("secret plan", str(self.client.get(GROUPS).json()))
        # A third member can't see it either.
        self.device(self.asha_token)
        self.assertEqual(self.client.get(f"{DEVICE_CHAT}{direct['id']}/messages/").status_code, 404)

    def test_django_admin_never_lists_direct_lines(self) -> None:
        g = self.make_group(ids=[self.karan.id, self.emily.id])
        self.device(self.karan_token)
        direct = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json").json()
        self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"body": "private"}, format="json")
        self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "public"}, format="json")
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser("root@zinwe.io", "pw-12345678")
        admin = site._registry[ChatMessage]
        bodies = [m.body for m in admin.get_queryset(request)]
        self.assertIn("public", bodies)
        self.assertNotIn("private", bodies)
        conv_admin = site._registry[Conversation]
        self.assertEqual(conv_admin.group_name(Conversation.objects.get(pk=direct["id"])), "(direct chat)")
        self.assertNotIn("body", str(conv_admin.fields))

    def test_direct_needs_a_shared_group_and_the_owner_setting(self) -> None:
        self.make_group(ids=[self.karan.id, self.emily.id])
        self.device(self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.jake.id}, format="json")
        self.assertEqual(r.json(), {"member_id": ["You can message people you share a group with."]})
        self.as_owner()
        self.assertEqual(self.client.get("/api/chat/settings/").json(), {"allow_member_dm": True})
        self.assertEqual(self.client.patch("/api/chat/settings/", {"allow_member_dm": False}, format="json").json(),
                         {"allow_member_dm": False})
        self.device(self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get(f"{DEVICE_CHAT}people/").json(), [])

    def test_existing_direct_turns_read_only_when_dm_is_off(self) -> None:
        self.make_group(ids=[self.karan.id, self.emily.id])
        self.device(self.karan_token)
        direct = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json").json()
        self.as_owner()
        self.client.patch("/api/chat/settings/", {"allow_member_dm": False}, format="json")
        self.device(self.karan_token)
        conv = next(c for c in self.client.get(DEVICE_CHAT).json() if c["id"] == direct["id"])
        self.assertEqual((conv["can_post"], conv["read_only"]), (False, True))
        r = self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"body": "x"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_announce_group_only_owner_posts(self) -> None:
        g = self.make_group(announce=True)
        self.assertEqual(self.owner_lines(g["id"])[0]["body"],
                         "You made the group and added Asha and Karan. Only you can post here.")
        self.device(self.karan_token)
        r = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "can I?"}, format="json")
        self.assertEqual(r.json(), {"detail": "Only the admin can post in this group."})
        conv = self.client.get(DEVICE_CHAT).json()[0]
        self.assertEqual((conv["can_post"], conv["read_only"], conv["announce"]), (False, False, True))
        self.assertEqual(self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").json()[0]["body"],
                         "Ananya made the group and added Asha and Karan. Only Ananya can post here.")

    def test_removed_member_keeps_history_read_only(self) -> None:
        g = self.make_group()
        self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": "before"}, format="json")
        self.client.patch(f"{GROUPS}{g['id']}/", {"member_ids": [self.karan.id]}, format="json")
        self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": "after"}, format="json")
        self.device(self.asha_token)
        conv = self.client.get(DEVICE_CHAT).json()[0]
        self.assertEqual((conv["read_only"], conv["can_post"]), (True, False))
        bodies = [x["body"] for x in self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").json()]
        self.assertIn("before", bodies)
        self.assertNotIn("after", bodies)
        self.assertEqual(self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "x"}, format="json").status_code, 403)
        ConversationMember.objects.filter(member=self.member).update(left_at=timezone.now() - timedelta(days=31))
        self.assertEqual(self.client.get(DEVICE_CHAT).json(), [])

    def test_switched_off_member_leaves_directs_which_stay_read_only(self) -> None:
        from z_devices.services import deactivate_member

        self.make_group(ids=[self.karan.id, self.emily.id])
        self.device(self.karan_token)
        direct = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json").json()
        deactivate_member(Member.objects.get(pk=self.emily.pk))
        conv = next(c for c in self.client.get(DEVICE_CHAT).json() if c["id"] == direct["id"])
        self.assertTrue(conv["read_only"])
        ConversationMember.objects.filter(member=self.emily).update(left_at=timezone.now() - timedelta(days=31))
        self.assertNotIn(direct["id"], [c["id"] for c in self.client.get(DEVICE_CHAT).json()])

    def test_member_flags_pins_mute_unread(self) -> None:
        groups = [self.make_group(f"G{i}", ids=[self.karan.id])["id"] for i in range(4)]
        self.client.post(f"{GROUPS}{groups[0]}/messages/", {"body": "hello"}, format="json")
        self.device(self.karan_token)
        for gid in groups[:3]:
            self.assertTrue(self.client.patch(f"{DEVICE_CHAT}{gid}/", {"pinned": True}, format="json").json()["pinned"])
        r = self.client.patch(f"{DEVICE_CHAT}{groups[3]}/", {"pinned": True}, format="json")
        self.assertEqual(r.json(), {"pinned": ["You can pin up to 3 chats."]})
        self.client.post(f"{DEVICE_CHAT}{groups[0]}/read/")
        r = self.client.patch(f"{DEVICE_CHAT}{groups[0]}/", {"unread": True, "muted": True}, format="json").json()
        self.assertEqual((r["muted"], r["unread"]), (True, 1))

    def test_member_edit_window_and_others_lines(self) -> None:
        g = self.make_group()
        self.device(self.karan_token)
        mid = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "typo"}, format="json").json()["id"]
        self.assertEqual(self.client.patch(f"{DEVICE_CHAT}{g['id']}/messages/{mid}/", {"body": "fixed"}, format="json").json()["body"], "fixed")
        self.device(self.asha_token)
        self.assertEqual(self.client.delete(f"{DEVICE_CHAT}{g['id']}/messages/{mid}/").status_code, 403)
        r = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "re", "reply_to": mid}, format="json").json()
        self.assertEqual(r["reply_to"]["id"], mid)

    def test_plan_without_team_chat(self) -> None:
        from z_auth.models import Plan

        g = self.make_group()
        Plan.objects.filter(code="businesses").update(modules=["usb_alerts"])
        self.as_owner()
        self.assertEqual(self.client.get(GROUPS).status_code, 403)
        self.device(self.karan_token)
        self.assertEqual(self.client.get(DEVICE_CHAT).json(), [])
        self.assertEqual(self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").status_code, 403)


class ReportTests(ChatCase):
    def test_report_shares_only_the_reported_line(self) -> None:
        self.make_group(ids=[self.karan.id, self.emily.id])
        self.device(self.karan_token)
        direct = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json").json()
        self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"body": "unrelated chat"}, format="json")
        bad = self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"body": "rude line"}, format="json").json()
        self.device(self.emily_token)
        r = self.client.post(f"{DEVICE_CHAT}{direct['id']}/report/", {"message_id": bad["id"], "note": "Not OK."}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        mine = self.client.post(f"{DEVICE_CHAT}{direct['id']}/messages/", {"body": "mine"}, format="json").json()
        r = self.client.post(f"{DEVICE_CHAT}{direct['id']}/report/", {"message_id": mine["id"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.as_owner()
        reports = self.client.get("/api/chat/reports/").json()
        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(rep["conversation"], {"id": None, "kind": "direct", "name": "A chat between team members"})
        self.assertEqual((rep["message"]["body"], rep["message"]["by"]), ("rude line", "Karan Malhotra"))
        self.assertEqual(rep["reporter"]["member_id"], self.emily.id)
        self.assertNotIn("unrelated chat", str(reports))
        r = self.client.post(f"/api/chat/reports/{rep['id']}/handled/").json()
        self.assertTrue(r["handled"])
        self.assertIsNotNone(ChatReport.objects.get().handled_at)


class DeleteGroupAndPrefsTests(ChatCase):
    def test_owner_deletes_a_group_for_everyone_reports_and_tasks_stay(self) -> None:
        from z_devices.models import Task

        g = self.make_group(ids=[self.karan.id, self.emily.id])
        self.device(self.karan_token)
        bad = self.client.post(f"{DEVICE_CHAT}{g['id']}/messages/", {"body": "rude line"}, format="json").json()
        self.device(self.emily_token)
        self.assertEqual(self.client.post(f"{DEVICE_CHAT}{g['id']}/report/", {"message_id": bad["id"]},
                                          format="json").status_code, 201)
        task = Task.objects.create(owner=self.owner, member=self.karan, title="Hero", group_id=g["id"])
        self.as_owner()
        self.client.put("/api/chat/prefs/", {"pins": [f"g{g['id']}", f"p{self.karan.id}"], "muted": [], "marked": []},
                        format="json")
        r = self.client.delete(f"{GROUPS}{g['id']}/")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.client.get(f"{GROUPS}{g['id']}/").status_code, 404)
        self.assertFalse(ChatMessage.objects.filter(conversation_id=g["id"]).exists())
        task.refresh_from_db()
        self.assertIsNone(task.group_id)
        rep = self.client.get("/api/chat/reports/").json()[0]
        self.assertEqual(rep["conversation"], {"id": None, "kind": "group", "name": "Design (deleted)"})
        self.assertEqual(rep["message"]["body"], "rude line")
        self.assertEqual(self.client.get("/api/chat/prefs/").json()["pins"], [f"p{self.karan.id}"])
        self.device(self.karan_token)
        self.assertEqual([c["id"] for c in self.client.get(DEVICE_CHAT).json() if c["kind"] == "group"], [])
        self.assertEqual(self.client.get(f"{DEVICE_CHAT}{g['id']}/messages/").status_code, 404)

    def test_a_direct_chat_cannot_be_deleted_by_the_owner(self) -> None:
        self.make_group(ids=[self.karan.id, self.emily.id])
        self.device(self.karan_token)
        direct = self.client.post(f"{DEVICE_CHAT}direct/", {"member_id": self.emily.id}, format="json").json()
        self.as_owner()
        self.assertEqual(self.client.delete(f"{GROUPS}{direct['id']}/").status_code, 404)
        self.assertTrue(Conversation.objects.filter(pk=direct["id"]).exists())

    def test_owner_prefs_round_trip_and_mirror_group_flags(self) -> None:
        g = self.make_group()
        self.as_owner()
        self.assertEqual(self.client.get("/api/chat/prefs/").json(), {"pins": [], "muted": [], "marked": []})
        body = {"pins": [f"p{self.karan.id}", f"g{g['id']}"], "muted": [f"g{g['id']}"], "marked": [f"p{self.emily.id}"]}
        r = self.client.put("/api/chat/prefs/", body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), body)
        group = self.client.get(f"{GROUPS}{g['id']}/").json()
        self.assertEqual((group["pinned"], group["muted"]), (True, True))
        self.client.patch(f"{GROUPS}{g['id']}/", {"muted": False}, format="json")
        self.assertEqual(self.client.get("/api/chat/prefs/").json()["pins"], body["pins"])
        r = self.client.put("/api/chat/prefs/", {"pins": ["p1", "p2", "p3", "p4"], "muted": [], "marked": []}, format="json")
        self.assertEqual(r.json(), {"pins": ["You can pin up to 3 chats."]})
        r = self.client.put("/api/chat/prefs/", {"pins": ["x1"], "muted": [], "marked": []}, format="json")
        self.assertEqual(r.status_code, 400)


class RetentionTests(ChatCase):
    def test_lines_past_the_plan_window_are_hidden_then_pruned(self) -> None:
        g = self.make_group()
        self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": "old"}, format="json")
        self.client.post(f"{GROUPS}{g['id']}/messages/", {"body": "new"}, format="json")
        ChatMessage.objects.filter(body="old").update(created_at=timezone.now() - timedelta(days=370))
        self.assertNotIn("old", [x["body"] for x in self.owner_lines(g["id"])])
        self.assertEqual(ChatMessage.objects.filter(body="old").count(), 1)  # hidden, kept for the grace month
        self.assertEqual(prune_chat().lines, 0)
        ChatMessage.objects.filter(body="old").update(created_at=timezone.now() - timedelta(days=400))
        result = prune_chat()
        self.assertEqual(result.lines, 1)
        self.assertFalse(ChatMessage.objects.filter(body="old").exists())
        self.assertTrue(ChatMessage.objects.filter(body="new").exists())

    def test_me_and_plans_show_chat_history_days(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get("/api/auth/me/").json()["entitlements"]["chat_history_days"], 365)

