"""The 100 Reviews campaign: categories, the Track, the campaign status and the seed (wa_flow.py, wa_seed.py)."""
from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from z_auth.models import Coupon

from . import wa_flow, wa_seed, whatsapp
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTemplate

Cat = WaTemplate.Category
CS = WaContact.CampaignStatus
BASE = "/api/admin/marketing/wa"
ME = "staff@zinwe.io"


def write(t: WaTemplate, en: str = "", hi: str = "") -> WaTemplate:
    t.text = en or f"{t.name} for {{{{FirstName}}}}. Reply STOP to opt out."
    t.text_hi = hi or f"{t.name} {{{{FirstName}}}} ke liye. Reply STOP to opt out."
    t.save()
    return t


class FlowBase(TestCase):
    def setUp(self) -> None:
        self.staff = get_user_model().objects.create_user(email=ME, password="x", is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.staff)
        wa_seed.templates()
        for t in WaTemplate.objects.exclude(category=""):
            write(t)
        self.today = whatsapp.today()
        self.campaign = WaCampaign.objects.create(
            name="100 Reviews", first=WaTemplate.objects.get(category=Cat.OUTREACH, step=1), daily_limit=10,
            plan=WaCampaign.Plan.NINETY, status=WaCampaign.Status.LIVE,
        )

    def contact(self, phone: str = "+919810000101", **extra: object) -> WaContact:
        values: dict[str, object] = dict(campaign=self.campaign, firm="Mehta & Co CA", first_name="Rohit", segment="S1",
                                         country="India", city="Delhi", source="Phone call")
        values.update(extra)
        return WaContact.objects.create(phone=phone, **values)

    def pass_days(self, c: WaContact, n: int) -> WaContact:
        """Move the contact n days into the past, as if n days went by."""
        for field in ("first_sent", "yes_date", "using_since", "offer_sent"):
            if getattr(c, field):
                setattr(c, field, getattr(c, field) - timedelta(days=n))
        c.save()
        for m in c.messages.all():
            WaMessage.objects.filter(pk=m.pk).update(at=m.at - timedelta(days=n))
        c.refresh_from_db()
        return c

    def due(self, c: WaContact) -> dict | None:
        return next((i for i in whatsapp.queue() if i["contact"].pk == c.pk), None)

    def send(self, c: WaContact, item: dict | None = None) -> WaContact:
        item = item or self.due(c)
        assert item is not None and item["template"] is not None, item
        whatsapp.mark_sent(c, "flow", "sent text", ME, item["template"].pk)
        c.refresh_from_db()
        return c


class SeedTests(TestCase):
    def test_the_86_rows_as_drafts_and_again_keeps_them(self) -> None:
        self.assertEqual(wa_seed.templates()["made"], 86)
        self.assertEqual(wa_seed.templates(), {"made": 0, "kept": 86})
        rows = WaTemplate.objects.exclude(category="")
        counts = {c: rows.filter(category=c).count() for c in Cat.values if c}
        self.assertEqual(counts, {"outreach": 30, "engage": 18, "demo_video": 18, "setup": 2, "demo_call": 1, "offer": 8, "testimonial": 2, "quick_reply": 7})
        self.assertTrue(all(wa_flow.is_draft(t) for t in rows))
        self.assertEqual(sorted(rows.filter(category=Cat.OUTREACH, stage=2).values_list("step", flat=True)), list(range(31, 59, 3)))
        for track in ("privacy", "activity", "both"):
            self.assertEqual(rows.filter(category=Cat.ENGAGE, track=track).count(), 6)
            self.assertEqual(rows.filter(category=Cat.DEMO_VIDEO, track=track).count(), 6)
        self.assertFalse(rows.exclude(category__in=wa_flow.TRACKED).exclude(track="").exists())  # Track only on Engage + Demo video

    def test_coupons_are_linked_to_their_offer_rows(self) -> None:
        wa_seed.templates()
        self.assertEqual(wa_seed.coupons()["made"], 8)
        self.assertEqual(wa_seed.coupons()["made"], 0)
        offers = WaTemplate.objects.filter(category=Cat.OFFER)
        self.assertFalse(offers.filter(coupon__isnull=True).exists())
        self.assertEqual(set(offers.filter(event=True).values_list("coupon__code", flat=True)), {"ZWNEWYEAR30", "ZWTIRANGA40"})
        tiranga = Coupon.objects.get(code="ZWTIRANGA40")
        self.assertEqual((tiranga.percent_off, tiranga.plan_codes, str(tiranga.ends_on)), (40, ["teams"], "2027-01-28"))
        self.assertEqual(Coupon.objects.get(code="ZWNEWYEAR30").plan_codes, [])

    @override_settings(ZINWE_ENV="dev")
    def test_real_rows_only_on_prod(self) -> None:
        with self.assertRaises(CommandError):
            call_command("seed_100_reviews", "--templates")
        with self.assertRaises(CommandError):
            call_command("seed_100_reviews", "--coupons")
        call_command("seed_100_reviews", "--sample", stdout=StringIO())
        self.assertTrue(WaTemplate.objects.filter(category=Cat.OUTREACH, step=1).exclude(text="").exists())
        self.assertFalse(Coupon.objects.filter(code="ZWSTARTUP15").exists())

    @override_settings(ZINWE_ENV="prod")
    def test_no_sample_on_prod(self) -> None:
        with self.assertRaises(CommandError):
            call_command("seed_100_reviews", "--sample")
        call_command("seed_100_reviews", "--templates", "--coupons", stdout=StringIO())
        self.assertEqual(WaTemplate.objects.exclude(category="").count(), 86)


class TrackTests(TestCase):
    def test_suggest_track(self) -> None:
        self.assertEqual(wa_flow.suggest_track("Client files. Juniors take them home."), "privacy")
        self.assertEqual(wa_flow.suggest_track("I want to see who is on what, the team's day"), "activity")
        self.assertEqual(wa_flow.suggest_track("Both honestly"), "both")
        self.assertEqual(wa_flow.suggest_track("Haan dono chahiye"), "both")
        self.assertEqual(wa_flow.suggest_track("ok"), "")


class FlowTests(FlowBase):
    def test_outreach_by_day_then_no_reply(self) -> None:
        c = self.contact()
        item = self.due(c)
        self.assertEqual((item["template"].category, item["template"].step), (Cat.OUTREACH, 1))
        c = self.send(c)
        self.assertEqual((c.campaign_status, c.out_day, c.first_sent, c.status), (CS.MESSAGED, 1, self.today, WaContact.Status.MESSAGED))
        self.assertIsNone(self.due(c))  # one a day
        c = self.pass_days(c, 1)
        self.assertIsNone(self.due(c))  # day 2: nothing; the next Intro is day 4
        c = self.pass_days(c, 2)
        self.assertEqual(self.due(c)["template"].step, 4)
        self.assertEqual(wa_flow.day_of(c, self.today), 4)
        # Skipped days never pile up: on day 20 the latest due day goes, not every missed one.
        c = self.pass_days(c, 16)
        self.assertEqual(self.due(c)["template"].step, 19)
        WaContact.objects.filter(pk=c.pk).update(out_day=88)
        c.refresh_from_db()
        self.assertIsNone(self.due(c))
        c.refresh_from_db()
        self.assertEqual(c.campaign_status, CS.NO_REPLY)

    def test_a_draft_is_never_sent(self) -> None:
        t = WaTemplate.objects.get(category=Cat.OUTREACH, step=1)
        t.text_hi = ""
        t.save()
        c = self.contact()
        self.assertTrue(wa_flow.is_draft(t))
        with self.assertRaisesMessage(whatsapp.WaError, "draft"):
            whatsapp.mark_sent(c, "flow", "hi", ME, t.pk)
        r = self.api.get(f"{BASE}/today/").json()
        row = next(q for q in r["queue"] if q["contact"]["id"] == c.pk)
        self.assertTrue(row["draft"])

    def test_the_yes_path_follows_the_track(self) -> None:
        c = self.send(self.contact())
        whatsapp.record_reply(c, "What is this?", ME)
        c.refresh_from_db()
        self.assertEqual(c.campaign_status, CS.REPLIED)
        whatsapp.mark_sent(c, "reply", "It protects client files.", ME)
        whatsapp.act(c, "interested")
        c.refresh_from_db()
        self.assertEqual(c.campaign_status, CS.YES)
        c = self.pass_days(c, 1)
        # Unassigned: a Need discovery question, leaning on what they said (nothing clear here: Data Privacy).
        item = self.due(c)
        self.assertEqual((item["template"].category, item["template"].type_name, item["template"].track), (Cat.ENGAGE, "Need discovery", "privacy"))
        with self.assertRaisesMessage(whatsapp.WaError, "Track"):
            whatsapp.mark_sent(c, "flow", "x", ME, WaTemplate.objects.get(category=Cat.DEMO_VIDEO, track="privacy", step=1).pk)
        c = self.send(c, item)
        self.assertEqual((c.campaign_status, c.engage_step), (CS.ENGAGED, 1))
        c = self.send(self.pass_days(c, 1))
        c = self.pass_days(c, 1)
        self.assertEqual(self.due(c)["note"], "set_track")  # two questions, no Track yet: staff decide
        # Staff set the Track from the answer: the rest of that Track's Engage steps, then its Demo videos.
        r = self.api.post(f"{BASE}/contacts/{c.pk}/act/", {"action": "track", "note": "activity"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["track"], r.json()["campaign_status"]), ("activity", CS.TRACK))
        c.refresh_from_db()
        steps = []
        for _ in range(4 + 6):
            item = self.due(c)
            steps.append((item["template"].category, item["template"].track, item["template"].step))
            c = self.pass_days(self.send(c, item), 1)
        self.assertEqual(steps[:4], [(Cat.ENGAGE, "activity", s) for s in (3, 4, 5, 6)])
        self.assertEqual(steps[4:], [(Cat.DEMO_VIDEO, "activity", s) for s in range(1, 7)])
        self.assertEqual(c.campaign_status, CS.DEMO)
        # Setup, then a three-day wait before the nudge; a setup error brings the Demo call first.
        c = self.send(c)
        self.assertEqual(c.campaign_status, CS.SETUP)
        c = self.pass_days(c, 1)
        self.assertIsNone(self.due(c))
        whatsapp.act(c, "setup_error")
        c.refresh_from_db()
        self.assertEqual(self.due(c)["template"].category, Cat.DEMO_CALL)
        c = self.pass_days(self.send(c), 1)
        self.assertEqual(c.campaign_status, CS.DEMO_CALL)
        whatsapp.act(c, "using")
        c.refresh_from_db()
        # Offer: the live code for their pack (no codes live in this test: nothing is due).
        self.assertIsNone(self.due(c))
        start = self.today - timedelta(days=1)
        live = Coupon.objects.create(code="ZWTESTLIVE1", percent_off=20, plan_codes=["startups"], starts_on=start, ends_on=self.today + timedelta(days=10))
        ended = Coupon.objects.create(code="ZWTESTGONE1", percent_off=15, plan_codes=["startups"], starts_on=start - timedelta(days=30), ends_on=start)
        party = Coupon.objects.create(code="ZWTESTPRTY1", percent_off=40, plan_codes=[], starts_on=start, ends_on=self.today + timedelta(days=3))
        o1, o2, o3 = WaTemplate.objects.filter(category=Cat.OFFER)[:3]
        WaTemplate.objects.filter(pk=o1.pk).update(coupon=ended, event=False)
        WaTemplate.objects.filter(pk=o2.pk).update(coupon=party, event=True)
        WaTemplate.objects.filter(pk=o3.pk).update(coupon=live, event=False)
        self.assertEqual(self.due(c)["template"].pk, o3.pk)  # not the ended code, not the 40% festival code
        with self.assertRaisesMessage(whatsapp.WaError, "ended"):
            whatsapp.mark_sent(c, "flow", "x", ME, o1.pk)
        c = self.pass_days(self.send(c), 1)
        self.assertEqual((c.campaign_status, c.pack), (CS.OFFER, WaContact.Pack.STARTUP))
        whatsapp.act(c, "paid")
        c.refresh_from_db()
        self.assertEqual(c.campaign_status, CS.PAID)
        self.assertIsNone(self.due(c))
        # Testimonial: 14 days after the first PC, paid or free; written first, then video.
        c = self.pass_days(c, 14)
        item = self.due(c)
        self.assertEqual((item["template"].category, item["template"].step), (Cat.TESTIMONIAL, 1))
        c = self.pass_days(self.send(c, item), 1)
        self.assertEqual(c.testimonial, WaContact.Testimonial.WRITTEN_ASKED)
        self.assertIsNone(self.due(c))
        whatsapp.act(c, "review_written")
        c.refresh_from_db()
        self.assertEqual(self.due(c)["template"].step, 2)

    def test_engage_leans_on_their_words(self) -> None:
        c = self.contact(yes_date=self.today - timedelta(days=1), status=WaContact.Status.INTERESTED, campaign_status=CS.YES)
        WaMessage.objects.create(contact=c, kind=WaMessage.Kind.THEIRS, text="I never know who is working on what, the team's day")
        self.assertEqual(self.due(c)["template"].track, "activity")

    def test_stop_at_any_point_opts_out(self) -> None:
        c = self.send(self.contact())
        whatsapp.record_reply(c, "band karo", ME)
        c.refresh_from_db()
        self.assertEqual((c.campaign_status, c.status), (CS.OPTED_OUT, WaContact.Status.STOP))
        self.assertTrue(WaBlock.objects.filter(phone=c.phone).exists())
        self.assertIsNone(self.due(self.pass_days(c, 10)))
        with self.assertRaises(whatsapp.WaError):
            whatsapp.mark_sent(c, "flow", "x", ME, WaTemplate.objects.get(category=Cat.OUTREACH, step=4).pk)

    def test_quick_reply_answers_without_moving_the_path(self) -> None:
        c = self.send(self.contact())
        whatsapp.record_reply(c, "Is this spyware?", ME)
        quick = WaTemplate.objects.get(category=Cat.QUICK, type_name="Is this spyware?")
        whatsapp.mark_sent(c, "reply", "No.", ME, quick.pk)
        c.refresh_from_db()
        self.assertEqual((c.needs_reply, c.campaign_status, c.out_day), (False, CS.REPLIED, 1))
        self.assertEqual(c.messages.last().template_id, quick.pk)

    def test_legacy_lookups_skip_campaign_rows(self) -> None:
        c = self.contact(campaign=None)
        self.assertEqual(whatsapp._template_for_any(c, "first").category, "")


class TemplateApiTests(FlowBase):
    def test_category_rules(self) -> None:
        r = self.api.post(f"{BASE}/templates/", {"category": "outreach", "type_name": "Problem", "track": "privacy", "step": 7}, format="json")
        self.assertEqual(r.status_code, 201, r.json())
        body = r.json()
        self.assertEqual((body["track"], body["stage"], body["kind"], body["draft"]), ("", 1, "first", True))
        self.assertEqual(body["name"], "Intro · Problem · day 7")
        r = self.api.post(f"{BASE}/templates/", {"category": "engage", "type_name": "Need discovery", "step": 1}, format="json")
        self.assertEqual((r.status_code, list(r.json())), (400, ["track"]))
        r = self.api.post(f"{BASE}/templates/", {"category": "outreach", "type_name": "Opener", "step": 95}, format="json")
        self.assertIn("step", r.json())
        gone = Coupon.objects.create(code="ZWTESTGONE2", percent_off=10, starts_on=self.today - timedelta(days=9), ends_on=self.today - timedelta(days=1))
        r = self.api.post(f"{BASE}/templates/", {"category": "offer", "type_name": "Startup pack", "stage": 1, "coupon": gone.pk}, format="json")
        self.assertIn("coupon", r.json())

    def test_list_carries_performance(self) -> None:
        c = self.send(self.contact())
        whatsapp.record_reply(c, "Tell me more", ME)
        rows = {t["id"]: t for t in self.api.get(f"{BASE}/templates/").json()}
        opener = WaTemplate.objects.get(category=Cat.OUTREACH, step=1)
        self.assertEqual(rows[opener.pk]["performance"], {"sends": 1, "replies": 1, "next": 0})

    def test_patching_the_track_assigns_it(self) -> None:
        c = self.contact(yes_date=self.today, status=WaContact.Status.INTERESTED, campaign_status=CS.ENGAGED)
        r = self.api.patch(f"{BASE}/contacts/{c.pk}/", {"track": "both"}, format="json")
        self.assertEqual((r.json()["track"], r.json()["campaign_status"]), ("both", CS.TRACK))
