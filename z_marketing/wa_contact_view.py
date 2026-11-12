"""
The Contacts tab (owner's board, 12 Nov 2026): for each contact, where they are on the path, what goes next and when,
what needs staff, and their last message; the counts for the list's menu; and one contact's path as done / now / next.

Everything here reads; nothing moves a contact or sends a message. Path campaigns (wa_paths.py) are walked with a
simulation (sim=True never writes). Old "slots" campaigns (whatsapp.py, wa_flow.py) get what their own rules say
honestly, and a blank where nothing is known.

    where  {key, label, sub}   key: intro | replied | yes | notnow | paid | stop | locked | none
    need   "reply" | "track" | "optin" | ""
    next   {state, label, when, slot, send_kind}
           state: due (today) | planned (on `when`) | hold (waits for something: label says what) | reply (answer
           them on Today) | paused (the contact or the campaign) | never (stopped) | locked (no opt-in) | done (the
           path ended) | none (no campaign)
    last_message  {text, at, direction: in | out, by} or None
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from . import wa_flow, wa_path_model, wa_paths, whatsapp
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTemplate

Status = WaContact.Status
CS = WaContact.CampaignStatus
Cat = WaTemplate.Category
PATH = WaCampaign.Engine.PATH
WHERE_KEYS = ("intro", "replied", "yes", "notnow", "paid", "stop", "locked", "none")
HOLD_WORDS = {
    "set_track": "Set the Track",
    "goal": "",  # the goal step's own title
    "answered": "Sort their reply on Today",
    "no_code": "Waits for a live offer code",
    "no_path": "The campaign has no path yet",
    "no_step": "Their step was removed",
    "loop": "The path loops: check it in Campaigns",
}
UPCOMING = 8  # next steps shown in a contact's path; the rest are summed up


def rel_day(d: date | None, today: date) -> str:
    """today / yesterday / tomorrow / Thu 12 Nov."""
    if d is None:
        return ""
    gap = (d - today).days
    if gap == 0:
        return "today"
    if gap == -1:
        return "yesterday"
    if gap == 1:
        return "tomorrow"
    return wa_paths.day_text(d)


def _messages(c: WaContact) -> list[WaMessage]:
    """The contact's messages, oldest first (the list view prefetches them, so this costs no query there)."""
    return list(c.messages.all())


def _tlabel(t: WaTemplate | None) -> str:
    if t is None:
        return ""
    a = t.version_of if t.version_of_id else t
    return wa_flow.label(a) if a.category else a.name


def stopped(c: WaContact) -> bool:
    return wa_paths.stopped(c)


def is_yes(c: WaContact) -> bool:
    return c.yes_date is not None or c.status in (Status.INTERESTED, Status.SIGNED) or c.lane == "yes"


def track_to_set(c: WaContact) -> bool:
    """Said YES on a campaign that uses a Track (the 90-day plan, or a path with a Set the Track step), and the Track
    isn't set yet."""
    if c.track or c.yes_date is None or c.campaign_status in (CS.PAID, CS.OPTED_OUT) or stopped(c) or not c.campaign_id:
        return False
    camp = c.campaign
    if camp.engine == PATH:
        return any(s["k"] == "track" for steps in wa_path_model.lanes(camp.path).values() for s in steps)
    return camp.plan == WaCampaign.Plan.NINETY


def where_key(c: WaContact) -> str:
    if stopped(c):
        return "stop"
    if not c.opted_in:
        return "locked"
    if c.campaign_status == CS.PAID:
        return "paid"
    if is_yes(c):
        return "yes"
    if c.status == Status.NOTNOW or c.lane == "notnow":
        return "notnow"
    if c.status == Status.REPLIED:
        return "replied"
    if c.campaign_id or c.step > 0:
        return "intro"
    return "none"


def need(c: WaContact) -> str:
    """What staff must do first: answer a reply, set the Track, or get an opt-in."""
    if stopped(c):
        return ""
    if c.needs_reply:
        return "reply"
    if track_to_set(c):
        return "track"
    if not c.opted_in:
        return "optin"
    return ""


def last_message(c: WaContact) -> dict[str, Any] | None:
    msgs = _messages(c)
    if not msgs:
        return None
    m = msgs[-1]
    return {"text": m.text[:200], "at": m.at.isoformat(), "direction": "in" if m.kind == WaMessage.Kind.THEIRS else "out",
            "by": m.by if m.kind != WaMessage.Kind.THEIRS else ""}


def _intro_day(c: WaContact, today: date) -> str:
    day = wa_flow.day_of(c, today)
    return f"Intro · day {day}" if day else "Intro · not started"


def where(c: WaContact, today: date | None = None, block: WaBlock | None = None) -> dict[str, str]:
    """{key, label, sub}: the board's WHERE ON THE PATH column."""
    today = today or whatsapp.today()
    key = where_key(c)
    camp = c.campaign if c.campaign_id else None
    if key == "stop":
        block = block if block is not None else WaBlock.objects.filter(phone=c.phone).first()
        sub = f"{block.reason}, {rel_day(timezone.localtime(block.created_at).date(), today)}" if block else "Never messaged again"
        return {"key": key, "label": "Stopped", "sub": sub}
    if key == "locked":
        return {"key": key, "label": "Not opted in", "sub": f"Added {rel_day(timezone.localtime(c.created_at).date(), today)}"}
    if key == "paid":
        pack = WaContact.Pack(c.pack).label if c.pack else ""
        return {"key": key, "label": "Paid" + (f" · {pack}" if pack else ""),
                "sub": f"First PC {rel_day(c.using_since, today)}" if c.using_since else ""}
    if key == "yes":
        if track_to_set(c):
            step = "Track to set"
        elif c.campaign_status in (CS.ENGAGED, CS.TRACK, CS.DEMO, CS.SETUP, CS.DEMO_CALL, CS.OFFER):
            step = CS(c.campaign_status).label
        elif c.status == Status.SIGNED:
            step = "Signed up"
        else:
            step = ""
        return {"key": key, "label": "YES path" + (f" · {step}" if step else ""),
                "sub": f"Said YES {rel_day(c.yes_date, today)}" if c.yes_date else "Said YES"}
    if key == "notnow":
        waiting = c.lane == "notnow" and camp is not None and camp.engine == PATH
        return {"key": key, "label": "Not now · waiting" if waiting else "Not now",
                "sub": "Then back to Intro" if waiting else "Said not now"}
    if key == "none":
        return {"key": key, "label": "No campaign yet", "sub": "Add them to a campaign"}
    label = _intro_day(c, today)
    if key == "replied":
        theirs = [m for m in _messages(c) if m.kind == WaMessage.Kind.THEIRS]
        when = rel_day(timezone.localtime(theirs[-1].at).date(), today) if theirs else ""
        return {"key": key, "label": label, "sub": f"Replied {when}".strip()}
    if camp is not None and camp.status != WaCampaign.Status.LIVE:
        word = "paused" if camp.status == WaCampaign.Status.PAUSED else "a draft"
        return {"key": key, "label": f"Paused · {label}" if c.first_sent else label, "sub": f"{camp.name} is {word}"}
    if c.paused_until and c.paused_until > today:
        return {"key": key, "label": label, "sub": f"Paused until {rel_day(c.paused_until, today)}"}
    return {"key": key, "label": label, "sub": "No reply yet" if c.first_sent else "Day 1 is their first message"}


# --- What goes next ------------------------------------------------------------------------------------------------


def _out(state: str, label: str = "", when: date | None = None, slot: str = "", send_kind: str = "") -> dict[str, Any]:
    return {"state": state, "label": label, "when": when.isoformat() if when else None, "slot": slot, "send_kind": send_kind}


def _sent_today(c: WaContact, today: date) -> bool:
    return any(m.kind != WaMessage.Kind.THEIRS and timezone.localtime(m.at).date() == today for m in _messages(c))


def _not_before(d: date, c: WaContact, today: date) -> date:
    """One message a day: a message due today after one already went today goes tomorrow (Monday after a Saturday)."""
    if d <= today and _sent_today(c, today):
        d = today + timedelta(days=1)
        if d.weekday() == 6:
            d += timedelta(days=1)
    return max(d, today)


def _path_next(c: WaContact, cache: dict[str, Any] | None, now: Any = None) -> dict[str, Any]:
    """Walk a copy of the contact's path (never saved) to the next message, or what it waits for."""
    ctx = wa_paths.Ctx(c, now, sim=True, cache=cache)
    today = ctx.today
    for _ in range(40):
        out = ctx.run()
        step = out.step or {}
        slot = str(step.get("slot") or "")
        if out.kind == "due":
            d = ctx.today if ctx.today > today else _not_before(today, c, today)  # after a wait: that day
            return _out("due" if d == today else "planned", _tlabel(out.template), d, slot, "flow")
        if out.kind == "wait":
            if out.template is not None:
                d = wa_paths.dloc(out.when)
                return _out("planned", _tlabel(out.template), d, slot, "flow")
            if out.when is None or wa_paths.dloc(out.when) <= ctx.today:
                break
            ctx.now = max(ctx.now, wa_paths.at_open(wa_paths.dloc(out.when)))
            continue
        if out.kind == "hold":
            words = HOLD_WORDS.get(out.note, "")
            if out.note == "goal":
                words = wa_paths.step_title(step, ctx)
            return _out("hold", words or "Waiting")
        if out.kind == "ended":
            return _out("done", (step.get("label") or "The path ended") if step.get("k") == "end" else "The path ended")
    return _out("done", "Nothing more planned")


def _ninety_next(c: WaContact, today: date) -> dict[str, Any]:
    if c.yes_date is not None:
        t, note = wa_flow.next_yes_path(c, today)
        if t is not None:
            d = _not_before(today, c, today)
            return _out("due" if d == today else "planned", _tlabel(t), d, send_kind="flow")
        if note == "set_track":
            return _out("hold", "Set the Track")
        if c.using_since and c.testimonial in ("", WaContact.Testimonial.WRITTEN):
            return _out("planned", "Testimonial", c.using_since + timedelta(days=wa_flow.TESTIMONIAL_AFTER))
        if c.campaign_status == CS.PAID:
            return _out("done", "Nothing more planned")
        return _out("hold", "Nothing due yet")
    rows = list(wa_flow._rows(Cat.OUTREACH, step__gt=c.out_day).order_by("step", "id"))
    if not rows:
        return _out("done", "Every Intro message went")
    if c.first_sent is None:
        return _out("due", _tlabel(rows[0]), _not_before(today, c, today), send_kind="flow")
    day = wa_flow.day_of(c, today)
    due = [r for r in rows if r.step <= day]
    if due:
        d = _not_before(today, c, today)
        return _out("due" if d == today else "planned", _tlabel(due[-1]), d, send_kind="flow")
    return _out("planned", _tlabel(rows[0]), c.first_sent + timedelta(days=rows[0].step - 1), send_kind="flow")


def _touches_next(c: WaContact, today: date) -> dict[str, Any]:
    camp = c.campaign
    if c.yes_date is not None and c.status in (Status.INTERESTED, Status.SIGNED):
        t = whatsapp.next_nurture(c)
        if t is None:
            return _out("done", "Every after-YES message went")
        d = _not_before(max(c.yes_date + timedelta(days=t.day), today), c, today)
        return _out("due" if d == today else "planned", t.name, d, send_kind="nurture")
    if c.status == Status.NOTNOW:
        return _out("done", "Nothing more: they said not now")
    if c.status == Status.REPLIED:
        return _out("hold", "Sort their reply on Today")
    kind = whatsapp.NEXT_KIND.get(c.step)
    t = getattr(camp, kind, None) if kind else None
    if kind is None or t is None:
        return _out("done", "Every message went")
    d = _not_before(max(c.next_date or today, today), c, today)
    return _out("due" if d == today else "planned", t.name, d, send_kind=kind)


def next_step(c: WaContact, cache: dict[str, Any] | None = None, today: date | None = None) -> dict[str, Any]:
    """{state, label, when, slot, send_kind}: the board's NEXT column."""
    today = today or whatsapp.today()
    if stopped(c):
        return _out("never", "Blocked in every campaign")
    if not c.opted_in:
        return _out("locked", "Until they agree")
    if c.needs_reply:
        return _out("reply", "Answer on Today")
    if not c.campaign_id:
        return _out("none", "Add them to a campaign")
    camp = c.campaign
    nxt = _path_next(c, cache) if camp.engine == PATH else _ninety_next(c, today) if camp.plan == WaCampaign.Plan.NINETY else _touches_next(c, today)
    if nxt["state"] in ("due", "planned"):
        if camp.status != WaCampaign.Status.LIVE and not (c.yes_date and camp.engine != PATH):
            # A paused or draft campaign queues nothing (the slots YES path still runs, as Today does).
            return {**nxt, "state": "paused", "when": None, "send_kind": ""}
        if c.paused_until and c.paused_until > today:
            return {**nxt, "state": "paused", "when": c.paused_until.isoformat(), "send_kind": ""}
    return nxt


# --- The menu's counts ---------------------------------------------------------------------------------------------


def counts(contacts: list[WaContact]) -> dict[str, Any]:
    """Counts for the whole list (the menu): where on the path, need, segment (with its cities), Track, campaign,
    language."""
    today = whatsapp.today()
    week = today - timedelta(days=7)
    out: dict[str, Any] = {
        "all": len(contacts), "added_week": 0, "need": 0, "need_by": {"reply": 0, "track": 0, "optin": 0},
        "where": {k: 0 for k in WHERE_KEYS}, "tracks": {"privacy": 0, "activity": 0, "both": 0, "unset": 0},
        "segments": [], "campaigns": {}, "no_campaign": 0, "languages": {"en": 0, "hi": 0},
    }
    segs: dict[str, dict[str, Any]] = {}
    for c in contacts:
        if timezone.localtime(c.created_at).date() > week:
            out["added_week"] += 1
        n = need(c)
        if n:
            out["need"] += 1
            out["need_by"][n] += 1
        out["where"][where_key(c)] += 1
        if c.track:
            out["tracks"][c.track] += 1
        if track_to_set(c):
            out["tracks"]["unset"] += 1
        s = segs.setdefault(c.segment, {"segment": c.segment, "count": 0, "cities": {}})
        s["count"] += 1
        if c.city:
            s["cities"][c.city] = s["cities"].get(c.city, 0) + 1
        if c.campaign_id:
            out["campaigns"][str(c.campaign_id)] = out["campaigns"].get(str(c.campaign_id), 0) + 1
        else:
            out["no_campaign"] += 1
        out["languages"][whatsapp.language_for(c)] = out["languages"].get(whatsapp.language_for(c), 0) + 1
    out["segments"] = [
        {"segment": s["segment"], "count": s["count"], "cities": [k for k, _ in sorted(s["cities"].items(), key=lambda x: (-x[1], x[0]))[:3]]}
        for s in sorted(segs.values(), key=lambda s: (-s["count"], s["segment"]))
    ]
    return out


# --- One contact's path: done, now, next ---------------------------------------------------------------------------


def _row(state: str, label: str, d: date | None = None, note: str = "") -> dict[str, Any]:
    return {"state": state, "label": label, "date": d.isoformat() if d else None, "note": note}


def _upcoming(c: WaContact) -> list[dict[str, Any]]:
    """The campaign's next messages for this contact if each goes on its due day and nobody replies (as the schedule
    endpoint): path campaigns directly, slots campaigns on the path they would convert to. [] when it can't tell."""
    from . import wa_paths_convert

    camp = c.campaign
    try:
        if camp.engine == PATH:
            return wa_paths.simulate(camp, c, limit=60)
        path = wa_path_model.clean(wa_paths_convert.build(camp))
        p = wa_paths_convert.place(camp, path, c, timezone.now())
        return wa_paths.simulate(camp, wa_paths_convert._virtual(c, camp, path, p), path=path, limit=60)
    except Exception:  # noqa: BLE001 -- the panel shows what went; a schedule it can't work out is left out
        return []


def path_of(c: WaContact) -> dict[str, Any]:
    """GET /contacts/<id>/path/: {campaign, campaign_name, campaign_status, engine, day, steps: [{state: done | now |
    next, label, date, note}], more: {count, last_date} | None}. Done = what went (our messages, their YES, the Track,
    the first PC), now = where they are, next = the coming messages."""
    today = whatsapp.today()
    camp = c.campaign if c.campaign_id else None
    added = [_row("done", f"Added to {camp.name}", timezone.localtime(c.created_at).date())] if camp is not None else []
    steps: list[dict[str, Any]] = []
    # Our messages on the path: campaign messages and quick replies. One-offs and plain replies are in the conversation.
    one_off = (WaMessage.Kind.THEIRS, WaMessage.Kind.REPLY, WaMessage.Kind.CUSTOM)
    sent = [m for m in _messages(c) if m.kind not in one_off or (m.template_id and m.template.category)]
    for m in sent:
        t = m.template
        label = _tlabel(t) if t is not None else WaMessage.Kind(m.kind).label
        steps.append(_row("done", label, timezone.localtime(m.at).date(), "quick" if t is not None and t.category == Cat.QUICK else ""))
    if c.yes_date:
        steps.append(_row("done", "Said YES", c.yes_date))
    if c.track:
        steps.append(_row("done", f"Track · {WaTemplate.Track(c.track).label}", None))
    if c.using_since:
        steps.append(_row("done", "First PC set up", c.using_since))
    if c.campaign_status == CS.PAID:
        steps.append(_row("done", "Goal · Paid", None))
    # Dated rows in date order; the Track and Paid (no date kept) after the last dated row before them.
    dated = sorted([s for s in steps if s["date"]], key=lambda s: s["date"])
    steps = added + dated + [s for s in steps if not s["date"]]

    nxt = next_step(c, today=today)
    more = None
    upcoming: list[dict[str, Any]] = []
    if nxt["state"] == "never":
        block = WaBlock.objects.filter(phone=c.phone).first()
        steps.append(_row("now", "Blocked in every campaign", None, block.reason if block else ""))
    elif nxt["state"] == "locked":
        steps.append(_row("now", "Waiting for their opt-in", timezone.localtime(c.created_at).date()))
        if camp is not None:
            steps.append(_row("next", f"Then {camp.name} picks them up"))
    elif nxt["state"] == "none":
        steps.append(_row("now", "In no campaign", None, "Add them to a campaign"))
    elif nxt["state"] == "reply":
        steps.append(_row("now", "Replied · answer it on Today", today))
    elif nxt["state"] in ("hold", "done"):
        steps.append(_row("now", nxt["label"], None))
    else:
        upcoming = _upcoming(c)
        if nxt["state"] == "paused":
            why = f"Paused until {rel_day(c.paused_until, today)}" if c.paused_until and c.paused_until > today else f"{camp.name} is {camp.get_status_display().lower()}"
            steps.append(_row("now", why, None))
            upcoming = [] if camp.status != WaCampaign.Status.LIVE else upcoming
        elif nxt["state"] == "planned" and c.campaign.engine == PATH and c.step_id:
            hit = wa_path_model.index(camp.path).get(c.step_id)
            if hit and hit[2]["k"] in ("wait", "reply"):
                steps.append(_row("now", wa_paths.step_title(hit[2]), None))
        if not upcoming and nxt["state"] in ("due", "planned"):
            upcoming = [{"label": nxt["label"], "date": nxt["when"]}]
        for i, r in enumerate(upcoming[:UPCOMING]):
            t = WaTemplate.objects.filter(pk=r["template_id"]).first() if r.get("template_id") else None
            label = _tlabel(t) if t is not None else r.get("label", "")
            d = date.fromisoformat(r["date"]) if r.get("date") else None
            state = "now" if i == 0 and not any(s["state"] == "now" for s in steps) else "next"
            steps.append(_row(state, label, d, f"moved from {r['moved_from']}" if r.get("moved_from") else ""))
        if len(upcoming) > UPCOMING:
            rest = upcoming[UPCOMING:]
            more = {"count": len(rest), "last_date": rest[-1].get("date"), "last_day": rest[-1].get("day")}
    return {
        "campaign": camp.pk if camp else None, "campaign_name": camp.name if camp else "",
        "campaign_status": camp.status if camp else "", "engine": camp.engine if camp else "",
        "day": wa_flow.day_of(c, today), "next": nxt, "steps": steps, "more": more,
    }
