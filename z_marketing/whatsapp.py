"""
WhatsApp outreach: contacts, the daily queue, drafts, and what a person recorded.

Zinwe never sends a WhatsApp message itself. WhatsApp bans numbers that send automatically through WhatsApp Web or
browser tools, and business-initiated messages through Meta's platform need an opt-in first. So staff open each chat
from the admin (wa.me with the text filled in), press Send on the Zinwe WhatsApp Business app, then mark it sent here.
Drafts come from the campaign's templates; with ANTHROPIC_API_KEY set, Claude personalises them from Useful Info.

Someone who never replies gets at most four messages (first, follow-up, touch 3, touch 4), each due a set number of
days after the first. A YES starts the after-YES sequence: templates of kind "nurture", picked by the contact's track
and due `day` days after the YES.

Campaigns with plan "ninety" (the 100 Reviews campaign) follow wa_flow.py instead: category templates by day, then the
YES path by Track.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from . import wa_conditions, wa_flow
from .contact_file import MAX_BYTES, MAX_ROWS, FileProblem, _read_rows
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTemplate

logger = logging.getLogger("zinwe.marketing")

Status = WaContact.Status
Kind = WaMessage.Kind
COUNTRY_CODES = {"India": "91", "USA": "1"}
HEADERS = {
    "firm": ("business name", "business", "firm", "firm name", "company", "company name"),
    "phone": ("phone", "phone number", "mobile", "whatsapp", "whatsapp number", "number"),
    "segment": ("segment", "business segment"),
    "country": ("country",),
    "city": ("city", "town"),
    "first_name": ("first name", "firstname", "first", "name", "contact name"),
    "owner_name": ("owner name", "owner", "owner's name", "owners name"),  # optional: older files have no such column
    "source": ("source", "where found", "found at"),
    "info": ("useful info", "info", "notes"),
    "opt_in": ("opt-in", "opt in", "optin", "opted in", "consent"),
}
OPT_IN = {"y", "yes", "true"}  # opted in or introduced; any other value loads the contact locked
LOCKED = "Not opted in yet. Mark them as opted in, with how they agreed, before messaging."
REQUIRED = {"firm": "Business Name", "phone": "Phone", "segment": "Segment", "country": "Country", "city": "City"}
TEMPLATE_COLUMNS = ["Business Name", "Phone", "Segment", "Country", "City", "First Name", "Source", "Opt-in", "Useful Info", "Owner Name"]
ACTIONS = ("interested", "signed", "notnow", "stop", "pause", "resume", "optin",
           # 90-day campaign (wa_flow.py): Track, paid, setup error, first PC connected, a testimonial back
           "track", "paid", "setup_error", "setup_ok", "using", "review_written", "review_video")
COLD = [WaMessage.Kind.FIRST, WaMessage.Kind.FOLLOWUP, WaMessage.Kind.TOUCH, WaMessage.Kind.CUSTOM]  # count against the daily limit
CS = WaContact.CampaignStatus


class WaError(Exception):
    """Refused, with a message for staff."""


class WaFieldError(WaError):
    """Refused because of the form: {field: message} for each field to fix."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("Check the highlighted fields.")


def today() -> date:
    return timezone.localdate()


def normalize_phone(raw: str, country: str) -> str | None:
    """E.164 (+919810000101), or None when it can't be a mobile number. A bare 10-digit number gets the country code."""
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    elif not text.startswith("+"):
        code = COUNTRY_CODES.get(country.strip())
        if digits.startswith("0") and len(digits) == 11:
            digits = digits[1:]
        if code and len(digits) == 10:
            digits = code + digits
    return f"+{digits}" if 10 <= len(digits) <= 15 else None


def blocked(phone: str) -> bool:
    return WaBlock.objects.filter(phone=phone).exists()


# --- Uploads -------------------------------------------------------------------------------------------------------


def _columns(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for at, title in enumerate(header):
        clean = " ".join(title.lower().replace("_", " ").replace("*", "").split())
        for field, names in HEADERS.items():
            if clean in names and field not in found:
                found[field] = at
    missing = [label for field, label in REQUIRED.items() if field not in found]
    if "opt_in" not in found:
        missing.append("Opt-in")
    if missing:
        raise FileProblem(f"This file has no {', '.join(missing)} column. Use the template, or rename the column.")
    return found


def _info(text: str) -> dict[str, Any]:
    """Useful Info: a JSON object from the sheet, or plain text kept as notes."""
    text = text.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {"notes": text[:500]}
    return {str(k)[:40]: str(v)[:300] for k, v in value.items()} if isinstance(value, dict) else {"notes": text[:500]}


def parse_file(upload: Any, name: str, size: int) -> list[dict[str, Any]]:
    """Rows from an .xlsx or .csv file, each checked: result is ready, warn (addable) or bad (skipped)."""
    if size > MAX_BYTES:
        raise FileProblem("This file is over 2 MB. Split it into smaller files.")
    rows = _read_rows(name.lower(), upload.read())
    start = next((i for i, r in enumerate(rows) if any(r)), None)
    if start is None:
        raise FileProblem("This file is empty.")
    columns = _columns(rows[start])
    body = rows[start + 1 :]
    if len(body) > MAX_ROWS:
        raise FileProblem(f"This file has more than {MAX_ROWS:,} rows. Split it into smaller files.")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    existing = set(WaContact.objects.values_list("phone", flat=True))
    stopped = set(WaBlock.objects.values_list("phone", flat=True))
    for at, row in enumerate(body, start=start + 2):
        if not any(row):
            continue
        cell = lambda f: row[columns[f]].strip() if f in columns and columns[f] < len(row) else ""  # noqa: E731, B023
        item: dict[str, Any] = {
            "row": at, "firm": cell("firm")[:160], "first_name": cell("first_name")[:80], "owner_name": cell("owner_name")[:120],
            "segment": cell("segment")[:120],
            "country": cell("country")[:40], "city": cell("city")[:80], "source": cell("source")[:160],
            "info": _info(cell("info")), "phone": "",
        }
        phone = normalize_phone(cell("phone"), item["country"])
        item["phone"] = phone or cell("phone")[:20]
        item["opted_in"] = cell("opt_in").lower() in OPT_IN
        missing = [label for f, label in REQUIRED.items() if not cell(f)]
        if missing:
            item.update(result="bad", message=f"Missing {', '.join(missing)}")
        elif not phone:
            item.update(result="bad", message="Not a phone number. Add the country code")
        elif phone in stopped:
            item.update(result="bad", message="Asked to stop before, skipped")
        elif phone in seen:
            item.update(result="bad", message="Repeat row, skipped")
        elif phone in existing:
            item.update(result="bad", message="Already a contact")
        elif item["country"] == "USA":
            item.update(result="warn", message="Check: may be an office landline")
        else:
            item.update(result="ready", message="Ready")
        if item["result"] != "bad" and not item["opted_in"]:
            item.update(result="locked", message="Added locked: no messages until they opt in")
        if phone:
            seen.add(phone)
        out.append(item)
    if not out:
        raise FileProblem("This file has headers but no contacts.")
    return out


@transaction.atomic
def add_rows(rows: list[dict[str, Any]], choices: dict[str, dict[str, Any]], actor: str) -> int:
    """Add the checked rows staff kept. `choices` per row number: include, segment, campaign (id or null)."""
    campaigns = {c.id: c for c in WaCampaign.objects.all()}
    added = 0
    for item in rows:
        pick = choices.get(str(item["row"]), {})
        if item["result"] == "bad" or not pick.get("include", True):
            continue
        campaign = campaigns.get(pick.get("campaign")) if pick.get("campaign") else None
        source = item["source"] or f"Uploaded {today():%d %b %Y}"
        _, made = WaContact.objects.get_or_create(
            phone=item["phone"],
            defaults=dict(
                campaign=campaign, first_name=item["first_name"], owner_name=item.get("owner_name", ""), firm=item["firm"],
                segment=str(pick.get("segment") or item["segment"])[:120], country=item["country"], city=item["city"],
                source=source, info=item["info"], added_by=actor, opted_in=item["opted_in"],
                opt_in_note="Opt-in in the uploaded sheet" if item["opted_in"] else "",
            ),
        )
        added += int(made)
    return added


@transaction.atomic
def add_one(data: dict[str, Any], actor: str) -> WaContact:
    """
    One contact typed in the admin (Add contact): the same checks as a sheet row. Required: business name, phone,
    segment, country, city. Not opted in = added locked, as from a sheet. Raises WaFieldError with what to fix.
    """
    text = lambda key, size: str(data.get(key) or "").strip()[:size]  # noqa: E731
    firm, segment, country, city = text("firm", 160), text("segment", 120), text("country", 40), text("city", 80)
    raw_phone = text("phone", 30)
    errors: dict[str, str] = {}
    for field, value in (("firm", firm), ("phone", raw_phone), ("segment", segment), ("country", country), ("city", city)):
        if not value:
            errors[field] = f"Add the {REQUIRED[field].lower()}."
    if country and country not in COUNTRY_CODES:
        errors["country"] = "Pick India or USA."
    phone = normalize_phone(raw_phone, country) if raw_phone and "country" not in errors else None
    if raw_phone and country in COUNTRY_CODES and not phone:
        errors["phone"] = "Not a phone number. Use the country code, or 10 digits for India or the USA."
    elif phone and blocked(phone):
        errors["phone"] = "This number asked to stop. It can't be added."
    elif phone:
        same = WaContact.objects.filter(phone=phone).first()
        if same:
            errors["phone"] = f"Already a contact: {same.firm}."
    campaign = None
    if data.get("campaign"):
        campaign = WaCampaign.objects.filter(pk=data.get("campaign")).first()
        if campaign is None:
            errors["campaign"] = "That campaign no longer exists."
    if errors:
        raise WaFieldError(errors)
    opted_in = str(data.get("opted_in", "")).lower() in OPT_IN
    note = text("opt_in_note", 200)
    notes = text("notes", 500)
    return WaContact.objects.create(
        phone=phone, campaign=campaign, first_name=text("first_name", 80), owner_name=text("owner_name", 120), firm=firm,
        segment=segment, country=country,
        city=city, source=text("source", 160) or f"Added by hand {today():%d %b %Y}", info={"notes": notes} if notes else {},
        added_by=actor, opted_in=opted_in,
        opt_in_note=(note or f"Marked opted in when added, {today():%d %b %Y}") if opted_in else "",
    )


def template_file() -> bytes:
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = "Contacts"
    sheet.append(TEMPLATE_COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.append(["Sample Exports (delete this row)", "+91 98100 00000", "Traders and export houses", "India", "Delhi", "Rakesh",
                  "Replied to our email", "Y", '{"website":"sample.example","team_size":"12"}', "Suresh Gupta"])
    for letter, width in zip("ABCDEFGHIJ", (30, 18, 26, 10, 14, 14, 22, 8, 40, 18), strict=True):
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = "A2"
    out = BytesIO()
    book.save(out)
    return out.getvalue()


# --- Drafts --------------------------------------------------------------------------------------------------------


def fill(text: str, contact: WaContact, language: str = "en") -> str:
    """{{Greeting}} (and an opening "Hi {{FirstName}}") is "Hi Rohit", or "Hello <Business> team" without a real name;
    {{FirstName}} is their first name only when it clearly is one, else "<Business> team" (wa_names.py);
    {{Business}} is the short clean name; {{OwnerName}} is the owner's name as staff typed it; {{HowWeKnow}} is one
    true line on where we found them. Conditions ({{#if FirstName}}...{{else}}...{{/if}}, wa_conditions.py) are
    resolved first, so their parts go through the same replacements."""
    from .wa_names import address

    a = address(contact)
    text = wa_conditions.apply(text, wa_conditions.known(contact, a))
    # An opening "Hi {{FirstName}}" becomes the salutation: "Hi Rohit", or "Hello Webgross team" without a real name.
    text = re.sub(r"^\s*(Hi|Hello)\s+\{\{FirstName\}\}", "{{Greeting}}", text)
    for key, value in (("{{Greeting}}", a["salutation_hi"] if language == "hi" else a["salutation"]), ("{{FirstName}}", a["greeting"]), ("{{Firm}}", a["business"]), ("{{Business}}", a["business"]),
                       ("{{City}}", contact.city), ("{{OwnerName}}", (contact.owner_name or "").strip()), ("{{HowWeKnow}}", a["how_we_know"])):
        text = text.replace(key, value)
    text = re.sub(r"[ \t]+\n", "\n", text)  # a blank {{HowWeKnow}} leaves no trailing space
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def language_for(contact: WaContact) -> str:
    """en or hi: the language this contact gets cold messages in. Fixed by their first message once sent; else the
    campaign's choice, and for a 50/50 campaign by their id, so every contact keeps one language."""
    if contact.lang:
        return contact.lang
    choice = contact.campaign.language if contact.campaign else "en"
    if choice == "ab":
        return "hi" if contact.pk % 2 else "en"
    return choice


def _in_language(template: WaTemplate | None, language: str) -> WaTemplate | None:
    """The template, or its twin in `language` when it has one."""
    if template is not None and template.language != language and template.twin_id and template.twin.language == language:
        return template.twin
    return template


def _template_for(contact: WaContact, kind: str) -> WaTemplate | None:
    return _in_language(_template_for_any(contact, kind), language_for(contact))


def _template_for_any(contact: WaContact, kind: str) -> WaTemplate | None:
    c = contact.campaign
    english = WaTemplate.objects.filter(kind=kind, language=WaTemplate.Language.EN, category="", archived=False)
    if c is None:
        return english.first()
    pick = {"first": c.first, "followup": c.followup, "touch3": c.touch3, "touch4": c.touch4, "interested": c.interested,
            "question": c.question, "notnow": c.notnow}
    return pick.get(kind) or english.first()


def next_nurture(contact: WaContact) -> WaTemplate | None:
    """The next after-YES message for this contact (due or not), or None when the sequence is done or never started."""
    if contact.yes_date is None or contact.status not in (Status.INTERESTED, Status.SIGNED):
        return None
    return (
        WaTemplate.objects.filter(kind=WaTemplate.Kind.NURTURE, language=WaTemplate.Language.EN, category="", archived=False,
                                  track__in=["", contact.track], day__gt=contact.nurture_day)
        .order_by("day", "track", "id")
        .first()
    )


# The 90-day plan's approved answers (owner, 3 Nov 2026). Also saved as "question" templates staff can copy.
ANSWERS = [
    ("Is this spyware?", "No. Zinwe protects company data. It flags things like files copied to a pen drive or remote-access "
     "sessions. It does not read messages, record screens or log keystrokes, and your team sees the Zinwe widget on their "
     "PC. More: zinwe.io/legal/privacy"),
    ("What does it cost?", "There is a Free forever plan; paid plans are on zinwe.io/plans. How many PCs do you have? I'll "
     "tell you which fits."),
    ("Do you support Mac?", "Windows only today. I'll tell you when that changes."),
    ("We have antivirus", "Antivirus stops viruses. Zinwe shows what happens to company files and tools on each PC. They "
     "work side by side."),
]

SYSTEM = (
    "You personalise WhatsApp messages that Vishal, founder of Zinwe (data protection and team tools for small offices "
    "on Windows PCs), sends by hand to small businesses. Rewrite the template for this one business using only the "
    "details given. Keep the template's structure: the greeting line exactly as given (open_with), then one paragraph "
    "each for the problem, the solution, the reward line and the ask, in that order. Address them exactly as "
    "address_as, the way the template does; never use any other name for them "
    "(first_name is empty when we don't know a person's name). If useful_info has notes, you may open with one short line "
    "drawn only from them. Keep its meaning, its "
    "offer and every instruction about replying (YES, HAAN, STOP, LATER). Keep it under 400 characters, plain text, "
    "warm and short, no emojis. Never invent facts, numbers, links, prices, discounts or features, and never "
    "mention their team size or anything that sounds like watching staff. Where the template says [Your answer], answer "
    "their_last_message in two or three lines using only the approved answers below; if none fits, keep [Your answer] "
    "for staff to fill. Reply with the message text only.\n\nApproved answers:\n" + "\n".join(f"- {q}: {a}" for q, a in ANSWERS)
)


def _claude(template_text: str, contact: WaContact, their_last: str) -> str | None:
    """One personalised draft from Claude, or None (no key, an error, or a refusal): the template is used instead."""
    if not settings.ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=30.0, max_retries=1)
        from .wa_names import address

        a = address(contact)
        details = {
            "open_with": a["salutation"], "address_as": a["greeting"], "first_name": a["first_name"], "business": a["business"], "segment": contact.segment, "city": contact.city,
            "country": contact.country, "useful_info": contact.info, "their_last_message": their_last,
        }
        response = client.messages.create(
            model=settings.ZINWE_WA_MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": f"Template:\n{template_text}\n\nBusiness details (JSON):\n{json.dumps(details)}"}],
        )
    except Exception:  # noqa: BLE001 -- any API or network failure falls back to the template
        logger.warning("WhatsApp draft: Claude call failed, using the template", exc_info=True)
        return None
    if response.stop_reason == "refusal":
        return None
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text[:1000] or None


def draft(contact: WaContact, kind: str, template_id: int | None = None) -> dict[str, Any]:
    """The text to send next: {text, template, by, media, creative, notes}. `by` is "claude" when Claude personalised it."""
    if not contact.opted_in:
        raise WaError(LOCKED)
    if template_id:  # staff picked a template for a one-off message (Contacts → Send WhatsApp)
        template = WaTemplate.objects.filter(pk=template_id).first()
        if template is None:
            raise WaError("That template no longer exists.")
    elif kind == "flow":
        template, note = wa_flow.next_for(contact, today())
        if template is None:
            raise WaError("Set the contact's Track first." if note == "set_track" else "Nothing is due for this contact.")
    else:
        template = next_nurture(contact) if kind == "nurture" else _template_for(contact, kind)
    if template is None:
        raise WaError("No after-YES message is left for this contact." if kind == "nurture" else "Add a template of this kind first.")
    if template.category:
        try:
            wa_flow.check_sendable(contact, template)
        except wa_flow.FlowError as exc:
            raise WaError(str(exc)) from exc
    language = language_for(contact) if template.category else template.language
    base = fill(wa_flow.text_in(template, language), contact, language)
    last = contact.messages.filter(kind=Kind.THEIRS).last()
    personal = _claude(base, contact, last.text if last else "")
    from .wa_media import describe

    parts = template.parts or {}
    return {
        "text": personal or base, "template": template.name, "by": "claude" if personal else "template",
        # A version B shows A's picture or video: the A/B test is about the words.
        "media": describe(template.version_of if template.version_of_id and not template.media else template),
        # From the outreach structure, never sent: what to attach, and context for whoever replies next.
        "creative": parts.get("creative", ""), "notes": fill(parts["notes"], contact) if parts.get("notes") else "",
        "language": language, "template_id": template.pk, "label": wa_flow.label(template) if template.category else "",
        "version": "B" if template.version_of_id else "",
    }


# --- Today ---------------------------------------------------------------------------------------------------------


def _sent_today(campaign: WaCampaign | None = None) -> int:
    """Cold messages sent today: first, follow-up, touches, one-offs, and 90-day Intro."""
    q = WaMessage.objects.filter(Q(kind__in=COLD) | Q(kind=Kind.FLOW, template__category=WaTemplate.Category.OUTREACH), at__date=today())
    return q.filter(contact__campaign=campaign).count() if campaign else q.count()


NEXT_KIND = {0: "first", 1: "followup", 2: "touch3", 3: "touch4"}


def queue() -> list[dict[str, Any]]:
    """
    What is due today: after-YES messages first (no daily limit: these people asked for them), then no-reply touches
    and first messages in live campaigns, within each campaign's daily limit. 90-day campaigns: wa_flow.queue.
    """
    now = today()
    room = {c.id: max(0, c.daily_limit - _sent_today(c)) for c in WaCampaign.objects.filter(plan=WaCampaign.Plan.NINETY)}
    out: list[dict[str, Any]] = wa_flow.queue(now, room)
    ninety = WaCampaign.Plan.NINETY
    warm = (
        WaContact.objects.filter(status__in=[Status.INTERESTED, Status.SIGNED], yes_date__isnull=False, needs_reply=False)
        .exclude(campaign__plan=ninety)
        .exclude(paused_until__gt=now)
        .select_related("campaign")
        .order_by("yes_date", "id")
    )
    for contact in warm:
        template = next_nurture(contact)
        if template is not None and contact.yes_date + timedelta(days=template.day) <= now:
            out.append({"contact": contact, "kind": "nurture", "template": template.name})
    for c in WaCampaign.objects.filter(status=WaCampaign.Status.LIVE).exclude(plan=ninety):
        room = max(0, c.daily_limit - _sent_today(c))
        due = c.contacts.filter(opted_in=True).exclude(paused_until__gt=now)
        touches = due.filter(status=Status.MESSAGED, step__in=[1, 2, 3], next_date__lte=now, needs_reply=False)
        firsts = due.filter(status=Status.READY, step=0).filter(Q(next_date__isnull=True) | Q(next_date__lte=now))
        for contact in list(touches.order_by("next_date", "id")) + list(firsts.order_by("id")):
            if room <= 0:
                break
            kind = NEXT_KIND[contact.step]
            if kind != "first" and not getattr(c, kind):
                continue
            out.append({"contact": contact, "kind": kind})
            room -= 1
    return out


def replies() -> list[WaContact]:
    return list(WaContact.objects.filter(needs_reply=True).exclude(status=Status.STOP).order_by("id"))


# --- What a person recorded ----------------------------------------------------------------------------------------


@transaction.atomic
def mark_sent(contact: WaContact, kind: str, text: str, actor: str, template_id: int | None = None) -> WaContact:
    if contact.status == Status.STOP or blocked(contact.phone):
        raise WaError("This number asked to stop. Don't message it.")
    if not contact.opted_in:
        raise WaError(LOCKED)
    text = text.strip()
    if not text:
        raise WaError("Write the message first.")
    if kind not in ("first", "followup", "touch3", "touch4", "nurture", "reply", "custom", "flow"):
        raise WaError("Unknown message type.")
    picked = WaTemplate.objects.filter(pk=template_id).select_related("coupon").first() if template_id else None
    if template_id and picked is None:
        raise WaError("That template no longer exists.")
    if kind == "flow" or (picked is not None and picked.category):
        # A 90-day campaign message, or a category template sent as a reply or one-off: it moves the path.
        if picked is None or not picked.category:
            raise WaError("Pick the campaign template this message came from.")
        try:
            wa_flow.check_sendable(contact, picked)
        except wa_flow.FlowError as exc:
            raise WaError(str(exc)) from exc
        WaMessage.objects.create(contact=contact, kind=Kind.FLOW, text=text[:2000], by=actor, template=picked,
                                 language=language_for(contact))
        if not contact.lang:
            contact.lang = language_for(contact)
        wa_flow.record_send(contact, picked, today())
        contact.save()
        return contact
    c = contact.campaign
    if kind == "nurture":
        template = next_nurture(contact)
        if template is None:
            raise WaError("No after-YES message is left for this contact.")
        contact.nurture_day = template.day
    stored = Kind.TOUCH if kind in ("touch3", "touch4") else kind
    WaMessage.objects.create(contact=contact, kind=stored, text=text[:2000], by=actor)
    start = contact.first_sent or today()
    if kind == "first":
        contact.status, contact.step, contact.first_sent = Status.MESSAGED, 1, today()
        if not contact.lang:  # the language they were first written to, for the English vs Hinglish comparison
            used = _template_for(contact, "first")
            contact.lang = used.language if used else "en"
        contact.next_date = today() + timedelta(days=c.followup_days if c else 3)
    elif kind == "followup":
        contact.step = 2
        contact.next_date = start + timedelta(days=c.touch3_days) if c and c.touch3_id else None
    elif kind == "touch3":
        contact.step = 3
        contact.next_date = start + timedelta(days=c.touch4_days) if c and c.touch4_id else None
    elif kind == "touch4":
        contact.step, contact.next_date = 4, None
    elif kind in ("reply", "custom"):  # a one-off message answers them too, and never moves the sequence
        contact.needs_reply = False
    contact.save()
    return contact


@transaction.atomic
def record_reply(contact: WaContact, text: str, actor: str) -> WaContact:
    text = text.strip()
    if not text:
        raise WaError("Paste what they wrote.")
    WaMessage.objects.create(contact=contact, kind=Kind.THEIRS, text=text[:2000], by=actor)
    if not contact.opted_in:  # they wrote to us first, so answering them is fine
        contact.opted_in, contact.opt_in_note = True, f"They messaged us on WhatsApp, {today():%d %b %Y}"
    if contact.status in (Status.READY, Status.MESSAGED, Status.NOTNOW):
        contact.status = Status.REPLIED
    if contact.campaign_status in (CS.NONE, CS.MESSAGED, CS.NO_REPLY):
        contact.campaign_status = CS.REPLIED
    contact.needs_reply, contact.next_date = True, None
    if re.fullmatch(r"\W*(stop|unsubscribe|band karo|mat bhejo)\W*", text, flags=re.I):
        _stop(contact, "Replied STOP")
    contact.save()
    return contact


def _stop(contact: WaContact, reason: str) -> None:
    contact.status, contact.next_date, contact.needs_reply = Status.STOP, None, False
    contact.campaign_status = CS.OPTED_OUT
    WaBlock.objects.get_or_create(phone=contact.phone, defaults={"reason": reason})


@transaction.atomic
def act(contact: WaContact, action: str, note: str = "") -> WaContact:
    if action not in ACTIONS:
        raise WaError("Unknown action.")
    if action == "optin":
        if not note.strip():
            raise WaError("Say how they agreed, for example: said yes by email on 5 Nov.")
        contact.opted_in, contact.opt_in_note = True, note.strip()[:200]
        contact.save(update_fields=["opted_in", "opt_in_note"])
        return contact
    if action in ("interested", "signed") and not contact.opted_in:  # a YES is their agreement
        contact.opted_in, contact.opt_in_note = True, f"Said yes, {today():%d %b %Y}"
    if action in ("interested", "signed", "track") and contact.yes_date is None:
        # A YES starts the after-YES sequence and ends the no-reply touches.
        contact.yes_date, contact.nurture_day, contact.next_date = today(), -1, None
    if action in ("interested", "signed"):
        wa_flow.advance(contact, CS.YES)
    if action == "track":
        try:
            wa_flow.set_track(contact, note.strip())
        except wa_flow.FlowError as exc:
            raise WaError(str(exc)) from exc
        if contact.status not in (Status.SIGNED, Status.STOP):
            contact.status = Status.INTERESTED
    elif action == "paid":
        if contact.yes_date is None:
            contact.yes_date = today()
        wa_flow.advance(contact, CS.PAID)
        contact.status, contact.needs_reply = Status.SIGNED, False
    elif action in ("setup_error", "setup_ok"):
        contact.setup_error = action == "setup_error"
    elif action == "using":
        contact.using_since = contact.using_since or today()
    elif action == "review_written":
        contact.testimonial = WaContact.Testimonial.WRITTEN
    elif action == "review_video":
        contact.testimonial = WaContact.Testimonial.VIDEO
    if action == "interested":
        contact.status = Status.INTERESTED
    elif action == "signed":
        contact.status, contact.needs_reply = Status.SIGNED, False
    elif action == "notnow":
        contact.status, contact.next_date = Status.NOTNOW, None
    elif action == "stop":
        _stop(contact, "Asked to stop")
    elif action == "pause":
        contact.paused_until = today() + timedelta(days=30)
    elif action == "resume":
        contact.paused_until = None
    contact.save()
    return contact


# --- Numbers -------------------------------------------------------------------------------------------------------


def stats(campaign: WaCampaign | None = None) -> dict[str, int]:
    contacts = WaContact.objects.filter(campaign=campaign) if campaign else WaContact.objects.all()
    replied = contacts.filter(messages__kind=Kind.THEIRS).distinct().count()
    return {
        "contacts": contacts.count(),
        "messaged": contacts.filter(step__gte=1).count(),
        "replied": replied,
        "interested": contacts.filter(status__in=[Status.INTERESTED, Status.SIGNED]).count(),
        "signed": contacts.filter(status=Status.SIGNED).count(),
        "stopped": contacts.filter(status=Status.STOP).count(),
        # Who can actually get a message: opted in, and of those, not messaged yet.
        "opted_in": contacts.filter(opted_in=True).exclude(status=Status.STOP).count(),
        "ready": contacts.filter(opted_in=True, step=0, status=Status.READY).count(),
    }


@transaction.atomic
def add_to_campaign(campaign: WaCampaign, ids: list[Any]) -> dict[str, int]:
    """Move contacts into a campaign by hand. Only those not messaged yet and not stopped move: a contact mid-sequence
    stays where it is, so nobody gets two first messages."""
    wanted = WaContact.objects.filter(pk__in=[int(i) for i in ids if str(i).isdigit()][:2000])
    movable = wanted.filter(step=0, yes_date__isnull=True).exclude(status=Status.STOP).exclude(campaign=campaign)
    added = movable.update(campaign=campaign)
    return {"added": added, "skipped": wanted.count() - added}


def daily(days: int = 14) -> list[dict[str, Any]]:
    start = today() - timedelta(days=days - 1)
    rows = (
        WaMessage.objects.filter(at__date__gte=start)
        .values("at__date")
        .annotate(sent=Count("id", filter=Q(kind__in=COLD)), replies=Count("id", filter=Q(kind=Kind.THEIRS)))
    )
    by_day = {r["at__date"]: r for r in rows}
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        r = by_day.get(d, {})
        out.append({"date": d.isoformat(), "sent": r.get("sent", 0), "replies": r.get("replies", 0)})
    return out


def by_language() -> list[dict[str, Any]]:
    """English vs Hinglish, by the language of each contact's first message: replies, YES and stops."""
    out = []
    for code, label in (("en", "English"), ("hi", "Hinglish")):
        s = WaContact.objects.filter(lang=code, step__gte=1)
        out.append({
            "language": code, "label": label, "messaged": s.count(),
            "replied": s.filter(messages__kind=Kind.THEIRS).distinct().count(),
            "interested": s.filter(status__in=[Status.INTERESTED, Status.SIGNED]).count(),
            "stopped": s.filter(status=Status.STOP).count(),
        })
    return out


def by_segment() -> list[dict[str, Any]]:
    out = []
    for seg in WaContact.objects.values_list("segment", flat=True).distinct().order_by("segment"):
        s = WaContact.objects.filter(segment=seg)
        messaged = s.filter(step__gte=1).count()
        replied = s.filter(messages__kind=Kind.THEIRS).distinct().count()
        out.append({"segment": seg, "messaged": messaged, "replied": replied})
    return out
