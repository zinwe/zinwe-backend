"""
Today (wa4, 12 Nov 2026): what the Today tab adds on top of whatsapp.queue() and whatsapp.replies().

- **Claude's read of each reply.** Every reply row gets a suggestion: YES, A question, Not now or STOP, or (an answer
  in the YES path while the Track is Unassigned) the Track. Rules first (keywords, English and Hinglish, and
  wa_flow.suggest_track); with ANTHROPIC_API_KEY set, Claude may refine it. Claude's read is kept on the message
  (WaMessage.read), so Today asks once per message. Staff always confirm or change it; Claude never sends anything.
- **What confirming does.** For each choice, one plain line on what happens to their path, worked out by running the
  engine on a copy of the contact (nothing is saved), and the template the answer starts from.
- **Send slots.** Each campaign has one send slot (WaCampaign.send_slot, 10-12 by default). Intro messages go in it;
  replies and the after-YES steps go any time in the send window. A path step may carry its own `slot`.
- **The day.** Sent today against the daily limit, replies, YES, the slot now and the next one, and the list of
  messages marked as sent today.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from . import wa_flow, wa_paths
from .models import WaCampaign, WaContact, WaMessage, WaTemplate
from .wa_path_model import lanes

logger = logging.getLogger("zinwe.marketing")

Kind = WaMessage.Kind
Cat = WaTemplate.Category
Status = WaContact.Status

# --- Send slots ----------------------------------------------------------------------------------------------------

SLOTS: list[tuple[str, str, int, int]] = [("10", "10–12", 10, 12), ("12", "12–14", 12, 14), ("14", "14–17", 14, 17), ("17", "17–19", 17, 19)]
SLOT_LABEL = {k: label for k, label, _, _ in SLOTS}
SLOT_START = {k: a for k, _, a, _ in SLOTS}


def slot_info(now: datetime | None = None) -> dict[str, Any]:
    """The slot we are in (India time). Before 10:00 it is the first slot; after 19:00 the last, so whatever is left
    is "due now". {now, now_label, open, next, next_label, next_at, list: [{key, label, start}]}."""
    local = timezone.localtime(now or timezone.now())
    h = local.hour + local.minute / 60
    sunday = local.weekday() == 6
    now_key = SLOTS[0][0]
    for key, _, a, _ in SLOTS:
        if h >= a:
            now_key = key
    nxt = next((k for k, _, a, _ in SLOTS if a > SLOT_START[now_key]), "")
    return {
        "now": now_key, "now_label": SLOT_LABEL[now_key], "open": not sunday and 10 <= h < 19, "sunday": sunday,
        "next": nxt, "next_label": SLOT_LABEL.get(nxt, ""), "next_at": f"{SLOT_START[nxt]:02d}:00" if nxt else "",
        "list": [{"key": k, "label": label, "start": f"{a:02d}:00"} for k, label, a, _ in SLOTS],
    }


def is_after_yes(item: dict[str, Any]) -> bool:
    """A step for someone who already replied (YES path, Not now path, after-YES): it goes any time, before Intro."""
    if item.get("kind") == "nurture" or item.get("note") == "set_track":
        return True
    lane = item.get("lane") or ""
    if lane:
        return lane != "main"
    t = item.get("template")
    if isinstance(t, WaTemplate) and t.category:
        return t.category != Cat.OUTREACH
    return False


def row_slot(item: dict[str, Any]) -> str:
    """The slot a queue row goes in: "" = any time (after-YES), else the path step's own slot or the campaign's."""
    if is_after_yes(item):
        return ""
    contact: WaContact = item["contact"]
    camp = contact.campaign if contact.campaign_id else None
    if camp is None:
        return SLOTS[0][0]
    step_id = item.get("step_id") or ""
    if step_id and camp.engine == WaCampaign.Engine.PATH:
        hit = wa_paths.index(camp.path).get(step_id)
        own = str((hit[2] if hit else {}).get("slot") or "")
        if own in SLOT_LABEL:
            return own
    return camp.send_slot if camp.send_slot in SLOT_LABEL else SLOTS[0][0]


# --- The day -------------------------------------------------------------------------------------------------------


def day_numbers(today: date, cold_sent: int) -> dict[str, int]:
    """Sent today (messages that count against the daily limit) of the live campaigns' limits, people who replied
    today, and people who said YES today."""
    limit = WaCampaign.objects.filter(status=WaCampaign.Status.LIVE).aggregate(n=Sum("daily_limit"))["n"] or 0
    replies = WaMessage.objects.filter(kind=Kind.THEIRS, at__date=today).values("contact_id").distinct().count()
    return {"sent": cold_sent, "limit": limit, "replies": replies, "yes": WaContact.objects.filter(yes_date=today).count()}


def _short(t: WaTemplate | None) -> str:
    """Intro · Question · day 34, Quick reply · Not now: the label without the Track or step."""
    if t is None:
        return ""
    if not t.category:
        return t.name
    bits = [t.get_category_display(), t.type_name]
    if t.category == Cat.OUTREACH and t.step:
        bits.append(f"day {t.step}")
    return " · ".join(b for b in bits if b)


def sent_rows(today: date) -> list[dict[str, Any]]:
    """Every message marked as sent today, newest first: {at, contact, name, what, replied_at, blocked}."""
    from .wa_names import address

    msgs = (WaMessage.objects.filter(at__date=today).exclude(kind=Kind.THEIRS)
            .select_related("contact", "template").order_by("-at", "-id")[:300])
    theirs: dict[int, list[datetime]] = {}
    for cid, at in WaMessage.objects.filter(kind=Kind.THEIRS, at__date=today).values_list("contact_id", "at"):
        theirs.setdefault(cid, []).append(at)
    out = []
    for m in msgs:
        c = m.contact
        later = sorted(a for a in theirs.get(c.pk, []) if a > m.at)
        out.append({
            "at": m.at.isoformat(), "contact": c.pk, "name": address(c)["greeting"] if c.first_name else c.firm,
            "what": _short(m.template) if m.template_id else Kind(m.kind).label,
            "replied_at": later[0].isoformat() if later else None, "blocked": c.status == Status.STOP,
        })
    return out


# --- Reading a reply -----------------------------------------------------------------------------------------------

SORTS = ("yes", "question", "notnow", "stop")
TRACKS = ("privacy", "activity", "both")
SORT_LABEL = {"yes": "YES", "question": "A question", "notnow": "Not now", "stop": "STOP"}
TRACK_LABEL = {"privacy": "Data Privacy", "activity": "Team Activity", "both": "Both Pack"}

_STOP = re.compile(r"\b(stop|unsubscribe|band karo|band kar do|mat bhejo|mat bhejiye|mat karo message|don'?t (message|send|text|contact)|"
                   r"do not (message|send|text|contact)|remove (me|my number|this number)|no more messages)\b", re.I)
_LATER = re.compile(r"\b(baad mein|baad me|later|busy|not now|abhi nahi|abhi nhi|next (week|month|year)|agle (hafte|mahine|saal)|"
                    r"kal baat|season|after (diwali|holi|january|february|march|april|may|june|july|august|september|october|"
                    r"november|december|the season)|not interested|no thanks|nahi chahiye|no need)\b", re.I)
_AFTER = re.compile(r"\b(?:after|ke baad|baad)\s+(diwali|holi|january|february|march|april|may|june|july|august|september|"
                    r"october|november|december)|\b(diwali|holi|january|february|march|april|may|june|july|august|september|"
                    r"october|november|december)\s+(?:ke baad|baad|ke bad)", re.I)
_YES = re.compile(r"\b(yes|yeah|yep|haan|haa|han ji|haan ji|ji haan|ok|okay|sure|send|bhejo|bhej do|bhejiye|video|interested|"
                  r"share|go ahead|tell me more|please do|zaroor|chalega)\b", re.I)
_VIDEO = re.compile(r"\b(video|send|bhejo|bhej do|bhejiye|share)\b", re.I)
_PROBLEM = re.compile(r"\b(problem|issue|happened|hua tha|hua hai|exactly|same thing|faced|lost|leak)\b", re.I)
_TOPICS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b(price|pricing|cost|costs|kitna|kitne|charges?|rate|how much|fees?|per pc|plans?)\b", re.I), "Price question", "a question about price"),
    (re.compile(r"\b(spy|spyware|monitor(ing)? (staff|my staff)|track(ing)? (my )?staff|privacy of (my )?staff)\b", re.I), "Is this spyware?", "asks if it is spyware"),
    (re.compile(r"\b(e-?mail|mail kar)\b", re.I), "Send it on email", "asks for it on email"),
    (re.compile(r"\b(where did you get|how did you get|number kahan|number kaha|who gave you)\b", re.I), "Where did you get my number?", "asks where we got the number"),
    (re.compile(r"\b(already use|already have|we use|pehle se)\b", re.I), "We already use something", "says they already use something"),
]
_QUESTION = re.compile(r"\?|\b(kya|kaise|kaun|kab|kyun|what|how|which|does it|can it|is it|do you)\b", re.I)
_TRACK_WORDS = {
    "privacy": [("laptop", "laptops"), ("pen ?drive|usb", "pen drives"), ("drive|cloud", "files on drives"), ("client", "client files"),
                ("file", "files"), ("data|leak|secur|safe|password|access|cop(y|ies)", "data safety")],
    "activity": [("team|staff", "the team"), ("who (is|does|works)|kaam", "who works on what"), ("\\bday\\b|\\bdin\\b|hours|working|attendance", "the working day"),
                 ("activity|monitor|productiv", "activity")],
}


def _words(text: str, track: str) -> str:
    found: list[str] = []
    for pattern, word in _TRACK_WORDS.get(track, []):
        if re.search(pattern, text, re.I) and word not in found:
            found.append(word)
    return " and ".join(found[:2])


def rules_read(text: str, track_mode: bool) -> dict[str, str]:
    """{kind, track, why, source: "rules"} from keywords. kind: yes · question · notnow · stop · track."""
    t = (text or "").strip()
    if track_mode:
        track = wa_flow.suggest_track(t)
        if track:
            why = _words(t, "privacy") if track == "privacy" else _words(t, "activity") if track == "activity" else "names both"
            return {"kind": "track", "track": track, "why": f"mentions {why}" if track != "both" else why, "source": "rules"}
        return {"kind": "track", "track": "", "why": "doesn't point to one Track yet", "source": "rules"}
    if _STOP.search(t):
        return {"kind": "stop", "track": "", "why": "asks us to stop messaging", "source": "rules"}
    if _LATER.search(t):
        if re.search(r"\b(not interested|no thanks|nahi chahiye|no need)\b", t, re.I):
            why = "says no for now, not STOP"
        else:
            m = _AFTER.search(t)
            month = (m.group(1) or m.group(2)) if m else ""
            busy = re.search(r"\b(busy|season)\b", t, re.I)
            why = ", ".join(x for x in ("busy now" if busy else "", f"asks to talk after {month.capitalize()}" if month else "asks to talk later") if x)
        return {"kind": "notnow", "track": "", "why": why, "source": "rules"}
    if _YES.search(t):
        bits = ["asks for the video" if _VIDEO.search(t) else "says yes"]
        if _PROBLEM.search(t):
            bits.append("names the problem")
        return {"kind": "yes", "track": "", "why": " and ".join(bits), "source": "rules"}
    for pattern, _, why in _TOPICS:
        if pattern.search(t):
            return {"kind": "question", "track": "", "why": f"{why}, no YES yet", "source": "rules"}
    if _QUESTION.search(t):
        return {"kind": "question", "track": "", "why": "asks a question, no YES yet", "source": "rules"}
    if re.fullmatch(r"\W*(no|nahi|nhi|na)\W*", t, re.I):
        return {"kind": "notnow", "track": "", "why": "says no, not STOP", "source": "rules"}
    return {"kind": "question", "track": "", "why": "not clear: read it and pick", "source": "rules"}


def quick_topic(text: str) -> str:
    """The Quick reply type a question points to ("Price question"...), or ""."""
    for pattern, type_name, _ in _TOPICS:
        if pattern.search(text or ""):
            return type_name
    return ""


READ_SYSTEM = (
    "You sort WhatsApp replies that small Indian and US businesses send to Zinwe (data protection and team tools for "
    "office Windows PCs). Staff read your suggestion and confirm or change it; you never send anything. Replies are in "
    "English, Hindi or Hinglish. Sort the reply into one kind:\n"
    "- yes: they want to hear more (yes, haan, send the video, interested, tell me more).\n"
    "- question: they ask something but have not said yes (price, kitna, how it works, is this spyware).\n"
    "- notnow: not now (busy, later, baad mein, after a festival or month, not interested for now).\n"
    "- stop: they ask us to stop messaging them (STOP, band karo, mat bhejo, remove my number).\n"
    "- track: only when track_mode is true. They already said YES and answer our question about what matters to them: "
    "set track to privacy (client files, data, laptops, pen drives, access), activity (the team's day, who works on "
    "what, staff hours) or both (both clearly), or \"\" when it is unclear.\n"
    "When track_mode is true the kind must be track; otherwise it must not be. why: at most 8 plain words on what in "
    "their message shows it, lower case, British English, no full stop, for example \"asks for the video and names the "
    "problem\" or \"busy now, asks to talk after January\"."
)
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["yes", "question", "notnow", "stop", "track"]},
        "track": {"type": "string", "enum": ["", "privacy", "activity", "both"]},
        "why": {"type": "string"},
    },
    "required": ["kind", "track", "why"],
    "additionalProperties": False,
}


def claude_read(text: str, ours: str, track_mode: bool) -> dict[str, str] | None:
    """Claude's read of one reply, or None (no key, an error, a refusal, or an answer that breaks the rules)."""
    if not settings.ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=20.0, max_retries=1)
        response = client.messages.create(
            model=settings.ZINWE_WA_MODEL,
            max_tokens=2048,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": READ_SCHEMA}},
            system=READ_SYSTEM,
            messages=[{"role": "user", "content": json.dumps({"our_last_message": ours[:1000], "their_reply": text[:1500], "track_mode": track_mode})}],
        )
    except Exception:  # noqa: BLE001 -- any API or network failure falls back to the rules
        logger.warning("WhatsApp Today: Claude's read failed, using the rules", exc_info=True)
        return None
    if response.stop_reason in ("refusal", "max_tokens"):
        return None
    try:
        data = json.loads("".join(b.text for b in response.content if b.type == "text"))
    except (ValueError, TypeError):
        return None
    kind, track, why = str(data.get("kind", "")), str(data.get("track", "")), str(data.get("why", "")).strip().rstrip(".")
    if (kind == "track") != track_mode or kind not in (*SORTS, "track") or track not in ("", *TRACKS):
        return None
    return {"kind": kind, "track": track if kind == "track" else "", "why": why[:80] or "Claude's read", "source": "claude"}


def track_mode(contact: WaContact) -> bool:
    """Their reply answers the Engage question: they said YES and the Track is still Unassigned (ninety or path)."""
    if contact.yes_date is None or contact.track or contact.status == Status.STOP:
        return False
    camp = contact.campaign if contact.campaign_id else None
    if camp is None:
        return False
    if camp.engine == WaCampaign.Engine.PATH:
        return contact.lane == "yes"
    return camp.plan == WaCampaign.Plan.NINETY


# --- What confirming does ------------------------------------------------------------------------------------------


def _unit(n: int, unit: str) -> str:
    one = unit[:-1] if unit.endswith("s") else unit
    return f"{n} {one if n == 1 else one + 's'}"


def _after(ctx: wa_paths.Ctx, step: dict[str, Any] | None) -> str:
    """What follows the step they are at, in a few words: ", then Zinwe waits up to 2 days for the answer"."""
    if step is None:
        return ""
    lane_steps = lanes(ctx.path).get(ctx.lane) or []
    ids = [s["id"] for s in lane_steps]
    if step["id"] not in ids or ids.index(step["id"]) + 1 >= len(ids):
        return ""
    nxt = lane_steps[ids.index(step["id"]) + 1]
    if nxt["k"] == "reply":
        return f", then Zinwe waits up to {_unit(int(nxt.get('n', 1)), nxt.get('unit', 'days'))} for the answer"
    if nxt["k"] == "wait":
        n = int(nxt.get("n", 1))
        return ". The next one goes tomorrow" if n == 1 and nxt.get("unit", "days") == "days" else f". The next one goes {_unit(n, nxt.get('unit', 'days'))} later"
    if nxt["k"] == "track":
        return ", then you set the Track from their answer"
    return ""


def _sim(contact: WaContact, now: datetime, lane: str = "", yes: bool = False, track: str = "") -> tuple[wa_paths.Ctx, wa_paths.Out]:
    """The path engine on a copy of the contact, after the choice. Never saves anything."""
    c = copy.copy(contact)
    if yes and c.yes_date is None:
        c.yes_date = timezone.localdate(now)
    if yes and c.status not in (Status.SIGNED, Status.STOP):
        c.status = Status.INTERESTED
    if track:
        c.track = track
    ctx = wa_paths.Ctx(c, now, sim=True)
    ctx.run()
    if lane:
        ctx.move(lane)
    return ctx, ctx.run()


def _ok(t: WaTemplate | None) -> WaTemplate | None:
    return t if t is not None and not t.archived and not wa_flow.is_draft(t) else None


def _quick(type_name: str) -> WaTemplate | None:
    return _ok(WaTemplate.objects.filter(category=Cat.QUICK, type_name=type_name, archived=False, version_of__isnull=True)
               .order_by("id").first())


def _lane_story(path: dict[str, Any], lane: str) -> str:
    """The Not now path in words: "this reply, a 30-day wait, then back to Intro at the next message"."""
    bits: list[str] = []
    for s in lanes(path).get(lane) or []:
        k = s["k"]
        if k == "send":
            bits.append("this reply" if not bits else "a message")
        elif k in ("wait", "reply"):
            n, unit = int(s.get("n", 1)), s.get("unit", "days")
            one = unit[:-1] if unit.endswith("s") else unit
            bits.append(f"a {n}-{one} wait")
        elif k == "goto":
            bits.append("then back to Intro at the next message" if s.get("step") == "resume" else f"then the {s.get('lane', '')} path")
            break
        elif k == "end":
            bits.append("then it ends")
            break
    return ", ".join(bits)


def _when(d: date | None) -> str:
    return f"{d:%a} {d.day} {d:%b}" if d else ""


def choices(contact: WaContact, now: datetime | None = None) -> dict[str, Any]:
    """{sorts, effect: {sort: line}, answers: {sort: template id or None}} for a reply row."""
    from .wa_names import address

    now = now or timezone.now()
    today = timezone.localdate(now)
    a = address(contact)
    name = a["first_name"] or a["business"]
    camp = contact.campaign if contact.campaign_id else None
    path = camp is not None and camp.engine == WaCampaign.Engine.PATH and bool(lanes(camp.path).get("main"))
    ninety = camp is not None and not path and camp.plan == WaCampaign.Plan.NINETY
    effect: dict[str, str] = {}
    answers: dict[str, int | None] = {}

    if track_mode(contact):
        for tr in TRACKS:
            t, follow = None, ""
            if path:
                ctx, out = _sim(contact, now, track=tr)
                t = out.template if out.kind == "due" else None
                follow = _after(ctx, out.step) if t else ""
            else:
                copy_ = copy.copy(contact)
                copy_.track = tr
                t, _ = wa_flow.next_yes_path(copy_, today)
            answers[tr] = t.pk if _ok(t) else None
            effect[tr] = (f"Sets the Track to {TRACK_LABEL[tr]} and sends {_short(t)} now{follow}." if t
                          else f"Sets the Track to {TRACK_LABEL[tr]}. Nothing more is due from it today.")
        return {"sorts": list(TRACKS), "effect": effect, "answers": answers}

    last_theirs = contact.messages.filter(kind=Kind.THEIRS).order_by("at", "id").last()
    topic = quick_topic(last_theirs.text if last_theirs else "")

    # YES
    if contact.yes_date is not None:
        effect["yes"] = f"{name} already said YES. Their YES path goes on as planned."
        answers["yes"] = None
    elif path:
        ctx, out = _sim(contact, now, lane="yes", yes=True)
        t = out.template if out.kind == "due" else None
        answers["yes"] = t.pk if _ok(t) else None
        if t:
            effect["yes"] = f"YES moves {name} to the YES path: {_short(t)} goes now{_after(ctx, out.step)}."
        elif out.kind == "hold" and out.note == "set_track":
            effect["yes"] = f"YES moves {name} to the YES path. You set the Track next."
        else:
            effect["yes"] = f"YES moves {name} to the YES path."
    elif ninety:
        copy_ = copy.copy(contact)
        copy_.yes_date = today
        t, note = wa_flow.next_yes_path(copy_, today)
        answers["yes"] = t.pk if _ok(t) else None
        effect["yes"] = (f"YES moves {name} to the YES path: {_short(t)} goes now." if t
                         else f"YES moves {name} to the YES path. You set the Track next." if note == "set_track"
                         else f"YES moves {name} to the YES path.")
    else:
        from .whatsapp import _template_for

        t = _template_for(contact, "interested")
        answers["yes"] = t.pk if _ok(t) and not t.category else None
        effect["yes"] = f"YES marks {name} as interested: the after-YES messages start, no more follow-ups."

    # A question
    q = _quick(topic) if topic else None
    if q is None and camp is not None and not path and not ninety and camp.question_id:
        q = _ok(camp.question)
    answers["question"] = q.pk if q else None
    where, which, nxt = ("the YES path" if contact.yes_date else "Intro"), "next", ""
    if path:
        where = {"main": "Intro", "yes": "the YES path", "notnow": "the Not now path"}.get(contact.lane or "main", "Intro")
        due = wa_paths.contact_view(contact)["next_due"]
        nxt = _when(date.fromisoformat(due)) if due else ""
    elif ninety and contact.yes_date is None and contact.first_sent:
        # The ninety plan's Intro rows go by day from the first message: the next one keeps its day.
        row = wa_flow._rows(Cat.OUTREACH, step__gt=contact.out_day).order_by("step", "id").first()
        if row:
            which, nxt = f"day {row.step}", _when(max(contact.first_sent + timedelta(days=row.step - 1), today))
    elif not ninety:
        nxt = _when(contact.next_date)
    effect["question"] = (f"A question doesn't move them. {name} stays on {where}"
                          + (f"; the {which} message still goes on {nxt}." if nxt else "."))

    # Not now
    if path and lanes(camp.path).get("notnow"):
        ctx, out = _sim(contact, now, lane="notnow")
        t = out.template if out.kind == "due" else None
        answers["notnow"] = t.pk if _ok(t) else None
        effect["notnow"] = f"Not now moves them to the Not now path: {_lane_story(camp.path, 'notnow')}."
    else:
        t = _quick("Not now") or (_ok(camp.notnow) if camp is not None and camp.notnow_id else None)
        answers["notnow"] = t.pk if t else None
        effect["notnow"] = ("Not now marks them as not now. The Intro messages carry on as planned." if ninety or path
                            else "Not now marks them as not now: no more follow-ups unless they write again.")

    # STOP
    t = None
    if path:
        stop_sends = [s for s in lanes(camp.path).get("stop") or [] if s["k"] == "send"]
        t = _ok(WaTemplate.objects.filter(pk=stop_sends[0].get("template")).first()) if stop_sends else None
    t = t or _quick("STOP / band karo")
    answers["stop"] = t.pk if t else None
    effect["stop"] = "STOP ends every campaign for this number. Send the confirmation, then the number is blocked for good."
    return {"sorts": list(SORTS), "effect": effect, "answers": answers}


# --- Reply rows ----------------------------------------------------------------------------------------------------

CLAUDE_PER_LOAD = 4  # Claude reads at most this many new replies per Today load; the rest show the rules until the next


def suggestions(contacts: list[WaContact]) -> dict[int, dict[str, str]]:
    """{contact id: {kind, track, why, source}} for each reply: Claude's stored read, else the rules (and with a key,
    Claude reads up to CLAUDE_PER_LOAD new ones now, in parallel, and the read is kept on the message)."""
    out: dict[int, dict[str, str]] = {}
    todo: list[tuple[WaContact, WaMessage, str, bool]] = []
    for c in contacts:
        msgs = list(c.messages.all())
        theirs = [m for m in msgs if m.kind == Kind.THEIRS]
        if not theirs:
            continue
        last = theirs[-1]
        # Everything they wrote since our last message, so "Yes" + "send the video" read as one reply.
        ours_at = max((m.at for m in msgs if m.kind != Kind.THEIRS), default=None)
        text = "\n".join(m.text for m in theirs if ours_at is None or m.at > ours_at) or last.text
        mode = track_mode(c)
        stored = last.read or {}
        if stored.get("kind") and (stored.get("kind") == "track") == mode:
            out[c.pk] = {k: str(stored.get(k, "")) for k in ("kind", "track", "why", "source")}
            continue
        out[c.pk] = rules_read(text, mode)
        if settings.ANTHROPIC_API_KEY and len(todo) < CLAUDE_PER_LOAD:
            ours = next((m.text for m in reversed(msgs) if m.kind != Kind.THEIRS), "")
            todo.append((c, last, text, mode))
            out[c.pk]["_ours"] = ours
    if todo:
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            reads = list(pool.map(lambda x: claude_read(x[2], out[x[0].pk].get("_ours", ""), x[3]), todo))
        for (c, last, _, _), read in zip(todo, reads):
            # Kept either way, so a failing call isn't retried on every load: the rules' read is stored as tried.
            last.read = read or {**{k: v for k, v in out[c.pk].items() if not k.startswith("_")}, "tried": "claude"}
            last.save(update_fields=["read"])
            if read:
                out[c.pk] = read
    for v in out.values():
        v.pop("_ours", None)
    return out


def reply_extras(contacts: list[WaContact], now: datetime | None = None) -> dict[int, dict[str, Any]]:
    """{contact id: {suggest, sorts, effect, answers}} for the reply rows."""
    now = now or timezone.now()
    reads = suggestions(contacts)
    out: dict[int, dict[str, Any]] = {}
    for c in contacts:
        try:
            extra = choices(c, now)
        except Exception:  # noqa: BLE001 -- a broken path must not take Today down: the row shows without the lines
            logger.warning("WhatsApp Today: couldn't work out the choices for contact %s", c.pk, exc_info=True)
            extra = {"sorts": list(TRACKS if track_mode(c) else SORTS), "effect": {}, "answers": {}}
        out[c.pk] = {"suggest": reads.get(c.pk), **extra}
    return out


__all__ = ["SLOTS", "slot_info", "row_slot", "is_after_yes", "day_numbers", "sent_rows", "rules_read", "claude_read",
           "choices", "reply_extras", "suggestions", "track_mode", "quick_topic"]
