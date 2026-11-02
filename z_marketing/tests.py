from __future__ import annotations

import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.mail import get_connection
from django.test import TestCase, override_settings
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.test import APIClient

from z_blog.models import NewsletterSubscriber

from . import contact_file, email_html, outreach
from .models import Audience, Campaign, Contact, ContactEvent, EmailTemplate, OutreachSend, Persona, Suppression

BOX = dict(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", EMAIL_OUTREACH_HOST="smtp.zoho.in",
    EMAIL_OUTREACH_HOST_USER="hello@zinwe.tech", EMAIL_OUTREACH_HOST_PASSWORD="x", ZINWE_OUTREACH_GAP_SECONDS=0,
)
Status = Contact.Status


class Base(TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("z_notification.channels.email.outreach_connection", side_effect=lambda: get_connection())
        patcher.start()
        self.addCleanup(patcher.stop)
        background = mock.patch("z_marketing.outreach.start_background")
        background.start()
        self.addCleanup(background.stop)
        self.campaign = Campaign.objects.get(name="US CPA launch")
        self.staff = get_user_model().objects.create_user(email="staff@zinwe.io", password="x", is_staff=True)
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.staff)

    def contact(self, email: str = "dana@mesaridge.example", **extra) -> Contact:  # type: ignore[no-untyped-def]
        fields = dict(first_name="Dana", firm="Mesa Ridge CPAs", city="Dallas", next_date=timezone.localdate())
        fields.update(extra)
        return Contact.objects.create(campaign=self.campaign, email=email, **fields)

    def ready(self) -> None:
        """Every launch check passed by hand."""
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.Status.ACTIVE, rules_checked_at=timezone.now(), test_sent_at=timezone.now(), emails_read_at=timezone.now(),
            dns_result={"spf": True, "dkim": True, "dmarc": True},
        )
        self.campaign.refresh_from_db()

    def send_batch(self) -> int:
        approved = outreach.approve(self.campaign, len(outreach.due(self.campaign)), "staff@zinwe.io")
        outreach.send_pending()
        return approved


class SeedTests(TestCase):
    def test_one_audience_one_draft_campaign_twelve_emails_no_contacts(self) -> None:
        self.assertEqual(Audience.objects.count(), 1)
        campaign = Campaign.objects.get()
        self.assertEqual(campaign.audience.persona.confidence, "Guess")
        self.assertEqual(campaign.audience.primary_problem, "Client files leaving with staff")
        self.assertEqual(campaign.audience.state, "Texas")
        self.assertIn("Is this spying on my staff?", campaign.audience.notes)
        self.assertEqual(campaign.status, Campaign.Status.DRAFT)
        self.assertEqual(campaign.emails.count(), 12)
        self.assertEqual(campaign.contacts.count(), 0)


class ImportTests(Base):
    def test_only_named_new_addresses_get_in(self) -> None:
        self.contact()
        Suppression.objects.create(email="ben@stonebridge.example", reason="Unsubscribed")
        NewsletterSubscriber.objects.create(email="left@letter.example", status=NewsletterSubscriber.Status.UNSUBSCRIBED)
        text = (
            "Ella, Prairie Wind CPAs, ella@prairiewind.example, Dallas\n"
            "Jon, Mesa Ridge CPAs, jon@mesaridge.example, Dallas\n"
            "Ben, Stonebridge CPAs, ben@stonebridge.example, Dallas\n"
            "Lee, Letter LLC, left@letter.example, Austin\n"
            "Ira, North Fork Tax, info@northfork.example, Houston\n"
            "broken line\n"
        )
        results = [r["message"] for r in outreach.import_contacts(self.campaign, text, "staff@zinwe.io")]
        self.assertEqual(results, [
            "Added", "Firm already in the list, skipped", "Blocked, skipped", "Blocked, skipped",
            "Shared inbox, find a named person", "Check the format",
        ])
        self.assertEqual(self.campaign.contacts.count(), 2)


class FlowTests(Base):
    def test_two_cold_emails_then_it_stops(self) -> None:
        c = self.contact()
        outreach.after_send(c, 1, {s.n: s for s in self.campaign.emails.all()})
        self.assertEqual((c.status, c.next_email, c.next_date), (Status.COLD, 2, timezone.localdate() + timedelta(days=4)))
        outreach.after_send(c, 2, {s.n: s for s in self.campaign.emails.all()})
        self.assertEqual((c.status, c.next_email, c.next_date), (Status.WAITING, 0, None))

    def test_yes_opens_the_setup_emails_and_keeps_the_proof(self) -> None:
        c = self.contact(status=Status.WAITING, last_email=2, next_email=0, next_date=None)
        outreach.act(c, "yes", "staff@zinwe.io", 'Reply: "yes please"')
        self.assertEqual((c.status, c.next_email), (Status.OPTED, 3))
        self.assertEqual(c.proof, 'Reply: "yes please"')

    def test_installed_skips_setup_emails(self) -> None:
        c = self.contact(status=Status.OPTED, last_email=2, next_email=3)
        outreach.act(c, "installed", "staff@zinwe.io")
        self.assertEqual(c.next_email, 5)

    def test_question_pauses_and_resume_restores(self) -> None:
        c = self.contact(status=Status.OPTED, last_email=5, next_email=6)
        outreach.act(c, "question", "staff@zinwe.io")
        self.assertEqual((c.status, c.next_email), (Status.PAUSED, 0))
        outreach.act(c, "resume", "staff@zinwe.io")
        self.assertEqual((c.status, c.next_email), (Status.OPTED, 6))

    def test_signed_up_without_a_yes_ends_after_email_5(self) -> None:
        c = self.contact(status=Status.SIGNED, last_email=4, next_email=5)
        outreach.after_send(c, 5, {s.n: s for s in self.campaign.emails.all()})
        self.assertEqual(c.status, Status.CLOSED)

    def test_no_blocks_for_good_in_every_campaign(self) -> None:
        c = self.contact()
        other = Campaign.objects.create(name="Second", audience=self.campaign.audience)
        twin = Contact.objects.create(campaign=other, first_name="D", firm="M", email=c.email, next_date=timezone.localdate())
        outreach.act(c, "no", "staff@zinwe.io")
        twin.refresh_from_db()
        self.assertEqual(twin.status, Status.SUPPRESSED)
        self.assertTrue(Suppression.objects.filter(email=c.email).exists())
        with self.assertRaises(outreach.OutreachError):
            outreach.act(c, "yes", "staff@zinwe.io")

    def test_a_spam_complaint_pauses_the_campaign(self) -> None:
        self.ready()
        c = self.contact()
        outreach.act(c, "complaint", "staff@zinwe.io")
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.PAUSED)
        with self.assertRaisesMessage(outreach.OutreachError, "marked your email as spam"):
            outreach.approve(self.campaign, 0, "staff@zinwe.io")


@override_settings(**BOX)
class SendTests(Base):
    def test_nothing_sends_without_launch_or_a_mailbox(self) -> None:
        self.contact()
        with self.assertRaises(outreach.OutreachError):
            outreach.approve(self.campaign, 1, "staff@zinwe.io")
        self.ready()
        with override_settings(EMAIL_OUTREACH_HOST=""), self.assertRaises(outreach.OutreachError):
            outreach.approve(self.campaign, 1, "staff@zinwe.io")
        self.assertEqual(OutreachSend.objects.count(), 0)

    def test_job_sends_only_what_a_person_approved(self) -> None:
        self.ready()
        self.contact()
        outreach.send_pending()
        self.assertEqual(mail.outbox, [])

    def test_email_is_personal_comes_from_outreach_and_can_be_unsubscribed(self) -> None:
        self.ready()
        c = self.contact()
        self.assertEqual(self.send_batch(), 1)
        m = mail.outbox[0]
        self.assertEqual(m.from_email, "Vishal Shakaya <hello@zinwe.tech>")
        self.assertEqual(m.reply_to, ["hello@zinwe.tech"])
        self.assertIn("Mesa Ridge CPAs", m.body)
        self.assertNotIn("{{", m.body + m.subject)
        self.assertIn(f"/api/marketing/unsubscribe/{c.token}/", m.extra_headers["List-Unsubscribe"])
        self.assertEqual(m.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertTrue(m.extra_headers["Message-ID"].endswith("@zinwe.tech>"))
        self.assertIn("SKYTECHNO", m.body)
        self.assertNotIn("<img", m.alternatives[0][0])  # reads as a personal note: no logo (owner, 2026-11-02)
        self.assertNotIn("utm_", m.body)  # the address is visible to the reader
        c.refresh_from_db()
        self.assertEqual((c.status, c.last_email, c.next_email), (Status.COLD, 1, 2))

    def test_daily_limit_holds_across_batches(self) -> None:
        self.ready()
        Campaign.objects.filter(pk=self.campaign.pk).update(daily_limit=2)
        self.campaign.refresh_from_db()
        for i in range(3):
            self.contact(f"p{i}@firm{i}.example")
        outreach.approve(self.campaign, 2, "staff@zinwe.io")
        outreach.send_pending()
        self.assertEqual(len(mail.outbox), 2)
        with self.assertRaisesMessage(outreach.OutreachError, "limit"):
            outreach.approve(self.campaign, 0, "staff@zinwe.io")

    def test_a_list_that_changed_is_refused(self) -> None:
        self.ready()
        self.contact()
        with self.assertRaisesMessage(outreach.OutreachError, "changed"):
            outreach.approve(self.campaign, 5, "staff@zinwe.io")

    def test_unsubscribing_before_its_turn_skips_the_email(self) -> None:
        self.ready()
        c = self.contact()
        outreach.approve(self.campaign, 1, "staff@zinwe.io")
        outreach.unsubscribe(c.token)
        outreach.send_pending()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutreachSend.objects.get().status, OutreachSend.Status.SKIPPED)

    def test_pausing_stops_the_rest_of_the_batch(self) -> None:
        self.ready()
        self.contact()
        outreach.approve(self.campaign, 1, "staff@zinwe.io")
        outreach.pause(self.campaign, "x")
        outreach.send_pending()
        self.assertEqual(mail.outbox, [])

    def test_a_crash_mid_send_is_never_resent_alone_but_can_be_approved_again(self) -> None:
        self.ready()
        c = self.contact()
        OutreachSend.objects.create(
            contact=c, n=1, status=OutreachSend.Status.SENDING, approved_by="s@zinwe.io",
            claimed_at=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(outreach.due(self.campaign), [])
        self.assertEqual(outreach.send_pending().stuck, 1)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.send_batch(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_failed_send_keeps_the_contact_due(self) -> None:
        self.ready()
        c = self.contact()
        outreach.approve(self.campaign, 1, "staff@zinwe.io")
        with mock.patch("z_notification.channels.email.outreach_connection", side_effect=RuntimeError("down")):
            result = outreach.send_pending()
        self.assertEqual(result.failed, 1)
        c.refresh_from_db()
        self.assertEqual((c.status, c.next_email), (Status.NEW, 1))
        self.assertEqual(outreach.due(self.campaign), [c])

    def test_signed_only_line_goes_only_to_signed_up_contacts(self) -> None:
        step = self.campaign.emails.get(n=5)
        signed = outreach._body(step, self.contact(status=Status.SIGNED))
        opted = outreach._body(step, self.contact("b@b.example", status=Status.OPTED))
        self.assertIn('reply "yes"', signed)
        self.assertNotIn("[", signed)
        self.assertNotIn("remaining tips", opted)
        self.assertNotIn("[", opted)

    def test_test_email_marks_the_check_and_touches_no_contact(self) -> None:
        outreach.send_test(self.campaign, 1, "staff@zinwe.io")
        self.assertTrue(mail.outbox[0].subject.startswith("[Test] "))
        self.campaign.refresh_from_db()
        self.assertIsNotNone(self.campaign.test_sent_at)
        self.assertEqual(Contact.objects.count(), 0)


@override_settings(**BOX)
class LaunchTests(Base):
    def test_launch_needs_every_check(self) -> None:
        with self.assertRaisesMessage(outreach.OutreachError, "Finish these first"):
            outreach.launch(self.campaign, "staff@zinwe.io")
        self.campaign.rules_checked_at = self.campaign.test_sent_at = self.campaign.emails_read_at = timezone.now()
        self.campaign.dns_result = {"spf": True, "dkim": True, "dmarc": True}
        self.campaign.save()
        self.contact()
        self.assertEqual(outreach.launch(self.campaign, "staff@zinwe.io").status, Campaign.Status.ACTIVE)

    def test_a_zinwe_io_sender_is_refused(self) -> None:
        with override_settings(ZINWE_OUTREACH_FROM="V <v@mail.zinwe.io>"):
            self.assertFalse(outreach.separate_domain())

    def test_dns_check_reads_public_records(self) -> None:
        records = {
            "zinwe.tech": ["v=spf1 include:zoho.in ~all"], "zmail._domainkey.zinwe.tech": ["v=DKIM1; k=rsa; p=x"],
            "_dmarc.zinwe.tech": ["v=DMARC1; p=none"],
        }
        with mock.patch("z_marketing.outreach._txt", side_effect=lambda n: records[n]):
            self.assertEqual(outreach.check_dns(self.campaign), {"spf": True, "dkim": True, "dmarc": True})
        records["zinwe.tech"] = ["v=spf1 include:_spf.reach.hostinger.com ~all"]
        with mock.patch("z_marketing.outreach._txt", side_effect=lambda n: records[n]):
            self.assertFalse(outreach.check_dns(self.campaign)["spf"])
        records["_dmarc.zinwe.tech"] = ["v=DMARC1; p=none", "v=DMARC1; p=none; rua=mailto:x@y.z"]
        with mock.patch("z_marketing.outreach._txt", side_effect=lambda n: records[n]):
            self.assertFalse(outreach.check_dns(self.campaign)["dmarc"])


class ApiTests(Base):
    def test_staff_only(self) -> None:
        self.assertEqual(APIClient().get("/api/admin/marketing/campaigns/").status_code, 401)

    def _audience(self, **extra: object) -> dict[str, object]:
        return {
            "persona": Persona.objects.first().pk, "name": "X", "country": "India", "business_category": "Clinics",
            "business_segment": "Single-doctor clinics", "business_type": "B2B", "primary_problem": "Scam calls",
            "product_service": "Zinwe", "how_solves": "Alerts", "key_benefits": ["Calm"], "primary_offer": "Free plan",
            "objective": "Leads", "cta": "Start free", "channels": ["Email"], **extra,
        }

    def test_personas_are_staff_only(self) -> None:
        self.assertEqual(APIClient().get("/api/admin/marketing/personas/").status_code, 401)

    def test_persona_create_edit_and_shape(self) -> None:
        r = self.client_api.post("/api/admin/marketing/personas/", {"name": "Office manager", "profile": {"business": {"brand": "Z"}}}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["confidence"], "Guess")
        self.assertEqual(self.client_api.post("/api/admin/marketing/personas/", {"name": "Blank", "profile": {}}, format="json").status_code, 201)
        pk = r.json()["id"]
        r = self.client_api.put(
            f"/api/admin/marketing/personas/{pk}/",
            {"name": "Office manager", "confidence": "Heard once", "profile": {"pains": [{"pain": "A", "solution": "B", "benefit": "C"}]}}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        for bad in ({"nope": {}}, {"business": "text"}, {"pains": {"a": "b"}}, {"business": {"brand": "x" * 2001}}, {"pains": [{"pain": 1}]}):
            r = self.client_api.put(f"/api/admin/marketing/personas/{pk}/", {"name": "O", "profile": bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)

    def test_persona_with_audiences_stays(self) -> None:
        persona = Persona.objects.get()
        self.assertEqual(self.client_api.delete(f"/api/admin/marketing/personas/{persona.pk}/").status_code, 400)
        spare = Persona.objects.create(name="Spare")
        self.assertEqual(self.client_api.delete(f"/api/admin/marketing/personas/{spare.pk}/").status_code, 204)

    def test_audience_needs_the_form_fields(self) -> None:
        url = "/api/admin/marketing/audiences/"
        r = self.client_api.post(url, self._audience(channels=[]), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("channels", r.json())
        r = self.client_api.post(url, self._audience(key_benefits=[]), format="json")
        self.assertIn("key_benefits", r.json())
        body = self._audience()
        del body["primary_problem"], body["persona"]
        r = self.client_api.post(url, body, format="json")
        self.assertEqual({"primary_problem", "persona"}, set(r.json()))
        r = self.client_api.post(url, self._audience(), format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["state"], "")  # blank: the whole country

    def test_audience_filter_edit_and_delete(self) -> None:
        url = "/api/admin/marketing/audiences/"
        pk = self.client_api.post(url, self._audience(), format="json").json()["id"]
        other = Persona.objects.create(name="Other")
        self.assertEqual(len(self.client_api.get(f"{url}?persona={other.pk}").json()), 0)
        self.assertEqual(len(self.client_api.get(f"{url}?persona={other.pk + 1000}").json()), 0)
        self.assertEqual(len(self.client_api.get(url).json()), 2)
        r = self.client_api.put(f"{url}{pk}/", self._audience(city="Pune"), format="json")
        self.assertEqual(r.json()["city"], "Pune")
        self.assertEqual(self.client_api.delete(f"{url}{pk}/").status_code, 204)
        used = self.campaign.audience_id
        self.assertEqual(self.client_api.get(f"{url}{used}/").json()["campaign_count"], 1)
        self.assertEqual(self.client_api.delete(f"{url}{used}/").status_code, 400)

    def test_campaign_detail_and_edit_email(self) -> None:
        pk = self.campaign.pk
        data = self.client_api.get(f"/api/admin/marketing/campaigns/{pk}/").json()
        self.assertEqual(len(data["emails"]), 12)
        self.assertEqual(data["status"], "draft")
        self.client_api.put(f"/api/admin/marketing/campaigns/{pk}/emails/1/", {"subject": "New"}, format="json")
        self.assertEqual(self.campaign.emails.get(n=1).subject, "New")

    def test_import_and_act_over_http(self) -> None:
        pk = self.campaign.pk
        r = self.client_api.post(f"/api/admin/marketing/campaigns/{pk}/contacts/import/", {"text": "Ella, Prairie CPAs, ella@prairie.example, Dallas"}, format="json")
        self.assertEqual(r.json()["results"][0]["result"], "good")
        c = Contact.objects.get()
        r = self.client_api.post(f"/api/admin/marketing/contacts/{c.pk}/act/", {"action": "question"}, format="json")
        self.assertEqual(r.json()["status"], "paused")
        self.assertEqual(self.client_api.get(f"/api/admin/marketing/campaigns/{pk}/contacts/").json()[0]["stage"], "On hold")

    def test_launch_refusal_reaches_staff(self) -> None:
        r = self.client_api.post(f"/api/admin/marketing/campaigns/{self.campaign.pk}/launch/", {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Finish these first", r.json()["detail"])


@override_settings(ZINWE_SHEET_IMPORT_TOKEN="secret")
class SheetImportTests(Base):
    URL = "/api/marketing/sheet-import/"

    def post(self, rows, token="secret"):  # type: ignore[no-untyped-def]
        return APIClient().post(self.URL, {"rows": rows}, format="json", HTTP_X_IMPORT_TOKEN=token)

    def row(self, n, **extra):  # type: ignore[no-untyped-def]
        return {"row": n, "first_name": "Ella", "firm": "Prairie CPAs", "email": "ella@prairie.example", "city": "Dallas", **extra}

    def test_a_wrong_token_or_no_token_setting_finds_nothing(self) -> None:
        self.assertEqual(self.post([self.row(2)], token="nope").status_code, 404)
        with override_settings(ZINWE_SHEET_IMPORT_TOKEN=""):
            self.assertEqual(self.post([self.row(2)], token="").status_code, 404)
        self.assertEqual(Contact.objects.count(), 0)

    def test_each_row_gets_a_status_and_remark(self) -> None:
        Suppression.objects.create(email="no@firm.example", reason="Unsubscribed")
        data = self.post([
            self.row(2),
            self.row(3),  # the same person again
            self.row(4, email="jon@prairie.example"),
            self.row(5, email="info@other.example"),
            self.row(6, email="sam@gmail.com"),
            self.row(7, email="no@firm.example"),
            self.row(8, firm=""),
        ]).json()["rows"]
        self.assertEqual([(r["row"], r["status"], r["remark"]) for r in data], [
            (2, "Added", "Added"), (3, "Added", "Already in Zinwe"), (4, "Skip", "Firm already in the list, skipped"),
            (5, "Skip", "Shared inbox, find a named person"), (6, "Skip", "Free email, use a work address"),
            (7, "Skip", "Blocked, skipped"), (8, "Need info", "Check the format"),
        ])
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(Contact.objects.get().events.get().text, "Added from the team's sheet")

    def test_it_only_adds_contacts(self) -> None:
        self.post([self.row(2)])
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutreachSend.objects.count(), 0)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.DRAFT)


class UnsubscribeTests(Base):
    def test_opening_the_link_changes_nothing_but_the_button_does(self) -> None:
        c = self.contact()
        url = f"/api/marketing/unsubscribe/{c.token}/"
        self.assertContains(self.client.get(url), "Unsubscribe")
        c.refresh_from_db()
        self.assertNotEqual(c.status, Status.SUPPRESSED)
        self.assertEqual(self.client.post(url).status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.status, Status.SUPPRESSED)
        self.assertTrue(Suppression.objects.filter(email=c.email, reason="Unsubscribed").exists())
        self.assertEqual(self.client.post(url).status_code, 200)  # the mail client may retry
        self.assertEqual(ContactEvent.objects.filter(contact=c, text="Unsubscribed").count(), 1)

    def test_unknown_token_gives_nothing_away(self) -> None:
        self.assertEqual(self.client.post("/api/marketing/unsubscribe/nope/").status_code, 200)


@override_settings(**BOX)
class PatternTests(Base):
    def make(self, **body: object) -> dict:
        data = {"name": "Clinics", "audience": self.campaign.audience_id, **body}
        return self.client_api.post("/api/admin/marketing/campaigns/", data, format="json").json()

    def test_a_campaign_needs_a_pattern(self) -> None:
        self.assertIn("pattern", self.make()["detail"].lower())
        self.assertEqual(self.make(pattern="nope").get("detail"), "Pick an email pattern.")

    def test_each_pattern_gives_its_own_copy(self) -> None:
        self.assertEqual(len(self.make(pattern="cold")["emails"]), 2)
        self.assertEqual(len(self.make(pattern="setup")["emails"]), 5)
        made = self.make(pattern="full")
        self.assertEqual(len(made["emails"]), 12)
        self.client_api.put(f"/api/admin/marketing/campaigns/{made['id']}/emails/1/", {"subject": "Mine"}, format="json")
        self.assertNotEqual(self.campaign.emails.get(n=1).subject, "Mine")

    def test_copying_a_campaign_keeps_its_edits(self) -> None:
        self.campaign.emails.filter(n=1).update(subject="Edited")
        made = self.make(copy_from=self.campaign.pk)
        self.assertEqual(made["emails"][0]["subject"], "Edited")
        self.assertEqual(made["pattern"], "full")

    def test_a_yes_ends_the_two_email_pattern(self) -> None:
        self.campaign.emails.filter(n__gt=2).delete()
        c = self.contact()
        outreach.act(c, "yes", "staff@zinwe.io")
        c.refresh_from_db()
        self.assertEqual((c.status, c.next_email, c.next_date), (Contact.Status.OPTED, 0, None))
        self.assertEqual(outreach.due(self.campaign), [])

    def test_the_last_email_finishes_any_length(self) -> None:
        self.campaign.emails.filter(n__gt=3).delete()
        self.ready()
        c = self.contact(status=Contact.Status.OPTED, next_email=3)
        self.send_batch()
        c.refresh_from_db()
        self.assertEqual(c.status, Contact.Status.FINISHED)

    def test_add_and_remove_until_sending_starts(self) -> None:
        url = f"/api/admin/marketing/campaigns/{self.campaign.pk}/emails/"
        self.assertEqual(len(self.client_api.post(url).json()["emails"]), 13)
        self.assertEqual(self.client_api.delete(f"{url}1/").status_code, 400)
        data = self.client_api.delete(f"{url}3/").json()
        self.assertEqual([e["n"] for e in data["emails"]], list(range(1, 13)))
        self.ready()
        self.contact()
        self.send_batch()
        self.assertEqual(self.client_api.post(url).status_code, 400)
        self.assertEqual(self.client_api.delete(f"{url}5/").status_code, 400)

    def test_only_an_unsent_draft_can_be_removed(self) -> None:
        url = f"/api/admin/marketing/campaigns/{self.campaign.pk}/"
        self.ready()
        self.assertEqual(self.client_api.delete(url).status_code, 400)
        Campaign.objects.filter(pk=self.campaign.pk).update(status=Campaign.Status.DRAFT)
        self.contact()
        self.assertEqual(self.client_api.delete(url).status_code, 204)
        self.assertFalse(Contact.objects.exists())

    def test_reading_the_emails_is_a_launch_check(self) -> None:
        url = f"/api/admin/marketing/campaigns/{self.campaign.pk}/"
        read = lambda: next(c for c in self.client_api.get(url).json()["checks"] if c["key"] == "emails")["done"]  # noqa: E731
        self.assertFalse(read())
        self.client_api.post(f"{url}emails-read/", {"checked": True}, format="json")
        self.assertTrue(read())

    def test_analytics_count_replies_by_email(self) -> None:
        self.ready()
        c = self.contact()
        self.send_batch()
        c.refresh_from_db()
        outreach.act(c, "question", "staff@zinwe.io")
        report = outreach.report(self.campaign)
        self.assertEqual(report["by_email"][0]["replies"], 1)
        self.assertEqual(report["by_email"][0]["sent"], 1)
        self.assertEqual(report["reply_rate"], 100)


@override_settings(**BOX)
class ContactFileTests(Base):
    def upload(self, name: str, data: bytes, add: bool = False) -> object:
        from django.core.files.uploadedfile import SimpleUploadedFile

        body = {"file": SimpleUploadedFile(name, data)}
        if add:
            body["add"] = "1"
        return self.client_api.post(f"/api/admin/marketing/campaigns/{self.campaign.pk}/contacts/upload/", body, format="multipart")

    def xlsx(self, rows: list[list[object]]) -> bytes:
        book = Workbook()
        for row in rows:
            book.active.append(row)
        out = io.BytesIO()
        book.save(out)
        return out.getvalue()

    def test_review_adds_nothing_and_add_matches_the_review(self) -> None:
        Suppression.objects.create(email="gone@blocked.example", reason="Unsubscribed")
        data = self.xlsx([
            ["First Name", "Company", "E-mail", "City"],
            ["Dana", "Mesa Ridge", "dana@mesaridge.example", "Dallas"],
            ["Sam", "Mesa Ridge", "sam@mesaridge.example", "Dallas"],
            ["Info", "Acme", "info@acme.example", ""],
            ["Lee", "Lee Co", "lee@gmail.com", ""],
            ["Gone", "Blocked", "gone@blocked.example", ""],
            ["", "No Name", "x@noname.example", ""],
            [None, None, None, None],
            ["Ella", "Prairie", "ELLA@prairie.example", None],
        ])
        rows = self.upload("list.xlsx", data).json()["rows"]
        self.assertEqual([r["result"] for r in rows], ["ready", "warn", "warn", "warn", "bad", "bad", "ready"])
        self.assertEqual(rows[1]["message"], "Same firm as an earlier row, skipped")
        self.assertEqual(rows[-1]["row"], 9)
        self.assertEqual(self.campaign.contacts.count(), 0)
        self.assertEqual(self.upload("list.xlsx", data, add=True).json()["added"], 2)
        self.assertEqual(sorted(self.campaign.contacts.values_list("email", flat=True)), ["dana@mesaridge.example", "ella@prairie.example"])
        rows = self.upload("list.xlsx", data).json()["rows"]
        self.assertEqual(rows[0]["message"], "Already in Zinwe")

    def test_a_contact_from_a_file_sends_nothing(self) -> None:
        self.ready()
        self.upload("a.csv", b"First name,Firm,Email,City\nDana,Mesa,dana@mesaridge.example,Dallas\n", add=True)
        self.assertEqual(self.campaign.contacts.get().sends.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_bad_files_say_why(self) -> None:
        cases = [
            ("a.xls", b"old", "xlsx"),
            ("a.xlsx", b"not a zip", "can't be opened"),
            ("a.csv", b"", "empty"),
            ("a.csv", b"Name,Town\nDana,Dallas\n", "Email"),
            ("a.csv", b"First name,Firm,Email\n", "no contacts"),
            ("a.csv", b"x" * (contact_file.MAX_BYTES + 1), "2 MB"),
        ]
        for name, data, word in cases:
            r = self.upload(name, data)
            self.assertEqual(r.status_code, 400, name)
            self.assertIn(word, r.json()["detail"], name)

    def test_too_many_rows(self) -> None:
        data = "First name,Firm,Email\n" + "A,B,c@d.example\n" * (contact_file.MAX_ROWS + 1)
        self.assertIn("rows", self.upload("a.csv", data.encode()).json()["detail"])

    def test_the_template_reads_back(self) -> None:
        r = self.client_api.get("/api/admin/marketing/contact-template/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.upload("t.xlsx", r.content).json()["detail"], "This file has headers but no contacts.")

    def test_upload_is_staff_only(self) -> None:
        url = f"/api/admin/marketing/campaigns/{self.campaign.pk}/contacts/upload/"
        self.assertEqual(APIClient().post(url).status_code, 401)
        self.assertEqual(APIClient().get("/api/admin/marketing/contact-template/").status_code, 401)


@override_settings(**BOX)
class TemplateTests(Base):
    base = "/api/admin/marketing"

    def own(self, kind: str = "Tips", name: str = "Mine") -> dict:
        body = {"name": name, "kind": kind, "subject": "Hello", "body": "Hi {{FirstName}}"}
        return self.client_api.post(f"{self.base}/templates/", body, format="json").json()

    def draft(self, pattern: str = "cold") -> dict:
        body = {"name": "D", "audience": self.campaign.audience_id, "pattern": pattern}
        return self.client_api.post(f"{self.base}/campaigns/", body, format="json").json()

    def use(self, campaign: int, n: int, template: int) -> object:
        return self.client_api.post(f"{self.base}/campaigns/{campaign}/emails/{n}/use-template/", {"template": template}, format="json")

    def test_staff_only(self) -> None:
        self.assertEqual(APIClient().get(f"{self.base}/templates/").status_code, 401)

    def test_the_twelve_emails_are_starter_templates(self) -> None:
        rows = self.client_api.get(f"{self.base}/templates/").json()
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(r["built_in"] for r in rows))
        self.assertEqual([r["kind"] for r in rows[:3]], ["Cold", "Cold", "Setup"])

    def test_a_pattern_uses_the_templates_as_they_read_now(self) -> None:
        EmailTemplate.objects.filter(step=1).update(subject="Better subject")
        made = self.draft()
        self.assertEqual(made["emails"][0]["subject"], "Better subject")
        self.assertEqual(made["emails"][0]["template_name"], "First cold email: client files")
        self.assertEqual(made["emails"][1]["when"], "Day 4–5")

    def test_template_and_campaign_edits_stay_on_their_own_side(self) -> None:
        made = self.draft()
        template = EmailTemplate.objects.get(step=1)
        before = template.subject
        self.client_api.put(f"{self.base}/campaigns/{made['id']}/emails/1/", {"subject": "Campaign only"}, format="json")
        template.refresh_from_db()
        self.assertEqual(template.subject, before)
        self.client_api.put(f"{self.base}/templates/{template.pk}/", {"name": "T", "kind": "Cold", "subject": "Template only", "body": "B"}, format="json")
        got = self.client_api.get(f"{self.base}/campaigns/{made['id']}/").json()
        self.assertEqual(got["emails"][0]["subject"], "Campaign only")

    def test_use_a_template_then_edit_the_copy(self) -> None:
        made = self.draft()
        mine = self.own("Cold")
        Campaign.objects.filter(pk=made["id"]).update(emails_read_at=timezone.now())
        data = self.use(made["id"], 1, mine["id"]).json()
        self.assertEqual(data["emails"][0]["subject"], "Hello")
        self.assertEqual(data["emails"][0]["template_name"], "Mine")
        self.assertIsNone(data["emails_read_at"])
        self.client_api.put(f"{self.base}/campaigns/{made['id']}/emails/1/", {"subject": "Mine now"}, format="json")
        self.assertEqual(EmailTemplate.objects.get(pk=mine["id"]).subject, "Hello")

    def test_cold_templates_only_fit_the_two_cold_emails(self) -> None:
        made = self.draft("setup")
        cold, tips = self.own("Cold"), self.own("Tips")
        self.assertEqual(self.use(made["id"], 1, tips["id"]).status_code, 400)
        self.assertEqual(self.use(made["id"], 3, cold["id"]).status_code, 400)
        url = f"{self.base}/campaigns/{made['id']}/emails/"
        self.assertEqual(self.client_api.post(url, {"template": cold["id"]}, format="json").status_code, 400)
        data = self.client_api.post(url, {"template": tips["id"]}, format="json").json()
        self.assertEqual((data["emails"][-1]["subject"], data["emails"][-1]["stage"]), ("Hello", "Tips"))

    def test_no_template_after_sending_starts(self) -> None:
        self.ready()
        self.contact()
        self.send_batch()
        self.assertEqual(self.use(self.campaign.pk, 1, self.own("Cold")["id"]).status_code, 400)

    def test_save_as_template_is_a_copy(self) -> None:
        url = f"{self.base}/campaigns/{self.campaign.pk}/emails/1/save-template/"
        self.assertEqual(self.client_api.post(url, {"name": " "}, format="json").status_code, 400)
        made = self.client_api.post(url, {"name": "Winner"}, format="json").json()
        self.assertEqual((made["kind"], made["built_in"]), ("Cold", False))
        self.campaign.emails.filter(n=1).update(subject="Changed later")
        self.assertNotEqual(EmailTemplate.objects.get(pk=made["id"]).subject, "Changed later")

    def test_starter_templates_stay_and_own_ones_can_go(self) -> None:
        starter = EmailTemplate.objects.get(step=1)
        self.assertEqual(self.client_api.delete(f"{self.base}/templates/{starter.pk}/").status_code, 400)
        r = self.client_api.put(f"{self.base}/templates/{starter.pk}/", {"name": "T", "kind": "Tips", "subject": "S", "body": "B"}, format="json")
        self.assertIn("kind", r.json())
        made = self.draft()
        mine = self.own("Cold")
        self.use(made["id"], 1, mine["id"])
        self.assertEqual(self.client_api.delete(f"{self.base}/templates/{mine['id']}/").status_code, 204)
        got = self.client_api.get(f"{self.base}/campaigns/{made['id']}/").json()
        self.assertEqual((got["emails"][0]["subject"], got["emails"][0]["template_name"]), ("Hello", ""))


@override_settings(**BOX)
class RichEmailTests(Base):
    def send_one(self, body: str, **extra: object) -> object:
        self.ready()
        self.campaign.emails.filter(n=1).update(body=body)
        self.contact(**extra)
        self.send_batch()
        return mail.outbox[0]

    def test_saved_emails_are_html_and_read_the_same(self) -> None:
        first = self.campaign.emails.get(n=1)
        self.assertTrue(first.body.startswith("<p>Hi {{FirstName}},</p>"))
        self.assertEqual(self.campaign.emails.get(n=3).body.count("<br>"), 3)
        self.assertTrue(EmailTemplate.objects.get(step=1).body.startswith("<p>"))
        m = self.send_one(first.body)
        self.assertIn("Hi Dana,\n\nWhen someone leaves a small firm like Mesa Ridge CPAs", m.body)
        html = m.alternatives[0][0]
        self.assertIn('<p style="margin:0 0 14px;">Hi Dana,</p>', html)
        self.assertIn('<a href="https://zinwe.io">https://zinwe.io</a>', html)
        self.assertEqual(html.count("https://zinwe.io</a>"), 1)

    def test_formatting_reaches_both_parts(self) -> None:
        m = self.send_one('<p><strong>Hi</strong> {{FirstName}}</p><ul><li>One</li></ul><p><a href="https://zinwe.io/plans">Plans</a></p>')
        html = m.alternatives[0][0]
        self.assertIn("<strong>Hi</strong> Dana", html)
        self.assertIn('<ul style="', html)
        self.assertIn("- One", m.body)
        self.assertIn("Plans (https://zinwe.io/plans)", m.body)
        self.assertNotIn("<strong>", m.body)

    def test_names_cannot_break_the_html(self) -> None:
        m = self.send_one("<p>Hi {{Firm}}</p>", firm="A&B <script>alert(1)</script>")
        html = m.alternatives[0][0]
        self.assertNotIn("<script>", html)
        self.assertIn("A&amp;B &lt;script&gt;", html)
        self.assertIn("A&B <script>", m.body)  # the plain-text part shows it as it is

    def test_the_signed_up_line_works_in_html(self) -> None:
        body = '<p>Top</p><p>[Signed-up contacts only: If you would like tips, reply "yes".]</p><p>End</p>'
        self.campaign.emails.filter(n=5).update(body=body)
        step = self.campaign.emails.get(n=5)
        self.assertEqual(outreach._body(step, Contact(status=Status.OPTED)), "<p>Top</p><p>End</p>")
        self.assertIn("If you would like tips", outreach._body(step, Contact(status=Status.SIGNED)))

    def test_saving_cleans_the_html(self) -> None:
        url = f"/api/admin/marketing/campaigns/{self.campaign.pk}/emails/1/"
        dirty = '<p onclick="x()">Hi<script>alert(1)</script></p><p></p><p><br></p><img src="x"><h1>Big</h1><p><a href="javascript:x()">Bad</a></p>'
        body = self.client_api.put(url, {"body": dirty}, format="json").json()["body"]
        for word in ("onclick", "script", "<img", "javascript", "<p></p>"):
            self.assertNotIn(word, body)
        self.assertIn("<p>Hi", body)

    def test_an_empty_email_is_refused(self) -> None:
        url = f"/api/admin/marketing/campaigns/{self.campaign.pk}/emails/1/"
        self.assertEqual(self.client_api.put(url, {"body": "<p><br></p>"}, format="json").status_code, 400)
        r = self.client_api.post("/api/admin/marketing/templates/", {"name": "N", "kind": "Tips", "subject": "S", "body": "<p></p>"}, format="json")
        self.assertIn("body", r.json())

    def test_plain_text_from_the_api_still_works(self) -> None:
        self.assertEqual(email_html.clean_body("One <b>\n\nTwo"), "<p>One &lt;b&gt;</p><p>Two</p>")
