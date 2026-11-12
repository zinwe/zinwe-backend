"""The Analytics tab (wa_analytics.py, GET /analytics/view/ and the period on /analytics/steps/): the funnel and KPIs,
send slots in India time, coupon codes and the period filter. Every test runs on a fixed clock, Thu 26 Nov 2026."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from z_auth._testing import COMPANY
from z_auth.models import Company, Coupon, CouponUse, Plan, User

from . import wa_analytics, wa_path_model
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTemplate
from .tests_wa_flow import BASE
from .tests_wa_paths import PathBase, at, clock, intro

Cat = WaTemplate.Category
CS = WaContact.CampaignStatus
Kind = WaMessage.Kind
NOW = at(2026, 11, 26, 20)


class AnalyticsBase(PathBase):
    def setUp(self) -> None:
        super().setUp()
        self.n = 0

    def who(self, camp: WaCampaign | None = None, **extra: Any) -> WaContact:
        self.n += 1
        return self.person(f"+9198100{self.n:05d}", camp=camp, **extra)

    def ours(self, c: WaContact, when: Any, template: WaTemplate | None = None, step_id: str = "", **extra: Any) -> WaMessage:
        m = WaMessage.objects.create(contact=c, kind=Kind.FLOW, text="ours", template=template, step_id=step_id,
                                     campaign=c.campaign if step_id else None, **extra)
        WaMessage.objects.filter(pk=m.pk).update(at=when)
        return m

    def theirs(self, c: WaContact, when: Any) -> None:
        m = WaMessage.objects.create(contact=c, kind=Kind.THEIRS, text="ok")
        WaMessage.objects.filter(pk=m.pk).update(at=when)

    def view(self, **params: str) -> dict[str, Any]:
        with clock(NOW):
            r = self.api.get(f"{BASE}/analytics/view/", params)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()


class FunnelTests(AnalyticsBase):
    def test_funnel_and_kpis_count_people_not_messages(self) -> None:
        startups = Plan.objects.get(code="startups").max_devices
        teams = Plan.objects.get(code="teams").max_devices
        a = self.who()  # messaged twice, replied, YES, Track, first PC, paid (Team pack)
        b = self.who()  # replied after 4 days: not a reply to the message
        c = self.who()  # YES and paid on the default pack (Startup)
        d = self.who()  # asked to stop
        self.ours(a, at(2026, 11, 16))
        self.ours(a, at(2026, 11, 19))
        self.theirs(a, at(2026, 11, 19, 15))
        self.ours(b, at(2026, 11, 16))
        self.theirs(b, at(2026, 11, 20, 12))
        self.ours(c, at(2026, 11, 17))
        self.ours(d, at(2026, 11, 17))
        WaContact.objects.filter(pk=a.pk).update(yes_date=date(2026, 11, 19), track="privacy", using_since=date(2026, 11, 22),
                                                 campaign_status=CS.PAID, pack=WaContact.Pack.TEAM)
        WaContact.objects.filter(pk=c.pk).update(yes_date=date(2026, 11, 18), campaign_status=CS.PAID)
        WaContact.objects.filter(pk=d.pk).update(status=WaContact.Status.STOP, campaign_status=CS.OPTED_OUT)
        body = self.view(campaign=str(self.camp.pk))
        self.assertEqual(body["kpis"], {"people": 4, "messages": 5, "replied": 1, "yes": 2, "track": 1, "first_pc": 1, "paid": 2,
                                        "paid_pcs": teams + startups, "stopped": 1})
        self.assertEqual([(f["label"], f["people"]) for f in body["funnel"]],
                         [("Messaged", 4), ("Replied", 1), ("Said YES", 2), ("Track set", 1), ("First PC", 1), ("Paid", 2)])
        self.assertEqual((body["campaign"], body["period"], body["since"], body["today"]), (self.camp.pk, "all", None, "2026-11-26"))
        menu = next(x for x in body["campaigns"] if x["id"] == self.camp.pk)
        self.assertEqual((menu["people"], menu["replied"], menu["engine"]), (4, 1, "path"))

    def test_campaign_scope_and_all(self) -> None:
        other = self.path_campaign(wa_path_model.preset_short(), name="Other")
        a = self.who()
        b = self.who(camp=other)
        self.ours(a, at(2026, 11, 16))
        self.ours(b, at(2026, 11, 16))
        self.assertEqual(self.view(campaign=str(other.pk))["kpis"]["people"], 1)
        self.assertEqual(self.view()["kpis"]["people"], 2)
        self.assertIsNone(self.view()["steps"])

    def test_days_split_sends_and_replies(self) -> None:
        a = self.who()
        self.ours(a, at(2026, 11, 24, 10))
        self.ours(a, at(2026, 11, 24, 18))
        self.theirs(a, at(2026, 11, 25, 9))
        body = self.view(period="7")
        self.assertEqual(len(body["days"]), 7)
        self.assertEqual(body["days"][-1]["date"], "2026-11-26")
        got = {d["date"]: (d["sent"], d["replies"]) for d in body["days"]}
        self.assertEqual((got["2026-11-24"], got["2026-11-25"]), ((2, 0), (0, 1)))
        self.assertEqual(len(self.view()["days"]), 30)


class PeriodTests(AnalyticsBase):
    def test_period_counts_people_first_messaged_in_it(self) -> None:
        old = self.who()  # first message 40 days ago, one more this week
        new = self.who()  # first message this week, replied
        self.ours(old, NOW - timedelta(days=40))
        self.ours(old, at(2026, 11, 24))
        self.ours(new, at(2026, 11, 23))
        self.theirs(new, at(2026, 11, 23, 14))
        week = self.view(period="7")
        self.assertEqual(week["since"], "2026-11-20")
        self.assertEqual((week["kpis"]["people"], week["kpis"]["messages"], week["kpis"]["replied"]), (1, 2, 1))
        all_time = self.view(period="all")
        self.assertEqual((all_time["kpis"]["people"], all_time["kpis"]["messages"]), (2, 3))
        month = self.view(period="30")
        self.assertEqual((month["since"], month["kpis"]["people"]), ("2026-10-28", 1))

    def test_bad_period_or_campaign(self) -> None:
        with clock(NOW):
            self.assertEqual(self.api.get(f"{BASE}/analytics/view/", {"period": "90"}).status_code, 400)
            self.assertEqual(self.api.get(f"{BASE}/analytics/view/", {"campaign": "x"}).status_code, 400)
            self.assertEqual(self.api.get(f"{BASE}/analytics/view/", {"campaign": "9999"}).status_code, 404)
            r = self.api.get(f"{BASE}/analytics/steps/", {"campaign": self.camp.pk, "period": "x"})
        self.assertEqual(r.status_code, 400)

    def test_steps_endpoint_period_and_went_on(self) -> None:
        a = self.who()
        b = self.who()
        self.ours(a, NOW - timedelta(days=20), template=intro(1), step_id="m1")
        self.ours(b, at(2026, 11, 24), template=intro(1), step_id="m1")
        self.theirs(b, at(2026, 11, 24, 15))
        WaContact.objects.filter(pk=b.pk).update(yes_date=date(2026, 11, 24))
        with clock(NOW):
            week = self.api.get(f"{BASE}/analytics/steps/", {"campaign": self.camp.pk, "period": "7"}).json()
            ever = self.api.get(f"{BASE}/analytics/steps/", {"campaign": self.camp.pk}).json()
        m1 = next(s for s in week["steps"] if s["step_id"] == "m1")
        self.assertEqual({k: m1[k] for k in ("sent", "replies", "reply_rate", "went_on")}, {"sent": 1, "replies": 1, "reply_rate": 100, "went_on": 1})
        m1 = next(s for s in ever["steps"] if s["step_id"] == "m1")
        self.assertEqual({k: m1[k] for k in ("sent", "replies", "went_on")}, {"sent": 2, "replies": 1, "went_on": 1})
        self.assertEqual(ever["period"], "all")
        # The tab's own step split: message steps only, with the same numbers.
        steps = self.view(campaign=str(self.camp.pk), period="7")["steps"]
        first = steps[0]
        self.assertEqual((first["step_id"], first["day"], first["title"], first["sent"], first["went_on"]), ("m1", 1, "Opener", 1, 1))
        self.assertTrue(all(s["k"] in ("send", "group") for s in steps))


class SlotTests(AnalyticsBase):
    def test_slots_in_india_time(self) -> None:
        a, b, c, d, e = (self.who() for _ in range(5))
        self.ours(a, at(2026, 11, 24, 10))  # 10-12, reply 1 h 30 min later, same day
        self.theirs(a, at(2026, 11, 24, 11) + timedelta(minutes=30))
        self.ours(b, at(2026, 11, 24, 11))  # 10-12, reply the next day
        self.theirs(b, at(2026, 11, 25, 11))
        self.ours(c, at(2026, 11, 24, 13))  # 12-14, no reply
        self.ours(d, at(2026, 11, 24, 17))  # 17-19, reply 30 min later
        self.theirs(d, at(2026, 11, 24, 17) + timedelta(minutes=30))
        self.ours(e, at(2026, 11, 24, 21))  # outside the slots
        rows = {r["slot"]: r for r in self.view()["slots"]}
        self.assertEqual(list(rows), ["10-12", "12-14", "14-17", "17-19", "other"])
        self.assertEqual({k: rows["10-12"][k] for k in ("label", "sent", "replied", "same_day", "first_reply_secs")},
                         {"label": "10–12", "sent": 2, "replied": 2, "same_day": 1, "first_reply_secs": (5400 + 86400) // 2})
        self.assertEqual((rows["12-14"]["sent"], rows["12-14"]["replied"], rows["12-14"]["first_reply_secs"]), (1, 0, None))
        self.assertEqual((rows["14-17"]["sent"], rows["17-19"]["same_day"], rows["17-19"]["first_reply_secs"]), (0, 1, 1800))
        self.assertEqual(rows["other"]["sent"], 1)
        self.assertEqual(wa_analytics.slot_of(at(2026, 11, 24, 12)), "12-14")

    def test_no_other_row_when_everything_is_in_a_slot(self) -> None:
        self.ours(self.who(), at(2026, 11, 24, 15))
        self.assertEqual([r["slot"] for r in self.view()["slots"]], ["10-12", "12-14", "14-17", "17-19"])


class CodeTests(AnalyticsBase):
    def company(self, n: int) -> tuple[Company, User]:
        owner = User.objects.create_user(f"o{n}@acme.in", "s3cure-Passphrase!", full_name="Asha Verma")
        return Company.objects.create(owner=owner, **COMPANY), owner

    def use(self, coupon: Coupon, n: int, state: str, plan: str = "startups", when: Any = None) -> None:
        company, owner = self.company(n)
        u = CouponUse.objects.create(coupon=coupon, company=company, owner=owner, plan_code=plan, plan_name=plan.title(), state=state,
                                     percent_off=coupon.percent_off, amount_inr=Decimal("825.35"), amount_off=Decimal("145.65"))
        if when is not None:
            CouponUse.objects.filter(pk=u.pk).update(created_at=when)

    def test_codes_shown_used_and_pcs_paid(self) -> None:
        live = Coupon.objects.create(code="ZWSTARTUP15", percent_off=15, starts_on=date(2026, 11, 1), plan_codes=["startups"])
        Coupon.objects.create(code="ZWTEAM25XYZ", percent_off=25, starts_on=date(2027, 1, 15), channel=Coupon.Channel.WHATSAPP)
        Coupon.objects.create(code="ZWEMAILONLY", percent_off=10, channel=Coupon.Channel.EMAIL)  # not WhatsApp: left out
        offer = WaTemplate.objects.filter(category=Cat.OFFER).order_by("id").first()
        WaTemplate.objects.filter(pk=offer.pk).update(coupon=live, stage=1)
        a, b, c = self.who(), self.who(), self.who()
        self.ours(a, at(2026, 11, 20), template=offer)
        self.ours(a, at(2026, 11, 21), template=offer)  # shown twice: one person
        self.theirs(a, at(2026, 11, 21, 15))
        self.ours(b, at(2026, 11, 22), template=offer)
        self.ours(c, at(2026, 11, 22), template=intro(1))  # no code
        self.use(live, 1, CouponUse.State.CONFIRMED, "startups", when=at(2026, 11, 23))
        self.use(live, 2, CouponUse.State.PENDING, "teams", when=at(2026, 11, 24))
        self.use(live, 3, CouponUse.State.VOID, "teams", when=at(2026, 11, 24))
        self.use(live, 4, CouponUse.State.CONFIRMED, "teams", when=NOW - timedelta(days=40))
        with clock(NOW):
            rows = {r["code"]: r for r in self.view()["codes"]}
            week = {r["code"]: r for r in self.view(period="7")["codes"]}
        self.assertEqual(set(rows), {"ZWSTARTUP15", "ZWTEAM25XYZ"})
        startups = Plan.objects.get(code="startups").max_devices
        teams = Plan.objects.get(code="teams").max_devices
        r = rows["ZWSTARTUP15"]
        self.assertEqual({k: r[k] for k in ("shown_to", "replied", "used", "pcs_paid", "percent_off", "status", "stage", "pack")},
                         {"shown_to": 2, "replied": 1, "used": 3, "pcs_paid": startups + teams, "percent_off": 15, "status": "live",
                          "stage": 1, "pack": offer.type_name})
        self.assertEqual((week["ZWSTARTUP15"]["used"], week["ZWSTARTUP15"]["pcs_paid"]), (2, startups))
        self.assertEqual({k: rows["ZWTEAM25XYZ"][k] for k in ("shown_to", "used", "status", "starts_on")},
                         {"shown_to": 0, "used": 0, "status": "scheduled", "starts_on": "2027-01-15"})


class SplitTests(AnalyticsBase):
    def test_templates_segments_and_languages(self) -> None:
        a = self.who(segment="S1", city="Jaipur", lang="hi")
        b = self.who(segment="S1", city="Delhi", lang="en")
        c = self.who(segment="S3", city="Jaipur", lang="en")
        self.ours(a, at(2026, 11, 20), template=intro(1), language="hi")
        self.theirs(a, at(2026, 11, 20, 14))
        self.ours(b, at(2026, 11, 20), template=intro(1), language="en")
        self.ours(c, at(2026, 11, 20), template=intro(4), language="en")
        WaContact.objects.filter(pk=a.pk).update(yes_date=date(2026, 11, 20))
        WaContact.objects.filter(pk=c.pk).update(status=WaContact.Status.STOP)
        WaBlock.objects.create(phone=c.phone, reason="test")
        body = self.view(campaign=str(self.camp.pk))
        t = {r["id"]: r for r in body["templates"]}
        self.assertEqual({k: t[intro(1).pk][k] for k in ("sends", "replies", "reply_rate", "went_on", "languages", "version", "category")},
                         {"sends": 2, "replies": 1, "reply_rate": 50, "went_on": 1, "languages": ["en", "hi"], "version": "A", "category": "outreach"})
        self.assertEqual(body["templates"][0]["id"], intro(1).pk)  # best reply rate first
        seg = {r["segment"]: r for r in body["segments"]}
        self.assertEqual(seg["S1"], {"segment": "S1", "cities": ["Jaipur", "Delhi"], "people": 2, "replied": 1, "yes": 1, "paid": 0})
        self.assertEqual(seg["S3"]["people"], 1)
        lang = {r["language"]: r for r in body["languages"]}
        self.assertEqual([r["language"] for r in body["languages"]], ["hi", "en"])
        self.assertEqual({k: lang["hi"][k] for k in ("people", "replied", "yes", "stopped", "messages")}, {"people": 1, "replied": 1, "yes": 1, "stopped": 0, "messages": 1})
        self.assertEqual({k: lang["en"][k] for k in ("people", "replied", "yes", "stopped")}, {"people": 2, "replied": 0, "yes": 0, "stopped": 1})

    def test_existing_analytics_keys_unchanged(self) -> None:
        with clock(NOW):
            body = self.api.get(f"{BASE}/analytics/").json()
        self.assertEqual(set(body), {"total", "daily", "segments", "languages", "campaigns", "blocked"})
