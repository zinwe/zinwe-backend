from __future__ import annotations

import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from openpyxl import Workbook
from rest_framework.test import APIClient

from . import whatsapp
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTemplate

Status = WaContact.Status
BASE = "/api/admin/marketing/wa"


def sheet(rows: list[list[str]]) -> SimpleUploadedFile:
    book = Workbook()
    ws = book.active
    ws.append(["Business Name", "Phone", "Segment", "Country", "City", "First Name", "Source", "Useful Info"])
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    book.save(out)
    return SimpleUploadedFile("contacts.xlsx", out.getvalue())


class WaBase(TestCase):
    def setUp(self) -> None:
        self.staff = get_user_model().objects.create_user(email="staff@zinwe.io", password="x", is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.staff)
        self.first = WaTemplate.objects.get(name="Pen drive check, traders")
        self.campaign = WaCampaign.objects.create(
            name="Delhi traders", first=self.first, followup=WaTemplate.objects.get(kind="followup"), daily_limit=2,
        )

    def contact(self, phone: str = "+919810000101", **extra) -> WaContact:  # type: ignore[no-untyped-def]
        values = dict(campaign=self.campaign, firm="Gupta Exim", first_name="Rakesh", segment="Traders", country="India",
                      city="Delhi", source="Trade fair")
        values.update(extra)
        return WaContact.objects.create(phone=phone, **values)


class PhoneTests(TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(whatsapp.normalize_phone("98100 00101", "India"), "+919810000101")
        self.assertEqual(whatsapp.normalize_phone("098100 00101", "India"), "+919810000101")
        self.assertEqual(whatsapp.normalize_phone("+1 (214) 555-0111", "USA"), "+12145550111")
        self.assertEqual(whatsapp.normalize_phone("214 555 0111", "USA"), "+12145550111")
        self.assertIsNone(whatsapp.normalize_phone("12345", "India"))


class UploadTests(WaBase):
    def test_review_flags_each_row_and_adds_only_kept_ones(self) -> None:
        self.contact("+919810000101")
        WaBlock.objects.create(phone="+919810000110", reason="Asked to stop")
        rows = [
            ["Gupta Exim", "98100 00101", "Traders", "India", "Delhi", "Rakesh", "Fair", ""],
            ["Bansal Bros", "98100 00110", "Traders", "India", "Delhi", "", "", ""],
            ["Verma Crafts", "98100 00201", "Traders", "India", "Noida", "Asha", "IndiaMART", '{"website": "verma.example"}'],
            ["Verma Crafts again", "+91 98100 00201", "Traders", "India", "Noida", "", "", ""],
            ["Lone Star Tax", "512 555 0207", "CPA firms", "USA", "Austin", "", "", "Has a new office"],
            ["", "98100 00300", "Traders", "India", "Delhi", "", "", ""],
        ]
        r = self.api.post(f"{BASE}/contacts/upload/", {"file": sheet(rows)}, format="multipart")
        self.assertEqual(r.status_code, 200)
        got = [(x["result"], x["message"]) for x in r.json()["rows"]]
        self.assertEqual(got[0], ("bad", "Already a contact"))
        self.assertEqual(got[1], ("bad", "Asked to stop before, skipped"))
        self.assertEqual(got[2], ("ready", "Ready"))
        self.assertEqual(got[3], ("bad", "Repeat row, skipped"))
        self.assertEqual(got[4][0], "warn")
        self.assertEqual(got[5], ("bad", "Missing Business Name"))
        self.assertFalse(WaContact.objects.filter(firm="Verma Crafts").exists())  # review adds nothing

        choices = {"6": {"include": False}, "4": {"include": True, "campaign": self.campaign.id, "segment": "Exporters"}}
        r = self.api.post(f"{BASE}/contacts/upload/", {"file": sheet(rows), "add": "1", "choices": json.dumps(choices)}, format="multipart")
        self.assertEqual(r.json(), {"added": 1})
        added = WaContact.objects.get(phone="+919810000201")
        self.assertEqual((added.campaign, added.segment, added.info), (self.campaign, "Exporters", {"website": "verma.example"}))
        self.assertEqual(added.added_by, "staff@zinwe.io")

    def test_missing_column(self) -> None:
        book = Workbook()
        book.active.append(["Business Name", "Phone"])
        out = io.BytesIO()
        book.save(out)
        r = self.api.post(f"{BASE}/contacts/upload/", {"file": SimpleUploadedFile("x.xlsx", out.getvalue())}, format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Segment", r.json()["detail"])


class FlowTests(WaBase):
    def test_queue_respects_live_status_and_daily_limit(self) -> None:
        for i in range(3):
            self.contact(f"+91981000020{i}")
        self.assertEqual(whatsapp.queue(), [])
        self.campaign.status = WaCampaign.Status.LIVE
        self.campaign.save()
        self.assertEqual(len(whatsapp.queue()), 2)  # daily limit 2
        c = whatsapp.queue()[0]["contact"]
        whatsapp.mark_sent(c, "first", "Hi Rakesh", "staff@zinwe.io")
        self.assertEqual(len(whatsapp.queue()), 1)

    def test_first_then_followup_then_reply(self) -> None:
        self.campaign.status = WaCampaign.Status.LIVE
        self.campaign.save()
        c = self.contact()
        r = self.api.post(f"{BASE}/contacts/{c.id}/sent/", {"kind": "first", "text": "Hi Rakesh"}, format="json")
        self.assertEqual(r.json()["status"], "messaged")
        c.refresh_from_db()
        self.assertEqual((c.step, c.next_date), (1, whatsapp.today() + timedelta(days=3)))
        WaContact.objects.filter(pk=c.pk).update(next_date=whatsapp.today())
        self.assertEqual(whatsapp.queue()[0]["kind"], "followup")
        r = self.api.post(f"{BASE}/contacts/{c.id}/reply/", {"text": "Yes please send"}, format="json")
        body = r.json()
        self.assertEqual((body["status"], body["needs_reply"], body["next_date"]), ("replied", True, None))
        self.assertEqual(whatsapp.queue(), [])  # no follow-up after a reply
        self.assertEqual([x.id for x in whatsapp.replies()], [c.id])
        self.api.post(f"{BASE}/contacts/{c.id}/act/", {"action": "interested"}, format="json")
        self.api.post(f"{BASE}/contacts/{c.id}/sent/", {"kind": "reply", "text": "Great, here's the link"}, format="json")
        c.refresh_from_db()
        self.assertEqual((c.status, c.needs_reply), (Status.INTERESTED, False))
        self.assertEqual(list(c.messages.values_list("kind", flat=True)), ["first", "theirs", "reply"])

    def test_stop_blocks_the_number_everywhere(self) -> None:
        c = self.contact()
        self.api.post(f"{BASE}/contacts/{c.id}/reply/", {"text": "STOP"}, format="json")
        c.refresh_from_db()
        self.assertEqual(c.status, Status.STOP)
        self.assertTrue(WaBlock.objects.filter(phone=c.phone).exists())
        r = self.api.post(f"{BASE}/contacts/{c.id}/sent/", {"kind": "reply", "text": "Sorry"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_draft_fills_the_template_without_a_key(self) -> None:
        c = self.contact(first_name="")
        r = self.api.post(f"{BASE}/contacts/{c.id}/draft/", {"kind": "first"}, format="json")
        body = r.json()
        self.assertEqual(body["by"], "template")
        self.assertTrue(body["text"].startswith("Hi there, this is Vishal"))
        self.assertIn("Gupta Exim", body["text"])

    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_draft_uses_claude_when_set_and_falls_back_on_refusal(self) -> None:
        c = self.contact()
        ok = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Hi Rakesh, personal note")])
        with mock.patch("anthropic.resources.messages.Messages.create", return_value=ok) as create:
            body = whatsapp.draft(c, "first")
        self.assertEqual((body["by"], body["text"]), ("claude", "Hi Rakesh, personal note"))
        self.assertEqual(create.call_args.kwargs["output_config"], {"effort": "low"})
        refused = SimpleNamespace(stop_reason="refusal", content=[])
        with mock.patch("anthropic.resources.messages.Messages.create", return_value=refused):
            self.assertEqual(whatsapp.draft(c, "first")["by"], "template")

    def test_campaign_start_needs_contacts_and_analytics_counts(self) -> None:
        r = self.api.post(f"{BASE}/campaigns/{self.campaign.id}/start/")
        self.assertEqual(r.status_code, 400)
        c = self.contact()
        self.assertEqual(self.api.post(f"{BASE}/campaigns/{self.campaign.id}/start/").json()["status"], "live")
        whatsapp.mark_sent(c, "first", "Hi", "staff@zinwe.io")
        whatsapp.record_reply(c, "Tell me more", "staff@zinwe.io")
        a = self.api.get(f"{BASE}/analytics/").json()
        self.assertEqual((a["total"]["messaged"], a["total"]["replied"]), (1, 1))
        self.assertEqual(a["daily"][-1], {"date": whatsapp.today().isoformat(), "sent": 1, "replies": 1})
        self.assertEqual(WaMessage.objects.count(), 2)

    def test_staff_only(self) -> None:
        self.assertEqual(APIClient().get(f"{BASE}/today/").status_code, 401)
