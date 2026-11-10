"""
The WhatsApp Templates page (owner, 10 Nov 2026): numbers per template, A/B versions, reach and duplicates.

- template_stats(): per template over the last 30 days, from its WaMessage sends: sends, replies (they wrote within
  3 days of it, as wa_flow.performance counts them), the next step it is for, and the same split by language.
- Versions: a version B row has `version_of` = A and the same category, type and step. While A has an un-archived B
  that is written in both languages, odd-numbered contacts get B and the rest A (wa_flow.version). Keep A archives B;
  Keep B copies B's texts into A and archives B. Both rows keep their message history.
- reach(): how many messageable contacts each detail is known for, by the same rule fill() uses.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from . import wa_conditions, wa_flow
from .models import WaBlock, WaContact, WaMessage, WaTemplate

WINDOW_DAYS = 30
REPLY_WITHIN = timedelta(days=3)
LANGS = ("en", "hi")
STAT_KEYS = ("sends", "replies", "reply_rate", "next", "next_rate",
             "sends_en", "replies_en", "reply_rate_en", "sends_hi", "replies_hi", "reply_rate_hi")
# Fields a version B shares with A, kept in step when A changes.
SHARED = ("kind", "category", "type_name", "track", "stage", "step", "day", "coupon", "event")


class VersionError(Exception):
    """Refused, with a message for staff."""


def rate(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def blank_stats() -> dict[str, int]:
    return dict.fromkeys(STAT_KEYS, 0)


def template_stats(ids: Iterable[int] | None = None, days: int = WINDOW_DAYS) -> dict[int, dict[str, int]]:
    """{template id: numbers} over the last `days` days. Templates with no sends are left out (use blank_stats())."""
    since = timezone.now() - timedelta(days=days)
    q = WaMessage.objects.filter(template__isnull=False, at__gte=since).exclude(kind=WaMessage.Kind.THEIRS)
    if ids is not None:
        q = q.filter(template_id__in=list(ids))
    sends = list(q.values("template_id", "template__category", "template__step", "template__language", "contact_id", "at",
                          "language").order_by("at", "id"))
    if not sends:
        return {}
    contact_ids = {s["contact_id"] for s in sends}
    theirs: dict[int, list[Any]] = {}
    for m in WaMessage.objects.filter(contact_id__in=contact_ids, kind=WaMessage.Kind.THEIRS, at__gte=since).values("contact_id", "at"):
        theirs.setdefault(m["contact_id"], []).append(m["at"])
    contacts = {c.id: c for c in WaContact.objects.filter(pk__in=contact_ids)}
    out: dict[int, dict[str, int]] = {}
    for s in sends:
        c = contacts[s["contact_id"]]
        row = out.setdefault(s["template_id"], blank_stats())
        at = s["at"]
        replied = any(at <= t <= at + REPLY_WITHIN for t in theirs.get(c.id, []))
        # The language it went out in; older messages: an older template's own language, else the contact's.
        lang = s["language"] or ("" if s["template__category"] else s["template__language"]) or c.lang or "en"
        lang = lang if lang in LANGS else "en"
        row["sends"] += 1
        row["replies"] += int(replied)
        row[f"sends_{lang}"] += 1
        row[f"replies_{lang}"] += int(replied)
        row["next"] += int(wa_flow.took_next_step(s["template__category"], s["template__step"], c, at))
    for row in out.values():
        row["reply_rate"] = rate(row["replies"], row["sends"])
        row["next_rate"] = rate(row["next"], row["sends"])
        for lang in LANGS:
            row[f"reply_rate_{lang}"] = rate(row[f"replies_{lang}"], row[f"sends_{lang}"])
    return out


def active_versions() -> dict[int, WaTemplate]:
    """{A's id: its running version B}, for the template list."""
    out: dict[int, WaTemplate] = {}
    for b in WaTemplate.objects.filter(version_of__isnull=False, archived=False).order_by("id"):
        out.setdefault(b.version_of_id, b)
    return out


@transaction.atomic
def add_version(a: WaTemplate, text: str, text_hi: str) -> WaTemplate:
    """Version B of a campaign template. Only one B at a time."""
    a = WaTemplate.objects.select_for_update().get(pk=a.pk)
    if a.version_of_id:
        raise VersionError("This is a version B. Keep A or B on the main template first.")
    if not a.category:
        raise VersionError("Versions are for campaign templates. Older templates can't be A/B tested.")
    if a.archived:
        raise VersionError("This template is archived. Bring it back first.")
    if wa_flow.active_b(a) is not None:
        raise VersionError("This template already has a version B. Keep A or B first.")
    b = WaTemplate(name=f"{a.name} · version B"[:120], text=text, text_hi=text_hi, language=a.language, version_of=a)
    for field in SHARED:
        setattr(b, field, getattr(a, field))
    b.save()
    return b


@transaction.atomic
def keep(a: WaTemplate, which: str) -> WaTemplate:
    """End the A/B test. B: A takes B's texts. Either way B is archived; both keep their messages."""
    a = WaTemplate.objects.select_for_update().get(pk=a.pk)
    b = wa_flow.active_b(a)
    if b is None:
        raise VersionError("This template has no version B running.")
    if which == "B":
        if wa_flow.is_draft(b):
            raise VersionError("Version B is a draft: write its English and Hinglish text first.")
        a.text, a.text_hi = b.text, b.text_hi
        a.save(update_fields=["text", "text_hi", "updated_at"])
    b.archived = True
    b.save(update_fields=["archived", "updated_at"])
    return a


def sync_versions(a: WaTemplate) -> None:
    """After A changes: its running B keeps the same category, type, Track and step."""
    if a.version_of_id is None:
        WaTemplate.objects.filter(version_of=a, archived=False).update(**{f: getattr(a, f) for f in SHARED})


@transaction.atomic
def duplicate(t: WaTemplate) -> WaTemplate:
    """A copy to edit: same texts, category, step and tags; no picture or video, no twin, not archived."""
    source = t.version_of if t.version_of_id else t
    copy = WaTemplate(
        name=f"{t.name.replace(' · version B', '')} (copy)"[:120], text=t.text, text_hi=t.text_hi, language=t.language,
        parts=t.parts, **{f: getattr(t, f) for f in SHARED},
    )
    copy.save()
    copy.tags.set(source.tags.all())
    return copy


def reach() -> dict[str, Any]:
    """{total, known: {detail: count}, examples}: over contacts who can be messaged (not opted out, not blocked).
    examples: a contact id with a first name, one without (preferring one with an owner name), one with neither."""
    stopped = set(WaBlock.objects.values_list("phone", flat=True))
    rows = (WaContact.objects.exclude(status=WaContact.Status.STOP).exclude(campaign_status=WaContact.CampaignStatus.OPTED_OUT)
            .order_by("id"))
    known = dict.fromkeys(wa_conditions.DETAILS, 0)
    total = 0
    examples: dict[str, int | None] = {"first_name": None, "no_first_name": None, "no_names": None}
    loose_no_first: int | None = None
    for c in rows:
        if c.phone in stopped:
            continue
        total += 1
        k = wa_conditions.known(c)
        for d, yes in k.items():
            known[d] += int(yes)
        if k["FirstName"]:
            examples["first_name"] = examples["first_name"] or c.pk
        elif k["OwnerName"]:
            examples["no_first_name"] = examples["no_first_name"] or c.pk
        else:
            loose_no_first = loose_no_first or c.pk
            examples["no_names"] = examples["no_names"] or c.pk
    examples["no_first_name"] = examples["no_first_name"] or loose_no_first
    return {"total": total, "known": known, "examples": examples}
