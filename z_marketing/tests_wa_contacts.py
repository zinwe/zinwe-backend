"""The Contacts tab (wa_contact_view.py): the contact JSON's where / need / next / last_message, the menu's counts
(GET /contacts/counts/) and one contact's path (GET /contacts/<id>/path/). Same fixed clock as tests_wa_paths: Mon 16
Nov 2026 is the campaign's first day."""
from __future__ import annotations

from . import wa_flow, whatsapp
from .models import WaCampaign, WaContact
from .tests_wa_flow import BASE, ME
from .tests_wa_paths import PathBase, at, clock, intro


class ContactKeysTests(PathBase):
    def keys(self, c: WaContact) -> dict:
        j = self.api.get(f"{BASE}/contacts/{c.pk}/").json()
        return {k: j[k] for k in ("where", "need", "next", "last_message", "language")}

    def test_on_intro_due_then_planned(self) -> None:
        with clock(at(2026, 11, 16)):
            c = self.person()
            k = self.keys(c)
            self.assertEqual(k["next"], {"state": "due", "label": wa_flow.label(intro(1)), "when": "2026-11-16", "slot": "", "send_kind": "flow"})
            self.assertEqual(k["where"], {"key": "intro", "label": "Intro · not started", "sub": "Day 1 is their first message"})
            self.assertEqual((k["need"], k["last_message"], k["language"]), ("", None, "en"))
            self.send_due(c)
            k = self.keys(c)
        # Day 4 is Thu 19 Nov; the message just sent is the last message.
        self.assertEqual((k["next"]["state"], k["next"]["label"], k["next"]["when"]), ("planned", wa_flow.label(intro(4)), "2026-11-19"))
        self.assertEqual(k["where"], {"key": "intro", "label": "Intro · day 1", "sub": "No reply yet"})
        self.assertEqual((k["last_message"]["direction"], k["last_message"]["text"], k["last_message"]["by"]), ("out", "sent text", ME))
        with clock(at(2026, 11, 19)):
            self.send_due(c)
            self.assertEqual(self.keys(c)["next"]["when"], "2026-11-23")  # day 7 is Sunday 22: Monday

    def test_a_reply_a_yes_and_the_track(self) -> None:
        c = self.person()
        with clock(at(2026, 11, 16)):
            self.send_due(c)
            whatsapp.record_reply(c, "Yes tell me more", ME)
            k = self.keys(c)
            self.assertEqual((k["need"], k["next"]["state"], k["where"]["key"], k["where"]["sub"]), ("reply", "reply", "replied", "Replied today"))
            self.assertEqual((k["last_message"]["direction"], k["last_message"]["by"]), ("in", ""))
            whatsapp.mark_sent(c, "reply", "Great", ME)
            whatsapp.act(c, "interested")
            self.assertEqual(self.keys(c)["where"]["label"], "YES path · Track to set")
        with clock(at(2026, 11, 17)):
            self.send_due(c)  # Engage
            whatsapp.record_reply(c, "Our juniors take client files home on pen drives", ME)
            whatsapp.mark_sent(c, "reply", "Thanks", ME)
            k = self.keys(c)
        self.assertEqual((k["need"], k["next"]["state"], k["next"]["label"]), ("track", "hold", "Set the Track"))
        with clock(at(2026, 11, 17)):
            whatsapp.act(c, "track", "privacy")
            k = self.keys(c)
        self.assertEqual(k["need"], "")
        self.assertEqual(k["where"]["label"], "YES path · Track assigned")
        self.assertIn("Demo video", k["next"]["label"])

    def test_locked_stopped_paused_and_no_campaign(self) -> None:
        with clock(at(2026, 11, 16)):
            locked = self.person("+919810000401", opted_in=False)
            stopped = self.person("+919810000402")
            loose = self.contact("+919810000403", campaign=None)
            paused = self.person("+919810000404")
            whatsapp.act(stopped, "stop")
            whatsapp.act(paused, "pause")
            got = {c.pk: self.keys(c) for c in (locked, stopped, loose, paused)}
        self.assertEqual((got[locked.pk]["need"], got[locked.pk]["next"]["state"], got[locked.pk]["where"]["key"]), ("optin", "locked", "locked"))
        self.assertEqual((got[stopped.pk]["need"], got[stopped.pk]["next"]["state"]), ("", "never"))
        self.assertEqual(got[stopped.pk]["where"], {"key": "stop", "label": "Stopped", "sub": "Asked to stop, today"})
        self.assertEqual((got[loose.pk]["next"]["state"], got[loose.pk]["where"]["key"]), ("none", "none"))
        self.assertEqual(got[paused.pk]["next"], {**got[paused.pk]["next"], "state": "paused", "when": "2026-12-16", "send_kind": ""})
        self.camp.status = WaCampaign.Status.PAUSED
        self.camp.save()
        with clock(at(2026, 11, 16)):
            self.assertEqual(self.keys(locked)["next"]["state"], "locked")
            k = self.keys(self.person("+919810000405"))
        self.assertEqual((k["next"]["state"], k["next"]["when"]), ("paused", None))

    def test_slots_contacts_get_what_their_rules_say(self) -> None:
        old = self.contact("+919810000999")  # FlowBase's slots ninety campaign
        with clock(at(2026, 11, 16)):
            j = self.api.get(f"{BASE}/contacts/").json()
        k = next(x for x in j if x["id"] == old.pk)
        self.assertEqual((k["next"]["state"], k["next"]["label"], k["next"]["send_kind"]), ("due", wa_flow.label(intro(1)), "flow"))
        self.assertEqual(k["where"]["key"], "intro")


class CountsAndPathTests(PathBase):
    def test_counts_for_the_whole_list(self) -> None:
        with clock(at(2026, 11, 16)):
            a = self.person("+919810000501", segment="S1", city="Jaipur")
            b = self.person("+919810000502", segment="S1", city="Delhi")
            self.person("+919810000503", segment="S2", city="Pune", opted_in=False)
            self.contact("+919810000504", segment="S3", campaign=None)
            self.send_due(a)
            self.send_due(b)
            whatsapp.record_reply(b, "Price?", ME)
            n = self.api.get(f"{BASE}/contacts/counts/").json()
        self.assertEqual(n["all"], 4)
        self.assertEqual(n["where"], {"intro": 1, "replied": 1, "yes": 0, "notnow": 0, "paid": 0, "stop": 0, "locked": 1, "none": 1})
        self.assertEqual((n["need"], n["need_by"]), (2, {"reply": 1, "track": 0, "optin": 1}))
        self.assertEqual(n["segments"][0], {"segment": "S1", "count": 2, "cities": ["Delhi", "Jaipur"]})
        self.assertEqual((n["campaigns"], n["no_campaign"]), ({str(self.camp.pk): 3}, 1))
        self.assertEqual(n["tracks"], {"privacy": 0, "activity": 0, "both": 0, "unset": 0})
        self.assertEqual(n["languages"]["en"], 4)

    def test_one_contacts_path(self) -> None:
        with clock(at(2026, 11, 16)):
            c = self.person()
            self.send_due(c)
        with clock(at(2026, 11, 17)):
            p = self.api.get(f"{BASE}/contacts/{c.pk}/path/").json()
        self.assertEqual((p["campaign"], p["campaign_name"], p["engine"], p["day"]), (self.camp.pk, "Diwali to Republic Day", "path", 2))
        done = [s for s in p["steps"] if s["state"] == "done"]
        self.assertEqual([(s["label"], s["date"]) for s in done], [("Added to Diwali to Republic Day", "2026-11-16"), (wa_flow.label(intro(1)), "2026-11-16")])
        now = [s for s in p["steps"] if s["state"] == "now"]
        self.assertEqual([s["label"] for s in now], ["Wait 3 days"])
        nxt = [s for s in p["steps"] if s["state"] == "next"]
        self.assertEqual((nxt[0]["label"], nxt[0]["date"]), (wa_flow.label(intro(4)), "2026-11-19"))
        self.assertEqual((nxt[1]["date"], nxt[1]["note"]), ("2026-11-23", "moved from Sun 22 Nov"))
        self.assertEqual(len(nxt), 8)
        self.assertGreater(p["more"]["count"], 10)
        with clock(at(2026, 11, 17)):
            whatsapp.act(c, "stop")
            p = self.api.get(f"{BASE}/contacts/{c.pk}/path/").json()
        self.assertEqual([(s["state"], s["label"], s["note"]) for s in p["steps"] if s["state"] != "done"],
                         [("now", "Blocked in every campaign", "Asked to stop")])
        self.assertEqual(self.api.get(f"{BASE}/contacts/999999/path/").status_code, 404)

    def test_a_slots_contacts_path(self) -> None:
        old = self.contact("+919810000999")
        with clock(at(2026, 11, 16)):
            p = self.api.get(f"{BASE}/contacts/{old.pk}/path/").json()
        self.assertEqual(p["engine"], "slots")
        upcoming = [s for s in p["steps"] if s["state"] in ("now", "next")]
        self.assertEqual((upcoming[0]["state"], upcoming[0]["label"], upcoming[0]["date"]), ("now", wa_flow.label(intro(1)), "2026-11-16"))
        self.assertGreater(len(upcoming), 1)
