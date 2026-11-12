"""
The WhatsApp Analytics tab (wa4, 12 Nov 2026): one campaign (or all), one period, split six ways.

Counting rules, the same everywhere on the tab:
- A message is ours when it isn't THEIRS. It belongs to a campaign by its own `campaign` (path campaigns tag every
  send), else by the contact's campaign. So a contact messaged in two campaigns counts in each.
- A message got a reply when the contact wrote within 3 days of it (as the Templates page counts them).
- People: everyone whose first message from the campaign went out in the period; what they did next counts to today.
  "Since day 1" is everyone ever messaged. Said YES = a YES date, Track set = a Track, First PC = a first PC date,
  Paid = campaign status Paid, Asked to stop = stopped or opted out.
- Message splits (template, step, send slot, coupon code) and the daily bars count the messages sent in the period.
- Send slots are India time: 10-12, 12-14, 14-17, 17-19; anything else is "other".
- Coupon codes: shown to = people who got a message carrying the code; used = the code's pending or paid uses in the
  period, from any channel (a use isn't tied to a WhatsApp contact); PCs paid = the PCs the paid plans cover.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from . import wa_flow, wa_paths
from .models import WaCampaign, WaContact, WaMessage, WaTemplate
from .wa_templates import REPLY_WITHIN, rate

Kind = WaMessage.Kind
CS = WaContact.CampaignStatus
PERIODS = {"all": None, "30": 30, "7": 7}
SLOTS = (("10-12", 10, 12, "Morning"), ("12-14", 12, 14, "Lunch"), ("14-17", 14, 17, "Afternoon"), ("17-19", 17, 19, "Evening"))
LANGS = (("hi", "Hinglish"), ("en", "English"))
SEND_KINDS = ("send", "group")


class PeriodError(ValueError):
    pass


def since_of(period: str) -> datetime | None:
    """The start of the period (midnight India time, `days` days ago counting today), or None for since day 1."""
    if period not in PERIODS:
        raise PeriodError("Pick all, 30 or 7.")
    days = PERIODS[period]
    if days is None:
        return None
    start = timezone.localdate() - timedelta(days=days - 1)
    return timezone.make_aware(datetime.combine(start, datetime.min.time()))


def _local(dt: datetime) -> datetime:
    return timezone.localtime(dt)


@dataclass
class Scope:
    """Every message of the campaign (or of all campaigns), loaded once; the splits read from here."""

    campaign: WaCampaign | None
    since: datetime | None
    ours: list[dict[str, Any]] = field(default_factory=list)  # every message we sent, any time, in path order
    theirs: dict[int, list[datetime]] = field(default_factory=dict)
    contacts: dict[int, WaContact] = field(default_factory=dict)

    @classmethod
    def load(cls, campaign: WaCampaign | None, since: datetime | None) -> Scope:
        q = WaMessage.objects.exclude(kind=Kind.THEIRS)
        if campaign is not None:
            q = q.filter(Q(campaign=campaign) | Q(campaign__isnull=True, contact__campaign=campaign))
        ours = list(q.values("id", "contact_id", "kind", "template_id", "language", "step_id", "at").order_by("at", "id"))
        ids = {m["contact_id"] for m in ours}
        theirs: dict[int, list[datetime]] = defaultdict(list)
        for cid, at in WaMessage.objects.filter(contact_id__in=ids, kind=Kind.THEIRS).values_list("contact_id", "at").order_by("at"):
            theirs[cid].append(at)
        contacts = {c.pk: c for c in WaContact.objects.filter(pk__in=ids)}
        return cls(campaign, since, ours, dict(theirs), contacts)

    def in_period(self, at: datetime) -> bool:
        return self.since is None or at >= self.since

    @property
    def sent(self) -> list[dict[str, Any]]:
        """Our messages in the period."""
        return [m for m in self.ours if self.in_period(m["at"])]

    def first_reply(self, m: dict[str, Any]) -> datetime | None:
        """When they first wrote within 3 days of this message, or None."""
        at = m["at"]
        return next((t for t in self.theirs.get(m["contact_id"], []) if at <= t <= at + REPLY_WITHIN), None)

    def people(self) -> dict[int, WaContact]:
        """The people whose first message from the campaign went out in the period."""
        first: dict[int, datetime] = {}
        for m in self.ours:
            first.setdefault(m["contact_id"], m["at"])
        return {cid: self.contacts[cid] for cid, at in first.items() if self.in_period(at) and cid in self.contacts}

    def replied(self) -> set[int]:
        """People who wrote within 3 days of any of our messages."""
        return {m["contact_id"] for m in self.ours if self.first_reply(m) is not None}


# --- Outcomes --------------------------------------------------------------------------------------------------------


def said_yes(c: WaContact) -> bool:
    return c.yes_date is not None


def paid(c: WaContact) -> bool:
    return c.campaign_status == CS.PAID


def stopped(c: WaContact) -> bool:
    return c.status == WaContact.Status.STOP or c.campaign_status == CS.OPTED_OUT


def pcs_of(c: WaContact, plans: dict[str, int]) -> int:
    """The PCs the pack they paid for covers (the plan behind their pack, as the Offer picks it)."""
    return plans.get(wa_flow.PACK_PLAN.get(c.pack or wa_flow.default_pack(c), ""), 0)


def _plans() -> dict[str, int]:
    from z_auth.models import Plan

    return dict(Plan.objects.values_list("code", "max_devices"))


def kpis(s: Scope, people: dict[int, WaContact], replied: set[int]) -> dict[str, int]:
    plans = _plans()
    who = list(people.values())
    payers = [c for c in who if paid(c)]
    return {
        "people": len(who),
        "messages": len(s.sent),
        "replied": sum(1 for c in who if c.pk in replied),
        "yes": sum(1 for c in who if said_yes(c)),
        "track": sum(1 for c in who if c.track),
        "first_pc": sum(1 for c in who if c.using_since is not None),
        "paid": len(payers),
        "paid_pcs": sum(pcs_of(c, plans) for c in payers),
        "stopped": sum(1 for c in who if stopped(c)),
    }


def funnel(k: dict[str, int]) -> list[dict[str, Any]]:
    rows = (("messaged", "Messaged", "people"), ("replied", "Replied", "replied"), ("yes", "Said YES", "yes"),
            ("track", "Track set", "track"), ("first_pc", "First PC", "first_pc"), ("paid", "Paid", "paid"))
    return [{"key": key, "label": label, "people": k[src]} for key, label, src in rows]


def days(s: Scope, period: str) -> list[dict[str, Any]]:
    """Messages sent and replies each day (India dates): the last 7 days for "7", else the last 30."""
    n = 7 if period == "7" else 30
    start = timezone.localdate() - timedelta(days=n - 1)
    sent: Counter[date] = Counter(_local(m["at"]).date() for m in s.ours)
    got: Counter[date] = Counter(_local(t).date() for ts in s.theirs.values() for t in ts)
    out = []
    for i in range(n):
        d = start + timedelta(days=i)
        out.append({"date": d.isoformat(), "sent": sent.get(d, 0), "replies": got.get(d, 0)})
    return out


# --- Splits ----------------------------------------------------------------------------------------------------------


def _went_on(m: dict[str, Any], c: WaContact, templates: dict[int, WaTemplate]) -> bool:
    t = templates.get(m["template_id"]) if m["template_id"] else None
    return bool(t and t.category and wa_flow.took_next_step(t.category, t.step, c, m["at"]))


def _templates(ids: set[int]) -> dict[int, WaTemplate]:
    return {t.pk: t for t in WaTemplate.objects.filter(pk__in=ids).select_related("coupon")}


def by_template(s: Scope) -> list[dict[str, Any]]:
    """Each template sent in the period: sends, replies, went on (took the step it is for) and the languages."""
    sent = [m for m in s.sent if m["template_id"]]
    ts = _templates({m["template_id"] for m in sent})
    rows: dict[int, dict[str, Any]] = {}
    for m in sent:
        t = ts.get(m["template_id"])
        c = s.contacts.get(m["contact_id"])
        if t is None or c is None:
            continue
        r = rows.setdefault(t.pk, {
            "id": t.pk, "name": t.name, "category": t.category, "type_name": t.type_name,
            "version": "B" if t.version_of_id else "A", "version_of": t.version_of_id, "archived": t.archived,
            "sends": 0, "replies": 0, "went_on": 0, "languages": set(),
        })
        r["sends"] += 1
        r["replies"] += int(s.first_reply(m) is not None)
        r["went_on"] += int(_went_on(m, c, ts))
        lang = m["language"] or ("" if t.category else t.language) or c.lang or "en"
        r["languages"].add(lang if lang in ("en", "hi") else "en")
    out = []
    for r in rows.values():
        r["languages"] = sorted(r["languages"])
        r["reply_rate"] = rate(r["replies"], r["sends"])
        out.append(r)
    out.sort(key=lambda r: (-r["reply_rate"], -r["sends"], r["id"]))
    return out


def _step_counts(s: Scope) -> dict[str, dict[str, int]]:
    """{step id: {sent, replies, reply_rate, went_on}}: the messages each path step sent in the period."""
    sent = [m for m in s.sent if m["step_id"]]
    ts = _templates({m["template_id"] for m in sent if m["template_id"]})
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"sent": 0, "replies": 0, "reply_rate": 0, "went_on": 0})
    for m in sent:
        n = out[m["step_id"]]
        n["sent"] += 1
        n["replies"] += int(s.first_reply(m) is not None)
        contact = s.contacts.get(m["contact_id"])
        n["went_on"] += int(contact is not None and _went_on(m, contact, ts))
    for n in out.values():
        n["reply_rate"] = rate(n["replies"], n["sent"])
    return dict(out)


def step_numbers(c: WaCampaign, since: datetime | None) -> dict[str, dict[str, int]]:
    """For GET /analytics/steps/: each step's numbers in the period."""
    return _step_counts(Scope.load(c, since))


_DAY = re.compile(r"^Day (\d+) · ")
BLANK = {"sent": 0, "replies": 0, "reply_rate": 0, "went_on": 0}


def by_step(s: Scope) -> list[dict[str, Any]] | None:
    """A path campaign's message steps in path order: got it (messages the step sent in the period), replies, went on,
    and people in the step now or waiting right after it. None for all campaigns or a slots campaign."""
    c = s.campaign
    if c is None or c.engine != WaCampaign.Engine.PATH:
        return None
    counts = _step_counts(s)
    out: list[dict[str, Any]] = []
    for r in wa_paths.steps_view(c):
        if r["k"] not in SEND_KINDS:
            if out and out[-1]["lane"] == r["lane"]:
                out[-1]["here"] += r.get("here", 0)  # waiting after a message counts at that message
            continue
        hit = _DAY.match(r["label"])
        out.append({
            "step_id": r["step_id"], "lane": r["lane"], "k": r["k"], "label": r["label"],
            "title": r["label"][hit.end():] if hit else r["label"], "day": int(hit.group(1)) if hit else None,
            "here": r.get("here", 0), **counts.get(r["step_id"], BLANK),
        })
    return out


def by_segment(people: dict[int, WaContact], replied: set[int]) -> list[dict[str, Any]]:
    groups: dict[str, list[WaContact]] = defaultdict(list)
    for c in people.values():
        groups[c.segment or "No segment"].append(c)
    out = []
    for seg, cs in groups.items():
        cities = [city for city, _ in Counter(c.city for c in cs if c.city).most_common(3)]
        out.append({
            "segment": seg, "cities": cities, "people": len(cs), "replied": sum(1 for c in cs if c.pk in replied),
            "yes": sum(1 for c in cs if said_yes(c)), "paid": sum(1 for c in cs if paid(c)),
        })
    out.sort(key=lambda r: (-r["people"], r["segment"]))
    return out


def by_language(s: Scope, people: dict[int, WaContact], replied: set[int]) -> list[dict[str, Any]]:
    """By the language of each person's first message: their own `lang`, else the first message's."""
    first_lang: dict[int, str] = {}
    for m in s.ours:
        first_lang.setdefault(m["contact_id"], m["language"])
    groups: dict[str, list[WaContact]] = defaultdict(list)
    for c in people.values():
        lang = c.lang or first_lang.get(c.pk) or "en"
        groups[lang if lang in ("en", "hi") else "en"].append(c)
    msgs = Counter()
    for m in s.sent:
        c = s.contacts.get(m["contact_id"])
        if c is not None and c.pk in people:
            lang = c.lang or first_lang.get(c.pk) or "en"
            msgs[lang if lang in ("en", "hi") else "en"] += 1
    return [{
        "language": code, "label": label, "people": len(groups[code]), "messages": msgs[code],
        "replied": sum(1 for c in groups[code] if c.pk in replied), "yes": sum(1 for c in groups[code] if said_yes(c)),
        "stopped": sum(1 for c in groups[code] if stopped(c)),
    } for code, label in LANGS]


def slot_of(at: datetime) -> str:
    h = _local(at).hour
    return next((key for key, a, b, _ in SLOTS if a <= h < b), "other")


def by_slot(s: Scope) -> list[dict[str, Any]]:
    """Our messages in the period by the India-time slot they went out in: replied within 3 days, replied the same day,
    and the average time to the first reply (seconds, over the messages that got one)."""
    rows = {key: {"slot": key, "label": key.replace("-", "–"), "name": name, "sent": 0, "replied": 0, "same_day": 0, "_waits": []}
            for key, _, _, name in SLOTS}
    rows["other"] = {"slot": "other", "label": "Other times", "name": "Outside the slots", "sent": 0, "replied": 0, "same_day": 0, "_waits": []}
    for m in s.sent:
        r = rows[slot_of(m["at"])]
        r["sent"] += 1
        first = s.first_reply(m)
        if first is not None:
            r["replied"] += 1
            r["_waits"].append((first - m["at"]).total_seconds())
            r["same_day"] += int(_local(first).date() == _local(m["at"]).date())
    out = []
    for key, r in rows.items():
        waits = r.pop("_waits")
        if key == "other" and not r["sent"]:
            continue
        r["first_reply_secs"] = round(sum(waits) / len(waits)) if waits else None
        out.append(r)
    return out


def by_code(s: Scope) -> list[dict[str, Any]]:
    """Every coupon code a WhatsApp template carries (or a WhatsApp code), with who saw it and what it brought."""
    from z_auth.models import Coupon, CouponUse

    carriers = list(WaTemplate.objects.filter(coupon__isnull=False).order_by("archived", "stage", "id"))
    by_coupon: dict[int, WaTemplate] = {}
    for t in carriers:
        by_coupon.setdefault(t.coupon_id, t)
    coupons = Coupon.objects.filter(Q(pk__in=by_coupon) | Q(channel=Coupon.Channel.WHATSAPP)).order_by("starts_on", "code")
    template_coupon = {t.pk: t.coupon_id for t in carriers}
    shown: dict[int, set[int]] = defaultdict(set)
    replied: dict[int, set[int]] = defaultdict(set)
    for m in s.sent:
        cid = template_coupon.get(m["template_id"])
        if cid is None:
            continue
        shown[cid].add(m["contact_id"])
        if s.first_reply(m) is not None:
            replied[cid].add(m["contact_id"])
    plans = _plans()
    uses = CouponUse.objects.filter(coupon__in=coupons, state__in=[CouponUse.State.PENDING, CouponUse.State.CONFIRMED])
    if s.since is not None:
        uses = uses.filter(created_at__gte=s.since)
    used: Counter[int] = Counter()
    pcs: Counter[int] = Counter()
    for coupon_id, state, plan_code in uses.values_list("coupon_id", "state", "plan_code"):
        used[coupon_id] += 1
        if state == CouponUse.State.CONFIRMED:
            pcs[coupon_id] += plans.get(plan_code, 0)
    today = timezone.localdate()
    out = []
    for c in coupons:
        t = by_coupon.get(c.pk)
        out.append({
            "code": c.code, "percent_off": c.percent_off, "status": c.status(today),
            "pack": t.type_name if t else "", "stage": t.stage if t else 0, "event": bool(t and t.event),
            "starts_on": c.starts_on.isoformat(), "ends_on": c.ends_on.isoformat() if c.ends_on else None,
            "shown_to": len(shown[c.pk]), "replied": len(replied[c.pk]), "used": used[c.pk], "pcs_paid": pcs[c.pk],
        })
    return out


def campaign_rows(since: datetime | None) -> list[dict[str, Any]]:
    """The menu: each campaign's people and how many replied, in the period."""
    out = []
    for c in WaCampaign.objects.all():
        s = Scope.load(c, since)
        people = s.people()
        replied = s.replied()
        out.append({"id": c.pk, "name": c.name, "status": c.status, "engine": c.engine,
                    "people": len(people), "replied": sum(1 for cid in people if cid in replied)})
    return out


def view(campaign: WaCampaign | None, period: str) -> dict[str, Any]:
    since = since_of(period)
    s = Scope.load(campaign, since)
    people = s.people()
    replied = s.replied()
    k = kpis(s, people, replied)
    return {
        "campaign": campaign.pk if campaign else None, "period": period,
        "since": _local(since).date().isoformat() if since else None, "today": timezone.localdate().isoformat(),
        "kpis": k, "funnel": funnel(k), "days": days(s, period), "campaigns": campaign_rows(since),
        "steps": by_step(s), "templates": by_template(s), "segments": by_segment(people, replied),
        "languages": by_language(s, people, replied), "slots": by_slot(s), "codes": by_code(s),
    }
