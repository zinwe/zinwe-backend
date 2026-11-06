"""F-11: moments pop-ups (Announcements placed as "popup") and the dashboard's feedback card (me.feedback_prompt_due)."""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from ._testing import give_plan
from .models import Announcement, AnnouncementView, Feedback, User

FEED = "/api/auth/announcements/"
PROMPT = "/api/auth/feedback/prompt/"


def popup(**fields):  # type: ignore[no-untyped-def]
    starts = timezone.now() - timedelta(hours=1)
    return Announcement.objects.create(
        **{"title": "Happy Diwali", "kind": "wish", "placement": "popup", "starts_at": starts, **fields}
    )


class PopupTests(APITestCase):
    def setUp(self) -> None:
        self.owner = User.objects.create_user("owner@acme.in", "pw-12345678", email_verified_at=timezone.now())
        token, _ = Token.objects.get_or_create(user=self.owner)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.now = timezone.now()

    def feed(self, at=None):  # type: ignore[no-untyped-def]
        with mock.patch("django.utils.timezone.now", return_value=at or self.now):
            return self.client.get(FEED).json()

    def titles(self, at=None) -> list[str]:  # type: ignore[no-untyped-def]
        return [p["title"] for p in self.feed(at)["popups"]]

    def seen(self, announcement: Announcement, at=None, **body):  # type: ignore[no-untyped-def]
        with mock.patch("django.utils.timezone.now", return_value=at or self.now):
            return self.client.post(f"{FEED}{announcement.pk}/seen/", body, format="json")

    def test_popups_are_their_own_list_with_the_same_shape(self) -> None:
        a = popup(body="From all of us", cta_label="Say hi", cta_href="/app/chat")
        body = self.feed()
        self.assertNotIn("Happy Diwali", [x["title"] for x in body["announcements"]])
        self.assertEqual(body["popups"], [{
            "id": f"a{a.pk}-{int(a.updated_at.timestamp())}", "kind": "wish", "title": "Happy Diwali",
            "body": "From all of us", "cta_label": "Say hi", "cta_href": "/app/chat", "dismissible": True,
            "image_url": None, "announcement_id": a.pk, "trigger": "signin", "coupon": None,  # F-22
        }])
        # Never on the public website.
        self.client.credentials()
        self.assertEqual(self.client.get(f"{FEED}public/").json()["announcements"], [])

    def test_rhythm_first_then_2_days_then_a_week_then_never(self) -> None:
        a = popup()
        self.assertEqual(self.titles(), ["Happy Diwali"])  # never seen
        self.assertEqual(self.seen(a).json()["views"], 1)
        self.assertEqual(self.titles(self.now + timedelta(days=1)), [])
        day2 = self.now + timedelta(days=2)
        self.assertEqual(self.titles(day2), ["Happy Diwali"])
        self.assertEqual(self.seen(a, day2).json()["views"], 2)
        self.assertEqual(self.titles(day2 + timedelta(days=6)), [])
        day9 = day2 + timedelta(days=7)
        self.assertEqual(self.titles(day9), ["Happy Diwali"])
        self.assertEqual(self.seen(a, day9).json()["views"], 3)
        self.assertEqual(self.titles(day9 + timedelta(days=60)), [])  # 3 views: done

    def test_a_repeat_on_the_same_day_is_one_view(self) -> None:
        a = popup()
        self.seen(a)
        self.assertEqual(self.seen(a).json()["views"], 1)

    def test_closed_stops_it(self) -> None:
        a = popup()
        body = self.seen(a, closed=True).json()
        self.assertEqual((body["views"], body["closed"]), (1, True))
        self.assertEqual(self.titles(self.now + timedelta(days=30)), [])

    def test_one_popup_a_day(self) -> None:
        first = popup(title="First", priority=5)
        popup(title="Second")
        self.assertEqual(self.titles(), ["First"])  # one at a time, highest priority first
        self.seen(first)
        self.assertEqual(self.titles(), [])  # already had one today
        self.assertEqual(self.titles(self.now + timedelta(days=1)), ["Second"])

    def shown_alone(self, announcement: Announcement) -> bool:
        """Is this pop-up offered to the owner when it's the only live one?"""
        Announcement.objects.exclude(pk=announcement.pk).update(is_active=False)
        Announcement.objects.filter(pk=announcement.pk).update(is_active=True)
        return bool(self.feed()["popups"])

    def test_paid_plans_get_news_versions_wishes_and_maintenance_only(self) -> None:
        for kind in ("news", "version", "wish", "maintenance", "offer", "discount"):
            popup(title=kind, kind=kind)
        free = {p.title for p in Announcement.objects.all() if self.shown_alone(p)}
        self.assertEqual(free, {"news", "version", "wish", "maintenance", "offer", "discount"})
        give_plan(self.owner, "startups")
        paid = {p.title for p in Announcement.objects.all() if self.shown_alone(p)}
        self.assertEqual(paid, {"news", "version", "wish", "maintenance"})

    def test_maintenance_reaches_everyone_and_the_audience_still_applies(self) -> None:
        give_plan(self.owner, "businesses")
        popup(title="Down at 2 AM", kind="maintenance")
        popup(title="Free only", kind="news", audience="free")
        self.assertEqual(self.titles(), ["Down at 2 AM"])

    def test_seen_needs_a_popup(self) -> None:
        bar = Announcement.objects.create(title="A bar", placement="dashboard")
        self.assertEqual(self.client.post(f"{FEED}{bar.pk}/seen/", {}, format="json").status_code,
                         status.HTTP_404_NOT_FOUND)
        self.assertFalse(AnnouncementView.objects.exists())


class FeedbackPromptTests(APITestCase):
    def setUp(self) -> None:
        self.owner = User.objects.create_user(
            "owner@acme.in", "pw-12345678", email_verified_at=timezone.now(),
            date_joined=timezone.now() - timedelta(days=10),
        )
        token, _ = Token.objects.get_or_create(user=self.owner)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def due(self) -> bool:
        return self.client.get("/api/auth/me/").json()["feedback_prompt_due"]

    def a_report_sent(self) -> None:
        from z_devices.models import ReportDelivery

        today = timezone.localdate()
        ReportDelivery.objects.create(owner=self.owner, period_from=today, period_to=today, status="sent")

    def test_not_without_a_real_moment(self) -> None:
        self.assertFalse(self.due())
        self.a_report_sent()
        self.assertTrue(self.due())

    def test_five_chat_lines_count(self) -> None:
        from z_devices.models import Member, Message

        asha = Member.objects.create(owner=self.owner, name="Asha", email="asha@acme.in")
        for i in range(5):
            self.assertFalse(self.due())
            Message.objects.create(owner=self.owner, member=asha, body=f"hi {i}")
        self.assertTrue(self.due())

    def test_seven_days_of_activity_count(self) -> None:
        from z_devices.models import Device, WorkLog

        pc = Device.objects.create(owner=self.owner, hostname="PC", machine_id="m-1")
        for day in range(7):
            self.assertFalse(self.due())
            WorkLog.objects.create(owner=self.owner, device=pc, state="working",
                                   at=timezone.now() - timedelta(days=day))
        self.assertTrue(self.due())

    def test_not_in_the_first_3_days(self) -> None:
        self.a_report_sent()
        User.objects.filter(pk=self.owner.pk).update(date_joined=timezone.now() - timedelta(days=2))
        self.assertFalse(self.due())

    def test_not_within_30_days_of_feedback(self) -> None:
        self.a_report_sent()
        Feedback.objects.create(owner=self.owner, rating=9)
        self.assertFalse(self.due())
        Feedback.objects.update(created_at=timezone.now() - timedelta(days=31))
        self.assertTrue(self.due())

    def test_shown_or_dismissed_waits_30_days(self) -> None:
        self.a_report_sent()
        for action in ("shown", "dismissed"):
            User.objects.filter(pk=self.owner.pk).update(feedback_prompt_at=None)
            response = self.client.post(PROMPT, {"action": action}, format="json")
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
            self.assertFalse(self.due())
        User.objects.filter(pk=self.owner.pk).update(feedback_prompt_at=timezone.now() - timedelta(days=31))
        self.assertTrue(self.due())
        self.assertEqual(self.client.post(PROMPT, {"action": "maybe"}, format="json").status_code,
                         status.HTTP_400_BAD_REQUEST)
