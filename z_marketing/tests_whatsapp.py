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
        self.assertEqual(got[6], ("locked", "Added locked: no messages until they opt in"))
        self.assertFalse(WaContact.objects.filter(firm="Verma Crafts").exists())  # review adds nothing

        choices = {"6": {"include": False}, "4": {"include": True, "campaign": self.campaign.id, "segment": "Exporters"}}
        r = self.api.post(f"{BASE}/contacts/upload/", {"file": sheet(rows), "add": "1", "choices": json.dumps(choices)}, format="multipart")
        self.assertEqual(r.json(), {"added": 2})  # the row without opt-in comes in locked
        self.assertFalse(WaContact.objects.get(firm="Cold Lead").opted_in)
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
        self.assertTrue(body["text"].startswith("Hello Gupta Exim team, this is Vishal"))  # no name: greet the business
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
        self.live(interested=WaTemplate.objects.get(name="YES reply: two questions + sign-up link"))
        c = self.contact(track="data")
        whatsapp.mark_sent(c, "first", "Hi", "staff@zinwe.io")
        whatsapp.record_reply(c, "HAAN", "staff@zinwe.io")
        whatsapp.act(c, "interested")
        reply = whatsapp.draft(c, "interested")  # the YES is answered once: two questions + the sign-up link
        self.assertEqual(reply["template"], "YES reply: two questions + sign-up link")
        self.assertIn("utm_campaign=2026-11-it-agency", reply["text"])
        whatsapp.mark_sent(c, "reply", reply["text"], "staff@zinwe.io")
        c.refresh_from_db()
        self.assertEqual((c.yes_date, c.next_date), (whatsapp.today(), None))
        self.assertEqual(whatsapp.queue(), [])  # no second day-0 message; day 1 is not due yet
        WaContact.objects.filter(pk=c.pk).update(yes_date=whatsapp.today() - timedelta(days=1))
        c.refresh_from_db()
        self.assertEqual(whatsapp.queue()[0]["template"], "Data track, day 1: setup + first alert")
        c.track = "chat"
        c.save()
        self.assertEqual(whatsapp.next_nurture(c).name, "Chat track, day 1: setup + first task")

    def test_a_reply_holds_the_sequence_and_stop_ends_it(self) -> None:
        c = self.contact()
        whatsapp.act(c, "interested")
        WaContact.objects.filter(pk=c.pk).update(yes_date=whatsapp.today() - timedelta(days=2))  # the install check is due
        c.refresh_from_db()
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
        outreach = WaTemplate.objects.filter(name__regex=r"^(IT-\d|AG-\d|Day [37] )")  # the IT firm and agency campaign
        self.assertEqual(outreach.count(), 9)
        for text in outreach.values_list("text", flat=True):
            self.assertIn("Cherry on top", text)

    def test_after_yes_runs_to_day_90(self) -> None:
        days = sorted(set(WaTemplate.objects.filter(kind="nurture").values_list("day", flat=True)))
        self.assertEqual(days, [1, 2, 4, 7, 10, 14, 21, 30, 45, 60, 90])
        self.assertFalse(WaTemplate.objects.filter(text__contains="2026-11-kelash").exists())
        self.assertEqual(WaTemplate.objects.filter(kind="question", name__startswith="Answer: ").count(), 4)
        self.assertIn("Approved answers:", whatsapp.SYSTEM)

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


class OptInTests(WaBase):
    def locked(self, phone: str = "+919810000501") -> WaContact:
        return self.contact(phone, opted_in=False)

    def test_an_upload_without_opt_in_adds_them_locked(self) -> None:
        rows = [["Cold Lead", "98100 00400", "Traders", "India", "Delhi", "", "Google Maps", "", "FALSE"],
                ["Warm Lead", "98100 00401", "Traders", "India", "Delhi", "", "Fair", "", "Y"]]
        r = self.api.post(f"{BASE}/contacts/upload/", {"file": sheet(rows), "add": "1", "choices": "{}"}, format="multipart")
        self.assertEqual(r.json(), {"added": 2})
        cold, warm = WaContact.objects.get(firm="Cold Lead"), WaContact.objects.get(firm="Warm Lead")
        self.assertEqual((cold.opted_in, warm.opted_in), (False, True))
        self.assertEqual(warm.opt_in_note, "Opt-in in the uploaded sheet")

    def test_locked_contacts_are_never_queued_drafted_or_sent(self) -> None:
        self.campaign.status = WaCampaign.Status.LIVE
        self.campaign.save()
        c = self.locked()
        self.assertNotIn(c.pk, [q["contact"].pk for q in whatsapp.queue()])
        with self.assertRaisesMessage(whatsapp.WaError, "Not opted in"):
            whatsapp.draft(c, "first")
        with self.assertRaisesMessage(whatsapp.WaError, "Not opted in"):
            whatsapp.mark_sent(c, "first", "Hi", "staff@zinwe.io")
        r = self.api.post(f"{BASE}/contacts/{c.pk}/draft/", {"kind": "first"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_opting_in_needs_a_note_then_they_are_queued(self) -> None:
        self.campaign.status = WaCampaign.Status.LIVE
        self.campaign.save()
        c = self.locked()
        url = f"{BASE}/contacts/{c.pk}/act/"
        self.assertEqual(self.api.post(url, {"action": "optin"}, format="json").status_code, 400)
        data = self.api.post(url, {"action": "optin", "note": "Said yes by email, 5 Nov"}, format="json").json()
        self.assertEqual((data["opted_in"], data["opt_in_note"]), (True, "Said yes by email, 5 Nov"))
        self.assertIn(c.pk, [q["contact"].pk for q in whatsapp.queue()])

    def test_a_yes_or_their_own_message_counts_as_agreement(self) -> None:
        a, b = self.locked("+919810000502"), self.locked("+919810000503")
        whatsapp.act(a, "interested")
        whatsapp.record_reply(b, "Hello, saw your site", "staff@zinwe.io")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertTrue(a.opted_in and b.opted_in)
        self.assertIn("Said yes", a.opt_in_note)
        self.assertIn("messaged us", b.opt_in_note)

    def test_contacts_from_before_count_as_opted_in(self) -> None:
        self.assertTrue(self.contact("+919810000504").opted_in)


class NameTests(WaBase):
    def address(self, first: str, firm: str, wa: str = "", source: str = "Google Maps", rating: str = "4.8") -> dict:
        from types import SimpleNamespace

        from .wa_names import address

        info = {"rating": rating, **({"wa_business_name": wa} if wa else {})}
        return address(SimpleNamespace(first_name=first, firm=firm, source=source, info=info))

    def test_scraps_of_business_names_are_not_people(self) -> None:
        for first, firm, wa in [
            ("IT", "It Solutions & Services (itss)", "IT Solutions and Services"), ("Sun", "Sun Info Global Services", ""),
            ("digital", "Startupway24 It Services Pvt. Ltd.", "digital Life"), ("Excel", "Excel Range Media", "Excel Range Media"),
            ("Trace", "Trace Presence", "Trace Presence"), ("Sole", "Sole Freelancer – Digital Marketing Expert", "solefreelancer"),
            ("Sales", "Comprompt Solutions Llp", "Sales Comprompt"), ("Mohit", "DigiHeadway - Digital Marketing", "~Mohit Uptown Properties"),
            ("", "Webgross - Digital Marketing Agency in Delhi", ""),
        ]:
            self.assertEqual(self.address(first, firm, wa)["first_name"], "", firm)

    def test_people_are_greeted_by_name(self) -> None:
        self.assertEqual(self.address("Kishan", "Appfinz Technologies | Certified Shopify", "Kishan Kumar Mishra")["greeting"], "Kishan")
        self.assertEqual(self.address("Aushi", "Headway Digital Solutions", "Aushi V Saxena")["greeting"], "Aushi")
        self.assertEqual(self.address("", "Dhanashree Kokate - Website Developer In Pune")["greeting"], "Dhanashree")
        self.assertEqual(self.address("Rakesh", "Gupta Exim", source="Trade fair")["greeting"], "Rakesh")  # typed in by staff

    def test_short_clean_business_names(self) -> None:
        cases = {
            "Webgross - Digital Marketing Agency in Delhi": "Webgross", "Startupway24 It Services Pvt. Ltd.": "Startupway24 It Services",
            "𝐒𝐓𝐒 𝐃𝐢𝐠𝐢𝐭𝐚𝐥 𝐒𝐨𝐥𝐮𝐭𝐢𝐨𝐧𝐬": "STS Digital Solutions", "✅ 𝗗𝗶𝗴𝗶𝘁𝗮𝗹 𝗘𝘅𝗽𝗲𝗿𝘁𝘇": "Digital Expertz",
            "Digital Samay Institute of Internet Marketing": "Digital Samay Institute", "Soft Wind Technology: Turning Ideas": "Soft Wind Technology",
        }
        for firm, short in cases.items():
            self.assertEqual(self.address("", firm)["business"], short, firm)
        self.assertEqual(self.address("", "Webgross - Digital Marketing Agency in Delhi")["greeting"], "Webgross team")

    def test_how_we_know_comes_from_their_data(self) -> None:
        self.assertEqual(self.address("", "Webgross")["how_we_know"], "I found you on Google Maps, where you're rated 4.8.")
        self.assertEqual(self.address("", "Webgross", rating="")["how_we_know"], "I found you on Google Maps.")
        self.assertEqual(self.address("Kishan", "Appfinz Technologies", "Kishan Kumar Mishra")["how_we_know"],
                         "I found Appfinz Technologies on Google Maps, where you're rated 4.8.")
        self.assertEqual(self.address("", "X", source="Uploaded 03 Nov 2026")["how_we_know"], "")
        self.assertEqual(self.address("", "Shanta Narang - Best Seo Expert and Consultant")["how_we_know"],
                         "I found you on Google Maps, where you're rated 4.8.")  # her own name: "you", not "Shanta Narang"

    def test_how_we_know_follows_the_opt_in_sources(self) -> None:
        from .wa_names import how_we_know

        cases = [
            ("Cold email reply (Zoho)", {}, "Thanks for replying to my email."),
            ("LinkedIn", {}, "Thanks for connecting on LinkedIn."),
            ("Referral", {"referred_by": "Amit from your CA's office"}, "Amit from your CA's office suggested I write."),
            ("Referral", {}, ""),  # never a vague "a friend"
            ("Website and sign-up forms", {}, "Thanks for leaving your number on zinwe.io."),
            ("Events and meetups", {"event": "Nasscom Delhi meetup"}, "Good to meet you at Nasscom Delhi meetup."),
            ("Events and meetups", {}, "Good to meet you the other day."),
            ("Google Maps", {"how_we_know": "We met at your Noida office"}, "We met at your Noida office."),  # staff's own line wins
        ]
        for source, info, line in cases:
            self.assertEqual(how_we_know(source, info, "you"), line, source)

    def test_the_draft_opens_with_the_right_line(self) -> None:
        t = WaTemplate.objects.create(name="Opening", kind="interested", text="Hi {{FirstName}}, Vishal from Zinwe here. {{HowWeKnow}}\n\nAbout {{Business}}?")
        c = self.contact(first_name="Excel", firm="Excel Range Media - SEO Services in Delhi", source="Google Maps",
                         info={"rating": "4.9", "wa_business_name": "Excel Range Media"})
        text = whatsapp.fill(t.text, c)
        self.assertEqual(text, "Hello Excel Range Media team, Vishal from Zinwe here. I found you on Google Maps, where you're rated 4.9.\n\nAbout Excel Range Media?")
        c.source = ""
        self.assertEqual(whatsapp.fill(t.text, c).split("\n")[0], "Hello Excel Range Media team, Vishal from Zinwe here.")

    def test_first_templates_got_how_we_know(self) -> None:
        firsts = WaTemplate.objects.filter(kind="first")
        hello = [t for t in firsts if "Vishal from Zinwe" in t.text]
        self.assertTrue(hello)
        self.assertTrue(all("{{HowWeKnow}}" in t.text for t in hello))

    def test_the_api_shows_how_they_are_greeted(self) -> None:
        c = self.contact(first_name="IT", firm="It Solutions & Services (itss)", source="Google Maps")
        data = self.api.get(f"{BASE}/contacts/{c.pk}/").json()
        self.assertEqual((data["greet_as"], data["short_name"]), ("It Solutions & Services team", "It Solutions & Services"))


class AddOneTests(WaBase):
    URL = f"{BASE}/contacts/"
    GOOD = {"firm": "Pixel Studio", "phone": "98100 00777", "segment": "Agencies", "country": "India", "city": "Noida"}

    def test_add_one_opted_in_with_campaign(self) -> None:
        r = self.api.post(self.URL, {**self.GOOD, "first_name": "Riya", "opted_in": True, "opt_in_note": "Replied to our email",
                                     "campaign": self.campaign.id, "notes": "New office in Sector 62"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        c = WaContact.objects.get(phone="+919810000777")
        self.assertEqual((c.firm, c.first_name, c.campaign, c.opted_in, c.opt_in_note, c.added_by),
                         ("Pixel Studio", "Riya", self.campaign, True, "Replied to our email", "staff@zinwe.io"))
        self.assertEqual(c.info, {"notes": "New office in Sector 62"})
        self.assertTrue(c.source.startswith("Added by hand"))

    def test_not_opted_in_is_added_locked(self) -> None:
        r = self.api.post(self.URL, self.GOOD, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual((body["opted_in"], body["opt_in_note"]), (False, ""))

    def test_field_errors(self) -> None:
        r = self.api.post(self.URL, {"phone": "123", "country": "India"}, format="json")
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(set(body) - {"detail"}, {"firm", "segment", "city", "phone"})
        self.assertIn("Not a phone number", body["phone"])
        r = self.api.post(self.URL, {**self.GOOD, "country": "UK"}, format="json")
        self.assertEqual(r.json()["country"], "Pick India or USA.")

    def test_duplicates_and_blocked_numbers(self) -> None:
        self.contact("+919810000777")
        r = self.api.post(self.URL, self.GOOD, format="json")
        self.assertEqual(r.json()["phone"], "Already a contact: Gupta Exim.")
        WaBlock.objects.create(phone="+919810000888", reason="Asked to stop")
        r = self.api.post(self.URL, {**self.GOOD, "phone": "+91 98100 00888"}, format="json")
        self.assertEqual(r.json()["phone"], "This number asked to stop. It can't be added.")
        self.assertEqual(WaContact.objects.count(), 1)


class StructureTests(WaBase):
    PARTS = {
        "creative": "After they reply: a clip of the alert", "intro": "Vishal from Zinwe here. {{HowWeKnow}}",
        "problem": "Client files often go to a pen drive.", "solution": "Zinwe alerts you and can set USB to read-only.",
        "reward": "Cherry on top: manage team activity, chat and share tasks without switching apps. Free forever.",
        "cta": "Useful for {{Business}}? Reply STOP to opt out.", "notes": "Value point V2. If YES, send the YES reply.",
    }

    def test_structured_template_is_composed_in_order(self) -> None:
        r = self.api.post(f"{BASE}/templates/", {"name": "AG test", "kind": "first", "parts": self.PARTS}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["text"], (
            "{{Greeting}}, Vishal from Zinwe here. {{HowWeKnow}}\n\nClient files often go to a pen drive.\n\n"
            "Zinwe alerts you and can set USB to read-only.\n\nCherry on top: manage team activity, chat and share tasks "
            "without switching apps. Free forever.\n\nUseful for {{Business}}? Reply STOP to opt out."))
        self.assertNotIn("clip", r.json()["text"])  # creative and notes are never sent

    def test_missing_parts_and_two_problems_are_refused(self) -> None:
        r = self.api.post(f"{BASE}/templates/", {"name": "x", "kind": "first", "parts": {"intro": "Hi", "problem": "a\nb"}}, format="json")
        self.assertEqual(r.status_code, 400)
        errors = {k: v[0] for k, v in r.json().items()}
        self.assertEqual(errors["problem"], "One line only: one problem, one solution.")
        self.assertIn("solution", errors)
        self.assertEqual(self.api.post(f"{BASE}/templates/", {"name": "y", "kind": "first", "text": " "}, format="json").status_code, 400)

    def test_greeting_hello_business_team_without_a_real_name(self) -> None:
        t = WaTemplate.objects.create(name="s", kind="first", parts=self.PARTS, text="{{Greeting}}, hi\n\nx")
        self.campaign.first = t
        self.campaign.save()
        nameless = self.contact(first_name="", firm="Webgross - Digital Marketing Agency in Delhi")
        self.assertTrue(whatsapp.draft(nameless, "first")["text"].startswith("Hello Webgross team, hi"))
        named = self.contact("+919810000102", first_name="Rohit", firm="Pixel Studio")
        body = whatsapp.draft(named, "first")
        self.assertTrue(body["text"].startswith("Hi Rohit, hi"))
        self.assertEqual((body["creative"], body["notes"]), (self.PARTS["creative"], self.PARTS["notes"]))
        # Older free-text templates that open "Hi {{FirstName}}" get the same greeting.
        self.assertTrue(whatsapp.fill("Hi {{FirstName}}, there", nameless).startswith("Hello Webgross team, there"))

    def test_the_90_day_first_messages_are_structured(self) -> None:
        t = WaTemplate.objects.get(name__startswith="IT-1 ")
        self.assertEqual(set(t.parts), {"creative", "intro", "problem", "solution", "reward", "cta", "notes"})
        self.assertTrue(t.text.startswith("{{Greeting}}, Vishal from Zinwe here."))
        self.assertTrue(t.parts["reward"].startswith("Cherry on top"))


class OneOffTests(WaBase):
    def test_draft_from_any_template_and_send_one_off(self) -> None:
        c = self.contact(first_name="")
        t = WaTemplate.objects.get(name__startswith="AG-1 ")
        r = self.api.post(f"{BASE}/contacts/{c.id}/draft/", {"kind": "first", "template": t.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["text"].startswith("Hello Gupta Exim team,"))
        self.assertEqual(r.json()["template"], t.name)
        r = self.api.post(f"{BASE}/contacts/{c.id}/sent/", {"kind": "custom", "text": "Hello, a quick one"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        c.refresh_from_db()
        self.assertEqual((c.step, c.status), (0, Status.READY))  # the sequence didn't move
        self.assertEqual(list(c.messages.values_list("kind", flat=True)), ["custom"])
        self.assertEqual(whatsapp._sent_today(), 1)  # counts against the daily limit

    def test_one_off_is_refused_for_locked_or_stopped_numbers(self) -> None:
        locked = self.contact(opted_in=False)
        r = self.api.post(f"{BASE}/contacts/{locked.id}/sent/", {"kind": "custom", "text": "Hi"}, format="json")
        self.assertEqual(r.status_code, 400)
        missing = self.api.post(f"{BASE}/contacts/{locked.id}/draft/", {"kind": "first", "template": 99999}, format="json")
        self.assertEqual(missing.status_code, 400)
