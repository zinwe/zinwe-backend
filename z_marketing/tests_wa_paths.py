"""The step-by-step path engine (wa_paths.py), its checks and presets (wa_path_model.py), the conversion from the
slots engine (wa_paths_convert.py, manage.py wa_paths) and the API. Every test runs on a fixed clock: Mon 16 Nov 2026
is the campaign's first day, so day 7 (Sun 22 Nov) shows the Mon-Sat window."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import date, datetime
from io import StringIO
from typing import Any, Iterator
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError

from . import wa_flow, wa_path_model, wa_paths, wa_paths_convert, whatsapp
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTemplate
from .tests_wa_flow import BASE, ME, FlowBase

Cat = WaTemplate.Category
CS = WaContact.CampaignStatus
IST = ZoneInfo("Asia/Kolkata")
PATH = WaCampaign.Engine.PATH


def at(y: int, m: int, d: int, h: int = 11) -> datetime:
    return datetime(y, m, d, h, tzinfo=IST)


@contextmanager
def clock(when: datetime) -> Iterator[None]:
    """Freeze the clock (timezone.now drives localdate, auto_now_add and the engine)."""
    with mock.patch("django.utils.timezone.now", return_value=when):
        yield


def intro(day: int) -> WaTemplate:
    return WaTemplate.objects.get(category=Cat.OUTREACH, step=day)


class PathBase(FlowBase):
    """FlowBase's 86 written templates and staff client, plus a live path campaign on the 100 Reviews preset."""

    def setUp(self) -> None:
        super().setUp()
        self.camp = self.path_campaign(wa_path_model.preset_100_reviews())

    def path_campaign(self, path: dict[str, Any], **extra: Any) -> WaCampaign:
        values: dict[str, Any] = dict(name="Diwali to Republic Day", first=intro(1), daily_limit=20, plan=WaCampaign.Plan.NINETY,
                                      status=WaCampaign.Status.LIVE, engine=PATH, path=wa_path_model.clean(path))
        values.update(extra)
        return WaCampaign.objects.create(**values)

    def person(self, phone: str = "+919810000201", camp: WaCampaign | None = None, **extra: Any) -> WaContact:
        return self.contact(phone, campaign=camp or self.camp, **extra)

    def row(self, c: WaContact) -> dict | None:
        return next((r for r in whatsapp.queue() if r["contact"].pk == c.pk), None)

    def send_due(self, c: WaContact) -> dict:
        r = self.row(c)
        assert r is not None and r["template"] is not None, r
        whatsapp.mark_sent(c, "flow", "sent text", ME, r["template"].pk)
        c.refresh_from_db()
        return r

    def at_step(self, c: WaContact) -> tuple[str, str]:
        c.refresh_from_db()
        return c.lane, c.step_id

    def simple(self, main: list[dict], daily_limit: int = 20, **lanes: list[dict]) -> WaCampaign:
        """A campaign on a small hand-made path (the reply lanes default to a single End)."""
        path = {"v": 1, "lanes": {
            "main": [{"id": "s0", "k": "start"}, *main],
            "yes": lanes.get("yes", [{"id": "ye", "k": "end", "status": "done"}]),
            "notnow": lanes.get("notnow", [{"id": "ne", "k": "end", "status": "done"}]),
            "stop": lanes.get("stop", [{"id": "te", "k": "end", "status": "opted_out"}]),
        }}
        return self.path_campaign(path, name="Small", daily_limit=daily_limit)


# --- The path model ------------------------------------------------------------------------------------------------


class PathModelTests(PathBase):
    def test_hard_errors(self) -> None:
        bad = {
            "duplicate": {"main": [{"id": "s0", "k": "start"}, {"id": "a", "k": "end"}], "yes": [{"id": "a", "k": "end"}]},
            "kind": {"main": [{"id": "s0", "k": "start"}, {"id": "a", "k": "jump"}]},
            "wait": {"main": [{"id": "s0", "k": "start"}, {"id": "a", "k": "wait", "n": 0, "unit": "days"}]},
            "template": {"main": [{"id": "s0", "k": "start"}, {"id": "a", "k": "send", "template": 999999}]},
            "start": {"main": [{"id": "a", "k": "end"}]},
            "lane": {"main": [{"id": "s0", "k": "start"}], "later": []},
        }
        words = {"duplicate": "used twice", "kind": "unknown step kind", "wait": "longer than 0", "template": "no longer exists",
                 "start": "starts with the Start", "lane": "Unknown lane"}
        for key, lanes in bad.items():
            with self.assertRaises(wa_path_model.PathError, msg=key) as err:
                wa_path_model.clean({"v": 1, "lanes": lanes})
            self.assertTrue(any(words[key] in e for e in err.exception.errors), (key, err.exception.errors))

    def test_issues(self) -> None:
        draft = intro(4)
        draft.text_hi = ""
        draft.save()
        path = wa_path_model.clean({"v": 1, "lanes": {
            "main": [{"id": "s0", "k": "start"}, {"id": "a", "k": "send", "template": None}, {"id": "b", "k": "send", "template": draft.pk}],
            "yes": [{"id": "y", "k": "goto", "lane": "main", "step": "nope"}],
            "notnow": [],
            "stop": [{"id": "t", "k": "send", "template": intro(1).pk}, {"id": "e", "k": "end", "status": "opted_out"}],
        }})
        found = {(i["level"], i["step_id"] or i["lane"]): i["text"] for i in wa_path_model.issues(path)}
        self.assertIn("Pick a template", found[("error", "a")])
        self.assertIn("must finish with End, Goal or Go to", found[("error", "b")])
        self.assertIn("draft", found[("warn", "b")])
        self.assertIn("no step nope", found[("error", "y")])
        self.assertIn("no steps yet", found[("error", "notnow")])
        self.assertIn("never queued", found[("info", "t")])
        draft.text_hi = "written"
        draft.save()
        self.assertEqual([i["level"] for i in wa_path_model.issues(wa_path_model.preset_100_reviews())], ["info"])  # the STOP note
        one = wa_path_model.issues(path)[0]
        self.assertEqual((one["step"], one["message"]), (one["step_id"], one["text"]))

    def test_presets_and_days(self) -> None:
        p = wa_path_model.clean(wa_path_model.preset_100_reviews())
        days = wa_path_model.plan_days(p)
        self.assertEqual([days[s]["day"] for s in ("m1", "m3", "m5", "m7")], [1, 4, 7, 10])
        self.assertEqual(days["m8"], {"day": 13, "last": 88})
        self.assertEqual(wa_path_model.summary(p), {"steps": 28, "days": 88, "lanes": 4})
        lanes = wa_path_model.lanes(p)
        self.assertEqual(lanes["main"][1]["template"], intro(1).pk)
        self.assertEqual(lanes["notnow"][-1], {"id": "n3", "k": "goto", "lane": "main", "step": "resume"})
        short = wa_path_model.clean(wa_path_model.preset_short())
        self.assertEqual(wa_path_model.plan_days(short)["m5"]["day"], 8)
        self.assertEqual(wa_path_model.lanes(wa_path_model.preset("blank"))["yes"], [])


# --- Every step kind -----------------------------------------------------------------------------------------------


class StepKindTests(PathBase):
    def test_send_and_wait_on_the_calendar_with_the_window(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            r = self.send_due(c)
            self.assertEqual((r["template"].pk, r["step_id"], r["lane"], r["step_label"]), (intro(1).pk, "m1", "main", "Day 1 · Opener"))
            self.assertEqual(self.at_step(c), ("main", "m2"))
            self.assertIsNone(self.row(c))
        with clock(at(2026, 11, 18)):
            self.assertIsNone(self.row(c))  # the wait isn't over
        with clock(at(2026, 11, 19, 18)):  # sent late in the day: the calendar still counts from day 4
            self.assertEqual(self.send_due(c)["step_label"], "Day 4 · Problem")
            sched = self.api.get(f"{BASE}/campaigns/{self.camp.pk}/schedule/?contact={c.pk}").json()
        self.assertEqual(sched[0], {**sched[0], "step_id": "m5", "date": "2026-11-23", "moved_from": "Sun 22 Nov", "day": 8})
        self.assertEqual((sched[1]["step_id"], sched[1]["date"], sched[1]["moved_from"]), ("m7", "2026-11-25", None))  # day 10
        self.assertEqual((sched[2]["step_id"], sched[2]["date"], sched[2]["day"]), ("m8", "2026-11-28", 13))
        self.assertEqual(len(sched), 4 + 26 - 2)  # m5, m7 and the 26 in the group
        with clock(at(2026, 11, 22)):
            self.assertIsNone(self.row(c))  # Sunday
        with clock(at(2026, 11, 23)):
            self.assertEqual(self.row(c)["template"].pk, intro(7).pk)

    def test_late_intro_messages_never_pile_up(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
        with clock(at(2026, 11, 26)):  # day 11: days 4, 7 and 10 are all due; only day 10 goes
            r = self.send_due(c)
            self.assertEqual(r["template"].pk, intro(10).pk)
            self.assertEqual(c.path_state["skipped"], ["m3", "m5"])
        with clock(at(2026, 11, 28)):  # the group: day 13
            self.assertEqual(self.row(c)["template"].pk, intro(13).pk)

    def test_one_message_a_day_and_the_daily_limit(self) -> None:
        camp = self.simple([{"id": "a", "k": "send", "template": intro(1).pk}, {"id": "b", "k": "send", "template": intro(4).pk},
                            {"id": "e", "k": "end", "status": "no_reply"}], daily_limit=1)
        x, y = self.person("+919810000301", camp), self.person("+919810000302", camp)
        with clock(at(2026, 11, 16)):
            rows = [r for r in whatsapp.queue() if r["contact"].campaign_id == camp.pk]
            self.assertEqual(len(rows), 1)  # the daily limit
            self.send_due(x)
            self.assertIsNone(self.row(x))  # one a day: b is due today but waits
            self.assertIsNone(self.row(y))  # the limit is used
        with clock(at(2026, 11, 17)):
            self.assertEqual(self.row(x)["template"].pk, intro(4).pk)
            whatsapp.mark_sent(x, "flow", "t", ME, intro(4).pk)
            x.refresh_from_db()
            self.assertEqual((x.step_id, x.campaign_status), ("e", CS.NO_REPLY))  # the end sets No reply
            self.assertIsNone(self.row(x))

    def test_hour_waits_and_the_evening(self) -> None:
        camp = self.simple([{"id": "a", "k": "send", "template": intro(1).pk},
                            {"id": "w", "k": "wait", "n": 20, "unit": "hours", "window": True},
                            {"id": "b", "k": "send", "template": intro(4).pk}, {"id": "e", "k": "end"}])
        c = self.person(camp=camp)
        with clock(at(2026, 11, 21, 17)):  # Saturday 17:00 + 20 h = Sunday 13:00 -> Monday 10:00
            self.send_due(c)
            sched = wa_paths.simulate(camp, c)
        self.assertEqual((sched[0]["date"], sched[0]["moved_from"]), ("2026-11-23", "Sun 22 Nov"))
        self.assertEqual(wa_paths.shift(at(2026, 11, 17, 19)), at(2026, 11, 18, 10))
        self.assertEqual(wa_paths.shift(at(2026, 11, 17, 8)), at(2026, 11, 17, 10))

    def test_only_if_no_pc_and_already_sent(self) -> None:
        setup2 = WaTemplate.objects.get(category=Cat.SETUP, step=2)
        camp = self.simple([{"id": "a", "k": "send", "template": setup2.pk, "only_if": "no_pc"},
                            {"id": "b", "k": "send", "template": intro(1).pk}, {"id": "c", "k": "send", "template": intro(1).pk},
                            {"id": "d", "k": "send", "template": intro(4).pk}, {"id": "e", "k": "end"}])
        c = self.person(camp=camp, using_since=date(2026, 11, 1))
        with clock(at(2026, 11, 16)):
            self.assertEqual(self.send_due(c)["step_id"], "b")  # a skipped: they have a PC
        with clock(at(2026, 11, 17)):
            self.assertEqual(self.row(c)["step_id"], "d")  # c skipped: they already got that template

    def test_a_draft_holds_and_shows_as_a_draft(self) -> None:
        t = intro(1)
        t.text_hi = ""
        t.save()
        c = self.person()
        with clock(at(2026, 11, 16)):
            r = self.api.get(f"{BASE}/today/").json()
            row = next(q for q in r["queue"] if q["contact"]["id"] == c.pk)
            self.assertEqual((row["draft"], row["step_id"]), (True, "m1"))
            with self.assertRaises(whatsapp.WaError):
                whatsapp.mark_sent(c, "flow", "x", ME, t.pk)

    def test_track_group_by_track_and_goal(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.record_reply(c, "Yes tell me more", ME)
            whatsapp.mark_sent(c, "reply", "Great", ME)
            whatsapp.act(c, "interested")
            self.assertEqual(self.at_step(c), ("yes", "y1"))
        with clock(at(2026, 11, 17)):
            r = self.send_due(c)  # Unassigned: Engage leans on their words (nothing clear: Data Privacy)
            self.assertEqual((r["template"].category, r["template"].track, r["lane"]), (Cat.ENGAGE, "privacy", "yes"))
            self.assertEqual(self.at_step(c), ("yes", "y2"))
            whatsapp.record_reply(c, "Our juniors take client files home on pen drives", ME)  # the answer: on to the Track
            self.assertEqual(self.at_step(c), ("yes", "y3"))
            whatsapp.mark_sent(c, "reply", "Thanks", ME)
        with clock(at(2026, 11, 18)):
            r = self.api.get(f"{BASE}/today/").json()
            row = next(q for q in r["queue"] if q["contact"]["id"] == c.pk)
            self.assertEqual((row["note"], row["template_id"], row["step_id"]), ("set_track", None, "y3"))
            whatsapp.act(c, "track", "activity")
            r = self.send_due(c)  # the overview video follows the Track
            self.assertEqual((r["template"].category, r["template"].track, r["template"].step), (Cat.DEMO_VIDEO, "activity", 1))
        steps = []
        for day in range(19, 25):  # wait 1 day, then the group: 5 more, one a day (Sunday 22 moves to Monday)
            with clock(at(2026, 11, day)):
                r = self.row(c)
                if r:
                    steps.append((day, r["template"].step))
                    self.send_due(c)
        self.assertEqual(steps, [(19, 2), (20, 3), (21, 4), (23, 5), (24, 6)])
        with clock(at(2026, 11, 25)):
            self.assertEqual(self.send_due(c)["template"].category, Cat.SETUP)
            self.assertEqual(self.at_step(c), ("yes", "y8"))
        with clock(at(2026, 11, 27)):
            self.assertIsNone(self.row(c))  # 3 days after they actually got the setup steps
        with clock(at(2026, 11, 28)):
            whatsapp.act(c, "using")
            self.assertIsNone(self.row(c))
        # The nudge is skipped (they have a PC); the Offer waits for a live code (none in this test).
        self.assertEqual(self.at_step(c), ("yes", "y10"))
        c.refresh_from_db()
        WaContact.objects.filter(pk=c.pk).update(lane="yes", step_id="y11", step_due=at(2026, 11, 28), path_state={**c.path_state, "c": self.camp.pk})
        c.refresh_from_db()
        with clock(at(2026, 11, 29)):
            out, _ = wa_paths.settle(c)
            self.assertEqual((out.kind, out.note), ("hold", "goal"))  # Goal: Paid
            whatsapp.act(c, "paid")
            self.assertEqual(self.at_step(c), ("yes", "y12"))  # on to the 14-day wait

    def test_main_lane_answer_holds_and_reply_lane_answer_goes_on(self) -> None:
        camp = self.simple([{"id": "a", "k": "send", "template": intro(1).pk}, {"id": "r", "k": "reply", "n": 2, "unit": "days"},
                            {"id": "b", "k": "send", "template": intro(4).pk}, {"id": "e", "k": "end"}])
        quiet, chatty = self.person("+919810000311", camp), self.person("+919810000312", camp)
        with clock(at(2026, 11, 16)):
            self.send_due(quiet)
            self.send_due(chatty)
            whatsapp.record_reply(chatty, "What is this?", ME)
            whatsapp.mark_sent(chatty, "reply", "It protects client files", ME)
        with clock(at(2026, 11, 18)):  # the 2 days are up
            self.assertEqual(self.row(quiet)["step_id"], "b")
            self.assertIsNone(self.row(chatty))  # answered: waits for staff to sort the reply
            self.assertEqual(self.at_step(chatty), ("main", "r"))
            whatsapp.act(chatty, "notnow")
            self.assertEqual(self.at_step(chatty)[0], "notnow")

    def test_goto_and_a_loop_stops(self) -> None:
        camp = self.simple([{"id": "a", "k": "goto", "lane": "yes", "step": "y1"}],
                           yes=[{"id": "y1", "k": "goto", "lane": "main", "step": "a"}])
        c = self.person(camp=camp)
        with clock(at(2026, 11, 16)):
            out, _ = wa_paths.settle(c)
        self.assertEqual((out.kind, out.note), ("hold", "loop"))
        camp2 = self.simple([{"id": "a", "k": "goto", "lane": "yes", "step": "y2"}],
                            yes=[{"id": "y1", "k": "send", "template": intro(1).pk}, {"id": "y2", "k": "send", "template": intro(4).pk},
                                 {"id": "ye", "k": "end"}])
        d = self.person("+919810000321", camp2)
        with clock(at(2026, 11, 16)):
            self.assertEqual(self.row(d)["step_id"], "y2")


# --- Reply lanes, STOP and resume ----------------------------------------------------------------------------------


class LaneTests(PathBase):
    def test_stop_ends_everything(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.act(c, "notnow")
            whatsapp.record_reply(c, "mat bhejo", ME)
        c.refresh_from_db()
        self.assertEqual((c.lane, c.step_id, c.campaign_status), ("stop", "t2", CS.OPTED_OUT))
        self.assertTrue(WaBlock.objects.filter(phone=c.phone).exists())
        for day in (17, 30):
            with clock(at(2026, 11, day)):
                self.assertIsNone(self.row(c))
        with clock(at(2026, 11, 30)):
            whatsapp.act(c, "interested")  # nothing moves a stopped number
        self.assertEqual(self.at_step(c), ("stop", "t2"))
        self.assertEqual(wa_paths.step_stats(self.camp)["t2"]["here"], 1)

    def test_not_now_then_back_where_they_left(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
        with clock(at(2026, 11, 19)):
            self.send_due(c)  # day 4
            self.assertEqual(self.at_step(c), ("main", "m4"))
        with clock(at(2026, 11, 20)):
            whatsapp.record_reply(c, "Not now, busy", ME)
            whatsapp.act(c, "notnow")
            self.assertEqual(self.at_step(c), ("notnow", "n1"))
            # Staff answer from Replies with the step's quick reply: it completes the step.
            quick = WaTemplate.objects.get(category=Cat.QUICK, type_name="Not now")
            whatsapp.mark_sent(c, "reply", "No problem", ME, quick.pk)
            self.assertEqual(self.at_step(c), ("notnow", "n2"))
            self.assertEqual(c.messages.last().step_id, "n1")
        with clock(at(2026, 12, 19)):
            self.assertIsNone(self.row(c))
        with clock(at(2026, 12, 21)):  # 30 days on (Sat 20 Dec... Mon 21 Dec): back at m4, the wait they left
            whatsapp.queue()
            self.assertEqual(self.at_step(c), ("main", "m4"))
        with clock(at(2026, 12, 24)):
            r = self.row(c)
            self.assertEqual((r["step_id"], r["template"].pk), ("m5", intro(7).pk))

    def test_yes_from_notnow_and_a_second_yes(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.act(c, "notnow")
            whatsapp.act(c, "interested")
            self.assertEqual(self.at_step(c), ("yes", "y1"))
            whatsapp.act(c, "signed")
            self.assertEqual(self.at_step(c), ("yes", "y1"))


# --- Editing a live path -------------------------------------------------------------------------------------------


class EditTests(PathBase):
    def test_contacts_at_a_removed_step_move_on(self) -> None:
        a, b = self.person("+919810000401"), self.person("+919810000402")
        with clock(at(2026, 11, 16)):
            self.send_due(a)
            self.send_due(b)
        with clock(at(2026, 11, 19)):
            whatsapp.queue()
            self.assertEqual(self.at_step(a), ("main", "m3"))
            path = copy.deepcopy(self.camp.path)
            main = path["lanes"]["main"]
            path["lanes"]["main"] = [s for s in main if s["id"] not in ("m3", "m4")]
            r = self.api.put(f"{BASE}/campaigns/{self.camp.pk}/path/", {"path": path}, format="json")
            self.assertEqual((r.status_code, r.json()["moved"]), (200, 2))
            self.assertEqual(self.at_step(a), ("main", "m5"))
            self.assertEqual(self.row(a)["template"].pk, intro(7).pk)
            # A step put in place of the removed one takes them.
            path2 = copy.deepcopy(path)
            path2["lanes"]["main"] = [s if s["id"] != "m5" else {**s, "id": "new5", "template": intro(13).pk} for s in path["lanes"]["main"]]
            r = self.api.put(f"{BASE}/campaigns/{self.camp.pk}/path/", {"path": path2}, format="json")
            self.assertEqual(r.json()["moved"], 2)
            self.assertEqual(self.at_step(b), ("main", "new5"))

    def test_a_changed_wait_keeps_those_already_waiting(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            path = copy.deepcopy(self.camp.path)
            path["lanes"]["main"][2]["n"] = 10
            r = self.api.put(f"{BASE}/campaigns/{self.camp.pk}/path/", {"path": path}, format="json")
            self.assertEqual(r.json()["moved"], 0)
        with clock(at(2026, 11, 19)):
            self.assertEqual(self.row(c)["step_id"], "m3")

    def test_a_running_campaign_refuses_an_unfinished_path(self) -> None:
        path = copy.deepcopy(self.camp.path)
        path["lanes"]["yes"] = []
        r = self.api.put(f"{BASE}/campaigns/{self.camp.pk}/path/", {"path": path}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("no steps yet", r.json()["detail"])
        self.camp.status = WaCampaign.Status.DRAFT
        self.camp.save()
        r = self.api.put(f"{BASE}/campaigns/{self.camp.pk}/path/", {"path": path}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(i["level"] == "error" for i in r.json()["issues"]))


# --- Conversion: both engines agree --------------------------------------------------------------------------------


class ConversionTests(FlowBase):
    """A live ninety campaign and a touches campaign, contacts at different days, run on the slots code; then
    `wa_paths --check` / `--convert` / `--revert`."""

    def setUp(self) -> None:
        super().setUp()
        self.ninety = self.campaign
        self.ninety.daily_limit = 50
        self.ninety.save()

    def slots_send(self, c: WaContact, when: datetime) -> None:
        with clock(when):
            r = next(i for i in whatsapp.queue() if i["contact"].pk == c.pk)
            whatsapp.mark_sent(c, "flow", "x", ME, r["template"].pk)
        c.refresh_from_db()

    def ninety_people(self) -> dict[str, WaContact]:
        p: dict[str, WaContact] = {}
        p["fresh"] = self.contact("+919810000501")
        p["day1"] = self.contact("+919810000502")
        self.slots_send(p["day1"], at(2026, 11, 16))
        p["late"] = self.contact("+919810000503")
        self.slots_send(p["late"], at(2026, 11, 4))
        self.slots_send(p["late"], at(2026, 11, 7))
        p["notnow"] = self.contact("+919810000504")
        self.slots_send(p["notnow"], at(2026, 11, 13))
        with clock(at(2026, 11, 14)):
            whatsapp.act(p["notnow"], "notnow")
        p["lean"] = self.contact("+919810000505", yes_date=date(2026, 11, 18), status=WaContact.Status.INTERESTED,
                                 campaign_status=CS.ENGAGED, engage_step=1, first_sent=date(2026, 11, 2), out_day=13)
        p["settrack"] = self.contact("+919810000506", yes_date=date(2026, 11, 10), status=WaContact.Status.INTERESTED,
                                     campaign_status=CS.ENGAGED, engage_step=2, first_sent=date(2026, 11, 1), out_day=7)
        p["demo"] = self.contact("+919810000507", yes_date=date(2026, 11, 5), status=WaContact.Status.INTERESTED, track="activity",
                                 campaign_status=CS.DEMO, engage_step=6, demo_step=2, first_sent=date(2026, 10, 20), out_day=13)
        p["noreply"] = self.contact("+919810000508", campaign_status=CS.NO_REPLY, first_sent=date(2026, 8, 1), out_day=88)
        p["stop"] = self.contact("+919810000509")
        self.slots_send(p["stop"], at(2026, 11, 16))
        with clock(at(2026, 11, 16)):
            whatsapp.record_reply(p["stop"], "STOP", ME)
        for c in p.values():
            c.refresh_from_db()
        return p

    def test_ninety_converts_with_the_same_messages(self) -> None:
        people = self.ninety_people()
        now = at(2026, 11, 20)
        with clock(now):
            before = {i["contact"].pk: (i["template"].pk if i["template"] else i["note"]) for i in whatsapp.queue()}
            report = wa_paths_convert.check(self.ninety)
        self.assertEqual([(r.contact.pk, r.problem, r.slots, r.path) for r in report.differences], [])
        self.assertEqual(before[people["late"].pk], intro(16).pk)  # day 17: the latest due day, not 7 or 10
        self.assertEqual(before[people["settrack"].pk], "set_track")
        self.assertEqual(WaTemplate.objects.get(pk=before[people["demo"].pk]).step, 3)
        places = {r.contact.pk: (r.place.lane, r.place.step_id) for r in report.rows}
        self.assertEqual(places[people["late"].pk], ("main", "g1"))
        self.assertEqual(places[people["demo"].pk], ("yes", "y4"))
        self.assertEqual(places[people["stop"].pk], ("stop", "t1"))
        with clock(now):
            out = StringIO()
            call_command("wa_paths", "--convert", stdout=out)
            self.assertIn("Converted: campaign", out.getvalue())
            self.ninety.refresh_from_db()
            self.assertEqual(self.ninety.engine, PATH)
            after = {i["contact"].pk: (i["template"].pk if i["template"] else i["note"]) for i in whatsapp.queue()}
            rows = {i["contact"].pk: i for i in whatsapp.queue()}
        self.assertEqual(after, before)
        self.assertEqual(rows[people["late"].pk]["step_label"], "Day 16 · Value")  # the plan's day for that message
        # Earlier messages got their step, for the numbers per step.
        self.assertTrue(WaMessage.objects.filter(contact=people["day1"], step_id="i1", campaign=self.ninety).exists())
        # Revert: the slots code runs it again, from where the path left the contacts.
        with clock(at(2026, 11, 20)):
            whatsapp.mark_sent(people["late"], "flow", "x", ME, intro(16).pk)
            call_command("wa_paths", "--revert", str(self.ninety.pk), stdout=StringIO())
        self.ninety.refresh_from_db()
        self.assertEqual(self.ninety.engine, WaCampaign.Engine.SLOTS)
        with clock(at(2026, 11, 22)):
            r = next(i for i in whatsapp.queue() if i["contact"].pk == people["late"].pk)
            self.assertEqual((r["template"].pk, r.get("step_id", "")), (intro(19).pk, ""))

    def test_touches_converts(self) -> None:
        WaCampaign.objects.filter(pk=self.ninety.pk).delete()
        first = WaTemplate.objects.get(name="Pen drive check, traders")
        camp = WaCampaign.objects.create(name="Delhi traders", first=first, followup=WaTemplate.objects.get(name="One gentle follow-up"),
                                         touch3=WaTemplate.objects.get(name="Day 7 polite close"), daily_limit=10,
                                         status=WaCampaign.Status.LIVE)
        ready = self.contact("+919810000601", campaign=camp)
        sent = self.contact("+919810000602", campaign=camp)
        replied = self.contact("+919810000603", campaign=camp)
        yes = self.contact("+919810000604", campaign=camp)
        for c, when in ((sent, at(2026, 11, 14)), (replied, at(2026, 11, 14)), (yes, at(2026, 11, 10))):
            with clock(when):
                whatsapp.mark_sent(c, "first", "hi", ME)
        with clock(at(2026, 11, 15)):
            whatsapp.record_reply(replied, "who is this", ME)
            whatsapp.record_reply(yes, "YES", ME)
            whatsapp.act(yes, "interested")
            whatsapp.mark_sent(yes, "reply", "Thank you!", ME)
        now = at(2026, 11, 17)
        with clock(now):
            before = {i["contact"].pk: i["kind"] for i in whatsapp.queue()}
            report = wa_paths_convert.check(camp)
            self.assertEqual([(r.contact.pk, r.problem, r.slots, r.path) for r in report.differences], [])
            self.assertEqual(before, {ready.pk: "first", sent.pk: "followup", yes.pk: "nurture"})
            wa_paths_convert.convert(report)
            rows = {i["contact"].pk: i for i in whatsapp.queue()}
            self.assertEqual(set(rows), set(before))
            self.assertEqual(rows[sent.pk]["template"].pk, camp.followup_id)
            # The old Today screen sends "flow" with no template id for an older template: the path's message.
            draft = self.api.post(f"{BASE}/contacts/{sent.pk}/draft/", {"kind": "flow"}, format="json").json()
            self.assertEqual(draft["template_id"], camp.followup_id)
            r = self.api.post(f"{BASE}/contacts/{sent.pk}/sent/", {"kind": "flow", "text": draft["text"]}, format="json")
            self.assertEqual(r.status_code, 200, r.json())
        sent.refresh_from_db()
        self.assertEqual((sent.step, sent.lane, sent.step_id), (2, "main", "r3"))  # slots fields kept for --revert
        self.assertEqual(sent.messages.exclude(kind="theirs").last().kind, WaMessage.Kind.FOLLOWUP)
        with clock(at(2026, 11, 21)):  # touch 3: day 7 after the first (14 Nov)
            self.assertEqual(next(i for i in whatsapp.queue() if i["contact"].pk == sent.pk)["template"].pk, camp.touch3_id)

    def test_differences_keep_a_campaign_on_slots(self) -> None:
        call = self.contact("+919810000701", yes_date=date(2026, 11, 1), status=WaContact.Status.INTERESTED, track="privacy",
                            campaign_status=CS.SETUP, engage_step=6, demo_step=6, setup_step=1, setup_error=True)
        with clock(at(2026, 11, 20)):
            report = wa_paths_convert.check(self.ninety)
            self.assertEqual([r.contact.pk for r in report.differences], [call.pk])
            self.assertIn("can't place", report.differences[0].problem)  # the Demo call isn't a step
            out = StringIO()
            with self.assertRaises(CommandError):
                call_command("wa_paths", "--convert", stdout=out)
        self.assertIn("Kept on slots: 1 differences", out.getvalue())
        self.ninety.refresh_from_db()
        self.assertEqual(self.ninety.engine, WaCampaign.Engine.SLOTS)
        # A paused campaign: the slots code still sends the YES path, a path campaign wouldn't.
        WaContact.objects.filter(pk=call.pk).update(setup_error=False)
        self.ninety.status = WaCampaign.Status.PAUSED
        self.ninety.save()
        with clock(at(2026, 11, 20)):
            report = wa_paths_convert.check(self.ninety)
        self.assertIn("paused", report.differences[0].problem)

    def test_check_is_read_only_and_needs_an_action(self) -> None:
        c = self.contact("+919810000801")
        with self.assertRaises(CommandError):
            call_command("wa_paths", stdout=StringIO())
        with clock(at(2026, 11, 20)):
            out = StringIO()
            call_command("wa_paths", "--check", stdout=out)
        self.assertIn("1 contacts, 1 same, 0 different", out.getvalue())
        c.refresh_from_db()
        self.ninety.refresh_from_db()
        self.assertEqual((c.lane, c.step_id, self.ninety.engine, self.ninety.path), ("", "", WaCampaign.Engine.SLOTS, {}))


# --- The API -------------------------------------------------------------------------------------------------------


class ApiTests(PathBase):
    def test_presets_and_create(self) -> None:
        r = self.api.get(f"{BASE}/campaigns/presets/").json()
        self.assertEqual([p["key"] for p in r], ["100_reviews", "short", "blank"])
        r = self.api.post(f"{BASE}/campaigns/", {"name": "Tax season", "preset": "100_reviews", "daily_limit": 12, "language": "hi"}, format="json")
        self.assertEqual(r.status_code, 201, r.json())
        body = r.json()
        self.assertEqual((body["engine"], body["plan"], body["first"], body["daily_limit"]), (PATH, "ninety", intro(1).pk, 12))
        self.assertEqual(body["path_summary"], {"steps": 28, "days": 88, "lanes": 4})
        self.assertEqual(body["path"]["lanes"]["main"][1]["id"], "m1")
        r = self.api.post(f"{BASE}/campaigns/", {"name": "Blank", "preset": "blank"}, format="json")
        self.assertEqual((r.status_code, r.json()["plan"]), (201, "touches"))
        r = self.api.post(f"{BASE}/campaigns/", {"name": "Bad", "preset": "long"}, format="json")
        self.assertEqual(r.status_code, 400)
        old = self.api.post(f"{BASE}/campaigns/", {"name": "Old style", "first": intro(1).pk}, format="json").json()
        self.assertEqual((old["engine"], old["path"]), ("slots", None))

    def test_campaign_list_numbers(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.record_reply(c, "YES", ME)
            whatsapp.act(c, "interested")
        with clock(at(2026, 11, 18)):
            row = next(x for x in self.api.get(f"{BASE}/campaigns/").json() if x["id"] == self.camp.pk)
        self.assertEqual({k: row[k] for k in ("engine", "contacts", "sent", "replies", "reply_rate", "yes", "paid", "day")},
                         {"engine": PATH, "contacts": 1, "sent": 1, "replies": 1, "reply_rate": 100, "yes": 1, "paid": 0, "day": 3})
        self.assertIn("stats", row)

    def test_path_get_put_and_start(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            body = self.api.get(f"{BASE}/campaigns/{self.camp.pk}/path/").json()
        self.assertEqual((body["engine"], body["read_only"]), (PATH, False))
        self.assertEqual(body["stats"]["m1"], {"here": 0, "sent": 1, "replies": 0, "reply_rate": 0})
        self.assertEqual(body["stats"]["m2"]["here"], 1)
        r = self.api.put(f"{BASE}/campaigns/{self.camp.pk}/path/", {"path": {"v": 1, "lanes": {"main": [{"id": "x", "k": "start"}, {"id": "x", "k": "end"}]}}}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("used twice", r.json()["errors"][0])
        draft = self.path_campaign(wa_path_model.preset_short(), status=WaCampaign.Status.DRAFT, name="New")
        self.person("+919810000901", draft)
        t = WaTemplate.objects.get(pk=wa_path_model.lanes(draft.path)["main"][1]["template"])
        t.text_hi = ""
        t.save()
        r = self.api.post(f"{BASE}/campaigns/{draft.pk}/start/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("draft", r.json()["detail"])
        t.text_hi = "written"
        t.save()
        self.assertEqual(self.api.post(f"{BASE}/campaigns/{draft.pk}/start/").json()["status"], "live")

    def test_a_slots_campaign_shows_its_path_read_only(self) -> None:
        body = self.api.get(f"{BASE}/campaigns/{self.campaign.pk}/path/").json()
        self.assertEqual((body["engine"], body["read_only"], body["stats"]), ("slots", True, {}))
        self.assertEqual(body["path"]["lanes"]["main"][1]["template"], intro(1).pk)
        r = self.api.put(f"{BASE}/campaigns/{self.campaign.pk}/path/", {"path": body["path"]}, format="json")
        self.assertEqual(r.status_code, 400)
        sched = self.api.get(f"{BASE}/campaigns/{self.campaign.pk}/schedule/").json()
        self.assertEqual(sched[0]["day"], 1)
        steps = self.api.get(f"{BASE}/analytics/steps/?campaign={self.campaign.pk}").json()
        self.assertEqual(steps["engine"], "slots")

    def test_today_rows_and_contact_json(self) -> None:
        c = self.person()
        old = self.contact("+919810000999")  # the slots ninety campaign
        with clock(at(2026, 11, 16)):
            r = self.api.get(f"{BASE}/today/").json()
        rows = {q["contact"]["id"]: q for q in r["queue"]}
        self.assertEqual({k: rows[c.pk][k] for k in ("kind", "step_id", "step_label", "lane", "template_id", "label")},
                         {"kind": "flow", "step_id": "m1", "step_label": "Day 1 · Opener", "lane": "main", "template_id": intro(1).pk,
                          "label": wa_flow.label(intro(1))})
        self.assertEqual((rows[old.pk]["step_id"], rows[old.pk]["step_label"], rows[old.pk]["lane"]), ("", "", ""))
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            j = self.api.get(f"{BASE}/contacts/{c.pk}/").json()
        self.assertEqual({k: j[k] for k in ("engine", "lane", "step_id", "step_label", "next_due")},
                         {"engine": PATH, "lane": "main", "step_id": "m2", "step_label": "Intro · Wait 3 days", "next_due": "2026-11-19"})

    def test_schedule_and_analytics_steps(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.record_reply(c, "ok", ME)
            fresh = self.api.get(f"{BASE}/campaigns/{self.camp.pk}/schedule/").json()
            steps = self.api.get(f"{BASE}/analytics/steps/?campaign={self.camp.pk}").json()
        self.assertEqual([(x["step_id"], x["date"], x["day"]) for x in fresh[:3]], [("m1", "2026-11-16", 1), ("m3", "2026-11-19", 4), ("m5", "2026-11-23", 8)])
        self.assertEqual(fresh[2]["moved_from"], "Sun 22 Nov")
        m1 = next(s for s in steps["steps"] if s["step_id"] == "m1")
        self.assertEqual({k: m1[k] for k in ("lane", "k", "label", "sent", "replies", "reply_rate", "here")},
                         {"lane": "main", "k": "send", "label": "Day 1 · Opener", "sent": 1, "replies": 1, "reply_rate": 100, "here": 0})
        self.assertEqual(self.api.get(f"{BASE}/analytics/steps/").status_code, 400)
        self.assertEqual(self.api.get(f"{BASE}/campaigns/{self.camp.pk}/schedule/?contact=x").status_code, 400)

    def test_paused_campaign_queues_nothing(self) -> None:
        c = self.person()
        self.camp.status = WaCampaign.Status.PAUSED
        self.camp.save()
        with clock(at(2026, 11, 16)):
            self.assertIsNone(self.row(c))

    def test_moving_campaigns_restarts_the_position(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
        other = self.path_campaign(wa_path_model.preset_short(), name="Other")
        WaContact.objects.filter(pk=c.pk).update(campaign=other)
        c.refresh_from_db()
        with clock(at(2026, 11, 17)):
            self.assertIsNone(self.row(c))  # the opener was already sent to them: skipped; then Short's 3-day wait
        c.refresh_from_db()
        self.assertEqual((c.step_id, c.path_state["c"]), ("m2", other.pk))
        with clock(at(2026, 11, 20)):
            self.assertEqual(self.row(c)["step_id"], "m3")

    def test_unchanged_slots_engine_alongside(self) -> None:
        old = self.contact("+919810000998")
        with clock(at(2026, 11, 16)):
            r = next(i for i in whatsapp.queue() if i["contact"].pk == old.pk)
            whatsapp.mark_sent(old, "flow", "x", ME, r["template"].pk)
        old.refresh_from_db()
        self.assertEqual((old.out_day, old.lane, old.step_id, old.path_state), (1, "", "", {}))
        self.assertFalse(WaMessage.objects.filter(contact=old).exclude(step_id="").exists())

