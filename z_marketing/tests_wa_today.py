"""Today (wa4): Claude's read of each reply (rules first, Claude cached per message), what confirming each choice does
to the contact's path, the send slots and the day's numbers. Runs on the path tests' fixed clock (Mon 16 Nov 2026)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings

from . import wa_path_model, wa_today, whatsapp
from .models import WaCampaign, WaContact, WaMessage, WaTemplate
from .tests_wa_flow import BASE, ME
from .tests_wa_paths import PathBase, at, clock, intro

Cat = WaTemplate.Category


class RulesTests(PathBase):
    def check(self, text: str, kind: str, why: str = "", track_mode: bool = False, track: str = "") -> None:
        got = wa_today.rules_read(text, track_mode)
        self.assertEqual(got["kind"], kind, text)
        self.assertEqual(got["source"], "rules")
        if why:
            self.assertEqual(got["why"], why, text)
        if track_mode:
            self.assertEqual(got["track"], track, text)

    def test_the_four_sorts_in_english_and_hinglish(self) -> None:
        self.check("Yes please send the video. We had exactly this problem in March with an article clerk.", "yes",
                   "asks for the video and names the problem")
        self.check("haan ji, bhejo", "yes", "asks for the video")
        self.check("Ok sure", "yes", "says yes")
        self.check("What is the price per PC? We have 22 systems.", "question", "a question about price, no YES yet")
        self.check("kitna lagega", "question", "a question about price, no YES yet")
        self.check("Is this spyware?", "question", "asks if it is spyware, no YES yet")
        self.check("How does it work", "question", "asks a question, no YES yet")
        self.check("Abhi busy hai season. January ke baad baat karte hain.", "notnow", "busy now, asks to talk after January")
        self.check("baad mein", "notnow", "asks to talk later")
        self.check("Not now, after Diwali", "notnow", "asks to talk after Diwali")
        self.check("no thanks", "notnow", "says no for now, not STOP")
        self.check("STOP", "stop", "asks us to stop messaging")
        self.check("please band karo", "stop")
        self.check("mat bhejo yeh sab", "stop")
        self.check("Don't send me these", "stop")  # "send" alone would read as YES
        self.check("hmm", "question", "not clear: read it and pick")
        self.assertEqual(wa_today.quick_topic("What is the price per PC?"), "Price question")
        self.assertEqual(wa_today.quick_topic("Send it on email please"), "Send it on email")

    def test_the_track_from_an_engage_answer(self) -> None:
        self.check("Mostly laptops, people work from home 2 days a week. Files are on Google Drive and pen drives also.", "track",
                   "mentions laptops and pen drives", track_mode=True, track="privacy")
        self.check("I want to see who is working on what, the team's day", "track", track_mode=True, track="activity")
        self.check("dono chahiye", "track", "names both", track_mode=True, track="both")
        self.check("not sure", "track", "doesn't point to one Track yet", track_mode=True, track="")


class EffectTests(PathBase):
    """The reply rows on a path campaign (the 100 Reviews preset): suggestion, choices, effect and answers."""

    def reply_row(self, c: WaContact) -> dict:
        r = self.api.get(f"{BASE}/today/").json()
        return next(x for x in r["replies"] if x["id"] == c.pk)

    def test_intro_reply_lines_and_nothing_is_saved(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.record_reply(c, "Yes please send the video. We had exactly this problem in March.", ME)
            before = self.at_step(c)
            row = self.reply_row(c)
        self.assertEqual(row["suggest"], {"kind": "yes", "track": "", "why": "asks for the video and names the problem", "source": "rules"})
        self.assertEqual(row["sorts"], ["yes", "question", "notnow", "stop"])
        engage = WaTemplate.objects.get(category=Cat.ENGAGE, track="privacy", step=1)
        self.assertEqual(row["effect"]["yes"],
                         "YES moves Rohit to the YES path: Engage · Need discovery goes now, then Zinwe waits up to 2 days for the answer.")
        self.assertEqual(row["answers"]["yes"], engage.pk)
        self.assertTrue(row["effect"]["question"].startswith("A question doesn't move them. Rohit stays on Intro"))
        self.assertEqual(row["effect"]["notnow"],
                         "Not now moves them to the Not now path: this reply, a 30-day wait, then back to Intro at the next message.")
        self.assertEqual(row["answers"]["notnow"], WaTemplate.objects.get(category=Cat.QUICK, type_name="Not now").pk)
        self.assertEqual(row["effect"]["stop"],
                         "STOP ends every campaign for this number. Send the confirmation, then the number is blocked for good.")
        self.assertEqual(self.at_step(c), before)  # working out the lines saved nothing
        # Confirming YES the way Today does: the act, then the answer from answers["yes"] completes the step.
        with clock(at(2026, 11, 16, 12)):
            whatsapp.act(c, "interested")
            whatsapp.mark_sent(c, "reply", "Thank you, Rohit.", ME, row["answers"]["yes"])
        self.assertEqual(self.at_step(c), ("yes", "y2"))

    def test_an_answer_in_the_yes_path_is_read_as_a_track(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.record_reply(c, "Yes", ME)
            whatsapp.act(c, "interested")
            whatsapp.mark_sent(c, "reply", "Where do client files live?", ME, WaTemplate.objects.get(category=Cat.ENGAGE, track="privacy", step=1).pk)
        with clock(at(2026, 11, 17)):
            whatsapp.record_reply(c, "Mostly on laptops and pen drives", ME)
            row = self.reply_row(c)
            # The draft can be read before the Track is set (track=...), and nothing is saved.
            demo = WaTemplate.objects.get(category=Cat.DEMO_VIDEO, track="privacy", step=1)
            d = self.api.post(f"{BASE}/contacts/{c.pk}/draft/", {"kind": "flow", "template": demo.pk, "track": "privacy"}, format="json")
        self.assertEqual(d.status_code, 200, d.content)
        c.refresh_from_db()
        self.assertEqual(c.track, "")
        self.assertEqual(row["sorts"], ["privacy", "activity", "both"])
        self.assertEqual((row["suggest"]["kind"], row["suggest"]["track"]), ("track", "privacy"))
        self.assertEqual(row["answers"]["privacy"], demo.pk)
        self.assertEqual(row["effect"]["privacy"], "Sets the Track to Data Privacy and sends Demo video · Overview video now. The next one goes tomorrow.")

    def test_ninety_campaign_lines(self) -> None:
        c = self.contact("+919810000777")  # the slots ninety campaign
        with clock(at(2026, 11, 16)):
            whatsapp.mark_sent(c, "flow", "Hi Rohit", ME, intro(1).pk)
            whatsapp.record_reply(c, "What is the price per PC?", ME)
            row = self.reply_row(c)
        self.assertEqual(row["suggest"]["kind"], "question")
        self.assertEqual(row["answers"]["question"], WaTemplate.objects.get(category=Cat.QUICK, type_name="Price question").pk)
        self.assertEqual(row["effect"]["yes"], "YES moves Rohit to the YES path: Engage · Need discovery goes now.")
        self.assertEqual(row["effect"]["question"], "A question doesn't move them. Rohit stays on Intro; the day 4 message still goes on Thu 19 Nov.")
        self.assertEqual(row["effect"]["notnow"], "Not now marks them as not now. The Intro messages carry on as planned.")


@override_settings(ANTHROPIC_API_KEY="test-key")
class ClaudeReadTests(PathBase):
    def fake(self, payload: dict | None, fail: bool = False) -> mock.MagicMock:
        client = mock.MagicMock()
        if fail:
            client.messages.create.side_effect = RuntimeError("network down")
        else:
            client.messages.create.return_value = SimpleNamespace(
                stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))])
        return client

    def replied(self, text: str) -> WaContact:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.record_reply(c, text, ME)
        return c

    def test_claude_refines_once_per_message(self) -> None:
        c = self.replied("Bhai abhi to nahi, par Holi ke aas paas dekhte hain")
        client = self.fake({"kind": "notnow", "track": "", "why": "not now, maybe around Holi"})
        with mock.patch("anthropic.Anthropic", return_value=client), clock(at(2026, 11, 16, 12)):
            first = wa_today.suggestions([c])[c.pk]
            again = wa_today.suggestions([c])[c.pk]
        self.assertEqual(first, {"kind": "notnow", "track": "", "why": "not now, maybe around Holi", "source": "claude"})
        self.assertEqual(again, first)
        self.assertEqual(client.messages.create.call_count, 1)  # kept on the message
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(WaMessage.objects.filter(contact=c, kind=WaMessage.Kind.THEIRS).last().read["source"], "claude")

    def test_a_failed_or_rule_breaking_read_falls_back_to_the_rules(self) -> None:
        c = self.replied("What is the price per PC?")
        client = self.fake(None, fail=True)
        with mock.patch("anthropic.Anthropic", return_value=client), clock(at(2026, 11, 16, 12)), self.assertLogs("zinwe.marketing", "WARNING"):
            got = wa_today.suggestions([c])[c.pk]
            wa_today.suggestions([c])
        self.assertEqual((got["kind"], got["source"]), ("question", "rules"))
        self.assertEqual(client.messages.create.call_count, 1)  # tried once, not on every load
        # A track answer outside track mode breaks the rules: ignored.
        self.assertIsNone(self.check_read({"kind": "track", "track": "privacy", "why": "x"}, track_mode=False))
        self.assertIsNone(self.check_read({"kind": "yes", "track": "", "why": "x"}, track_mode=True))

    def check_read(self, payload: dict, track_mode: bool) -> dict | None:
        with mock.patch("anthropic.Anthropic", return_value=self.fake(payload)):
            return wa_today.claude_read("text", "ours", track_mode)

    @override_settings(ANTHROPIC_API_KEY="")
    def test_no_key_no_call(self) -> None:
        c = self.replied("Yes")
        with mock.patch("anthropic.Anthropic") as made:
            self.assertEqual(wa_today.suggestions([c])[c.pk]["source"], "rules")
        made.assert_not_called()


class SlotTests(PathBase):
    def test_slot_now_and_next(self) -> None:
        cases = [(at(2026, 11, 16, 9), "10", True, "12"), (at(2026, 11, 16, 13), "12", True, "14"),
                 (at(2026, 11, 16, 17), "17", True, ""), (at(2026, 11, 16, 20), "17", False, "")]
        for when, now, _, nxt in cases:
            info = wa_today.slot_info(when)
            self.assertEqual((info["now"], info["next"]), (now, nxt), when)
        self.assertFalse(wa_today.slot_info(at(2026, 11, 16, 9))["open"])
        self.assertTrue(wa_today.slot_info(at(2026, 11, 16, 13))["open"])
        self.assertEqual(wa_today.slot_info(at(2026, 11, 16, 13))["next_at"], "14:00")
        self.assertFalse(wa_today.slot_info(at(2026, 11, 22, 13))["open"])  # Sunday

    def test_campaign_send_slot_field(self) -> None:
        self.assertEqual(self.camp.send_slot, "10")
        r = self.api.patch(f"{BASE}/campaigns/{self.camp.pk}/", {"send_slot": "14"}, format="json")
        self.assertEqual((r.status_code, r.json()["send_slot"]), (200, "14"))
        r = self.api.patch(f"{BASE}/campaigns/{self.camp.pk}/", {"send_slot": "11"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_today_rows_carry_their_slot(self) -> None:
        self.camp.send_slot = "14"
        self.camp.save()
        cold, warm = self.person("+919810000501"), self.person("+919810000502")
        own = self.simple([{"id": "a", "k": "send", "template": intro(1).pk, "slot": "17"}, {"id": "e", "k": "end"}])
        late = self.person("+919810000503", own)
        old = self.contact("+919810000504")  # the slots ninety campaign, default slot
        with clock(at(2026, 11, 16)):
            self.send_due(warm)
            whatsapp.act(warm, "interested")  # their YES, sorted: the YES path's first step is due
        with clock(at(2026, 11, 17)):
            r = self.api.get(f"{BASE}/today/").json()
        rows = {q["contact"]["id"]: q for q in r["queue"]}
        self.assertEqual((rows[cold.pk]["slot"], rows[cold.pk]["after_yes"]), ("14", False))
        self.assertEqual((rows[warm.pk]["slot"], rows[warm.pk]["after_yes"]), ("", True))
        self.assertEqual(rows[late.pk]["slot"], "17")  # the path step's own slot
        self.assertEqual(rows[old.pk]["slot"], "10")
        self.assertEqual(set(r["slots"]), {"now", "now_label", "open", "sunday", "next", "next_label", "next_at", "list"})

    def test_the_day_and_sent_today(self) -> None:
        a, b = self.person("+919810000601"), self.person("+919810000602", first_name="", firm="Webgross")
        with clock(at(2026, 11, 16)):
            self.send_due(a)
            self.send_due(b)
        with clock(at(2026, 11, 16, 12)):
            whatsapp.record_reply(a, "Yes", ME)
            whatsapp.act(a, "interested")
            r = self.api.get(f"{BASE}/today/").json()
        limit = sum(WaCampaign.objects.filter(status="live").values_list("daily_limit", flat=True))
        self.assertEqual(r["day"], {"sent": 2, "limit": limit, "replies": 1, "yes": 1})
        rows = {x["contact"]: x for x in r["sent_list"]}
        self.assertEqual(rows[a.pk]["what"], "Intro · Opener · day 1")
        self.assertIsNotNone(rows[a.pk]["replied_at"])
        self.assertEqual((rows[b.pk]["name"], rows[b.pk]["replied_at"]), ("Webgross", None))
        self.assertEqual(wa_path_model.clean(self.camp.path)["v"], 1)  # the campaign's path untouched





@override_settings(ANTHROPIC_API_KEY="")
class SettingsGateTests(PathBase):
    """Settings > Claude: switching "Read each reply" off removes the suggestion (rules and Claude alike)."""

    def test_claude_read_off_gives_no_suggestion(self) -> None:
        from . import wa_settings
        from .models import WaSettings
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.record_reply(c, "Yes please send the video", ME)
        with clock(at(2026, 11, 16, 12)):
            self.assertEqual(wa_today.suggestions([c])[c.pk]["kind"], "yes")
            WaSettings.objects.update_or_create(pk=1, defaults={"claude_read": False})
            wa_settings.clear()
            self.assertEqual(wa_today.suggestions([c]), {})
