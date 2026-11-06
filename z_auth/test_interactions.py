"""F-22 user interactions: moments, audiences, frequencies, the one-a-day cap, {first name}, answers, staff pages."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_analytics.models import Event
from z_devices.models import Device

from ._testing import give_plan
from .announcements import personalise
from .models import Announcement, AnnouncementView, Coupon, Feedback, Plan, User

FEED = "/api/auth/announcements/"
ADMIN = "/api/admin/announcements/"


def popup(**fields):  # type: ignore[no-untyped-def]
    starts = timezone.now() - timedelta(hours=1)
    return Announcement.objects.create(
        **{"title": "Tell us", "kind": "feedback_ask", "placement": "popup", "starts_at": starts, **fields}
    )


class InteractionTestCase(APITestCase):
    def setUp(self) -> None:
        self.owner = User.objects.create_user(
            "owner@acme.in", "pw-12345678", email_verified_at=timezone.now(), full_name="Asha Verma",
        )
        token, _ = Token.objects.get_or_create(user=self.owner)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.now = timezone.now()

    def feed(self, at=None, **params):  # type: ignore[no-untyped-def]
        with mock.patch("django.utils.timezone.now", return_value=at or self.now):
            return self.client.get(FEED, params).json()

    def titles(self, at=None, **params) -> list[str]:  # type: ignore[no-untyped-def]
        return [p["title"] for p in self.feed(at, **params)["popups"]]

    def seen(self, announcement: Announcement, at=None, **body):  # type: ignore[no-untyped-def]
        with mock.patch("django.utils.timezone.now", return_value=at or self.now):
            return self.client.post(f"{FEED}{announcement.pk}/seen/", body, format="json")

    def joined(self, days: int) -> None:
        User.objects.filter(pk=self.owner.pk).update(date_joined=self.now - timedelta(days=days))
        self.owner.refresh_from_db()


class DefaultsTests(InteractionTestCase):
    def test_old_rows_keep_their_behaviour(self) -> None:
        # Start an hour before the frozen clock: a default starts_at (made after setUp's now) is "not yet live".
        starts = self.now - timedelta(hours=1)
        a = Announcement.objects.create(title="Diwali", kind="wish", placement="popup", starts_at=starts)
        bar = Announcement.objects.create(title="News", kind="news", starts_at=starts)
        self.assertEqual((a.trigger, a.frequency, a.effective_frequency), ("signin", "", "rhythm"))
        self.assertEqual(bar.effective_frequency, "until_closed")
        item = self.feed()["popups"][0]
        self.assertEqual((item["trigger"], item["coupon"]), ("signin", None))


class MomentTests(InteractionTestCase):
    def test_sign_out_moment(self) -> None:
        popup(title="Before you go", trigger="signout")
        popup(title="Welcome", kind="wish")
        self.assertEqual(self.titles(), ["Welcome"])
        body = self.feed(moment="signout")
        self.assertEqual(set(body), {"popups"})
        self.assertEqual([p["title"] for p in body["popups"]], ["Before you go"])
        self.assertEqual(body["popups"][0]["trigger"], "signout")

    def test_one_interaction_a_day_across_kinds(self) -> None:
        welcome = popup(title="Welcome", kind="wish")
        popup(title="Before you go", trigger="signout")
        self.seen(welcome)
        self.assertEqual(self.titles(moment="signout"), [])
        self.assertEqual(self.titles(at=self.now + timedelta(days=1), moment="signout"), ["Before you go"])

    def test_days_after_signup(self) -> None:
        popup(title="Three days in", trigger="days_after_signup", trigger_days=3)
        self.joined(2)
        self.assertEqual(self.titles(), [])
        self.joined(3)
        self.assertEqual(self.titles(), ["Three days in"])

    def test_first_pc(self) -> None:
        popup(title="First PC", trigger="first_pc")
        self.assertEqual(self.titles(), [])
        Device.objects.create(owner=self.owner, machine_id="m1", hostname="PC-1", token_hash="t" * 64)
        self.assertEqual(self.titles(), ["First PC"])

    def test_before_plan_end(self) -> None:
        popup(title="Ends soon", kind="wish", trigger="before_plan_end", trigger_days=7)
        sub = give_plan(self.owner, "startups")  # ends in 30 days
        self.assertEqual(self.titles(), [])
        sub.ends_at = self.now + timedelta(days=5)
        sub.save(update_fields=["ends_at"])
        self.assertEqual(self.titles(), ["Ends soon"])

    def test_date_without_end_is_that_day_only(self) -> None:
        popup(title="New Year", kind="wish", trigger="date", starts_at=self.now - timedelta(minutes=5))
        self.assertEqual(self.titles(), ["New Year"])
        self.assertEqual(self.titles(at=self.now + timedelta(days=2)), [])


class AudienceTests(InteractionTestCase):
    def test_new_in_7_days(self) -> None:
        popup(title="Hello", audience="new7")
        self.joined(3)
        self.assertEqual(self.titles(), ["Hello"])
        self.joined(8)
        self.assertEqual(self.titles(), [])

    def test_plan_ending(self) -> None:
        popup(title="Renew", kind="wish", audience="plan_ending")
        sub = give_plan(self.owner, "startups")
        self.assertEqual(self.titles(), [])
        sub.ends_at = self.now + timedelta(days=6)
        sub.save(update_fields=["ends_at"])
        self.assertEqual(self.titles(), ["Renew"])

    def test_quiet(self) -> None:
        popup(title="Welcome back", audience="quiet", audience_days=10)
        Event.objects.create(name="auth.login", actor_email=self.owner.email)
        self.assertEqual(self.titles(), [])
        # Came back today after 12 days away.
        Event.objects.create(name="auth.login", actor_email=self.owner.email)
        Event.objects.filter(pk=Event.objects.order_by("id").first().pk).update(created_at=self.now - timedelta(days=12))
        self.assertEqual(self.titles(), ["Welcome back"])

    def test_feedback_ask_reaches_paid_owners(self) -> None:
        give_plan(self.owner, "startups")
        popup(title="Rate us")
        popup(title="Upgrade", kind="upgrade_ask", priority=5)
        self.assertEqual(self.titles(), ["Rate us"])


class FrequencyTests(InteractionTestCase):
    def test_once(self) -> None:
        a = popup(frequency="once")
        self.assertEqual(self.titles(), ["Tell us"])
        self.seen(a)
        self.assertEqual(self.titles(at=self.now + timedelta(days=30)), [])

    def test_every_time(self) -> None:
        a = popup(frequency="every")
        for day in range(5):
            at = self.now + timedelta(days=day)
            self.assertEqual(self.titles(at=at), ["Tell us"])
            self.seen(a, at=at, closed=True)

    def test_every_14_days(self) -> None:
        a = popup(frequency="every_14d")
        self.seen(a)
        self.assertEqual(self.titles(at=self.now + timedelta(days=13)), [])
        self.assertEqual(self.titles(at=self.now + timedelta(days=14)), ["Tell us"])

    def test_until_closed(self) -> None:
        a = popup(frequency="until_closed")
        for day in (1, 2, 3, 4):
            at = self.now + timedelta(days=day)
            self.assertEqual(self.titles(at=at), ["Tell us"])
            self.seen(a, at=at)
        self.seen(a, at=self.now + timedelta(days=4), closed=True)
        self.assertEqual(self.titles(at=self.now + timedelta(days=5)), [])


class TextAndCouponTests(InteractionTestCase):
    def test_first_name(self) -> None:
        popup(title="Happy New Year, {first name}!", body="{first name}, thank you.")
        item = self.feed()["popups"][0]
        self.assertEqual((item["title"], item["body"]), ("Happy New Year, Asha!", "Asha, thank you."))
        nobody = User(full_name="")
        self.assertEqual(personalise("Happy New Year, {first name}", nobody), "Happy New Year")
        self.assertEqual(personalise("Happy New Year, {first name}!", nobody), "Happy New Year!")
        self.assertEqual(personalise("{first name}, thank you.", nobody), "thank you.")

    def test_coupon_on_an_offer(self) -> None:
        Plan.objects.filter(code="startups").update(band_min_inr=None)
        coupon = Coupon.objects.create(
            code="ZWNEWYEAR30", percent_off=30, plan_codes=["startups"], starts_on=timezone.localdate(),
            ends_on=timezone.localdate().replace(month=1, day=5, year=timezone.localdate().year + 1),
        )
        popup(title="New Year offer", kind="offer", coupon=coupon)
        item = self.feed()["popups"][0]
        self.assertEqual((item["cta_label"], item["cta_href"]), ("Use the code", "/onboarding/plan?coupon=ZWNEWYEAR30"))
        self.assertEqual(item["coupon"], {
            "code": "ZWNEWYEAR30", "plan_code": "startups", "ends_on": coupon.ends_on.isoformat(),
            "note": "Startups ₹679.70 + GST for the first month, about ₹68 per PC. Ends 5 Jan.",
        })


class AnswerTests(InteractionTestCase):
    def test_answer_saves_feedback(self) -> None:
        a = popup(trigger="signout")
        response = self.client.post(f"{FEED}{a.pk}/answer/", {"rating": 9, "message": " Great "}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        fb = Feedback.objects.get()
        self.assertEqual((fb.rating, fb.message, fb.announcement_id, fb.trigger, fb.source), (9, "Great", a.pk, "signout", "owner"))
        view = AnnouncementView.objects.get()
        self.assertEqual((view.views, view.clicks), (1, 1))

    def test_bad_rating(self) -> None:
        a = popup()
        for rating in (0, 11, "x", None, True):
            response = self.client.post(f"{FEED}{a.pk}/answer/", {"rating": rating}, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, rating)
        self.assertFalse(Feedback.objects.exists())

    def test_bars_take_no_answer(self) -> None:
        bar = Announcement.objects.create(title="News", kind="news")
        response = self.client.post(f"{FEED}{bar.pk}/answer/", {"rating": 8}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class StaffInteractionTests(InteractionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        self.client.force_authenticate(self.staff)

    def test_create_with_new_fields(self) -> None:
        coupon = Coupon.objects.create(code="ZWSTARTUP15", percent_off=15)
        response = self.client.post(ADMIN, {
            "title": "How are we doing?", "kind": "feedback_ask", "trigger": "days_after_signup", "trigger_days": 7,
            "audience": "quiet", "audience_days": 21, "frequency": "once",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        row = response.json()
        self.assertEqual((row["placement"], row["trigger"], row["trigger_days"], row["frequency"]),
                         ("popup", "days_after_signup", 7, "once"))
        offer = self.client.post(ADMIN, {
            "title": "15% off", "kind": "offer", "placement": "popup", "coupon": "zwstartup15",
        }, format="json")
        self.assertEqual(offer.status_code, status.HTTP_201_CREATED, offer.content)
        self.assertEqual((offer.json()["coupon"], offer.json()["coupon_code"]), (coupon.pk, "ZWSTARTUP15"))
        bar = self.client.post(ADMIN, {"title": "Bar", "kind": "news"}, format="json").json()
        self.assertEqual((bar["frequency"], bar["trigger"]), ("until_closed", "signin"))

    def test_validation(self) -> None:
        coupon = Coupon.objects.create(code="ZWSTARTUP15", percent_off=15)
        cases = [
            ({"title": "x", "kind": "feedback_ask", "placement": "dashboard"}, "placement"),
            ({"title": "x", "kind": "news", "trigger": "signout"}, "trigger"),
            ({"title": "x", "kind": "wish", "placement": "popup", "trigger": "before_plan_end"}, "trigger_days"),
            ({"title": "x", "kind": "offer", "placement": "popup", "coupon": "ZWNOSUCHONE"}, "coupon"),
            ({"title": "x", "kind": "offer", "placement": "popup", "coupon": coupon.pk, "offer_plan": "businesses",
              "offer_price_inr": "1.00"}, "coupon"),
            ({"title": "x", "kind": "wish", "placement": "popup", "frequency": "hourly"}, "frequency"),
        ]
        for data, field in cases:
            response = self.client.post(ADMIN, data, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, data)
            self.assertIn(field, response.json(), (data, response.json()))

    def test_interactions_view(self) -> None:
        a = popup(title="Rate us", trigger="signout")
        Announcement.objects.create(title="News bar", kind="news")
        AnnouncementView.objects.create(owner=self.owner, announcement=a, views=2, last_seen_at=timezone.now(),
                                        clicks=1, clicked_at=timezone.now() - timedelta(days=1))
        Feedback.objects.create(owner=self.owner, rating=9, announcement=a, trigger="signout")
        Feedback.objects.create(owner=self.owner, rating=6, announcement=a, trigger="signout")
        sub = give_plan(self.owner, "startups")
        sub.payments.create(method="upi", amount_inr=Decimal("1145.78"), paid_on=timezone.localdate(),
                            payer_name="Asha", status="verified", reviewed_at=timezone.now())
        body = self.client.get(ADMIN, {"view": "interactions"}).json()
        self.assertEqual(body["rows"], body["interactions"])
        row = next(r for r in body["interactions"] if r["id"] == a.pk)
        self.assertEqual(row["stats"], {"shown": 1, "views": 2, "closed": 0, "clicks": 1, "feedback_count": 2,
                                        "feedback_avg": 7.5, "paid_after": 1})
        self.assertEqual(body["totals"], {"live": 2, "scheduled": 0, "paused": 0, "shown_this_month": 1,
                                          "feedback_count": 2, "feedback_avg": 7.5, "paid_after": 1})
        # The plain list is unchanged.
        self.assertIsInstance(self.client.get(ADMIN).json(), list)
        # Inbox -> Feedback names the interaction.
        rows = self.client.get("/api/admin/feedback/").json()["rows"]
        self.assertEqual((rows[0]["interaction"], rows[0]["trigger"]), ({"id": a.pk, "title": "Rate us"}, "signout"))

    def test_reach_with_new_audiences(self) -> None:
        self.joined(3)
        reach = self.client.get(f"{ADMIN}reach/", {"audience": "new7"}).json()
        self.assertEqual(reach, {"customers": 1, "total": 1})
        quiet = self.client.get(f"{ADMIN}reach/", {"audience": "quiet", "audience_days": 30}).json()
        self.assertEqual(quiet["customers"], 0)
        self.assertEqual(self.client.get(f"{ADMIN}reach/", {"audience": "quiet", "audience_days": 2}).json()["customers"], 1)
