"""
Moving the old ("slots") campaigns onto step-by-step paths, safely (`manage.py wa_paths`).

For each slots campaign: build its path (plan "ninety" -> the 100 Reviews path as it runs today, the touches plan ->
the Short path made of the campaign's own messages), place every contact on the step the slots code would send them
next, then ask both engines, per contact, for the next message and its due day. A campaign switches to "path" only
when every contact gets the same message on the same day either way.

Both answers ignore what both engines gate the same way (one message a day, the daily limit, a reply waiting for an
answer, a contact's 30-day pause). One gate differs: on a paused or draft campaign the slots code still sends the YES
path and a path campaign sends nothing, so such a contact counts as a difference.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from . import wa_flow, wa_path_model, wa_paths
from .models import WaCampaign, WaContact, WaMessage, WaTemplate
from .wa_path_model import index, lanes, plan_days
from .wa_paths import Ctx, a_id, at_open, first_due, local

Cat = WaTemplate.Category
CS = WaContact.CampaignStatus
Status = WaContact.Status
NEXT_KIND = {0: "first", 1: "followup", 2: "touch3", 3: "touch4"}
LOOK_AHEAD = 60  # days


class Unplaceable(Exception):
    """The path has no step for what the slots code would send this contact next."""


def build(campaign: WaCampaign) -> dict[str, Any]:
    if campaign.plan == WaCampaign.Plan.NINETY:
        return wa_path_model.converted_ninety()
    return wa_path_model.converted_touches(campaign)


# --- What the slots code sends next --------------------------------------------------------------------------------


def slots_next(contact: WaContact, on: date) -> tuple[Any, date] | None:
    """(template id or "set_track", day) the slots code queues next for this contact, ignoring the shared gates."""
    c = contact.campaign
    if wa_paths.stopped(contact):
        return None
    if c.plan == WaCampaign.Plan.NINETY:
        if contact.yes_date is not None:
            for d in range(LOOK_AHEAD + 1):
                t, note = wa_flow._next_yes_path(contact, on + timedelta(days=d))
                if t is not None:
                    return a_id(t), on + timedelta(days=d)
                if note:
                    return "set_track", on + timedelta(days=d)
            return None
        if contact.campaign_status == CS.NO_REPLY:
            return None
        rows = list(wa_flow._rows(Cat.OUTREACH, step__gt=contact.out_day).order_by("step", "id"))
        if not rows:
            return None
        if contact.first_sent is None:
            return rows[0].pk, on
        day = wa_flow.day_of(contact, on)
        due = [r for r in rows if r.step <= day]
        if due:
            return due[-1].pk, on
        return rows[0].pk, max(on, contact.first_sent + timedelta(days=rows[0].step - 1))
    # touches
    if contact.yes_date is not None and contact.status in (Status.INTERESTED, Status.SIGNED):
        from .whatsapp import next_nurture

        t = next_nurture(contact)
        return (t.pk, max(on, contact.yes_date + timedelta(days=t.day))) if t else None
    if contact.status == Status.READY and contact.step == 0:
        return c.first_id, max(on, contact.next_date or on)
    if contact.status == Status.MESSAGED and contact.step in (1, 2, 3) and contact.next_date:
        tid = getattr(c, f"{NEXT_KIND[contact.step]}_id")
        return (tid, max(on, contact.next_date)) if tid else None
    return None


# --- Placing a contact ---------------------------------------------------------------------------------------------


@dataclass
class Place:
    lane: str
    step_id: str
    due: datetime | None
    state: dict[str, Any] = field(default_factory=dict)


def _on(d: date) -> datetime:
    return at_open(d)


def _end_of(path: dict[str, Any], lane: str) -> dict[str, Any] | None:
    steps = lanes(path)[lane]
    return next((s for s in reversed(steps) if s["k"] == "end"), steps[-1] if steps else None)


def _ended(path: dict[str, Any], lane: str, campaign: WaCampaign, now: datetime) -> Place:
    end = _end_of(path, lane)
    return Place(lane, end["id"] if end else "", None, {"c": campaign.pk, "ended": end["id"] if end else "", "finished": True,
                                                        "lane_at": now.isoformat()})


def _send_step(path: dict[str, Any], lane: str, template_id: int) -> dict[str, Any] | None:
    for s in lanes(path)[lane]:
        if s["k"] == "send" and s.get("template") == template_id:
            return s
    return None


def place(campaign: WaCampaign, path: dict[str, Any], contact: WaContact, now: datetime) -> Place:
    """The step the slots code has this contact on, as a path position. Raises Unplaceable."""
    if wa_paths.stopped(contact):
        return _ended(path, "stop", campaign, now)
    today = local(now).date()
    nxt = slots_next(contact, today)
    if campaign.plan == WaCampaign.Plan.NINETY:
        if contact.yes_date is not None:
            return _place_ninety_yes(campaign, path, contact, nxt, now)
        return _place_ninety_main(campaign, path, contact, nxt, now)
    return _place_touches(campaign, path, contact, nxt, now)


def _state(campaign: WaCampaign, cal: bool, lane_at: datetime | None = None, **extra: Any) -> dict[str, Any]:
    out = {"c": campaign.pk, "cal": cal, "win": False, "am": False, **extra}
    if lane_at:
        out["lane_at"] = lane_at.isoformat()
    return out


def _place_ninety_main(campaign: WaCampaign, path: dict[str, Any], contact: WaContact, nxt: Any, now: datetime) -> Place:
    main = lanes(path)["main"]
    if nxt is None:
        if contact.first_sent is None and contact.campaign_status != CS.NO_REPLY:
            return Place("main", main[0]["id"], now, _state(campaign, False))
        return _ended(path, "main", campaign, now)
    tid, _ = nxt
    t = WaTemplate.objects.get(pk=tid)
    first = contact.first_sent
    planned = _on(first + timedelta(days=max(t.step, 1) - 1)) if first else now
    step = _send_step(path, "main", tid)
    if step is not None:
        return Place("main", step["id"], planned, _state(campaign, first is not None))
    group = next((s for s in main if s["k"] == "group" and s["category"] == Cat.OUTREACH), None)
    if group is None or first is None:
        raise Unplaceable(f"{wa_flow.label(t)} isn't a step of the path")
    days = plan_days(path)[group["id"]]
    explicit = [WaTemplate.objects.get(pk=s["template"]).step for s in main if s["k"] == "send" and s.get("template")]
    before = max(explicit or [0])
    rows = list(wa_flow._rows(Cat.OUTREACH).order_by("step", "id"))
    done = sum(1 for r in rows if before < r.step <= contact.out_day)
    if done >= group["count"]:
        raise Unplaceable(f"{wa_flow.label(t)} is past the group's {group['count']} messages")
    due = _on(first + timedelta(days=days["day"] + group["every"] * done - 1))
    last = due - timedelta(days=group["every"])
    g = {"id": group["id"], "cursor": max(contact.out_day, before), "done": done, "last": last.isoformat()}
    return Place("main", group["id"], due, _state(campaign, True, g=g))


def _place_ninety_yes(campaign: WaCampaign, path: dict[str, Any], contact: WaContact, nxt: Any, now: datetime) -> Place:
    yes = {s["id"]: s for s in lanes(path)["yes"]}
    lane_at = at_open(contact.yes_date)
    if nxt is None:
        if contact.using_since is not None or contact.testimonial:
            return _ended(path, "yes", campaign, now)
        if contact.campaign_status == CS.PAID or contact.offer_sent:
            return Place("yes", "y9", now, _state(campaign, False, lane_at))
        return Place("yes", "y8", now, _state(campaign, False, lane_at))
    key, d = nxt
    due = _on(d)
    if key == "set_track":
        return Place("yes", "y2", due, _state(campaign, False, lane_at))
    t = WaTemplate.objects.get(pk=key)
    cat = t.category

    def group(sid: str, cursor: int, done: int) -> Place:
        g = {"id": sid, "cursor": cursor, "done": done, "last": (due - timedelta(days=1)).isoformat()}
        return Place("yes", sid, due, _state(campaign, True, lane_at, g=g))

    if cat == Cat.ENGAGE:
        n = contact.engage_step
        if n < 2:
            return group("y1", n, n)
        return group("y3", n, n - 2)
    if cat == Cat.DEMO_VIDEO:
        return group("y4", contact.demo_step, contact.demo_step)
    if cat == Cat.SETUP:
        return Place("yes", "y5" if t.step <= 1 else "y7", due, _state(campaign, True, lane_at))
    if cat == Cat.OFFER and "y8" in yes:
        return Place("yes", "y8", due, _state(campaign, True, lane_at))
    if cat == Cat.TESTIMONIAL and t.step <= 1 and "y11" in yes:
        return Place("yes", "y11", due, _state(campaign, True, lane_at))
    raise Unplaceable(f"{wa_flow.label(t)} isn't a step of the YES path")


def _place_touches(campaign: WaCampaign, path: dict[str, Any], contact: WaContact, nxt: Any, now: datetime) -> Place:
    main = lanes(path)["main"]
    if contact.yes_date is not None and contact.status in (Status.INTERESTED, Status.SIGNED):
        lane_at = at_open(contact.yes_date)
        if nxt is None:
            return _ended(path, "yes", campaign, now)
        step = _send_step(path, "yes", nxt[0])
        if step is None:
            raise Unplaceable("that after-YES message isn't a step of the YES path")
        return Place("yes", step["id"], _on(nxt[1]), _state(campaign, True, lane_at))
    if contact.yes_date is not None:
        return _ended(path, "yes", campaign, now)
    if contact.status == Status.NOTNOW:
        return _ended(path, "notnow", campaign, now)
    if contact.status == Status.READY and contact.step == 0:
        due = _on(contact.next_date) if contact.next_date else now
        return Place("main", "m1", due, _state(campaign, False))
    if contact.status == Status.REPLIED or (contact.needs_reply and contact.step):
        # They answered: the touches stopped. They wait at the answer step after their last message.
        reply = next((s for s in main if s["k"] == "reply" and s["id"] == f"r{contact.step + 1}"), None)
        if reply is None:
            return _ended(path, "main", campaign, now)
        return Place("main", reply["id"], now, _state(campaign, True, answered=True))
    if nxt is not None and contact.status == Status.MESSAGED:
        step = _send_step(path, "main", nxt[0])
        if step is None:
            raise Unplaceable("that touch isn't a step of the path")
        return Place("main", step["id"], _on(nxt[1]), _state(campaign, True))
    return _ended(path, "main", campaign, now)


# --- The check -----------------------------------------------------------------------------------------------------


@dataclass
class Row:
    contact: WaContact
    slots: Any
    path: Any
    place: Place | None
    problem: str = ""

    @property
    def same(self) -> bool:
        return not self.problem and self.slots == self.path


@dataclass
class Report:
    campaign: WaCampaign
    path: dict[str, Any]
    rows: list[Row]

    @property
    def differences(self) -> list[Row]:
        return [r for r in self.rows if not r.same]


def _virtual(contact: WaContact, campaign: WaCampaign, path: dict[str, Any], p: Place) -> WaContact:
    """The contact as the path engine would see it after placement (never saved)."""
    v = WaContact.objects.get(pk=contact.pk)
    v.campaign = campaign
    v.lane, v.step_id, v.step_due, v.step_entered, v.path_state = p.lane, p.step_id, p.due, None, p.state
    return v


def check(campaign: WaCampaign, now: datetime | None = None) -> Report:
    """Build, place and compare, writing nothing."""
    now = now or timezone.now()
    today = local(now).date()
    path = wa_path_model.clean(build(campaign))
    rows: list[Row] = []
    for contact in campaign.contacts.select_related("campaign").order_by("id"):
        s = slots_next(contact, today)
        try:
            p = place(campaign, path, contact, now)
        except Unplaceable as exc:
            rows.append(Row(contact, s, None, None, f"can't place: {exc}"))
            continue
        v = _virtual(contact, campaign, path, p)
        got = first_due(campaign, v, now, path=path)
        row = Row(contact, s, got, p)
        if (campaign.status != WaCampaign.Status.LIVE and campaign.plan == WaCampaign.Plan.NINETY
                and contact.yes_date is not None and s is not None and not wa_paths.stopped(contact)):
            row.problem = f"the campaign is {campaign.status}: the slots code still sends the YES path, a path campaign doesn't"
        rows.append(row)
    return Report(campaign, path, rows)


def describe(value: Any) -> str:
    if value is None:
        return "nothing"
    key, d = value
    if key == "set_track":
        return f"set the Track, {d:%a %d %b}"
    t = WaTemplate.objects.filter(pk=key).first()
    name = (wa_flow.label(t) if t and t.category else t.name) if t else f"template {key}"
    return f"{name}, {d:%a %d %b}"


@transaction.atomic
def convert(report: Report, now: datetime | None = None) -> int:
    """Switch a campaign with no differences to the path engine: the path, every contact's position, and the step of
    the messages it already sent (for the numbers per step). Returns the contacts placed."""
    if report.differences:
        raise ValueError("This campaign has differences; it stays on slots.")
    now = now or timezone.now()
    c = WaCampaign.objects.select_for_update().get(pk=report.campaign.pk)
    c.path, c.engine = report.path, WaCampaign.Engine.PATH
    c.save(update_fields=["path", "engine"])
    for row in report.rows:
        p = row.place
        WaContact.objects.filter(pk=row.contact.pk).update(lane=p.lane, step_id=p.step_id, step_due=p.due, step_entered=now,
                                                           path_state=p.state)
    backfill(c)
    return len(report.rows)


def backfill(c: WaCampaign) -> int:
    """Tag the campaign's earlier messages with the step that sends that template now (best effort, numbers only)."""
    by_template: dict[int, tuple[str, str]] = {}
    groups: dict[str, tuple[str, str]] = {}
    for lane, steps in lanes(c.path).items():
        for s in steps:
            if s["k"] == "send" and s.get("template"):
                by_template.setdefault(s["template"], (lane, s["id"]))
            elif s["k"] == "group":
                groups.setdefault(s["category"], (lane, s["id"]))
    n = 0
    msgs = WaMessage.objects.filter(contact__campaign=c, step_id="", template__isnull=False).exclude(kind=WaMessage.Kind.THEIRS).select_related("template")
    for m in msgs:
        t = m.template
        hit = by_template.get(a_id(t)) or groups.get(t.category)
        if hit:
            WaMessage.objects.filter(pk=m.pk).update(campaign=c, lane=hit[0], step_id=hit[1])
            n += 1
    return n


def revert(c: WaCampaign) -> None:
    """Back on slots. The path and the positions stay (unused), so a later --convert starts from a fresh check."""
    c.engine = WaCampaign.Engine.SLOTS
    c.save(update_fields=["engine"])


__all__ = ["Report", "Row", "Unplaceable", "build", "check", "convert", "describe", "place", "revert", "slots_next", "index", "Ctx"]
