"""The WhatsApp Templates page: conditions in the text, owner name, tags, numbers per template, A/B versions, reach."""
from __future__ import annotations

import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from rest_framework.test import APIClient

from . import wa_conditions, wa_flow, wa_templates, whatsapp
from .models import WaBlock, WaContact, WaMessage, WaTag, WaTemplate
from .tests_wa_flow import BASE, ME, Cat, FlowBase

CS = WaContact.CampaignStatus
GREETING = "{{#if FirstName}}Hi {{FirstName}}{{else}}Hello {{Business}} team{{/if}}"


def contact(phone: str = "+919810000301", **extra: object) -> WaContact:
    values: dict[str, object] = dict(firm="Gupta Exim", first_name="Rakesh", segment="Traders", country="India", city="Delhi",
                                     source="Phone call")
    values.update(extra)
    return WaContact.objects.create(phone=phone, **values)


def sheet(header: list[str], rows: list[list[str]]) -> SimpleUploadedFile:
    book = Workbook()
    book.active.append(header)
    for r in rows:
        book.active.append(r)
    out = io.BytesIO()
    book.save(out)
    return SimpleUploadedFile("contacts.xlsx", out.getvalue())


class Api(TestCase):
    def setUp(self) -> None:
        self.staff = get_user_model().objects.create_user(email=ME, password="x", is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.staff)


class ConditionSyntaxTests(TestCase):
    def test_problems(self) -> None:
        ok = [GREETING, "No conditions at all.", "{{#if City}}In {{City}}.{{/if}} {{#if Firm}}x{{/if}}", "{{ #if OwnerName }}a{{ else }}b{{ /if }}"]
        for text in ok:
            self.assertIsNone(wa_conditions.problem(text), text)
        bad = {
            "{{#if FirstName}}Hi": wa_conditions.NO_END,
            "Hi{{/if}}": wa_conditions.NO_END,
            "{{#if FirstName}}a{{#if City}}b{{/if}}{{/if}}": wa_conditions.NESTED,
            "a{{else}}b": wa_conditions.STRAY_ELSE,
            "{{#if City}}a{{else}}b{{else}}c{{/if}}": wa_conditions.TWO_ELSE,
        }
        for text, message in bad.items():
            self.assertEqual(wa_conditions.problem(text), message, text)
        self.assertIn("Phone isn't a detail", wa_conditions.problem("{{#if Phone}}x{{/if}}"))

    def test_apply_and_outside(self) -> None:
        both = "A {{#if City}}in {{City}}{{else}}somewhere{{/if}} B {{#if OwnerName}}for {{OwnerName}}{{/if}} C"
        self.assertEqual(wa_conditions.apply(both, {"City": True, "OwnerName": False}), "A in {{City}} B  C")
        self.assertEqual(wa_conditions.apply(both, {"City": False, "OwnerName": True}), "A somewhere B for {{OwnerName}} C")
        self.assertEqual(wa_conditions.apply("{{#if FirstName}}x", {}), "{{#if FirstName}}x")  # broken text is left alone
        self.assertEqual(wa_conditions.outside("{{FirstName}}, {{#if City}}{{City}}{{/if}} {{Firm}} {{HowWeKnow}}"), ["FirstName", "Business", "HowWeKnow"])
        self.assertEqual(wa_conditions.used(both), {"City", "OwnerName"})


class FillTests(TestCase):
    def test_english_with_and_without_a_first_name(self) -> None:
        text = GREETING + ", Vishal here. {{#if City}}Many offices in {{City}} use it.{{else}}Many offices use it.{{/if}}"
        self.assertEqual(whatsapp.fill(text, contact()), "Hi Rakesh, Vishal here. Many offices in Delhi use it.")
        nameless = contact("+919810000302", first_name="", city="")
        self.assertEqual(whatsapp.fill(text, nameless), "Hello Gupta Exim team, Vishal here. Many offices use it.")
        # A scrap of the business name isn't a first name: the same rule as the greeting.
        scrap = contact("+919810000303", first_name="Exim")
        self.assertTrue(whatsapp.fill("{{#if FirstName}}named{{else}}team{{/if}}", scrap).startswith("team"))

    def test_hinglish_owner_name_and_the_greeting_rule(self) -> None:
        text = "{{#if City}}Hi {{FirstName}}{{/if}}, {{#if OwnerName}}{{OwnerName}} ji ko bhej dijiye.{{else}}Aap dekh lijiye.{{/if}} Reply STOP to opt out."
        c = contact(owner_name="Suresh Gupta")
        # The condition goes first, so the opening "Hi {{FirstName}}" still becomes the Hinglish greeting.
        self.assertEqual(whatsapp.fill(text, c, "hi"), "Namaste Rakesh ji, Suresh Gupta ji ko bhej dijiye. Reply STOP to opt out.")
        c.owner_name = ""
        self.assertEqual(whatsapp.fill(text, c, "hi"), "Namaste Rakesh ji, Aap dekh lijiye. Reply STOP to opt out.")
        self.assertEqual(whatsapp.fill("Hi {{FirstName}}, ok", contact("+919810000304", first_name="")), "Hello Gupta Exim team, ok")

    def test_business_is_always_known_and_how_we_know(self) -> None:
        self.assertEqual(whatsapp.fill("{{#if Firm}}yes{{else}}no{{/if}} {{#if Business}}yes{{/if}}", contact()), "yes yes")
        uploaded = contact("+919810000305", source="Uploaded 1 Nov 2026")
        text = "{{#if HowWeKnow}}{{HowWeKnow}}{{else}}Hello.{{/if}}"
        self.assertEqual(whatsapp.fill(text, uploaded), "Hello.")
        self.assertEqual(whatsapp.fill(text, contact("+919810000306")), "I found Gupta Exim through Phone call.")


class ValidationTests(Api):
    def test_an_unbalanced_or_nested_if_is_refused(self) -> None:
        r = self.api.post(f"{BASE}/templates/", {"category": "outreach", "type_name": "Opener", "step": 2, "text": "{{#if FirstName}}Hi"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"text": ["An If has no matching end."]}))
        r = self.api.post(f"{BASE}/templates/", {"category": "outreach", "type_name": "Opener", "step": 2, "text": GREETING,
                                                 "text_hi": "{{#if City}}{{#if OwnerName}}x{{/if}}{{/if}}"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"text_hi": [wa_conditions.NESTED]}))
        r = self.api.post(f"{BASE}/templates/", {"category": "outreach", "type_name": "Opener", "step": 2, "text": GREETING}, format="json")
        self.assertEqual(r.status_code, 201, r.json())
        pk = r.json()["id"]
        r = self.api.patch(f"{BASE}/templates/{pk}/", {"text_hi": "{{/if}}"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"text_hi": ["An If has no matching end."]}))
        r = self.api.patch(f"{BASE}/templates/{pk}/", {"text_hi": "Namaste. {{#if City}}{{City}} mein.{{/if}} Reply STOP to opt out."}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["draft"])
        self.assertEqual(self.api.get(f"{BASE}/templates/{pk}/").json()["text"], GREETING)  # stored as written


class OwnerNameTests(Api):
    def test_import_with_and_without_the_column(self) -> None:
        old = ["Business Name", "Phone", "Segment", "Country", "City", "First Name", "Source", "Useful Info", "Opt-in"]
        r = self.api.post(f"{BASE}/contacts/upload/", {"file": sheet(old, [["Bansal Bros", "98100 00401", "Traders", "India", "Delhi", "", "", "", "Y"]]),
                                                       "add": "1", "choices": "{}"}, format="multipart")
        self.assertEqual(r.json(), {"added": 1})
        self.assertEqual(WaContact.objects.get(firm="Bansal Bros").owner_name, "")
        r = self.api.post(f"{BASE}/contacts/upload/", {"file": sheet([*old, "Owner Name"], [["Verma Crafts", "98100 00402", "Traders", "India", "Noida", "", "", "", "Y", "Asha Verma"]])},
                          format="multipart")
        self.assertEqual(r.json()["rows"][0]["owner_name"], "Asha Verma")
        self.api.post(f"{BASE}/contacts/upload/", {"file": sheet([*old, "Owner Name"], [["Verma Crafts", "98100 00402", "Traders", "India", "Noida", "", "", "", "Y", "Asha Verma"]]),
                                                   "add": "1", "choices": "{}"}, format="multipart")
        self.assertEqual(WaContact.objects.get(firm="Verma Crafts").owner_name, "Asha Verma")

    def test_the_template_file_has_the_column_and_reads_back(self) -> None:
        book = load_workbook(io.BytesIO(whatsapp.template_file()))
        header = [c.value for c in book.active[1]]
        self.assertEqual(header[-1], "Owner Name")
        rows = whatsapp.parse_file(io.BytesIO(whatsapp.template_file()), "t.xlsx", 100)
        self.assertEqual(rows[0]["owner_name"], "Suresh Gupta")

    def test_add_edit_and_the_contact_json(self) -> None:
        r = self.api.post(f"{BASE}/contacts/", {"firm": "Mehta CA", "phone": "98100 00403", "segment": "CA", "country": "India",
                                                "city": "Delhi", "owner_name": "Anil Mehta", "opted_in": "yes"}, format="json")
        self.assertEqual(r.status_code, 201, r.json())
        body = r.json()
        self.assertEqual(body["owner_name"], "Anil Mehta")
        self.assertEqual(body["known"], {"FirstName": False, "OwnerName": True, "Business": True, "City": True, "HowWeKnow": False})
        self.assertEqual(body["salutation_hi"], "Namaste Mehta CA team")
        r = self.api.patch(f"{BASE}/contacts/{body['id']}/", {"owner_name": "", "first_name": "Rohit"}, format="json")
        self.assertEqual((r.json()["owner_name"], r.json()["known"]["OwnerName"], r.json()["known"]["FirstName"]), ("", False, True))


class TagTests(Api):
    def test_tags_crud_and_on_templates(self) -> None:
        r = self.api.post(f"{BASE}/tags/", {"name": " CA  firms "}, format="json")
        self.assertEqual(r.status_code, 201)
        ca = r.json()
        self.assertEqual((ca["name"], ca["color"], ca["templates"]), ("CA firms", WaTag.COLORS[0], 0))
        r = self.api.post(f"{BASE}/tags/", {"name": "ca FIRMS"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"name": ["There is already a tag called CA firms."]}))
        self.assertEqual(self.api.post(f"{BASE}/tags/", {"name": "Odd", "color": "#123456"}, format="json").status_code, 400)
        fest = self.api.post(f"{BASE}/tags/", {"name": "Festival"}, format="json").json()
        self.assertEqual(fest["color"], WaTag.COLORS[1])  # the next colour in turn
        r = self.api.post(f"{BASE}/templates/", {"category": "quick_reply", "type_name": "Price", "text": "x", "text_hi": "y",
                                                 "tag_ids": [ca["id"], fest["id"]]}, format="json")
        self.assertEqual(r.status_code, 201, r.json())
        pk = r.json()["id"]
        self.assertEqual({t["name"] for t in r.json()["tags"]}, {"CA firms", "Festival"})
        self.assertEqual(self.api.patch(f"{BASE}/templates/{pk}/", {"tag_ids": [999999]}, format="json").json(), {"tag_ids": ["That tag no longer exists."]})
        r = self.api.patch(f"{BASE}/templates/{pk}/", {"tag_ids": [fest["id"]]}, format="json")
        self.assertEqual(r.json()["tags"], [{"id": fest["id"], "name": "Festival", "color": WaTag.COLORS[1]}])
        listed = next(t for t in self.api.get(f"{BASE}/templates/").json() if t["id"] == pk)
        self.assertEqual([t["name"] for t in listed["tags"]], ["Festival"])
        self.assertEqual({t["name"]: t["templates"] for t in self.api.get(f"{BASE}/tags/").json()}, {"CA firms": 0, "Festival": 1})
        r = self.api.patch(f"{BASE}/tags/{fest['id']}/", {"name": "Festivals", "color": WaTag.COLORS[4]}, format="json")
        self.assertEqual((r.json()["name"], r.json()["color"]), ("Festivals", WaTag.COLORS[4]))
        self.assertEqual(self.api.delete(f"{BASE}/tags/{fest['id']}/").status_code, 204)
        self.assertEqual(self.api.get(f"{BASE}/templates/{pk}/").json()["tags"], [])  # the template stays


class StatsTests(FlowBase):
    def test_per_template_and_per_language_over_30_days(self) -> None:
        opener = WaTemplate.objects.get(category=Cat.OUTREACH, step=1)
        en = self.contact("+919810000501", lang="en")
        hi = self.contact("+919810000502", lang="hi")
        hi2 = self.contact("+919810000503", lang="hi")
        for c in (en, hi, hi2):
            self.send(c, {"template": opener})
        whatsapp.record_reply(hi, "Haan, batao", ME)
        whatsapp.act(hi, "interested")
        # An old send (40 days ago) and one with no language recorded (counts by the contact's language).
        old = self.contact("+919810000504", lang="en")
        m = WaMessage.objects.create(contact=old, kind=WaMessage.Kind.FLOW, template=opener, text="x", language="en")
        WaMessage.objects.filter(pk=m.pk).update(at=timezone.now() - timedelta(days=40))
        WaMessage.objects.create(contact=self.contact("+919810000505", lang="hi"), kind=WaMessage.Kind.FLOW, template=opener, text="x")
        row = next(t for t in self.api.get(f"{BASE}/templates/").json() if t["id"] == opener.pk)
        self.assertEqual(
            {k: row[k] for k in wa_templates.STAT_KEYS},
            {"sends": 4, "replies": 1, "reply_rate": 25, "next": 1, "next_rate": 25, "sends_en": 1, "replies_en": 0,
             "reply_rate_en": 0, "sends_hi": 3, "replies_hi": 1, "reply_rate_hi": 33},
        )
        self.assertEqual(row["stats_days"], 30)
        self.assertEqual(row["performance"]["sends"], 5)  # the all-time numbers stay as they were
        untouched = WaTemplate.objects.get(category=Cat.OUTREACH, step=4)
        self.assertEqual(self.api.get(f"{BASE}/templates/{untouched.pk}/").json()["sends"], 0)
        self.assertEqual(WaMessage.objects.filter(contact=hi, template=opener).get().language, "hi")


class VersionTests(FlowBase):
    def setUp(self) -> None:
        super().setUp()
        self.a = WaTemplate.objects.get(category=Cat.OUTREACH, step=1)
        one, two = self.contact("+919810000601"), self.contact("+919810000602")
        self.odd, self.even = (one, two) if one.pk % 2 else (two, one)

    def add_b(self, **body: str) -> dict:
        r = self.api.post(f"{BASE}/templates/{self.a.pk}/versions/", body or {"text": "B for {{FirstName}}. Reply STOP to opt out.",
                                                                              "text_hi": "B {{FirstName}} ji. Reply STOP to opt out."}, format="json")
        self.assertEqual(r.status_code, 201, r.json())
        return r.json()

    def test_b_is_made_once_and_hidden_from_the_list(self) -> None:
        body = self.add_b()
        b = WaTemplate.objects.get(version_of=self.a)
        self.assertEqual(body["version_b"]["id"], b.pk)
        self.assertEqual((b.category, b.type_name, b.step, b.stage, b.kind), (self.a.category, self.a.type_name, 1, 1, self.a.kind))
        self.assertEqual(body["version_b"]["sends"], 0)
        r = self.api.post(f"{BASE}/templates/{self.a.pk}/versions/", {"text": "again"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("already has a version B", r.json()["detail"])
        ids = [t["id"] for t in self.api.get(f"{BASE}/templates/").json()]
        self.assertNotIn(b.pk, ids)
        self.assertEqual(self.api.post(f"{BASE}/templates/{self.a.pk}/versions/", {"text": " "}, format="json").json(), {"text": ["Write version B first."]})
        self.assertEqual(self.api.post(f"{BASE}/templates/{b.pk}/versions/", {"text": "x"}, format="json").status_code, 400)
        older = WaTemplate.objects.filter(category="").first()
        self.assertIn("campaign templates", self.api.post(f"{BASE}/templates/{older.pk}/versions/", {"text": "x"}, format="json").json()["detail"])
        r = self.api.post(f"{BASE}/templates/{self.a.pk}/versions/", {"text": "{{#if City}}"}, format="json")
        self.assertEqual(r.json(), {"text": ["An If has no matching end."]})
        # Changing A's step moves B with it.
        self.api.patch(f"{BASE}/templates/{self.a.pk}/", {"step": 2}, format="json")
        b.refresh_from_db()
        self.assertEqual((b.step, b.stage), (2, 1))

    def test_the_queue_alternates_by_contact_until_keep(self) -> None:
        self.add_b()
        b = WaTemplate.objects.get(version_of=self.a)
        self.assertEqual(self.due(self.odd)["template"].pk, b.pk)
        self.assertEqual(self.due(self.even)["template"].pk, self.a.pk)
        today = self.api.get(f"{BASE}/today/").json()["queue"]
        self.assertEqual({q["contact"]["id"]: q["version"] for q in today}, {self.odd.pk: "B", self.even.pk: ""})
        draft = self.api.post(f"{BASE}/contacts/{self.odd.pk}/draft/", {"kind": "flow"}, format="json").json()
        self.assertEqual((draft["template_id"], draft["version"], draft["text"]), (b.pk, "B", "B for Rohit. Reply STOP to opt out."))
        odd = self.send(self.odd)
        self.send(self.even)
        self.assertEqual((odd.out_day, odd.campaign_status), (1, CS.MESSAGED))
        whatsapp.record_reply(odd, "Tell me more", ME)
        row = next(t for t in self.api.get(f"{BASE}/templates/").json() if t["id"] == self.a.pk)
        self.assertEqual((row["sends"], row["replies"], row["version_b"]["sends"], row["version_b"]["replies"], row["version_b"]["reply_rate"]), (1, 0, 1, 1, 100))
        # Keep B: A takes B's words, B is archived, both keep their messages; everyone gets A again.
        r = self.api.post(f"{BASE}/templates/{self.a.pk}/keep/", {"version": "B"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["text"], r.json()["version_b"]), ("B for {{FirstName}}. Reply STOP to opt out.", None))
        b.refresh_from_db()
        self.assertTrue(b.archived)
        self.assertEqual(WaMessage.objects.filter(template=b).count(), 1)
        self.assertEqual(WaMessage.objects.filter(template=self.a).count(), 1)
        fresh = next(c for c in (self.contact("+919810000603"), self.contact("+919810000604")) if c.pk % 2)
        self.assertEqual(wa_flow.next_outreach(fresh, self.today).pk, self.a.pk)
        self.assertEqual(self.api.post(f"{BASE}/templates/{self.a.pk}/keep/", {"version": "A"}, format="json").status_code, 400)  # no B left
        self.add_b()  # a new test can start

    def test_keep_a_and_a_draft_b(self) -> None:
        self.add_b(text="Only English so far.")
        b = WaTemplate.objects.get(version_of=self.a)
        self.assertEqual(self.due(self.odd)["template"].pk, self.a.pk)  # a draft B is never sent
        self.assertEqual(self.api.post(f"{BASE}/templates/{self.a.pk}/keep/", {"version": "B"}, format="json").status_code, 400)
        self.assertEqual(self.api.post(f"{BASE}/templates/{self.a.pk}/keep/", {"version": "C"}, format="json").json(), {"version": ["Pick A or B."]})
        before = self.a.text
        self.assertEqual(self.api.post(f"{BASE}/templates/{self.a.pk}/keep/", {"version": "a"}, format="json").status_code, 200)
        self.a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((self.a.text, b.archived), (before, True))

    def test_editing_b_and_deleting_a(self) -> None:
        self.add_b(text="Only English so far.")
        b = WaTemplate.objects.get(version_of=self.a)
        r = self.api.patch(f"{BASE}/templates/{b.pk}/", {"text_hi": "Ab Hinglish bhi. Reply STOP to opt out."}, format="json")
        self.assertEqual(r.status_code, 200, r.json())
        self.assertFalse(self.api.get(f"{BASE}/templates/{self.a.pk}/").json()["version_b"]["draft"])
        self.assertEqual(self.due(self.odd)["template"].pk, b.pk)
        # A is the campaign opener (PROTECT), so use another Intro row for the delete.
        other = WaTemplate.objects.get(category=Cat.OUTREACH, step=4)
        self.api.post(f"{BASE}/templates/{other.pk}/versions/", {"text": "x"}, format="json")
        self.assertEqual(self.api.delete(f"{BASE}/templates/{other.pk}/").status_code, 204)
        self.assertFalse(WaTemplate.objects.filter(version_of_id=other.pk).exists())


class ReachTests(Api):
    def test_reach_counts_messageable_contacts_by_the_fill_rules(self) -> None:
        one = contact("+919810000701", first_name="Rohit")
        two = contact("+919810000702", first_name="", owner_name="Suresh", source="Uploaded 1 Nov 2026")
        three = contact("+919810000703", first_name="", city="", source="")
        contact("+919810000704", status=WaContact.Status.STOP)
        contact("+919810000705", campaign_status=CS.OPTED_OUT)
        WaBlock.objects.create(phone="+919810000706", reason="Asked to stop")
        contact("+919810000706")
        r = self.api.get(f"{BASE}/templates/reach/").json()
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["known"], {"FirstName": 1, "OwnerName": 1, "Business": 3, "City": 2, "HowWeKnow": 1})
        self.assertEqual(r["examples"], {"first_name": one.pk, "no_first_name": two.pk, "no_names": three.pk})


class PreviewAndDuplicateTests(FlowBase):
    def test_preview_fills_for_the_contact(self) -> None:
        c = self.contact(first_name="")
        r = self.api.post(f"{BASE}/templates/preview/", {"contact": c.pk, "text": GREETING + ", {{OwnerName}} here.",
                                                         "text_hi": "{{#if FirstName}}Namaste {{FirstName}} ji{{else}}Namaste {{Business}} team{{/if}}"}, format="json")
        body = r.json()
        self.assertEqual(body["text"], "Hello Mehta & Co CA team, here.")
        self.assertEqual(body["text_hi"], "Namaste Mehta & Co CA team")
        self.assertEqual((body["missing_en"], body["missing_hi"]), (["OwnerName"], []))
        self.assertEqual(self.api.post(f"{BASE}/templates/preview/", {"contact": c.pk, "text": "{{else}}"}, format="json").json(), {"text": [wa_conditions.STRAY_ELSE]})

    def test_duplicate_archive_and_bring_back(self) -> None:
        tag = WaTag.objects.create(name="CA firms")
        t = WaTemplate.objects.get(category=Cat.ENGAGE, track="privacy", step=1)
        t.tags.add(tag)
        r = self.api.post(f"{BASE}/templates/{t.pk}/duplicate/")
        self.assertEqual(r.status_code, 201)
        copy = r.json()
        self.assertEqual((copy["name"], copy["category"], copy["track"], copy["step"], copy["text"], [x["name"] for x in copy["tags"]]),
                         (f"{t.name} (copy)", "engage", "privacy", 1, t.text, ["CA firms"]))
        self.assertTrue(self.api.patch(f"{BASE}/templates/{copy['id']}/", {"archived": True}, format="json").json()["archived"])
        self.assertFalse(self.api.patch(f"{BASE}/templates/{copy['id']}/", {"archived": False}, format="json").json()["archived"])
