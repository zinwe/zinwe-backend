"""v1.4 team tasks: people, step owners, in-order gating, any-mode, repeat, owner actions, task chat (owner + device)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone
from rest_framework import status

from .models import Device, Member, Message, Task, TaskPerson
from .test_tasks import TASKS_URL, TaskTestCase, action_url, task_url
from .tests import ENROLL_URL


def chat_url(task_id: int) -> str:
    return f"{TASKS_URL}{task_id}/chat/"


class TeamTaskCase(TaskTestCase):
    """The owner, Asha (enrolled, self.token) and two more people with their own PCs."""

    def setUp(self) -> None:
        super().setUp()
        self.karan = Member.objects.create(owner=self.owner, name="Karan Malhotra", email="karan@acme.in", role="developer")
        self.emily = Member.objects.create(owner=self.owner, name="Emily Carter", email="emily@acme.in", role="designer")
        self.karan_token = self._enroll(self.karan, "KARAN-PC")
        self.emily_token = self._enroll(self.emily, "EMILY-PC")

    def _enroll(self, member: Member, host: str) -> str:
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(ENROLL_URL, {
                "email": member.email, "member_id": member.member_code, "machine_id": f"MID-{host}",
                "hostname": host, "agent_version": "1.4.0",
            }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return response.json()["device_token"]

    def team(self, **body: Any) -> dict[str, Any]:
        self.as_owner()
        payload = {
            "title": "Launch the Lumen landing page", "member_ids": [self.member.id, self.karan.id, self.emily.id],
            "in_order": True,
            "steps": [
                {"title": "Write the copy", "owner_id": self.member.id},
                {"title": "Build the hero", "owner_id": self.karan.id},
                {"title": "Check on phones", "owner_id": self.emily.id},
            ],
            **body,
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(TASKS_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return response.json()

    def device_act(self, token: str, task_id: int, action: str, **extra: Any):  # type: ignore[no-untyped-def]
        return self.member_act(task_id, action, token=token, **extra)

    def device_get(self, token: str) -> list[dict[str, Any]]:
        self.as_device(token)
        return self.client.get(TASKS_URL).json()


class CreateTeamTaskTests(TeamTaskCase):
    def test_create_shape_people_steps_and_first_person_line(self) -> None:
        body = self.team()
        self.assertEqual(body["status"], "sent")
        self.assertEqual(body["mode"], "all")
        self.assertTrue(body["in_order"])
        self.assertEqual(body["member"], self.member.id)
        self.assertEqual(body["member_id"], self.member.id)
        self.assertEqual([p["member_id"] for p in body["people"]], [self.member.id, self.karan.id, self.emily.id])
        self.assertTrue(all(p["state"] == "sent" and p["steps_total"] == 1 for p in body["people"]))
        self.assertEqual([s["owner_id"] for s in body["steps"]], [self.member.id, self.karan.id, self.emily.id])
        self.assertEqual(set(body["steps"][0]), {"id", "title", "done", "owner_id", "done_by_id", "done_at"})
        self.assertEqual(body["chat"], {"count": 0, "unread": 0, "last": None})
        for key in ("repeat", "next_on", "group_id", "cantdo_note", "reopen_note", "nudged_at", "repeat_of_id"):
            self.assertIn(key, body)
        # The legacy task line goes to the first person's chat.
        self.assertEqual(Message.objects.get(pk=body["message_id"]).member, self.member)

    def test_every_person_sees_it_on_their_pc_with_me_block(self) -> None:
        body = self.team()
        for token, mid in ((self.token, self.member.id), (self.karan_token, self.karan.id), (self.emily_token, self.emily.id)):
            tasks = self.device_get(token)
            self.assertEqual([t["id"] for t in tasks], [body["id"]])
            self.assertIn("me", tasks[0])
        asha = self.device_get(self.token)[0]["me"]
        self.assertTrue(asha["turn"])
        self.assertEqual(asha["can_tick"], [body["steps"][0]["id"]])
        self.assertEqual(self.device_get(self.karan_token)[0]["me"]["can_tick"], [])

    def test_validation(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.post(TASKS_URL, {"title": "x"}, format="json").status_code, 400)
        other = Member.objects.create(owner=self.other_owner()[0], name="Z", email="z@z.in")
        r = self.client.post(TASKS_URL, {"title": "x", "member_ids": [other.id]}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(TASKS_URL, {"title": "x", "member_ids": [self.karan.id],
                                          "steps": [{"title": "s", "owner_id": self.emily.id}]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("steps", r.json())
        self.assertFalse(Task.objects.exists())

    def test_one_person_is_always_mode_all(self) -> None:
        body = self.team(member_ids=[self.karan.id], mode="any", steps=[])
        self.assertEqual(body["mode"], "all")

    def test_device_cannot_create(self) -> None:
        self.as_device(self.token)
        self.assertEqual(self.client.post(TASKS_URL, {"title": "x", "member_ids": [self.member.id]}, format="json").status_code, 403)

    def test_group_task_goes_to_the_group_and_posts_a_card(self) -> None:
        from z_chat.models import ChatMessage
        from z_chat.services import create_group

        group = create_group(self.owner, "Design", "", [self.karan.id, self.emily.id])
        body = self.team(member_ids=[], group_id=group.pk, steps=[], in_order=False)
        self.assertEqual(body["group_id"], group.pk)
        self.assertEqual({p["member_id"] for p in body["people"]}, {self.karan.id, self.emily.id})
        card = ChatMessage.objects.get(task_id=body["id"])
        self.assertEqual((card.conversation_id, card.body), (group.pk, "Launch the Lumen landing page"))

    def test_group_without_post_in_group_has_no_card(self) -> None:
        from z_chat.models import ChatMessage
        from z_chat.services import create_group

        group = create_group(self.owner, "Design", "", [self.karan.id])
        body = self.team(member_ids=[self.karan.id], group_id=group.pk, post_in_group=False, steps=[])
        self.assertIsNone(body["group_id"])
        self.assertFalse(ChatMessage.objects.filter(task_id=body["id"]).exists())

    def test_old_create_endpoint_still_makes_a_person(self) -> None:
        body = self.make(steps=["a"])
        self.assertEqual([p["member_id"] for p in body["people"]], [self.member.id])
        self.assertEqual(TaskPerson.objects.filter(task_id=body["id"]).count(), 1)


class StepRuleTests(TeamTaskCase):
    def test_only_the_step_owner_ticks(self) -> None:
        body = self.team(in_order=False)
        r = self.device_act(self.karan_token, body["id"], "tick", step_id=body["steps"][0]["id"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"step_id": ["Only Asha can tick this."]})

    def test_in_order_gating_and_relay_to_review(self) -> None:
        body = self.team()
        s1, s2, s3 = (s["id"] for s in body["steps"])
        r = self.device_act(self.karan_token, body["id"], "tick", step_id=s2)
        self.assertEqual(r.json(), {"step_id": ["Step 1 comes first."]})
        r = self.device_act(self.token, body["id"], "tick", step_id=s1)
        self.assertEqual(r.status_code, 200)
        task = r.json()
        self.assertEqual(task["status"], "progress")
        self.assertEqual(task["people"][0]["state"], "done")
        self.assertEqual(task["steps"][0]["done_by_id"], self.member.id)
        self.assertEqual(task["me"]["can_tick"], [])
        karan = self.device_get(self.karan_token)[0]["me"]
        self.assertEqual((karan["turn"], karan["can_tick"]), (True, [s2]))
        self.device_act(self.karan_token, body["id"], "tick", step_id=s2)
        r = self.device_act(self.emily_token, body["id"], "tick", step_id=s3)
        task = r.json()
        self.assertEqual(task["status"], "review")
        self.assertTrue(all(p["state"] == "done" for p in task["people"]))
        actions = [(a["action"], a["member_id"]) for a in task["activity"]]
        self.assertIn(("done", self.emily.id), actions)

    def test_untick_from_review_goes_back_to_progress(self) -> None:
        body = self.team(member_ids=[self.member.id], steps=[{"title": "one", "owner_id": None}], in_order=False)
        sid = body["steps"][0]["id"]
        self.assertEqual(self.device_act(self.token, body["id"], "tick", step_id=sid).json()["status"], "review")
        self.assertEqual(self.device_act(self.token, body["id"], "untick", step_id=sid).json()["status"], "progress")

    def test_anyone_step_can_be_ticked_by_anyone(self) -> None:
        body = self.team(steps=[{"title": "Shared", "owner_id": None}, {"title": "Mine", "owner_id": self.member.id}],
                         in_order=False)
        r = self.device_act(self.karan_token, body["id"], "tick", step_id=body["steps"][0]["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["steps"][0]["done_by_id"], self.karan.id)

    def test_finish_without_steps_needs_everyone(self) -> None:
        body = self.team(steps=[], in_order=False)
        self.assertEqual(self.device_act(self.token, body["id"], "finish").json()["status"], "progress")
        self.device_act(self.karan_token, body["id"], "finish")
        task = self.device_act(self.emily_token, body["id"], "finish").json()
        self.assertEqual(task["status"], "review")

    def test_finish_with_steps_is_refused(self) -> None:
        body = self.team()
        r = self.device_act(self.token, body["id"], "finish")
        self.assertEqual(r.json(), {"action": ["Tick your steps instead."]})

    def test_not_on_task_is_not_found(self) -> None:
        body = self.team(member_ids=[self.member.id], steps=[])
        r = self.device_act(self.karan_token, body["id"], "start")
        self.assertEqual(r.status_code, 404)


class AnyModeTests(TeamTaskCase):
    def test_first_to_start_takes_it(self) -> None:
        body = self.team(mode="any", steps=[], in_order=False)
        self.assertEqual(body["mode"], "any")
        task = self.device_act(self.karan_token, body["id"], "start").json()
        self.assertEqual([p["member_id"] for p in task["people"]], [self.karan.id])
        self.assertEqual(task["member"], self.karan.id)
        self.assertIn("took", [a["action"] for a in task["activity"]])
        self.assertEqual(self.device_get(self.token), [])
        self.assertEqual(self.device_act(self.emily_token, body["id"], "start").status_code, 404)
        self.assertEqual(self.device_act(self.karan_token, body["id"], "finish").json()["status"], "review")

    def test_a_tick_takes_it_too(self) -> None:
        body = self.team(mode="any", in_order=False, steps=[{"title": "x", "owner_id": None}])
        task = self.device_act(self.emily_token, body["id"], "tick", step_id=body["steps"][0]["id"]).json()
        self.assertEqual([p["member_id"] for p in task["people"]], [self.emily.id])


class OwnerActionTests(TeamTaskCase):
    def test_help_reply_moves_to_progress_and_posts_in_chat(self) -> None:
        body = self.team(steps=[])
        r = self.device_act(self.karan_token, body["id"], "help", note="Which image?")
        task = r.json()
        self.assertEqual(task["status"], "help")
        self.assertEqual(next(p for p in task["people"] if p["member_id"] == self.karan.id)["state"], "help")
        r = self.owner_act(body["id"], "reply")
        self.assertEqual(r.status_code, 400)
        task = self.owner_act(body["id"], "reply", note="Use B.").json()
        self.assertEqual(task["status"], "progress")
        self.assertEqual(task["chat"]["count"], 1)
        self.assertEqual(task["chat"]["last"]["text"], "Use B.")

    def test_cantdo_and_reassign(self) -> None:
        body = self.team(member_ids=[self.karan.id], steps=[{"title": "Renew", "owner_id": self.karan.id}])
        task = self.device_act(self.karan_token, body["id"], "cantdo", note="No admin access.").json()
        self.assertEqual((task["status"], task["cantdo_note"]), ("cantdo", "No admin access."))
        self.assertEqual(task["people"][0]["state"], "cantdo")
        r = self.device_act(self.karan_token, body["id"], "cantdo")
        self.assertEqual(r.status_code, 400)
        task = self.owner_act(body["id"], "reassign", member_id=self.emily.id).json()
        self.assertEqual(task["status"], "sent")
        self.assertEqual(task["member"], self.emily.id)
        self.assertEqual([p["member_id"] for p in task["people"]], [self.emily.id])
        self.assertEqual(task["steps"][0]["owner_id"], self.emily.id)
        self.assertEqual(task["cantdo_note"], "")
        self.assertEqual(self.device_get(self.karan_token), [])
        self.assertEqual(len(self.device_get(self.emily_token)), 1)

    def test_old_window_sees_cantdo_as_help(self) -> None:
        body = self.team(member_ids=[self.member.id], steps=[])
        self.device_act(self.token, body["id"], "cantdo", note="Not my area.")
        Device.objects.filter(member=self.member).update(agent_version="1.3.11")
        task = self.device_get(self.token)[0]
        self.assertEqual((task["status"], task["help_note"]), ("help", "Not my area."))
        self.as_owner()
        self.assertEqual(self.client.get(task_url(body["id"])).json()["status"], "cantdo")

    def test_nudge_once_an_hour_targets_whose_turn(self) -> None:
        body = self.team()
        task = self.owner_act(body["id"], "nudge").json()
        nudge = [a for a in task["activity"] if a["action"] == "nudged"][0]
        self.assertEqual((nudge["member_id"], nudge["note"]), (self.member.id, "Asha"))
        r = self.owner_act(body["id"], "nudge")
        self.assertEqual(r.json(), {"action": ["You nudged them less than an hour ago."]})
        Task.objects.filter(pk=body["id"]).update(nudged_at=timezone.now() - timedelta(hours=2))
        r = self.owner_act(body["id"], "nudge", member_id=self.emily.id)
        self.assertEqual(r.status_code, 200)

    def test_move_due_cancel_restore(self) -> None:
        body = self.team(steps=[])
        self.device_act(self.karan_token, body["id"], "start")
        new_due = (timezone.now() + timedelta(days=3)).replace(microsecond=0)
        task = self.owner_act(body["id"], "move_due", due_at=new_due.isoformat()).json()
        self.assertIn("due_moved", [a["action"] for a in task["activity"]])
        self.assertEqual(self.owner_act(body["id"], "restore").status_code, 400)
        self.assertEqual(self.owner_act(body["id"], "cancel").json()["status"], "cancelled")
        self.assertEqual(self.owner_act(body["id"], "restore").json()["status"], "progress")

    def test_reopen_asks_everyone_to_start_again(self) -> None:
        body = self.team(member_ids=[self.karan.id], steps=[])
        self.device_act(self.karan_token, body["id"], "finish")
        task = self.owner_act(body["id"], "reopen", note="Fix the header spacing.").json()
        self.assertEqual((task["status"], task["reopen_note"]), ("progress", "Fix the header spacing."))
        me = self.device_get(self.karan_token)[0]["me"]
        self.assertTrue(me["restart"])
        task = self.device_act(self.karan_token, body["id"], "start").json()
        self.assertFalse(task["me"]["restart"])

    def test_repeat_makes_one_copy_on_approve(self) -> None:
        due = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
        body = self.team(member_ids=[self.karan.id], steps=[{"title": "Pay", "owner_id": self.karan.id}],
                         repeat="month", due_at=due.isoformat())
        self.assertIsNotNone(body["next_on"])
        self.owner_act(body["id"], "approve")
        copies = Task.objects.filter(repeat_of_id=body["id"])
        self.assertEqual(copies.count(), 1)
        copy = copies.get()
        self.assertEqual((copy.status, copy.repeat, copy.title), ("sent", "month", "Launch the Lumen landing page"))
        self.assertEqual(copy.steps.get().owner_id, self.karan.id)
        self.assertGreater(copy.due_at, due + timedelta(days=27))
        self.owner_act(body["id"], "reopen")
        self.owner_act(body["id"], "approve")
        self.assertEqual(Task.objects.filter(repeat_of_id=body["id"]).count(), 1)

    def test_patch_steps_owner_repeat_in_order(self) -> None:
        body = self.team()
        self.as_owner()
        steps = [{"id": body["steps"][0]["id"], "title": "Write the copy", "owner_id": self.karan.id},
                 {"title": "New step", "owner_id": None}]
        r = self.client.patch(task_url(body["id"]), {"steps": steps, "repeat": "week", "in_order": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        task = r.json()
        self.assertEqual([(s["title"], s["owner_id"]) for s in task["steps"]],
                         [("Write the copy", self.karan.id), ("New step", None)])
        self.assertEqual((task["repeat"], task["in_order"]), ("week", False))
        r = self.client.patch(task_url(body["id"]), {"steps": [{"title": "x", "owner_id": 99999}]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_owner_get_detail_and_member_filter(self) -> None:
        body = self.team()
        self.as_owner()
        self.assertEqual(self.client.get(task_url(body["id"])).json()["id"], body["id"])
        self.assertEqual([t["id"] for t in self.client.get(f"{TASKS_URL}?member={self.emily.id}").json()], [body["id"]])
        self.as_device(self.karan_token)
        self.assertIn("me", self.client.get(task_url(body["id"])).json())
        self.assertEqual(self.client.patch(task_url(body["id"]), {"title": "x"}, format="json").status_code, 403)


class TaskChatTests(TeamTaskCase):
    def test_chat_lines_and_unread_per_viewer(self) -> None:
        body = self.team()
        self.as_device(self.karan_token)
        r = self.client.post(chat_url(body["id"]), {"body": "Starting after lunch."}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["by"], {"kind": "member", "member_id": self.karan.id, "name": "Karan Malhotra"})
        self.as_owner()
        self.assertEqual(self.client.get(task_url(body["id"])).json()["chat"]["unread"], 1)
        r = self.client.post(chat_url(body["id"]), {"body": "Great."}, format="json")
        self.assertEqual(r.json()["by"]["kind"], "owner")
        line = Message.objects.get(pk=r.json()["id"])
        self.assertEqual((line.member_id, line.sender, line.about_task_id), (self.member.id, "owner", body["id"]))
        self.assertEqual(self.client.get(task_url(body["id"])).json()["chat"]["unread"], 0)  # writing reads it
        lines = self.client.get(chat_url(body["id"])).json()
        self.assertEqual([x["body"] for x in lines], ["Starting after lunch.", "Great."])
        self.assertEqual([x["body"] for x in self.client.get(f"{chat_url(body['id'])}?after={lines[0]['id']}").json()],
                         ["Great."])
        # Emily hasn't read either line; Karan has read his own but not the owner's.
        emily = self.device_get(self.emily_token)[0]["chat"]
        self.assertEqual((emily["count"], emily["unread"]), (2, 2))
        self.assertEqual(self.device_get(self.karan_token)[0]["chat"]["unread"], 1)
        self.as_device(self.emily_token)
        self.assertEqual(self.client.post(f"{chat_url(body['id'])}read/").json(), {"ok": True})
        self.assertEqual(self.device_get(self.emily_token)[0]["chat"]["unread"], 0)

    def test_someone_not_on_the_task_cannot_read_its_chat(self) -> None:
        body = self.team(member_ids=[self.karan.id], steps=[])
        self.as_device(self.emily_token)
        self.assertEqual(self.client.get(chat_url(body["id"])).status_code, 404)
        self.assertEqual(self.client.post(chat_url(body["id"]), {"body": "hi"}, format="json").status_code, 404)

    def test_member_reply_via_old_messages_endpoint_reaches_team_task(self) -> None:
        from .tests import MESSAGES_URL

        body = self.team()
        self.as_device(self.karan_token)
        self.client.post(MESSAGES_URL, {"body": "On it", "about_task": body["id"]}, format="json")
        self.assertEqual(Message.objects.filter(about_task_id=body["id"]).count(), 1)
