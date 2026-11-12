"""
The step-by-step campaign path (owner, 11 Nov 2026): its JSON shape, its checks, the ready presets, and the paths the
old campaigns convert to. The engine that moves contacts along a path is wa_paths.py.

    WaCampaign.path = {"v": 1, "lanes": {"main": [...], "yes": [...], "notnow": [...], "stop": [...]}}

Each step is {"id": "m1", "k": <kind>, ...}:

    start                                 the contacts who join (the main lane's first step, and only there)
    send    template, only_if             a message for staff to send on Today. only_if: "" | "no_pc" | "track:<track>"
    wait    n, unit, window               hours | days | weeks; window = Mon-Sat 10:00-19:00 (a Sunday moves to Monday)
    reply   n, unit                       wait up to n for their answer
    track                                 staff set the Track on Today; the contact goes on once it is set
    group   category, count, every[, window]   the next `count` unsent templates of that category, one every `every` days
    goal    goal                          yes | signup | first_pc | paid: the contact waits here until it happens
    goto    lane, step                    a step id, or step "resume": back where they left the main lane
    end     label, note, status           no_reply | opted_out | done
    empty                                 the builder's placeholder in a lane with no steps yet; ignored

Hard errors (a 400 on every save): the shape, unknown kinds, duplicate ids, a template id that doesn't exist, a wait
that isn't above 0. Issues (saved on a draft, refused on a live or paused campaign, and they stop Start): a lane that
doesn't end in end, goal or goto, an empty lane, a send with no template, a goto to a step that isn't there, a wait
between Intro messages shorter than Settings allows (shortest wait between Intro messages).
"""
from __future__ import annotations

import re
from typing import Any

from .models import WaCampaign, WaTemplate

Cat = WaTemplate.Category
LANES = ("main", "yes", "notnow", "stop")
LANE_NAMES = {"main": "Intro path", "yes": "YES path", "notnow": "Not now path", "stop": "STOP path"}
KINDS = ("start", "send", "wait", "reply", "track", "group", "goal", "goto", "end", "empty")
UNITS = ("hours", "days", "weeks")
GOALS = ("yes", "signup", "first_pc", "paid")
END_STATUS = ("no_reply", "opted_out", "done")
TRACK_IFS = tuple(f"track:{t}" for t in (WaTemplate.Track.PRIVACY, WaTemplate.Track.ACTIVITY, WaTemplate.Track.BOTH))
ONLY_IF = ("", "no_pc", *TRACK_IFS)
GROUP_CATEGORIES = tuple(c for c in Cat.values if c and c != Cat.QUICK)
ENDS_LANE = ("end", "goal", "goto")
ID = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
MAX_STEPS = 300
# Fields each kind keeps. Anything else on a step (the builder's lang, slot, who, cond...) is kept if it is a short value.
FIELDS: dict[str, tuple[str, ...]] = {
    "start": (), "send": ("template", "only_if"), "wait": ("n", "unit", "window"), "reply": ("n", "unit"), "track": (),
    "group": ("category", "count", "every", "window"), "goal": ("goal",), "goto": ("lane", "step"),
    "end": ("label", "note", "status"), "empty": (),
}
EXTRA_KEYS = 12


class PathError(Exception):
    """The path can't be saved: {"detail": ..., "errors": [...]}."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(errors[0] if errors else "This path can't be saved.")


def blank() -> dict[str, Any]:
    return {"v": 1, "lanes": {"main": [{"id": "s0", "k": "start"}], "yes": [], "notnow": [], "stop": []}}


def lanes(path: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """The four lanes, without the builder's empty placeholders."""
    got = (path or {}).get("lanes") or {}
    return {lane: [s for s in (got.get(lane) or []) if s.get("k") != "empty"] for lane in LANES}


def index(path: dict[str, Any] | None) -> dict[str, tuple[str, int, dict[str, Any]]]:
    """step id -> (lane, position in the lane, step)."""
    out: dict[str, tuple[str, int, dict[str, Any]]] = {}
    for lane, steps in lanes(path).items():
        for i, s in enumerate(steps):
            out[s["id"]] = (lane, i, s)
    return out


def hours(step: dict[str, Any]) -> int:
    return int(step.get("n", 0)) * {"hours": 1, "days": 24, "weeks": 168}.get(step.get("unit", "days"), 24)


# --- Checks --------------------------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def clean(path: Any) -> dict[str, Any]:
    """The path as it is stored, or PathError with every hard error found."""
    errors: list[str] = []
    if not isinstance(path, dict):
        raise PathError(["Send the path as {v, lanes}."])
    raw = path.get("lanes")
    if path.get("v", 1) != 1 or not isinstance(raw, dict):
        raise PathError(["Send the path as {v: 1, lanes: {main, yes, notnow, stop}}."])
    unknown = [k for k in raw if k not in LANES]
    if unknown:
        errors.append(f"Unknown lane: {', '.join(map(str, unknown))}. Use main, yes, notnow and stop.")
    out: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    total = 0
    template_ids: set[int] = set()
    for lane in LANES:
        steps = raw.get(lane) or []
        if not isinstance(steps, list):
            errors.append(f"The {LANE_NAMES[lane]} must be a list of steps.")
            continue
        kept: list[dict[str, Any]] = []
        for at, step in enumerate(steps):
            total += 1
            where = f"{LANE_NAMES[lane]}, step {at + 1}"
            if not isinstance(step, dict):
                errors.append(f"{where}: a step is an object.")
                continue
            sid, k = step.get("id"), step.get("k")
            if not isinstance(sid, str) or not ID.match(sid):
                errors.append(f"{where}: the id must be 1-24 letters, digits, - or _.")
                continue
            if sid in seen:
                errors.append(f"{where}: the id {sid} is used twice. Every step needs its own id.")
                continue
            seen.add(sid)
            if k not in KINDS:
                errors.append(f"{where} ({sid}): unknown step kind {k!r}.")
                continue
            if k == "start" and (lane != "main" or at != 0):
                errors.append(f"{where} ({sid}): Start can only be the first step of the Intro path.")
                continue
            s: dict[str, Any] = {"id": sid, "k": k}
            problem = _fields(k, step, s, template_ids)
            if problem:
                errors.append(f"{where} ({sid}): {problem}")
                continue
            extra = [key for key in step if key not in ("id", "k", *FIELDS[k])]
            for key in extra[:EXTRA_KEYS]:
                v = step[key]
                if v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200):
                    s[key] = v
            kept.append(s)
        out[lane] = kept
    if total > MAX_STEPS:
        errors.append(f"Keep a path to {MAX_STEPS} steps.")
    main = [s for s in out.get("main", []) if s["k"] != "empty"]
    if not main or main[0]["k"] != "start":
        errors.append("The Intro path starts with the Start step.")
    if template_ids:
        found = set(WaTemplate.objects.filter(pk__in=template_ids).values_list("pk", flat=True))
        for missing in sorted(template_ids - found):
            errors.append(f"Template {missing} no longer exists. Pick another.")
    if errors:
        raise PathError(errors)
    return {"v": 1, "lanes": out}


def _fields(k: str, step: dict[str, Any], s: dict[str, Any], template_ids: set[int]) -> str:
    """Copy kind k's fields from step into s, or say what's wrong."""
    if k == "send":
        t = step.get("template")
        if t in (None, "", 0):
            s["template"] = None
        else:
            n = _int(t)
            if n is None or n <= 0:
                return "the template is a template id."
            s["template"] = n
            template_ids.add(n)
        only = step.get("only_if") or ""
        if only not in ONLY_IF:
            return "only_if is \"\", \"no_pc\" or \"track:<privacy|activity|both>\"."
        s["only_if"] = only
    elif k in ("wait", "reply"):
        n = _int(step.get("n"))
        unit = step.get("unit") or "days"
        if n is None or n < 1:
            return "a wait must be longer than 0."
        if unit not in UNITS:
            return "the unit is hours, days or weeks."
        if hours({"n": n, "unit": unit}) > 365 * 24:
            return "keep a wait under a year."
        s.update(n=n, unit=unit)
        if k == "wait":
            s["window"] = bool(step.get("window", True))
    elif k == "group":
        count, every = _int(step.get("count")), _int(step.get("every"))
        category = step.get("category")
        if category not in GROUP_CATEGORIES:
            return "pick the category to send (Intro, Demo video...)."
        if count is None or count < 1 or every is None or every < 1:
            return "the count and the days between them must be above 0."
        if count > 120 or every > 90:
            return "keep it to 120 messages, at most 90 days apart."
        s.update(category=category, count=count, every=every, window=bool(step.get("window", True)))
    elif k == "goal":
        if step.get("goal") not in GOALS:
            return "the goal is yes, signup, first_pc or paid."
        s["goal"] = step["goal"]
    elif k == "goto":
        target = step.get("step")
        lane = step.get("lane") or "main"
        if target != "resume" and (not isinstance(target, str) or not ID.match(target)):
            return "go to a step id, or \"resume\"."
        if lane not in LANES:
            return "go to the main, yes, notnow or stop lane."
        s.update(lane="main" if target == "resume" else lane, step=target)
    elif k == "end":
        status = step.get("status") or "done"
        if status not in END_STATUS:
            return "the status is no_reply, opted_out or done."
        s.update(label=str(step.get("label") or "Ends here")[:120], note=str(step.get("note") or "")[:300], status=status)
    return ""


def issues(path: dict[str, Any], live: bool = False) -> list[dict[str, Any]]:
    """What stops the path from running ("error"), what it will do that staff may not expect ("warn"), and notes
    ("info"). {level, lane, step_id, text}."""
    out: list[dict[str, Any]] = []
    L = lanes(path)
    idx = index(path)
    ids = [s.get("template") for steps in L.values() for s in steps if s["k"] == "send" and s.get("template")]
    templates = {t.pk: t for t in WaTemplate.objects.filter(pk__in=ids)}
    from . import wa_flow

    def add(level: str, lane: str, sid: str, text: str) -> None:
        # step / message are the same as step_id / text (the Campaigns UI reads either).
        out.append({"level": level, "lane": lane, "step_id": sid, "step": sid, "text": text, "message": text})

    for lane, steps in L.items():
        name = LANE_NAMES[lane]
        real = [s for s in steps if s["k"] != "start"]
        if not real:
            add("error", lane, "", f"The {name} has no steps yet. Add at least an End.")
            continue
        if steps[-1]["k"] not in ENDS_LANE:
            add("error", lane, steps[-1]["id"], f"The {name} must finish with End, Goal or Go to.")
        for s in steps:
            if s["k"] == "send":
                t = templates.get(s.get("template")) if s.get("template") else None
                if t is None:
                    add("error", lane, s["id"], "Pick a template for this message.")
                elif t.archived:
                    add("error", lane, s["id"], f"{t.name} is archived. Pick another template.")
                elif wa_flow.is_draft(t):
                    add("warn", lane, s["id"], f"{t.name} is a draft. Write its English and Hinglish in Templates first: contacts wait here until then.")
                if lane == "stop":
                    add("info", lane, s["id"], "STOP ends every campaign for the number at once, so this message is never queued.")
            elif s["k"] == "goto":
                if s["step"] == "resume":
                    if lane == "main":
                        add("error", lane, s["id"], "Back to where they left the Intro path only works from a reply path.")
                elif s["step"] not in idx or idx[s["step"]][0] != s["lane"]:
                    add("error", lane, s["id"], f"Go to: there is no step {s['step']} in the {LANE_NAMES[s['lane']]}.")
                elif idx[s["step"]][2]["k"] == "start":
                    add("error", lane, s["id"], "Go to Start would add them again. Pick a step after it.")
            elif s["k"] == "group" and s["category"] == Cat.QUICK:
                add("error", lane, s["id"], "Quick replies answer a message; they can't be a group.")
    from . import wa_settings

    rules = wa_settings.current()
    shortest = rules.min_intro_wait
    started = False
    for s in L["main"]:
        if s["k"] in ("send", "group"):
            if s["k"] == "group" and s["every"] < shortest and s["count"] > 1:
                add("error", "main", s["id"], f"These go {s['every']} day{'s' if s['every'] > 1 else ''} apart. Intro messages wait at least {shortest} day{'s' if shortest > 1 else ''} (Settings).")
            started = True
        elif s["k"] in ("wait", "reply") and started and hours(s) < shortest * 24:
            add("error", "main", s["id"], f"Intro messages wait at least {shortest} day{'s' if shortest > 1 else ''} (Settings). Make this wait longer.")
    days = plan_days(path)
    limit = rules.max_days
    for s in L["main"]:
        last = days.get(s["id"], {}).get("last") or days.get(s["id"], {}).get("day")
        if s["k"] in ("group", "send") and last and last > limit:
            add("warn", "main", s["id"], f"Runs to day {last}, past day {limit}: shorten a wait. After day {limit} no reply means No reply.")
    return out


def blocking(found: list[dict[str, Any]], drafts_block: bool = False) -> list[dict[str, Any]]:
    """The issues that refuse a save on a running campaign (errors), or a Start (errors and drafts)."""
    return [i for i in found if i["level"] == "error" or (drafts_block and i["level"] == "warn" and "draft" in i["text"])]


# --- Days and labels -------------------------------------------------------------------------------------------------


def plan_days(path: dict[str, Any]) -> dict[str, dict[str, int]]:
    """The planned day of each main-lane message: Day 1 is the first message. {step id: {day, last?}} (a group has
    its first and last day). Waits for an answer count as their full length."""
    out: dict[str, dict[str, int]] = {}
    h = 0
    started = after_message = False
    for s in lanes(path)["main"]:
        day = 1 + h // 24
        if s["k"] == "send":
            out[s["id"]] = {"day": day}
            started = after_message = True
        elif s["k"] in ("wait", "reply"):
            if started:
                h += hours(s)
            after_message = False
        elif s["k"] == "group":
            # Right after a message the group's first one waits `every` days; after a wait it goes at once.
            first = day + s["every"] if after_message else day
            last = first + s["every"] * (s["count"] - 1)
            out[s["id"]] = {"day": first, "last": last}
            h = (last - 1) * 24
            started = after_message = True
        elif s["k"] in ("track", "goal", "goto"):
            after_message = False
    return out


def summary(path: dict[str, Any]) -> dict[str, int]:
    """For the campaign list: {steps, days, lanes}."""
    L = lanes(path)
    days = plan_days(path)
    last = max([d.get("last", d["day"]) for d in days.values()] or [0])
    return {
        "steps": sum(1 for steps in L.values() for s in steps if s["k"] != "start"),
        "days": last,
        "lanes": sum(1 for steps in L.values() if any(s["k"] != "start" for s in steps)),
    }


def type_of(t: WaTemplate | None) -> str:
    if t is None:
        return "Pick a template"
    return t.type_name or t.name


# --- Presets -------------------------------------------------------------------------------------------------------


def _a(qs: Any) -> WaTemplate | None:
    return qs.filter(archived=False, version_of__isnull=True).order_by("step", "id").first()


def _intro(day: int) -> int | None:
    t = _a(WaTemplate.objects.filter(category=Cat.OUTREACH, step=day))
    return t.pk if t else None


def _first(category: str, **filters: Any) -> int | None:
    t = _a(WaTemplate.objects.filter(category=category, **filters))
    return t.pk if t else None


def _quick(type_name: str) -> int | None:
    return _first(Cat.QUICK, type_name=type_name)


def _send(sid: str, template: int | None, only_if: str = "") -> dict[str, Any]:
    return {"id": sid, "k": "send", "template": template, "only_if": only_if}


def _wait(sid: str, n: int, unit: str = "days", window: bool = True) -> dict[str, Any]:
    return {"id": sid, "k": "wait", "n": n, "unit": unit, "window": window}


def _end(sid: str, label: str, note: str, status: str) -> dict[str, Any]:
    return {"id": sid, "k": "end", "label": label, "note": note, "status": status}


STOP_LANE_NOTE = "This number never gets a message again, in any campaign."
NO_REPLY_NOTE = "They are marked No reply and leave the campaign."


def preset_100_reviews() -> dict[str, Any]:
    """The 100 Reviews path from the board (path90): four Intro messages three days apart, 26 more one every three
    days, then the paths a reply opens."""
    P = WaTemplate.Track.PRIVACY
    return {"v": 1, "lanes": {
        "main": [
            {"id": "s0", "k": "start"},
            _send("m1", _intro(1)), _wait("m2", 3), _send("m3", _intro(4)), _wait("m4", 3),
            _send("m5", _intro(7)), _wait("m6", 3), _send("m7", _intro(10)),
            {"id": "m8", "k": "group", "category": Cat.OUTREACH, "count": 26, "every": 3, "window": True},
            _end("m9", "No reply by day 90", "They are marked No reply and leave the campaign. They can join a new one after 60 days.", "no_reply"),
        ],
        "yes": [
            _send("y1", _first(Cat.ENGAGE, track=P, step=1)), {"id": "y2", "k": "reply", "n": 2, "unit": "days"},
            {"id": "y3", "k": "track"}, _send("y4", _first(Cat.DEMO_VIDEO, track=P, step=1)), _wait("y5", 1),
            {"id": "y6", "k": "group", "category": Cat.DEMO_VIDEO, "count": 5, "every": 1, "window": True},
            _send("y7", _first(Cat.SETUP, step=1)), _wait("y8", 3), _send("y9", _first(Cat.SETUP, step=2), "no_pc"),
            _send("y10", _first(Cat.OFFER, stage=1, type_name="Startup pack")), {"id": "y11", "k": "goal", "goal": "paid"},
            _wait("y12", 14), _send("y13", _first(Cat.TESTIMONIAL, step=1)),
            _end("y14", "Done", "They finished the YES path.", "done"),
        ],
        "notnow": [_send("n1", _quick("Not now")), _wait("n2", 30), {"id": "n3", "k": "goto", "lane": "main", "step": "resume"}],
        "stop": [_send("t1", _quick("STOP / band karo")), _end("t2", "Opted out", STOP_LANE_NOTE, "opted_out")],
    }}


def preset_short() -> dict[str, Any]:
    """Short: Opener, Problem, Offer in 8 days (the board's fresh('short'))."""
    P = WaTemplate.Track.PRIVACY
    return {"v": 1, "lanes": {
        "main": [
            {"id": "s0", "k": "start"}, _send("m1", _intro(1)), _wait("m2", 3), _send("m3", _intro(4)), _wait("m4", 4),
            _send("m5", _first(Cat.OUTREACH, type_name="Offer")), _end("m6", "No reply after day 8", NO_REPLY_NOTE, "no_reply"),
        ],
        "yes": [
            _send("y1", _first(Cat.ENGAGE, track=P, step=1)), {"id": "y2", "k": "reply", "n": 2, "unit": "days"},
            _send("y3", _first(Cat.DEMO_VIDEO, track=P, step=1)), _send("y4", _first(Cat.SETUP, step=1)),
            {"id": "y5", "k": "goal", "goal": "signup"},
        ],
        "notnow": [_send("n1", _quick("Not now")), _end("n2", "Ends here", "They leave this campaign.", "done")],
        "stop": [_send("t1", _quick("STOP / band karo")), _end("t2", "Opted out", STOP_LANE_NOTE, "opted_out")],
    }}


PRESETS = (("100_reviews", "100 Reviews · 90 days"), ("short", "Short · 3 messages in 8 days"), ("blank", "Blank"))


def preset(key: str) -> dict[str, Any]:
    if key == "100_reviews":
        return preset_100_reviews()
    if key == "short":
        return preset_short()
    if key == "blank":
        return blank()
    raise PathError(["Pick a ready path: 100_reviews, short or blank."])


# --- The paths old campaigns convert to (wa_paths_convert.py) ------------------------------------------------------


def _intro_rows() -> list[WaTemplate]:
    """The live Intro rows by day; on a day with two rows only the last one (by id) is ever sent, as wa_flow does."""
    rows = list(WaTemplate.objects.filter(category=Cat.OUTREACH, archived=False, version_of__isnull=True).order_by("step", "id"))
    by_day: dict[int, WaTemplate] = {}
    for r in rows:
        by_day[r.step] = r
    return [by_day[d] for d in sorted(by_day)]


def converted_ninety() -> dict[str, Any]:
    """The 100 Reviews path as the "ninety" plan runs it today (wa_flow.py), with no Mon-Sat window so every due day
    stays what it is: the Intro rows by day (the first four as steps, the rest as a group when they are three days
    apart, else every one as a step), then the YES path by Track."""
    rows = _intro_rows()
    days = [1] + [r.step for r in rows[1:]]  # day 1 is the first message, whatever day its row says
    main: list[dict[str, Any]] = [{"id": "s0", "k": "start"}]
    head = rows[:4]
    rest = rows[4:]
    uniform = bool(rest) and all(b - a == 3 for a, b in zip(days[3:], days[4:]))
    shown = head if uniform else rows
    for i, r in enumerate(shown):
        if i:
            main.append(_wait(f"w{i}", days[i] - days[i - 1], window=False))
        main.append(_send(f"i{i + 1}", r.pk))
    if uniform:
        main.append({"id": "g1", "k": "group", "category": Cat.OUTREACH, "count": len(rest), "every": 3, "window": False})
    main.append(_end("e1", "No reply by day 90", NO_REPLY_NOTE, "no_reply"))
    g = lambda sid, cat, count: {"id": sid, "k": "group", "category": cat, "count": count, "every": 1, "window": False}  # noqa: E731
    return {"v": 1, "lanes": {
        "main": main,
        "yes": [
            g("y1", Cat.ENGAGE, 2), {"id": "y2", "k": "track"}, g("y3", Cat.ENGAGE, 4), g("y4", Cat.DEMO_VIDEO, 6),
            _send("y5", _first(Cat.SETUP, step=1)), _wait("y6", 3, window=False), _send("y7", _first(Cat.SETUP, step=2), "no_pc"),
            _send("y8", _first(Cat.OFFER, event=False)), {"id": "y9", "k": "goal", "goal": "first_pc"},
            _wait("y10", 14, window=False), _send("y11", _first(Cat.TESTIMONIAL, step=1)),
            _end("y12", "Done", "They finished the YES path.", "done"),
        ],
        "notnow": [_send("n1", _quick("Not now")), _wait("n2", 30, window=False), {"id": "n3", "k": "goto", "lane": "main", "step": "resume"}],
        "stop": [_end("t1", "Opted out", STOP_LANE_NOTE, "opted_out")],
    }}


def converted_touches(c: WaCampaign) -> dict[str, Any]:
    """The Short path as a "touches" campaign runs today (whatsapp.py): the first message, then each touch while the
    one before exists, waiting for an answer in between (an answer stops the touches), all counted from the first
    message. A YES gets the after-YES messages by day, by Track."""
    main: list[dict[str, Any]] = [{"id": "s0", "k": "start"}, _send("m1", c.first_id)]
    prev = 0
    chain = (("followup", c.followup_id, c.followup_days), ("touch3", c.touch3_id, c.touch3_days), ("touch4", c.touch4_id, c.touch4_days))
    for i, (_kind, tid, day) in enumerate(chain, start=2):
        if not tid:
            break
        main.append({"id": f"r{i}", "k": "reply", "n": max(1, day - prev), "unit": "days"})
        main.append(_send(f"m{i}", tid))
        prev = day
    main.append(_end("e1", "No reply", NO_REPLY_NOTE, "no_reply"))
    yes: list[dict[str, Any]] = []
    nurture = WaTemplate.objects.filter(kind=WaTemplate.Kind.NURTURE, language=WaTemplate.Language.EN, category="",
                                        archived=False).order_by("day", "track", "id")
    last = 0
    for i, t in enumerate(nurture, start=1):
        if t.track and f"track:{t.track}" not in TRACK_IFS:
            continue  # a retired Track: nobody can be on it
        if t.day > last:
            yes.append(_wait(f"yw{i}", t.day - last, window=False))
            last = t.day
        yes.append(_send(f"y{i}", t.pk, f"track:{t.track}" if t.track in (WaTemplate.Track.PRIVACY, WaTemplate.Track.ACTIVITY, WaTemplate.Track.BOTH) else ""))
    yes.append(_end("ye", "Done", "They had every after-YES message.", "done"))
    return {"v": 1, "lanes": {
        "main": main, "yes": yes,
        "notnow": [_end("n1", "Not now", "They said not now. Staff answer them on Today.", "done")],
        "stop": [_end("t1", "Opted out", STOP_LANE_NOTE, "opted_out")],
    }}
