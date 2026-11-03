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

from . import wa_media, whatsapp
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTemplate

Status = WaContact.Status
BASE = "/api/admin/marketing/wa"


def sheet(rows: list[list[str]]) -> SimpleUploadedFile:
    book = Workbook()
    ws = book.active
    ws.append(["Business Name", "Phone", "Segment", "Country", "City", "First Name", "Source", "Useful Info", "Opt-in"])
    for r in rows:
        ws.append(r if len(r) > 8 else [*r, "Y"])
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
            name="Delhi traders", first=self.first, followup=WaTemplate.objects.get(name="One gentle follow-up"), daily_limit=2,
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
            ["Cold Lead", "98100 00400", "Traders", "India", "Delhi", "", "Directory", "", "N"],
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
        self.assertEqual(got[6], ("bad", "No opt-in: email or LinkedIn first, WhatsApp once they agree"))
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
        self.assertIn("Opt-in", r.json()["detail"])


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


class SequenceTests(WaBase):
    def live(self, **extra) -> WaCampaign:  # type: ignore[no-untyped-def]
        for k, v in extra.items():
            setattr(self.campaign, k, v)
        self.campaign.status = WaCampaign.Status.LIVE
        self.campaign.save()
        return self.campaign

    def test_four_touches_then_nothing(self) -> None:
        touch4 = WaTemplate.objects.create(name="Extra", kind="touch4", text="Hi {{FirstName}}")
        self.live(daily_limit=10, touch3=WaTemplate.objects.get(name="Day 7 polite close"), touch4=touch4)
        c = self.contact()
        start = whatsapp.today()
        whatsapp.mark_sent(c, "first", "Hi", "staff@zinwe.io")
        whatsapp.mark_sent(c, "followup", "Nudge", "staff@zinwe.io")
        c.refresh_from_db()
        self.assertEqual((c.step, c.next_date), (2, start + timedelta(days=7)))  # the polite close, day 7
        WaContact.objects.filter(pk=c.pk).update(next_date=start)
        self.assertEqual([i["kind"] for i in whatsapp.queue()], ["touch3"])
        whatsapp.mark_sent(c, "touch3", "New point", "staff@zinwe.io")
        c.refresh_from_db()
        self.assertEqual((c.step, c.next_date), (3, start + timedelta(days=60)))
        WaContact.objects.filter(pk=c.pk).update(next_date=start)
        self.assertEqual([i["kind"] for i in whatsapp.queue()], ["touch4"])
        whatsapp.mark_sent(c, "touch4", "Last one", "staff@zinwe.io")
        c.refresh_from_db()
        self.assertEqual((c.step, c.next_date), (4, None))
        self.assertEqual(whatsapp.queue(), [])
        self.assertEqual(list(c.messages.values_list("kind", flat=True)), ["first", "followup", "touch", "touch"])
        self.assertEqual(whatsapp._sent_today(), 4)  # every no-reply touch counts against the daily limit

    def test_no_touch3_template_means_the_followup_is_the_last(self) -> None:
        self.live()
        c = self.contact()
        whatsapp.mark_sent(c, "first", "Hi", "staff@zinwe.io")
        whatsapp.mark_sent(c, "followup", "Nudge", "staff@zinwe.io")
        c.refresh_from_db()
        self.assertEqual((c.step, c.next_date), (2, None))

    def test_yes_starts_the_track_sequence(self) -> None:
        self.live()
        c = self.contact(track="data")
        whatsapp.mark_sent(c, "first", "Hi", "staff@zinwe.io")
        whatsapp.record_reply(c, "HAAN", "staff@zinwe.io")
        whatsapp.act(c, "interested")
        whatsapp.mark_sent(c, "reply", "Thanks!", "staff@zinwe.io")
        c.refresh_from_db()
        self.assertEqual((c.yes_date, c.next_date), (whatsapp.today(), None))
        item = whatsapp.queue()[0]
        self.assertEqual((item["kind"], item["template"]), ("nurture", "Every YES, day 0: two questions + link"))
        self.assertIn("utm_campaign=2026-11-kelash", whatsapp.draft(c, "nurture")["text"])
        whatsapp.mark_sent(c, "nurture", "Two questions", "staff@zinwe.io")
        self.assertEqual(whatsapp.queue(), [])  # day 1 is not due yet
        WaContact.objects.filter(pk=c.pk).update(yes_date=whatsapp.today() - timedelta(days=1))
        c.refresh_from_db()
        self.assertEqual(whatsapp.queue()[0]["template"], "Data track, day 1: setup + first alert")
        c.track = "chat"
        c.save()
        self.assertEqual(whatsapp.next_nurture(c).name, "Chat track, day 1: setup + first task")

    def test_a_reply_holds_the_sequence_and_stop_ends_it(self) -> None:
        c = self.contact()
        whatsapp.act(c, "interested")
        self.assertEqual(len(whatsapp.queue()), 1)
        whatsapp.record_reply(c, "Kitne ka hai?", "staff@zinwe.io")
        self.assertEqual(whatsapp.queue(), [])  # answer them first
        whatsapp.record_reply(c, "mat bhejo", "staff@zinwe.io")
        c.refresh_from_db()
        self.assertEqual(c.status, Status.STOP)
        self.assertIsNone(whatsapp.next_nurture(c))

    def test_touch_days_must_stay_in_order_within_90(self) -> None:
        url = f"{BASE}/campaigns/{self.campaign.id}/"
        self.assertEqual(self.api.patch(url, {"touch3_days": 2}, format="json").status_code, 400)
        self.assertEqual(self.api.patch(url, {"touch4_days": 120}, format="json").status_code, 400)
        r = self.api.patch(url, {"touch3_days": 21, "touch4_days": 45}, format="json")
        self.assertEqual((r.status_code, r.json()["touch4_days"]), (200, 45))

    def test_seeded_outreach_stops_after_day_7(self) -> None:
        self.assertEqual(WaCampaign().touch3_days, 7)
        self.assertFalse(WaTemplate.objects.filter(kind="touch4").exists())
        outreach = WaTemplate.objects.filter(name__regex=r"^(V\d|Day [37] )")  # the IT firm and agency campaign
        self.assertEqual(outreach.count(), 9)
        for text in outreach.values_list("text", flat=True):
            self.assertIn("Cherry on top", text)

    def test_seeded_copy_has_no_pc_count(self) -> None:
        for text in WaTemplate.objects.values_list("text", flat=True):
            self.assertNotIn("3 PCs", text)


def picture(name: str = "offer.png", size: tuple[int, int] = (40, 30)) -> SimpleUploadedFile:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, (253, 109, 2)).save(out, format="PNG")
    return SimpleUploadedFile(name, out.getvalue(), content_type="image/png")


def video(name: str = "demo.mp4", extra: int = 100) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * extra, content_type="video/mp4")


@override_settings(MEDIA_ROOT=__import__("tempfile").mkdtemp(prefix="zinwe-wa-media-"))
class MediaTests(WaBase):
    def setUp(self) -> None:
        super().setUp()
        self.warm = WaTemplate.objects.create(name="After YES offer", kind=WaTemplate.Kind.NURTURE, text="Hi {{FirstName}}", track="", day=0)
        self.url = f"{BASE}/templates/{self.warm.pk}/media/"

    def test_a_picture_is_kept_and_served_to_staff_only(self) -> None:
        r = self.api.post(self.url, {"file": picture()}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        media = r.json()["media"]
        self.assertEqual((media["kind"], media["name"]), ("image", "offer.png"))
        self.warm.refresh_from_db()
        self.assertNotIn("offer", self.warm.media.name)  # stored under a random name
        got = self.api.get(self.url)
        self.assertEqual((got.status_code, got["Content-Type"]), (200, "image/png"))
        self.assertEqual(APIClient().get(self.url).status_code, 401)

    def test_a_video_and_replacing_it(self) -> None:
        self.api.post(self.url, {"file": picture()}, format="multipart")
        self.warm.refresh_from_db()
        old = self.warm.media.name
        r = self.api.post(self.url, {"file": video()}, format="multipart")
        self.assertEqual(r.json()["media"]["kind"], "video")
        self.assertFalse(self.warm.media.storage.exists(old))

    def test_cold_messages_stay_text_only(self) -> None:
        r = self.api.post(f"{BASE}/templates/{self.first.pk}/media/", {"file": picture()}, format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertIn("text only", r.json()["detail"])
        self.api.post(self.url, {"file": picture()}, format="multipart")
        body = {"name": "x", "kind": "first", "text": "Hi", "track": "", "day": 0}
        self.assertIn("kind", self.api.put(f"{BASE}/templates/{self.warm.pk}/", body, format="json").json())

    def test_bad_files_say_why(self) -> None:
        cases = [
            (SimpleUploadedFile("a.png", b"not a picture"), "can't be opened"),
            (SimpleUploadedFile("a.mp4", b"0123456789abcdef"), "can't be read"),
            (SimpleUploadedFile("a.gif", b"GIF89a"), "JPG, PNG or WebP"),
            (SimpleUploadedFile("a.exe", b"MZ"), "JPG, PNG or WebP"),
            (video(extra=wa_media.VIDEO_MAX), "9 MB"),
        ]
        for upload, words in cases:
            r = self.api.post(self.url, {"file": upload}, format="multipart")
            self.assertEqual(r.status_code, 400, upload.name)
            self.assertIn(words, r.json()["detail"], upload.name)
        self.assertEqual(self.api.post(self.url, {}, format="multipart").status_code, 400)

    def test_the_draft_carries_it_and_removing_cleans_up(self) -> None:
        c = self.contact(status=Status.INTERESTED, yes_date=whatsapp.today(), step=1)
        used = whatsapp.next_nurture(c)  # the after-YES message this contact gets next
        url = f"{BASE}/templates/{used.pk}/media/"
        self.api.post(url, {"file": picture()}, format="multipart")
        self.assertEqual(whatsapp.draft(c, "nurture")["media"]["kind"], "image")
        used.refresh_from_db()
        stored = used.media.name
        self.assertIsNone(self.api.delete(url).json()["media"])
        self.assertFalse(used.media.storage.exists(stored))
        self.assertEqual(self.api.get(url).status_code, 404)
        self.assertIsNone(whatsapp.draft(c, "nurture")["media"])

    def test_deleting_the_template_deletes_the_file(self) -> None:
        self.api.post(self.url, {"file": picture()}, format="multipart")
        self.warm.refresh_from_db()
        stored, storage = self.warm.media.name, self.warm.media.storage
        self.assertEqual(self.api.delete(f"{BASE}/templates/{self.warm.pk}/").status_code, 204)
        self.assertFalse(storage.exists(stored))
