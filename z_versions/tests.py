"""The weekly new version plan: made once from Thursday 03:00 IST, drafted from feedback + issues, emailed as a PDF."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_auth.models import Feedback, Issue, User

from . import services
from .models import VersionPlan
from .pdf import plan_pdf

URL = "/api/admin/versions/"


def local(*args: int) -> datetime:
    return timezone.make_aware(datetime(*args), timezone.get_current_timezone())


def claude_reply(payload: dict) -> SimpleNamespace:  # type: ignore[type-arg]
    return SimpleNamespace(
        stop_reason="end_turn", model="claude-opus-5-5",
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


@override_settings(ANTHROPIC_API_KEY="", ZINWE_FOUNDER_EMAIL="founder@zinwe.test")
class WeeklyPlanTests(TestCase):
    def setUp(self) -> None:
        self.owner = User.objects.create_user("owner@acme.in", "pw-12345678", email_verified_at=timezone.now())

    def add(self, when: datetime) -> tuple[Feedback, Issue]:
        fb = Feedback.objects.create(owner=self.owner, rating=4, message="Reports are slow to open", page="/app/reports")
        issue = Issue.objects.create(owner=self.owner, description="Team page keeps loading", page="/app/team")
        Feedback.objects.filter(pk=fb.pk).update(created_at=when)
        Issue.objects.filter(pk=issue.pk).update(created_at=when)
        return fb, issue

    def test_week_run_at_is_the_last_thursday_3am(self) -> None:
        self.assertEqual(services.week_run_at(local(2026, 11, 5, 3, 30)), local(2026, 11, 5, 3))  # Thursday after 3
        self.assertEqual(services.week_run_at(local(2026, 11, 5, 2, 59)), local(2026, 10, 29, 3))  # Thursday before 3
        self.assertEqual(services.week_run_at(local(2026, 11, 3, 12)), local(2026, 10, 29, 3))  # Tuesday

    def test_makes_one_plan_a_week_and_emails_the_pdf(self) -> None:
        self.add(local(2026, 11, 2, 10))
        result = services.plan_due(local(2026, 11, 5, 3, 10))
        self.assertEqual(result.made, 1)
        self.assertEqual(services.plan_due(local(2026, 11, 5, 4, 10)).made, 0)  # the next hourly run

        plan = VersionPlan.objects.get()
        self.assertEqual(plan.status, VersionPlan.Status.READY)
        self.assertEqual((plan.period_start, plan.period_end), (local(2026, 10, 29, 3), local(2026, 11, 5, 3)))
        self.assertEqual(plan.counts, {"feedback": 1, "issues": 1, "average_rating": 4.0})
        self.assertEqual(plan.drafted_by, "list")  # no API key: a plain list to review
        self.assertEqual([i["decision"] for i in plan.items], ["review", "review"])
        self.assertEqual(plan.email_status, "sent")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["founder@zinwe.test"])
        self.assertIn("New version plan", message.subject)
        name, content, mimetype = message.attachments[0]
        self.assertEqual((name, mimetype), ("New-version-plan-2026-11-05.pdf", "application/pdf"))
        self.assertTrue(content.startswith(b"%PDF"))

    def test_next_week_starts_where_the_last_plan_ended(self) -> None:
        services.plan_due(local(2026, 11, 5, 3, 10))
        self.add(local(2026, 11, 4, 10))  # already counted last week
        services.plan_due(local(2026, 11, 12, 3, 10))
        latest = VersionPlan.objects.first()
        self.assertEqual(latest.period_start, local(2026, 11, 5, 3))
        self.assertEqual(latest.counts["feedback"], 0)
        self.assertEqual(latest.items, [])

    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_claude_draft_keeps_only_real_sources(self) -> None:
        fb, issue = self.add(local(2026, 11, 2, 10))
        reply = claude_reply({
            "version": "1.3.4", "headline": "Fix the Team page", "summary": "One bug blocks owners.",
            "items": [
                {"title": "Team page never loads", "kind": "bug", "decision": "in", "why": "Owners can't see their team.",
                 "sources": [{"type": "issue", "id": issue.pk}, {"type": "issue", "id": 9999}]},
                {"title": "Faster reports", "kind": "improvement", "decision": "later", "why": "Slow, but works.",
                 "sources": [{"type": "feedback", "id": fb.pk}]},
            ],
        })
        with mock.patch("anthropic.Anthropic") as client:
            client.return_value.beta.messages.create.return_value = reply
            services.plan_due(local(2026, 11, 5, 3, 10))
        sent = client.return_value.beta.messages.create.call_args.kwargs
        self.assertNotIn("owner@acme.in", sent["messages"][0]["content"])  # no personal data goes to Claude
        plan = VersionPlan.objects.get()
        self.assertEqual(plan.version, "1.3.4")
        self.assertEqual(plan.items[0]["sources"], [{"type": "issue", "id": issue.pk}])
        self.assertEqual([i["decision"] for i in plan.items], ["in", "later"])
        self.assertIn("Team page never loads", mail.outbox[0].body)

    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_claude_failure_still_sends_the_list(self) -> None:
        self.add(local(2026, 11, 2, 10))
        with mock.patch("anthropic.Anthropic", side_effect=RuntimeError("network down")):
            services.plan_due(local(2026, 11, 5, 3, 10))
        plan = VersionPlan.objects.get()
        self.assertEqual(plan.status, VersionPlan.Status.READY)
        self.assertIn("network down", plan.error)
        self.assertEqual(len(plan.items), 2)

    def test_a_failure_is_saved_and_emailed(self) -> None:
        with mock.patch.object(services, "collect", side_effect=RuntimeError("db gone")):
            services.plan_due(local(2026, 11, 5, 3, 10))
        plan = VersionPlan.objects.get()
        self.assertEqual(plan.status, VersionPlan.Status.FAILED)
        self.assertIn("failed", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].attachments, [])

    def test_pdf_handles_hindi_text(self) -> None:
        plan = VersionPlan.objects.create(
            period_start=local(2026, 10, 29, 3), period_end=local(2026, 11, 5, 3), status=VersionPlan.Status.READY,
            headline="रिपोर्ट धीमी है", items=[{"title": "रिपोर्ट", "kind": "bug", "decision": "in", "why": "", "sources": []}],
        )
        self.assertTrue(plan_pdf(plan).startswith(b"%PDF"))


@override_settings(ANTHROPIC_API_KEY="", ZINWE_FOUNDER_EMAIL="founder@zinwe.test")
class AdminApiTests(APITestCase):
    def login(self, staff: bool) -> User:
        user = (User.objects.create_superuser("staff@zinwe.test", "pw-12345678") if staff
                else User.objects.create_user("owner@acme.in", "pw-12345678", email_verified_at=timezone.now()))
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return user

    def test_staff_only(self) -> None:
        self.login(staff=False)
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_make_now_list_decide_and_download(self) -> None:
        self.login(staff=True)
        made = self.client.post(URL)
        self.assertEqual(made.status_code, 201)
        self.assertEqual(made.json()["kind"], "manual")
        rows = self.client.get(URL).json()["rows"]
        self.assertEqual(len(rows), 1)
        pk = rows[0]["id"]
        approved = self.client.patch(f"{URL}{pk}/", {"status": "approved"}, format="json")
        self.assertEqual(approved.json()["status"], "approved")
        self.assertEqual(approved.json()["decided_by"], "staff@zinwe.test")
        self.assertEqual(self.client.patch(f"{URL}{pk}/", {"status": "drafting"}, format="json").status_code, 400)
        pdf = self.client.get(f"{URL}{pk}/pdf/")
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_manual_plans_dont_block_the_weekly_one(self) -> None:
        self.login(staff=True)
        self.client.post(URL)
        self.assertEqual(services.plan_due(timezone.now() + timedelta(days=8)).made, 1)
