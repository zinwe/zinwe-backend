"""WhatsApp Settings (wa_settings.py, GET/PUT /wa/settings/): the defaults are what the code did before, and changing a
setting changes what the engines do (window, days, limits, stop words, the discount ceiling, waits, the last day)."""
from __future__ import annotations

from datetime import time, timedelta
from typing import Any
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from z_auth.models import Coupon

from . import wa_flow, wa_path_model, wa_paths, wa_settings, whatsapp
from .models import WaBlock, WaCampaign, WaContact, WaSettings, WaTemplate
from .tests_wa_flow import BASE, ME, FlowBase
from .tests_wa_paths import PathBase, at, clock, intro

Cat = WaTemplate.Category
CS = WaContact.CampaignStatus
URL = f"{BASE}/settings/"


class Rules:
    """Settings start from the defaults in every test, and the cache never leaks into the next one."""

    def setUp(self) -> None:
        wa_settings.clear()
        super().setUp()  # type: ignore[misc]

    def tearDown(self) -> None:
        wa_settings.clear()
        super().tearDown()  # type: ignore[misc]

    def rules(self, **values: Any) -> WaSettings:
        return wa_settings.update(values, "Vishal")


class SettingsApiTests(Rules, FlowBase):
    def test_defaults_are_what_the_code_did_before(self) -> None:
        r = self.api.get(URL)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual({k: body[k] for k in (
            "send_days", "send_from", "send_to", "skip_holidays", "new_per_day", "per_person_day", "min_intro_wait", "max_days",
            "slots", "stop_words", "stop_pause_pct", "language", "switch_en", "claude_read", "claude_draft", "claude_track",
            "discount_max", "updated_at")}, {
            "send_days": [0, 1, 2, 3, 4, 5], "send_from": "10:00", "send_to": "19:00", "skip_holidays": False,
            "new_per_day": 15, "per_person_day": 1, "min_intro_wait": 1, "max_days": 90,
            "slots": ["10-12", "12-14", "14-17", "17-19"], "stop_words": ["STOP", "unsubscribe", "band karo", "mat bhejo"],
            "stop_pause_pct": 0, "language": "en", "switch_en": False, "claude_read": True, "claude_draft": True,
            "claude_track": True, "discount_max": 40, "updated_at": None,
        })
        self.assertEqual(body["per_number_day"], 120)
        self.assertFalse(WaSettings.objects.exists())  # reading never writes

    def test_staff_only(self) -> None:
        member = get_user_model().objects.create_user(email="member@example.com", password="x")
        api = APIClient()
        api.force_authenticate(member)
        self.assertEqual(api.get(URL).status_code, 403)
        self.assertEqual(api.put(URL, {"max_days": 30}, format="json").status_code, 403)
        self.assertEqual(APIClient().get(URL).status_code, 401)

    def test_refuses_what_cannot_work(self) -> None:
        r = self.api.put(URL, {"send_days": [], "send_from": "19:00", "send_to": "10:00", "discount_max": 60,
                               "stop_words": ["band karo"], "per_person_day": 3, "min_intro_wait": 0, "max_days": 120,
                               "per_number_day": 0, "new_per_day": 51, "language": "fr", "slots": ["9-10"],
                               "claude_draft": "yes"}, format="json")
        self.assertEqual(r.status_code, 400)
        errors = r.json()
        self.assertEqual(set(errors), {"send_days", "send_to", "discount_max", "stop_words", "per_person_day", "min_intro_wait",
                                       "max_days", "per_number_day", "new_per_day", "language", "slots", "claude_draft"})
        self.assertEqual(errors["send_days"], "Pick at least one day.")
        self.assertEqual(errors["send_to"], "The end comes after the start.")
        self.assertIn("STOP always stays", errors["stop_words"])
        self.assertFalse(WaSettings.objects.exists())  # nothing half-saved

    def test_saves_and_says_who(self) -> None:
        self.staff.full_name = "Vishal Shakya"
        self.staff.save()
        r = self.api.put(URL, {"send_days": [5, 0, 1, 1], "send_from": "11:30", "stop_words": ["STOP", " not   interested ", "stop"],
                               "discount_max": 25, "language": "hi", "claude_track": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["send_days"], body["send_from"], body["send_to"]), ([0, 1, 5], "11:30", "19:00"))
        self.assertEqual(body["stop_words"], ["STOP", "not interested"])
        self.assertEqual((body["discount_max"], body["language"], body["claude_track"], body["updated_by"]), (25, "hi", False, "Vishal Shakya"))
        self.assertIsNotNone(body["updated_at"])
        self.assertEqual(self.api.get(URL).json()["send_from"], "11:30")
        self.assertEqual(wa_settings.current().send_from, time(11, 30))

    def test_a_new_campaign_starts_from_the_settings(self) -> None:
        self.rules(new_per_day=25, language="hi")
        r = self.api.post(f"{BASE}/campaigns/", {"name": "From settings", "preset": "short"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["daily_limit"], r.json()["language"]), (25, "hi"))
        r = self.api.post(f"{BASE}/campaigns/", {"name": "Its own", "preset": "short", "daily_limit": 8, "language": "en"}, format="json")
        self.assertEqual((r.json()["daily_limit"], r.json()["language"]), (8, "en"))  # a campaign can set its own


class WindowTests(Rules, PathBase):
    def test_days_hours_and_holidays_move_a_due_time(self) -> None:
        sat = at(2026, 11, 21, 11)
        self.assertEqual(wa_paths.shift(sat), sat)  # Mon-Sat by default
        self.rules(send_days=[0, 1, 2, 3, 4])  # Mon-Fri
        self.assertEqual(wa_paths.shift(sat), at(2026, 11, 23, 10))
        self.rules(send_from="11:00", send_to="17:00")
        self.assertEqual(wa_paths.shift(at(2026, 11, 23, 10)), at(2026, 11, 23, 11))
        self.assertEqual(wa_paths.shift(at(2026, 11, 23, 18)), at(2026, 11, 24, 11))
        self.assertEqual(wa_paths.add(at(2026, 11, 23, 15), 1, "days"), at(2026, 11, 24, 11))  # a day's wait lands at the opening
        republic = at(2027, 1, 26, 12)  # a Tuesday
        self.assertEqual(wa_paths.shift(republic), republic)
        self.rules(skip_holidays=True)
        self.assertEqual(wa_paths.shift(republic), at(2027, 1, 27, 11))

    def test_a_day_off_moves_the_message_on_today(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)  # day 1, Monday; day 4 is Thursday 19 Nov
        self.rules(send_days=[0, 1, 2, 4, 5])  # no Thursday
        with clock(at(2026, 11, 19)):
            self.assertIsNone(self.row(c))
        with clock(at(2026, 11, 20)):
            self.assertEqual(self.row(c)["template"].pk, intro(4).pk)


class LimitTests(Rules, FlowBase):
    def test_the_number_limit_caps_today_across_campaigns(self) -> None:
        other = WaCampaign.objects.create(name="Second", first=intro(1), daily_limit=10, plan=WaCampaign.Plan.NINETY,
                                          status=WaCampaign.Status.LIVE)
        people = [self.contact("+91981000010%d" % i) for i in range(3)] + [self.contact("+919810000200", campaign=other)]
        cold = lambda: [r for r in whatsapp.queue() if r["contact"].pk in {p.pk for p in people}]  # noqa: E731
        self.assertEqual(len(cold()), 4)
        self.rules(per_number_day=3)
        self.assertEqual(len(cold()), 3)
        self.send(people[0])  # one Intro sent today leaves room for two
        self.assertEqual(len(cold()), 2)
        yes = self.contact("+919810000300", yes_date=self.today - timedelta(days=1), status=WaContact.Status.INTERESTED, campaign_status=CS.YES)
        self.rules(per_number_day=1)
        rows = whatsapp.queue()
        self.assertIn(yes.pk, [r["contact"].pk for r in rows])  # the YES path never counts
        self.assertEqual(len([r for r in rows if r["contact"].pk in {p.pk for p in people}]), 0)

    def test_messages_a_day_to_one_person(self) -> None:
        c = self.contact()
        whatsapp.mark_sent(c, "custom", "A one-off hello", ME)
        self.assertIsNone(self.due(c))  # one a day by default
        self.rules(per_person_day=2)
        self.assertEqual(self.due(c)["template"].pk, intro(1).pk)

    def test_longest_campaign_on_the_ninety_plan(self) -> None:
        c = self.pass_days(self.send(self.contact()), 70)  # day 71, never replied
        self.assertIsNotNone(self.due(c))
        self.rules(max_days=60)
        self.assertIsNone(self.due(c))
        c.refresh_from_db()
        self.assertEqual(c.campaign_status, CS.NO_REPLY)


class PathRuleTests(Rules, PathBase):
    def test_longest_campaign_on_a_path(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
        with clock(at(2027, 1, 18)):  # day 64
            self.assertIsNotNone(self.row(c))
            self.rules(max_days=60)
            self.assertIsNone(self.row(c))
        c.refresh_from_db()
        self.assertEqual((c.lane, c.step_id, c.campaign_status), ("main", "m9", CS.NO_REPLY))

    def test_shortest_wait_between_intro_messages(self) -> None:
        path = wa_path_model.clean({"v": 1, "lanes": {
            "main": [{"id": "s0", "k": "start"}, {"id": "a", "k": "send", "template": intro(1).pk},
                     {"id": "w", "k": "wait", "n": 2, "unit": "days"}, {"id": "b", "k": "send", "template": intro(4).pk},
                     {"id": "g", "k": "group", "category": Cat.OUTREACH, "count": 3, "every": 2}, {"id": "e", "k": "end", "status": "no_reply"}],
            "yes": [{"id": "ye", "k": "end"}], "notnow": [{"id": "ne", "k": "end"}], "stop": [{"id": "te", "k": "end"}],
        }})
        errors = lambda: {i["step_id"]: i["text"] for i in wa_path_model.issues(path) if i["level"] == "error"}  # noqa: E731
        self.assertEqual(errors(), {})
        self.rules(min_intro_wait=3)
        self.assertEqual(set(errors()), {"w", "g"})
        self.assertIn("at least 3 days", errors()["w"])
        camp = self.path_campaign(path, name="Too close")
        r = self.api.put(f"{BASE}/campaigns/{camp.pk}/path/", {"path": path}, format="json")
        self.assertEqual(r.status_code, 400)  # a live campaign can't save it
        self.assertIn("at least 3 days", r.json()["detail"])


class StopTests(Rules, FlowBase):
    def test_stop_words_come_from_settings(self) -> None:
        a = self.send(self.contact())
        whatsapp.record_reply(a, "Not interested.", ME)
        a.refresh_from_db()
        self.assertNotEqual(a.status, WaContact.Status.STOP)
        self.rules(stop_words=["STOP", "not interested", "mat bhejo"])
        b = self.send(self.contact("+919810000102"))
        whatsapp.record_reply(b, "  NOT   interested!! ", ME)
        b.refresh_from_db()
        self.assertEqual((b.status, b.campaign_status), (WaContact.Status.STOP, CS.OPTED_OUT))
        self.assertTrue(WaBlock.objects.filter(phone=b.phone).exists())
        d = self.send(self.contact("+919810000103"))
        whatsapp.record_reply(d, "band karo", ME)  # removed from the list
        d.refresh_from_db()
        self.assertNotEqual(d.status, WaContact.Status.STOP)

    def test_a_campaign_pauses_when_too_many_stop(self) -> None:
        people = [WaContact.objects.create(phone=f"+9198200{i:05d}", campaign=self.campaign, firm=f"Firm {i}", segment="S1",
                                           country="India", city="Delhi", step=1, status=WaContact.Status.MESSAGED)
                  for i in range(whatsapp.STOP_SAMPLE)]
        WaContact.objects.filter(pk=people[0].pk).update(status=WaContact.Status.STOP)
        whatsapp.record_reply(people[1], "STOP", ME)  # 2 of 50 = 4 %: never pauses by default
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, WaCampaign.Status.LIVE)
        self.rules(stop_pause_pct=5)
        whatsapp.record_reply(people[2], "stop", ME)  # 3 of 50 = 6 % > 5 %
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, WaCampaign.Status.PAUSED)


class OfferTests(Rules, FlowBase):
    def test_the_biggest_discount(self) -> None:
        start = self.today - timedelta(days=1)
        big = Coupon.objects.create(code="ZWTESTBIG30", percent_off=30, plan_codes=[], starts_on=start, ends_on=self.today + timedelta(days=10))
        offer = WaTemplate.objects.filter(category=Cat.OFFER).order_by("id").first()
        WaTemplate.objects.filter(category=Cat.OFFER).exclude(pk=offer.pk).update(archived=True)
        WaTemplate.objects.filter(pk=offer.pk).update(coupon=big, event=False)
        c = self.contact(yes_date=self.today, status=WaContact.Status.INTERESTED, campaign_status=CS.YES)
        self.assertEqual(wa_flow.offer_for(c, self.today).pk, offer.pk)
        self.rules(discount_max=25)
        self.assertIsNone(wa_flow.offer_for(c, self.today))
        offer.refresh_from_db()
        with self.assertRaisesMessage(wa_flow.FlowError, "biggest discount in a message is 25 %"):
            wa_flow.check_sendable(c, offer)
        other = WaTemplate.objects.filter(category=Cat.OFFER).exclude(pk=offer.pk).first()
        r = self.api.patch(f"{BASE}/templates/{other.pk}/", {"coupon": big.pk}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("biggest discount in a message is 25 %", r.json()["coupon"][0])
        self.rules(discount_max=30)
        r = self.api.patch(f"{BASE}/templates/{other.pk}/", {"coupon": big.pk}, format="json")
        self.assertEqual(r.status_code, 200, r.content)


class ClaudeTests(Rules, FlowBase):
    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_draft_the_answer_off_uses_the_template(self) -> None:
        c = self.contact()
        self.rules(claude_draft=False)
        with mock.patch("anthropic.Anthropic") as client:
            self.assertIsNone(whatsapp._claude("Hi {{FirstName}}", c, ""))
        client.assert_not_called()
