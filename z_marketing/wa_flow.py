"""
The 90-day WhatsApp campaign (the "100 Reviews" campaign, owner 10 Nov 2026), for campaigns with plan "ninety".

Templates carry a category (WaTemplate.Category) and, for Engage and Demo video only, a Track. A contact moves along
one path, and `campaign_status` records how far it got:

    Intro (by day 1-90, no Track)      -> they reply: Replied
    a YES                                 -> YES
    Engage (one or two Need discovery questions while the Track is Unassigned) -> Engaged
    staff set the Track from the answer   -> Track assigned (an unclear answer: the Track stays Unassigned)
    the rest of that Track's Engage steps, then its 6 Demo videos             -> Demo video sent
    Setup (registration, then a nudge while no PC is connected)               -> Setup
    Demo call, only when staff mark a setup error                             -> Demo call
    Offer & discount: the live stage code for their pack, never an event code -> Offer
    paid                                  -> Paid
    Testimonial: 14 days after their first PC, paid or free: written, then video

Quick replies answer a message at any time and never move the path. STOP, "band karo" or "mat bhejo" at any point:
Opted out, and the number is never messaged again (whatsapp._stop). At most one message per contact per day. A
template is a draft until both its English and Hinglish texts are written; drafts are never sent.

Nothing here sends a WhatsApp message: staff send each one themselves from the admin.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .models import WaCampaign, WaContact, WaMessage, WaTemplate

Cat = WaTemplate.Category
Track = WaTemplate.Track
CS = WaContact.CampaignStatus

PATH = [CS.NONE, CS.MESSAGED, CS.REPLIED, CS.YES, CS.ENGAGED, CS.TRACK, CS.DEMO, CS.SETUP, CS.DEMO_CALL, CS.OFFER, CS.PAID]
TRACKED = {Cat.ENGAGE, Cat.DEMO_VIDEO}  # the only categories with a Track
STATUS_FOR = {Cat.OUTREACH: CS.MESSAGED, Cat.ENGAGE: CS.ENGAGED, Cat.DEMO_VIDEO: CS.DEMO, Cat.SETUP: CS.SETUP,
              Cat.DEMO_CALL: CS.DEMO_CALL, Cat.OFFER: CS.OFFER}
# The legacy kind each category row gets, so older code (media rules, campaign pickers) treats it right:
# Intro goes to strangers (text only), quick replies answer a message, the rest go to people who said YES.
KIND_FOR = {Cat.OUTREACH: WaTemplate.Kind.FIRST, Cat.QUICK: WaTemplate.Kind.QUESTION}
PACK_PLAN = {WaContact.Pack.STARTUP: "startups", WaContact.Pack.TEAM: "teams"}
TESTIMONIAL_AFTER = 14  # days of real use before we ask
SETUP_NUDGE_AFTER = 3  # days after the registration steps before the incomplete-setup nudge
DISCOVERY_STEPS = 2  # Need discovery questions an Unassigned contact gets before staff must set the Track
LAST_DAY = 90


class FlowError(Exception):
    """Refused, with a message for staff."""


def kind_for(category: str) -> str:
    return KIND_FOR.get(category, WaTemplate.Kind.NURTURE)


def is_draft(t: WaTemplate) -> bool:
    """A category row is a draft until both texts are written. Older templates have one language each."""
    if not t.category:
        return not t.text.strip()
    return not (t.text.strip() and t.text_hi.strip())


def text_in(t: WaTemplate, language: str) -> str:
    if t.category:
        return t.text_hi if language == "hi" and t.text_hi.strip() else t.text
    return t.text


def label(t: WaTemplate) -> str:
    """Intro · Problem · day 4 / Engage · Need discovery · Data Privacy · step 1."""
    bits = [t.get_category_display(), t.type_name]
    if t.category in TRACKED and t.track:
        bits.append(t.get_track_display())
    if t.category == Cat.OUTREACH and t.step:
        bits.append(f"day {t.step}")
    elif t.category == Cat.OFFER and t.coupon_id:
        bits.append(t.coupon.code)
    elif t.category in (Cat.ENGAGE, Cat.DEMO_VIDEO, Cat.SETUP, Cat.TESTIMONIAL) and t.step:
        bits.append(f"step {t.step}")
    return " · ".join(b for b in bits if b)


def day_of(contact: WaContact, on: date) -> int:
    """Where the contact is in the 90 days: day 1 is their first message. 0 = not started."""
    if contact.first_sent is None:
        return 0
    return max(1, min(LAST_DAY, (on - contact.first_sent).days + 1))


def stage_of(day: int) -> int:
    return 0 if day <= 0 else min(3, (day - 1) // 30 + 1)


def advance(contact: WaContact, status: str) -> None:
    """Move the campaign status forward along the path, never back (Opted out and No reply are set directly)."""
    current = contact.campaign_status
    if current == CS.OPTED_OUT:
        return
    if current not in PATH or PATH.index(status) > PATH.index(current):
        contact.campaign_status = status


def suggest_track(text: str) -> str:
    """The Track an Engage answer points to, or "" when it is unclear (spec §7). Staff confirm it."""
    t = (text or "").lower()
    if re.search(r"\b(both|dono|donon)\b", t):
        return Track.BOTH
    privacy = bool(re.search(r"file|data|pen ?drive|usb|access|client|laptop|cop(y|ies)|leak|secur|safe|password", t))
    activity = bool(re.search(r"\bteam|staff|who (is|does|works)|\bday\b|activity|monitor|productiv|kaam|\bdin\b|attendance|hours|working", t))
    if privacy and not activity:
        return Track.PRIVACY
    if activity and not privacy:
        return Track.ACTIVITY
    return ""


def _rows(category: str, **filters: Any) -> Any:
    """A category's live rows. Version B rows never count as rows of their own: version() swaps them in."""
    return WaTemplate.objects.filter(category=category, archived=False, version_of__isnull=True, **filters).select_related("coupon")


def active_b(t: WaTemplate) -> WaTemplate | None:
    """A's version B while the A/B test runs (not archived), or None."""
    return WaTemplate.objects.filter(version_of=t, archived=False).select_related("coupon").order_by("id").first()


def version(t: WaTemplate | None, contact: WaContact) -> WaTemplate | None:
    """The version this contact gets: while A has a written B, odd-numbered contacts get B, the rest A, until Keep.
    A B that is still a draft is never sent, so everyone gets A meanwhile."""
    if t is None or t.version_of_id or contact.pk % 2 == 0:
        return t
    b = active_b(t)
    return b if b is not None and not is_draft(b) else t


def _last_theirs(contact: WaContact) -> str:
    m = contact.messages.filter(kind=WaMessage.Kind.THEIRS).last()
    return m.text if m else ""


def next_outreach(contact: WaContact, on: date) -> WaTemplate | None:
    """The Intro row due today (or its version B for this contact)."""
    return version(_next_outreach(contact, on), contact)


def _next_outreach(contact: WaContact, on: date) -> WaTemplate | None:
    """The Intro row due today: the opener for a new contact, then the latest day reached and not sent yet."""
    rows = list(_rows(Cat.OUTREACH, step__gt=contact.out_day).order_by("step", "id"))
    if not rows:
        return None
    if contact.first_sent is None:
        return rows[0]
    day = day_of(contact, on)
    due = [r for r in rows if r.step <= day]
    return due[-1] if due else None


def default_pack(contact: WaContact) -> str:
    size = re.sub(r"\D", "", str((contact.info or {}).get("team_size", "")))
    return WaContact.Pack.TEAM if size and int(size) > 10 else WaContact.Pack.STARTUP


def offer_for(contact: WaContact, on: date, include_events: bool = False) -> WaTemplate | None:
    """The Offer row for their pack whose code is live today. Ended, paused or cancelled codes never come back;
    festival codes only when staff pick them (40% is a ceiling, never a default)."""
    plan = PACK_PLAN[contact.pack or default_pack(contact)]
    rows = _rows(Cat.OFFER, coupon__isnull=False)
    if not include_events:
        rows = rows.filter(event=False)
    stage = stage_of(day_of(contact, on))
    best = None
    for t in rows.order_by("stage", "id"):
        c = t.coupon
        if c.status(on) != "live" or (c.plan_codes and plan not in c.plan_codes):
            continue
        if best is None or t.stage == stage:
            best = t
    return best


def next_yes_path(contact: WaContact, on: date) -> tuple[WaTemplate | None, str]:
    """(template, note) for a contact past their YES (the template's version B for this contact, while one runs)."""
    t, note = _next_yes_path(contact, on)
    return version(t, contact), note


def _next_yes_path(contact: WaContact, on: date) -> tuple[WaTemplate | None, str]:
    """(template, note) for a contact past their YES. note "set_track" = waiting for staff to set the Track."""
    if contact.yes_date is None or contact.campaign_status == CS.OPTED_OUT:
        return None, ""
    if contact.using_since and on >= contact.using_since + timedelta(days=TESTIMONIAL_AFTER):
        step = {"": 1, WaContact.Testimonial.WRITTEN: 2}.get(contact.testimonial)
        if step:
            t = _rows(Cat.TESTIMONIAL, step=step).order_by("id").first()
            if t:
                return t, ""
    if contact.campaign_status == CS.PAID:
        return None, ""
    track = contact.track
    if not track:
        if contact.engage_step >= DISCOVERY_STEPS:
            return None, "set_track"
        lean = suggest_track(_last_theirs(contact)) or Track.PRIVACY
        t = _rows(Cat.ENGAGE, track=lean, step__gt=contact.engage_step).order_by("step", "id").first()
        return t, "" if t else "set_track"
    for category, done in ((Cat.ENGAGE, contact.engage_step), (Cat.DEMO_VIDEO, contact.demo_step)):
        t = _rows(category, track=track, step__gt=done).order_by("step", "id").first()
        if t:
            return t, ""
    if contact.setup_error and not contact.demo_call_sent:
        t = _rows(Cat.DEMO_CALL).order_by("step", "id").first()
        if t:
            return t, ""
    t = _rows(Cat.SETUP, step__gt=contact.setup_step).order_by("step", "id").first()
    if t and contact.setup_step >= 1:
        # The nudge is only for a setup that stopped: no PC connected 3 days after the registration steps.
        last = contact.messages.filter(template__category=Cat.SETUP).order_by("at").last()
        if contact.using_since:
            t = None
        elif last and on < last.at.date() + timedelta(days=SETUP_NUDGE_AFTER):
            return None, ""  # give them three days to connect the first PC before the nudge
    if t:
        return t, ""
    if contact.offer_sent is None:
        return offer_for(contact, on), ""
    return None, ""


def next_for(contact: WaContact, on: date) -> tuple[WaTemplate | None, str]:
    if contact.yes_date is not None:
        return next_yes_path(contact, on)
    return next_outreach(contact, on), ""


def _sent_today(contact: WaContact, on: date) -> bool:
    return contact.messages.exclude(kind=WaMessage.Kind.THEIRS).filter(at__date=on).exists()


def queue(on: date, cold_room: dict[int, int]) -> list[dict[str, Any]]:
    """Today's campaign messages for "ninety" campaigns: the YES path first (no daily limit), then Intro within
    each campaign's daily limit (`cold_room`: campaign id -> sends left today)."""
    out: list[dict[str, Any]] = []
    base = (
        WaContact.objects.filter(campaign__plan=WaCampaign.Plan.NINETY, opted_in=True, needs_reply=False)
        .exclude(status=WaContact.Status.STOP).exclude(campaign_status=CS.OPTED_OUT)
        .exclude(paused_until__gt=on).select_related("campaign")
    )
    for contact in base.filter(yes_date__isnull=False).order_by("yes_date", "id"):
        if _sent_today(contact, on):
            continue
        template, note = next_yes_path(contact, on)
        if template is not None or note:
            out.append({"contact": contact, "kind": "flow", "template": template, "note": note})
    cold = base.filter(yes_date__isnull=True, campaign__status=WaCampaign.Status.LIVE).exclude(campaign_status=CS.NO_REPLY)
    for contact in cold.order_by("first_sent", "id"):
        room = cold_room.get(contact.campaign_id, 0)
        if room <= 0 or _sent_today(contact, on):
            continue
        template = next_outreach(contact, on)
        if template is None:
            if contact.first_sent and not _rows(Cat.OUTREACH, step__gt=contact.out_day).exists() and contact.campaign_status == CS.MESSAGED:
                contact.campaign_status = CS.NO_REPLY  # every Intro message went and nothing came back
                contact.save(update_fields=["campaign_status"])
            continue
        out.append({"contact": contact, "kind": "flow", "template": template, "note": ""})
        cold_room[contact.campaign_id] = room - 1
    return out


def record_send(contact: WaContact, template: WaTemplate, on: date) -> None:
    """What a sent campaign message changes on the contact (the caller saves it and stores the message)."""
    c = template.category
    if c == Cat.OUTREACH:
        if contact.first_sent is None:
            contact.first_sent = on
        contact.out_day = max(contact.out_day, template.step)
        contact.step = max(contact.step, 1)
        if contact.status == WaContact.Status.READY:
            contact.status = WaContact.Status.MESSAGED
    elif c == Cat.ENGAGE:
        contact.engage_step = max(contact.engage_step, template.step)
    elif c == Cat.DEMO_VIDEO:
        contact.demo_step = max(contact.demo_step, template.step)
    elif c == Cat.SETUP:
        contact.setup_step = max(contact.setup_step, template.step)
    elif c == Cat.DEMO_CALL:
        contact.demo_call_sent = True
    elif c == Cat.OFFER:
        contact.offer_sent = on
        if not contact.pack and template.coupon_id:
            plans = template.coupon.plan_codes or []
            contact.pack = WaContact.Pack.TEAM if plans == ["teams"] else (WaContact.Pack.STARTUP if plans == ["startups"] else "")
    elif c == Cat.TESTIMONIAL:
        contact.testimonial = WaContact.Testimonial.WRITTEN_ASKED if template.step <= 1 else WaContact.Testimonial.VIDEO_ASKED
    if c in STATUS_FOR:
        advance(contact, STATUS_FOR[c])
    contact.needs_reply = False  # any message from us answers them


def check_sendable(contact: WaContact, template: WaTemplate) -> None:
    if template.archived:
        raise FlowError("This template is archived. Pick another.")
    if is_draft(template):
        raise FlowError("This template is a draft: write its English and Hinglish text in Templates first.")
    if template.category in TRACKED and template.track and contact.track and template.track != contact.track:
        raise FlowError(f"This is for {template.get_track_display()}; this contact's Track is {contact.get_track_display()}.")
    if template.category == Cat.DEMO_VIDEO and not contact.track:
        raise FlowError("Set the contact's Track first: the Demo videos follow it.")
    if template.category == Cat.OFFER and template.coupon_id and template.coupon.status() != "live":
        raise FlowError(f"{template.coupon.code} is {template.coupon.status()}, not live. Pick the current code.")


def set_track(contact: WaContact, track: str) -> None:
    if track not in (Track.PRIVACY, Track.ACTIVITY, Track.BOTH):
        raise FlowError("Pick Data Privacy, Team Activity Monitoring or Both Pack.")
    contact.track = track
    advance(contact, CS.TRACK)


def performance() -> dict[int, dict[str, int]]:
    """Per template: sends, replies (they wrote within 3 days of it) and the step it is for (YES for Intro and
    Offer, Track set for Engage, a later step for the YES path, a testimonial for Testimonial)."""
    sends = list(
        WaMessage.objects.filter(template__isnull=False)
        .values("template_id", "template__category", "template__step", "contact_id", "at").order_by("at")
    )
    if not sends:
        return {}
    ids = {s["contact_id"] for s in sends}
    theirs: dict[int, list[Any]] = {}
    for m in WaMessage.objects.filter(contact_id__in=ids, kind=WaMessage.Kind.THEIRS).values("contact_id", "at"):
        theirs.setdefault(m["contact_id"], []).append(m["at"])
    contacts = {c.id: c for c in WaContact.objects.filter(pk__in=ids)}
    out: dict[int, dict[str, int]] = {}
    for s in sends:
        row = out.setdefault(s["template_id"], {"sends": 0, "replies": 0, "next": 0})
        row["sends"] += 1
        at, c = s["at"], contacts[s["contact_id"]]
        if any(at <= t <= at + timedelta(days=3) for t in theirs.get(c.id, [])):
            row["replies"] += 1
        row["next"] += int(took_next_step(s["template__category"], s["template__step"], c, at))
    return out


def took_next_step(cat: str, step: int, c: WaContact, at: Any) -> bool:
    """Did the contact take the step a message of this category is for, after it went out at `at`?"""
    if cat in (Cat.OUTREACH, Cat.OFFER):
        return c.yes_date is not None and c.yes_date >= at.date() if cat == Cat.OUTREACH else c.campaign_status == CS.PAID
    if cat == Cat.ENGAGE:
        return bool(c.track)
    if cat == Cat.TESTIMONIAL:
        return c.testimonial in ((WaContact.Testimonial.WRITTEN, WaContact.Testimonial.VIDEO_ASKED, WaContact.Testimonial.VIDEO) if step <= 1 else (WaContact.Testimonial.VIDEO,))
    if cat in STATUS_FOR:
        return c.campaign_status in PATH and PATH.index(c.campaign_status) > PATH.index(STATUS_FOR[cat])
    return False


def counts() -> dict[str, Any]:
    """For the Contacts tiles: the campaign path, the Tracks and the reviews."""
    contacts = WaContact.objects.all()
    by_status = {s: contacts.filter(campaign_status=s).count() for s in CS.values if s}
    yes = contacts.filter(yes_date__isnull=False)
    return {
        "status": by_status,
        "track": {t: yes.filter(track=t).count() for t in (Track.PRIVACY, Track.ACTIVITY, Track.BOTH)},
        "track_to_set": yes.filter(track="").exclude(campaign_status__in=[CS.PAID, CS.OPTED_OUT]).count(),
        "reviews_written": contacts.filter(testimonial__in=[WaContact.Testimonial.WRITTEN, WaContact.Testimonial.VIDEO_ASKED, WaContact.Testimonial.VIDEO]).count(),
        "reviews_video": contacts.filter(testimonial=WaContact.Testimonial.VIDEO).count(),
    }


def template_name(category: str, type_name: str, track: str, stage: int, step: int) -> str:
    """A readable unique name for a category row, when staff leave it blank."""
    bits = [Cat(category).label, type_name]
    if category in TRACKED and track:
        bits.append(Track(track).label)
    if category == Cat.OUTREACH:
        bits.append(f"day {step}")
    elif category == Cat.OFFER and stage:
        bits.append(f"stage {stage}")
    elif step:
        bits.append(f"step {step}")
    return " · ".join(b for b in bits if b)[:120]

