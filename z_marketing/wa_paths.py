"""
The step-by-step path engine (owner, 11 Nov 2026), for campaigns with engine "path". The slots code (whatsapp.py,
wa_flow.py) is untouched and keeps running every campaign that isn't converted.

Where a contact is: WaContact.lane / step_id / step_due / step_entered, plus path_state (the engine's notes: the
campaign the position belongs to, whether step_due is on the lane's calendar, the Mon-Sat window, a group's cursor,
where they left the main lane, when they entered the lane). Positions are worked out lazily: a contact with no
position (just added, or moved in from another campaign) starts at Start the first time the engine looks at them.

The rules:

- Today is by day. A step is due when its due date (after the window) is today or earlier.
- One message per contact per day, as today. The daily limit counts main-lane messages (people who haven't replied).
- Main lane waits count on its calendar: from the planned day of the message before, not from when staff pressed Mark
  as sent, so a late Intro message doesn't push the later ones back (as the ninety and touches plans count from the
  first message). The calendar starts at the first message actually sent, and again after a step whose length isn't
  known in advance (an answer, the Track, a goal, a goto). In a reply lane every wait counts from when the message
  before it actually went (a conversation: "3 days after the setup steps" means after they got them).
- Window (Mon-Sat 10:00-19:00): a message due on a Sunday moves to Monday; the calendar after it doesn't move.
- Late Intro messages never pile up: when the next Intro message is already due too, the late one is skipped (as the
  ninety plan does). Messages after a reply are never skipped.
- A send step whose template this contact already got is skipped (except Quick replies). Engage and Demo video steps
  follow the contact's Track (Unassigned Engage leans on their words); an Offer step sends the live code for their
  pack, and waits while none is live. A draft template holds the contact there; Today shows it as a draft.
- A group sends the next `count` unsent templates of its category (step order, after the highest step they already
  got), one every `every` days. Right after a message its first one waits `every` days; after a wait it goes at once.
- Lanes: YES (Interested, Signed up, or a Track set after a YES) moves them to "yes", Not now to "notnow". STOP, band
  karo or mat bhejo ends everything for the number at once, whatever the path says: they sit at the STOP path's end
  and no message there is ever queued.
- Wait for their answer: an answer in a reply lane moves them to the next step; in the main lane it holds them there
  until staff sort the reply (YES, Not now, STOP). No answer by the end of the wait moves them to the next step.
- A paused or draft campaign queues nothing (replies still show on Today).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from . import wa_flow
from .models import WaCampaign, WaContact, WaMessage, WaTemplate
from .wa_path_model import LANE_NAMES, index, lanes, plan_days, type_of

PATH = WaCampaign.Engine.PATH
Cat = WaTemplate.Category
Track = WaTemplate.Track
CS = WaContact.CampaignStatus
Kind = WaMessage.Kind
OPEN, CLOSE = 10, 19
MAX_HOPS = 400
COLD = [Kind.FIRST, Kind.FOLLOWUP, Kind.TOUCH, Kind.CUSTOM]  # whatsapp.COLD
LANE_SHORT = {"main": "Intro", "yes": "YES", "notnow": "Not now", "stop": "STOP"}
GOAL_NAMES = {"yes": "Said YES", "signup": "Signed up", "first_pc": "First PC set up", "paid": "Paid"}


def on_path(contact: WaContact) -> bool:
    c = contact.campaign if contact.campaign_id else None
    return c is not None and c.engine == PATH


# --- Time ----------------------------------------------------------------------------------------------------------


def local(dt: datetime) -> datetime:
    return timezone.localtime(dt)


def at_open(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time(OPEN)))


def add(base: datetime, n: int, unit: str) -> datetime:
    """base + n units. Days and weeks land at the window's opening on that day, so a wait is by calendar day."""
    if unit == "hours":
        return base + timedelta(hours=n)
    return at_open(local(base).date() + timedelta(days=n * (7 if unit == "weeks" else 1)))


def shift(dt: datetime) -> datetime:
    """Into the window: before 10:00 goes to 10:00, after 19:00 to the next morning, a Sunday to Monday."""
    x = local(dt)
    if x.hour < OPEN:
        x = at_open(x.date())
    elif x.hour >= CLOSE:
        x = at_open(x.date() + timedelta(days=1))
    if x.weekday() == 6:
        x = at_open(x.date() + timedelta(days=1))
    return x


def dloc(dt: datetime) -> date:
    """The Indian date of an aware datetime (Today is by day)."""
    return local(dt).date()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def day_text(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def a_id(t: WaTemplate) -> int:
    return t.version_of_id or t.pk


def same_template(sent: WaTemplate, due: WaTemplate) -> bool:
    """The message staff sent is the step's: the same row, its version B, or its other-language twin."""
    return a_id(sent) == a_id(due) or (sent.twin_id is not None and sent.twin_id == due.pk) or (due.twin_id is not None and due.twin_id == sent.pk)


def stopped(c: WaContact) -> bool:
    return c.status == WaContact.Status.STOP or c.campaign_status == CS.OPTED_OUT


# --- The engine ----------------------------------------------------------------------------------------------------


@dataclass
class Out:
    """What the engine found. kind: due (send it today), wait (nothing until `when`), hold (waiting for something:
    note set_track | goal | answered | no_code | loop), ended."""

    kind: str
    step: dict[str, Any] | None = None
    template: WaTemplate | None = None
    when: datetime | None = None
    note: str = ""
    moved_from: datetime | None = None


class Ctx:
    """One contact on one campaign's path, at `now`. sim=True never writes (the schedule, the conversion check)."""

    def __init__(self, contact: WaContact, now: datetime | None = None, sim: bool = False,
                 cache: dict[str, Any] | None = None, path: dict[str, Any] | None = None) -> None:
        self.c = contact
        self.camp = contact.campaign
        self.path = path if path is not None else self.camp.path
        self.L = lanes(self.path)
        self.idx = index(self.path)
        self.now = now or timezone.now()
        self.sim = sim
        self.cache = cache if cache is not None else {}
        self.lane = contact.lane
        self.step_id = contact.step_id
        self.due = contact.step_due
        self.entered = contact.step_entered
        self.st: dict[str, Any] = copy.deepcopy(contact.path_state or {})
        self._start = (self.lane, self.step_id, self.due, self.entered, copy.deepcopy(self.st))
        self.effects: dict[str, Any] = {}
        self._theirs: str | None = None
        self.sent: set[int] = set()
        self.max_step: dict[str, int] = {}
        self.cats: set[str] = set()
        if contact.pk:
            rows = (WaMessage.objects.filter(contact=contact, template__isnull=False).exclude(kind=Kind.THEIRS)
                    .values_list("template_id", "template__version_of_id", "template__category", "template__step"))
            for tid, of, cat, step in rows:
                self.note_sent(of or tid, cat, step)

    # bookkeeping
    @property
    def today(self) -> date:
        return local(self.now).date()

    def note_sent(self, aid: int, cat: str, step: int) -> None:
        self.sent.add(aid)
        if cat:
            self.cats.add(cat)
            self.max_step[cat] = max(self.max_step.get(cat, 0), step or 0)

    def tpl(self, pk: Any) -> WaTemplate | None:
        if not pk:
            return None
        cache = self.cache.setdefault("tpl", {})
        if pk not in cache:
            cache[pk] = WaTemplate.objects.select_related("coupon", "version_of").filter(pk=pk).first()
        return cache[pk]

    def pool(self, category: str, track: str = "") -> list[WaTemplate]:
        cache = self.cache.setdefault("pool", {})
        key = (category, track)
        if key not in cache:
            cache[key] = list(wa_flow._rows(category, **({"track": track} if track else {})).order_by("step", "id"))
        return cache[key]

    def last_theirs(self) -> str:
        if self._theirs is None:
            self._theirs = wa_flow._last_theirs(self.c) if self.c.pk else ""
        return self._theirs

    def step(self) -> dict[str, Any] | None:
        hit = self.idx.get(self.step_id)
        return hit[2] if hit and hit[0] == self.lane else None

    def eff(self) -> datetime:
        due = self.due or self.now
        return shift(due) if self.st.get("win") else due

    def moved(self) -> datetime | None:
        due = self.due or self.now
        return due if self.st.get("win") and shift(due).date() != local(due).date() else None

    # moving
    def enter(self, step: dict[str, Any], base: datetime, cal: bool, win: bool = False, after_message: bool = False) -> None:
        lane = self.idx[step["id"]][0]
        if lane != self.lane:
            self.st["lane_at"] = _iso(self.now)
        self.lane, self.step_id, self.entered = lane, step["id"], self.now
        for key in ("answered", "finished", "ended"):
            self.st.pop(key, None)
        self.st.update(cal=cal, win=win, am=after_message)
        k = step["k"]
        if k in ("wait", "reply"):
            self.due = add(base, step["n"], step["unit"])
            self.st.update(cal=True, win=bool(step.get("window")) if k == "wait" else False)
        elif k == "group":
            self.st["g"] = {"id": step["id"], "cursor": self.max_step.get(step["category"], 0), "done": 0, "last": _iso(base)}
            self.due = add(base, step["every"], "days") if after_message else base
            self.st.update(cal=cal or after_message, win=bool(step.get("window", True)))
        else:
            self.due = base

    def enter_next(self, base: datetime, cal: bool, win: bool = False, after_message: bool = False) -> bool:
        steps = self.L.get(self.lane) or []
        at = next((i for i, s in enumerate(steps) if s["id"] == self.step_id), None)
        if at is not None and at + 1 < len(steps):
            self.enter(steps[at + 1], base, cal, win, after_message)
            return True
        self.st["finished"] = True
        return False

    def start(self) -> None:
        """A contact the engine hasn't placed on this campaign's path yet: they start at Start, now."""
        self.st = {"c": self.camp.id}
        self.lane = ""
        self.enter(self.L["main"][0], self.now, cal=False)

    def ensure(self) -> None:
        if self.st.get("c") != self.camp.id or not self.step_id or not self.L["main"]:
            if self.L["main"]:
                self.start()
            return
        if self.step() is None:  # the step went (an edit that didn't move them): the start of their lane
            steps = self.L.get(self.lane) or self.L["main"]
            first = steps[1] if steps is self.L["main"] and len(steps) > 1 else steps[0]
            self.enter(first, self.now, cal=False)

    def to_stop(self) -> Out:
        """STOP ends everything: they sit at the STOP path's end, and nothing there is ever queued."""
        steps = self.L["stop"]
        end = next((s for s in steps if s["k"] == "end"), steps[-1] if steps else None)
        if end is None:
            if self.lane != "stop":
                self.lane, self.step_id, self.due, self.entered = "stop", "", None, self.now
            self.st["finished"] = True
            return Out("ended")
        if self.lane != "stop" or self.step_id != end["id"]:
            if self.lane == "main":
                self.st["resume"] = {"step": self.step_id}
            self.st["lane_at"] = _iso(self.now)
            self.lane, self.step_id, self.due, self.entered = "stop", end["id"], None, self.now
        self.st["ended"] = end["id"]
        self.st.update(cal=False, win=False)
        return Out("ended", end)

    def move(self, lane: str) -> bool:
        """A reply sorted on Today moves them to that lane's first step (once: a second YES keeps them where they are)."""
        if stopped(self.c) or lane == "stop" or self.lane == lane:
            return False
        steps = self.L.get(lane) or []
        if not steps:
            return False
        if self.lane == "main" and not self.st.get("finished") and not self.st.get("ended"):
            self.st["resume"] = {"step": self.step_id, "g": copy.deepcopy(self.st.get("g"))}
        self.enter(steps[0], self.now, cal=False)
        return True

    # what a step needs
    def reached(self, goal: str) -> bool:
        c = self.c
        if goal == "yes":
            return c.yes_date is not None
        if goal == "signup":
            return c.status == WaContact.Status.SIGNED or c.campaign_status == CS.PAID or c.using_since is not None
        if goal == "first_pc":
            return c.using_since is not None
        return c.campaign_status == CS.PAID

    def _track_for(self, category: str) -> tuple[str, str]:
        """(track, note): the contact's Track for a tracked category; Unassigned Engage leans on their words."""
        if self.c.track:
            return self.c.track, ""
        if category == Cat.DEMO_VIDEO:
            return "", "set_track"
        return wa_flow.suggest_track(self.last_theirs()) or Track.PRIVACY, ""

    def _offer_ok(self, t: WaTemplate) -> bool:
        cp = t.coupon
        if cp is None or t.event or cp.status(self.today) != "live":
            return False
        plan = wa_flow.PACK_PLAN[self.c.pack or wa_flow.default_pack(self.c)]
        return not cp.plan_codes or plan in cp.plan_codes

    def resolve(self, t: WaTemplate | None, only_if: str = "") -> tuple[str, Any]:
        """("send", template) | ("skip", None) | ("hold", note) for a send step's template."""
        c = self.c
        if t is None or t.archived:
            return "skip", None
        if only_if == "no_pc" and c.using_since:
            return "skip", None
        if only_if.startswith("track:") and c.track != only_if[6:]:
            return "skip", None
        a = t.version_of if t.version_of_id else t
        cat = a.category
        if cat in wa_flow.TRACKED:
            track, note = self._track_for(cat)
            if note:
                return "hold", note
            if a.track != track:
                a = next((r for r in self.pool(cat, track) if r.step == a.step), a)
        if cat == Cat.OFFER:
            if Cat.OFFER in self.cats or c.offer_sent:
                return "skip", None
            if not self._offer_ok(a):
                a = wa_flow.offer_for(c, self.today)
                if a is None:
                    return "hold", "no_code"
        if a.pk in self.sent and cat != Cat.QUICK:
            return "skip", None
        return "send", wa_flow.version(a, c)

    def group_state(self, s: dict[str, Any]) -> dict[str, Any]:
        g = self.st.get("g")
        if not g or g.get("id") != s["id"]:
            g = self.st["g"] = {"id": s["id"], "cursor": self.max_step.get(s["category"], 0), "done": 0,
                                "last": _iso(self.due or self.now)}
        return g

    def group_item(self, s: dict[str, Any], cursor: int) -> tuple[str, Any]:
        cat = s["category"]
        track = ""
        if cat in wa_flow.TRACKED:
            track, note = self._track_for(cat)
            if note:
                return "hold", note
        for r in self.pool(cat, track):
            if r.step > cursor and r.pk not in self.sent:
                return "send", wa_flow.version(r, self.c)
        return "done", None

    def next_message(self, after: int, base: datetime, win: bool, cursor: int) -> tuple[WaTemplate, datetime] | None:
        """The next message on the lane's calendar after step index `after` (for the Intro catch-up)."""
        after_message = True
        for s in (self.L.get(self.lane) or [])[after + 1:]:
            k = s["k"]
            if k == "wait":
                base, win, after_message = add(base, s["n"], s["unit"]), bool(s.get("window")), False
            elif k == "send":
                kind, t = self.resolve(self.tpl(s.get("template")), s.get("only_if", ""))
                if kind == "skip":
                    continue
                if kind != "send" or t.category != Cat.OUTREACH or a_id(t) in self.sent:
                    return None
                return t, shift(base) if win else base
            elif k == "group":
                kind, t = self.group_item(s, cursor)
                if kind != "send":
                    return None
                due = add(base, s["every"], "days") if after_message else base
                return t, shift(due) if s.get("window", True) else due
            else:
                return None
        return None

    def _late_intro(self, t: WaTemplate) -> bool:
        return (self.lane == "main" and t.category == Cat.OUTREACH and bool(self.st.get("cal"))
                and self.due is not None and dloc(self.eff()) < self.today)

    def _catch_up_send(self, s: dict[str, Any], t: WaTemplate) -> bool:
        if not self._late_intro(t):
            return False
        at = next(i for i, x in enumerate(self.L[self.lane]) if x["id"] == s["id"])
        nxt = self.next_message(at, self.due, bool(self.st.get("win")), max(self.max_step.get(Cat.OUTREACH, 0), t.step))
        if nxt is None or dloc(nxt[1]) > self.today:
            return False
        self.st.setdefault("skipped", []).append(s["id"])
        self.st["skipped"] = self.st["skipped"][-20:]
        self.enter_next(self.due, True, bool(self.st.get("win")), after_message=True)
        return True

    def consume(self, s: dict[str, Any], g: dict[str, Any], t: WaTemplate, at: datetime | None) -> None:
        """A group item went (at = when it was sent) or was skipped (at None)."""
        base = self.due if (at is None or (self.st.get("cal") and self.lane == "main")) else at
        g["cursor"] = max(g["cursor"], t.step)
        g["done"] += 1
        g["last"] = _iso(base)
        self.due = add(base, s["every"], "days")
        self.st["cal"] = True

    def _catch_up_group(self, s: dict[str, Any], g: dict[str, Any], t: WaTemplate) -> bool:
        """A late Intro item of a group is skipped when the next Intro message (in the group or after it) is due too."""
        if not self._late_intro(t):
            return False
        cursor = max(g["cursor"], t.step)
        nxt = None
        if g["done"] + 1 < s["count"]:
            kind, x = self.group_item(s, cursor)
            if kind == "send":
                when = add(self.due, s["every"], "days")
                nxt = (x, shift(when) if self.st.get("win") else when)
        if nxt is None:  # t is the group's last one: the message after the group
            at = next(i for i, x in enumerate(self.L[self.lane]) if x["id"] == s["id"])
            nxt = self.next_message(at, self.due, bool(self.st.get("win")), cursor)
        if nxt is None or nxt[0].category != Cat.OUTREACH or dloc(nxt[1]) > self.today:
            return False
        self.consume(s, g, t, None)
        return True

    def group_done(self, g: dict[str, Any]) -> None:
        base = _dt(g.get("last")) or self.due or self.now
        self.enter_next(base, bool(self.st.get("cal")), bool(self.st.get("win")), after_message=g.get("done", 0) > 0)

    def goto(self, s: dict[str, Any]) -> None:
        if s["step"] == "resume":
            r = self.st.pop("resume", None) or {}
            main = self.L["main"]
            target = self.idx.get(r.get("step") or "")
            step = target[2] if target and target[0] == "main" and target[2]["k"] not in ("start", "end") else (main[1] if len(main) > 1 else main[0])
            self.enter(step, self.now, cal=False)
            if step["k"] == "group" and (r.get("g") or {}).get("id") == step["id"]:
                self.st["g"] = r["g"]
                self.due = self.now
            return
        hit = self.idx.get(s["step"])
        if hit is None:
            self.st["finished"] = True
            return
        self.enter(hit[2], self.now, cal=False)

    def end(self, s: dict[str, Any]) -> None:
        if self.st.get("ended") == s["id"]:
            return
        self.st["ended"] = s["id"]
        status = s.get("status")
        if status == "no_reply" and self.c.campaign_status in (CS.NONE, CS.MESSAGED):
            self.effects["campaign_status"] = CS.NO_REPLY
        elif status == "opted_out":
            self.effects["campaign_status"] = CS.OPTED_OUT

    # the walk
    def run(self) -> Out:
        if not self.L["main"]:
            return Out("hold", note="no_path")
        if stopped(self.c):
            return self.to_stop()
        self.ensure()
        for _ in range(MAX_HOPS):
            s = self.step()
            if s is None:
                return Out("hold", note="no_step")
            if self.st.get("finished"):
                return Out("ended", s)
            k = s["k"]
            if k in ("start", "empty"):
                self.enter_next(self.due or self.now, False)
            elif k == "send":
                kind, t = self.resolve(self.tpl(s.get("template")), s.get("only_if", ""))
                if kind == "skip":
                    self.enter_next(self.due or self.now, bool(self.st.get("cal")), bool(self.st.get("win")), bool(self.st.get("am")))
                    continue
                if kind == "hold":
                    return Out("hold", s, note=t, when=self.eff())
                when = self.eff()
                if dloc(when) > self.today:
                    return Out("wait", s, t, when, moved_from=self.moved())
                if self._catch_up_send(s, t):
                    continue
                return Out("due", s, t, when, moved_from=self.moved())
            elif k == "wait":
                when = self.eff()
                if dloc(when) > self.today:
                    return Out("wait", s, None, when)
                self.enter_next(self.due, True, bool(self.st.get("win")))
            elif k == "reply":
                if self.st.get("answered"):
                    return Out("hold", s, note="answered")
                if self.due is not None and dloc(self.due) > self.today:
                    return Out("wait", s, None, self.due)
                self.enter_next(self.due or self.now, True)
            elif k == "track":
                if not self.c.track:
                    return Out("hold", s, note="set_track")
                self.enter_next(self.now, False)
            elif k == "goal":
                if not self.reached(s["goal"]):
                    return Out("hold", s, note="goal")
                self.enter_next(self.now, False)
            elif k == "group":
                g = self.group_state(s)
                kind, t = ("done", None) if g["done"] >= s["count"] else self.group_item(s, g["cursor"])
                if kind == "done":
                    self.group_done(g)
                    continue
                if kind == "hold":
                    return Out("hold", s, note=t, when=self.eff())
                when = self.eff()
                if dloc(when) > self.today:
                    return Out("wait", s, t, when, moved_from=self.moved())
                if self._catch_up_group(s, g, t):
                    continue
                return Out("due", s, t, when, moved_from=self.moved())
            elif k == "goto":
                self.goto(s)
            elif k == "end":
                self.end(s)
                return Out("ended", s)
        return Out("hold", self.step(), note="loop")

    def complete(self, t: WaTemplate, at: datetime) -> None:
        """The due message went at `at`: on to what follows."""
        s = self.step()
        self.note_sent(a_id(t), t.category, t.step)
        if s is None:
            return
        if s["k"] == "group":
            self.consume(s, self.group_state(s), t, at)
        elif s["k"] == "send":
            base = self.due if (self.st.get("cal") and self.due and self.lane == "main") else at
            self.enter_next(base, True, bool(self.st.get("win")), after_message=True)

    def answered(self) -> None:
        s = self.step()
        if s is None or s["k"] != "reply" or self.st.get("answered"):
            return
        if self.lane == "main":
            self.st["answered"] = True
        else:
            self.enter_next(self.now, False)

    # labels
    def day1(self) -> date | None:
        return self.c.first_sent

    def label(self, out: Out) -> str:
        """Day 4 · Problem (main lane) / YES · Need discovery."""
        if out.step is None:
            return ""
        what = type_of(out.template) if out.template is not None else step_title(out.step, self)
        if self.lane == "main" and out.template is not None:
            first = self.day1()
            when = (out.when or self.now)
            day = 1 if first is None else max(1, (local(when).date() - first).days + 1)
            return f"Day {day} · {what}"
        return f"{LANE_SHORT.get(self.lane, self.lane)} · {what}"

    # writing back
    def changed(self) -> bool:
        return (self.lane, self.step_id, self.due, self.entered, self.st) != self._start or bool(self.effects)

    def write(self) -> None:
        if self.sim or not self.changed():
            return
        c = self.c
        c.lane, c.step_id, c.step_due, c.step_entered, c.path_state = self.lane, self.step_id, self.due, self.entered, self.st
        fields = ["lane", "step_id", "step_due", "step_entered", "path_state"]
        for f, v in self.effects.items():
            setattr(c, f, v)
            fields.append(f)
        c.save(update_fields=fields)
        self._start = (self.lane, self.step_id, self.due, self.entered, copy.deepcopy(self.st))
        self.effects = {}


def _template(pk: Any, cache: dict[Any, Any] | None) -> WaTemplate | None:
    if not pk:
        return None
    if cache is None:
        return WaTemplate.objects.filter(pk=pk).first()
    if pk not in cache:
        cache[pk] = WaTemplate.objects.filter(pk=pk).first()
    return cache[pk]


def step_title(s: dict[str, Any], ctx: Ctx | None = None, days: dict[str, dict[str, int]] | None = None,
               cache: dict[Any, Any] | None = None) -> str:
    """A step in a few words, for labels and the numbers per step."""
    k = s["k"]
    unit = lambda x: f"{x['n']} {x['unit'][:-1] if x['n'] == 1 else x['unit']}"  # noqa: E731
    if k == "start":
        return "Start"
    if k == "send":
        return type_of(ctx.tpl(s.get("template")) if ctx else _template(s.get("template"), cache))
    if k == "wait":
        return f"Wait {unit(s)}"
    if k == "reply":
        return f"Wait for their answer · up to {unit(s)}"
    if k == "track":
        return "Set the Track"
    if k == "group":
        name = WaTemplate.Category(s["category"]).label
        d = (days or {}).get(s["id"])
        return f"{s['count']} {name} messages" + (f" · days {d['day']}-{d['last']}" if d else "")
    if k == "goal":
        return f"Goal · {GOAL_NAMES.get(s['goal'], s['goal'])}"
    if k == "goto":
        return "Back to the Intro path" if s["step"] == "resume" else f"Go to {LANE_NAMES.get(s['lane'], s['lane'])}"
    if k == "end":
        return s.get("label") or "Ends here"
    return k


# --- Entry points --------------------------------------------------------------------------------------------------


def settle(contact: WaContact, now: datetime | None = None, cache: dict[str, Any] | None = None) -> tuple[Out, Ctx]:
    """Bring the contact's position up to `now` and save it."""
    ctx = Ctx(contact, now, cache=cache)
    out = ctx.run()
    ctx.write()
    return out, ctx


def due_template(contact: WaContact, now: datetime | None = None) -> tuple[WaTemplate | None, str]:
    """(template, note) for the next message on the contact's path (whatsapp.draft): due today, or held with a note."""
    out, _ = settle(contact, now)
    if out.kind == "due":
        return out.template, ""
    if out.kind == "hold":
        return None, out.note
    return None, ""


def _sent_today_main(campaign: WaCampaign, today: date) -> int:
    return (WaMessage.objects.filter(contact__campaign=campaign, at__date=today)
            .filter(Q(kind__in=COLD) | Q(lane="main") | Q(kind=Kind.FLOW, template__category=Cat.OUTREACH)).count())


def queue(now: datetime | None = None) -> list[dict[str, Any]]:
    """Today's messages for live path campaigns: reply lanes first (no daily limit), then the main lane within each
    campaign's daily limit. Rows have the slots row's keys plus step_id, step_label and lane."""
    now = now or timezone.now()
    today = local(now).date()
    first: list[dict[str, Any]] = []
    cold: list[dict[str, Any]] = []
    cache: dict[str, Any] = {}
    for camp in WaCampaign.objects.filter(engine=PATH, status=WaCampaign.Status.LIVE):
        room = max(0, camp.daily_limit - _sent_today_main(camp, today))
        done_today = set(WaMessage.objects.filter(contact__campaign=camp, at__date=today).exclude(kind=Kind.THEIRS)
                         .values_list("contact_id", flat=True))
        contacts = (camp.contacts.filter(opted_in=True).exclude(status=WaContact.Status.STOP)
                    .exclude(campaign_status=CS.OPTED_OUT).exclude(paused_until__gt=today).select_related("campaign"))
        mine: list[tuple[datetime, int, dict[str, Any]]] = []
        for contact in contacts.order_by("step_due", "id"):
            out, ctx = settle(contact, now, cache)
            if contact.needs_reply or contact.pk in done_today:
                continue
            if out.kind == "due":
                row = {"contact": contact, "kind": "flow", "template": out.template, "note": ""}
            elif out.kind == "hold" and out.note == "set_track":
                row = {"contact": contact, "kind": "flow", "template": None, "note": "set_track"}
            else:
                continue
            row.update(step_id=ctx.step_id, step_label=ctx.label(out), lane=ctx.lane)
            if ctx.lane == "main":
                mine.append((out.when or now, contact.pk, row))
            else:
                first.append(row)
        for _, _, row in sorted(mine, key=lambda x: (x[0], x[1])):
            if room <= 0:
                break
            cold.append(row)
            room -= 1
    return first + cold


def after_act(contact: WaContact, action: str, now: datetime | None = None) -> None:
    """Today's reply buttons move the lane: YES -> yes, Not now -> notnow, STOP -> the end of everything."""
    ctx = Ctx(contact, now)
    ctx.run()
    if action in ("interested", "signed") or (action == "track" and contact.yes_date is not None):
        ctx.move("yes")
    elif action == "notnow":
        ctx.move("notnow")
    ctx.run()
    ctx.write()


def after_reply(contact: WaContact, now: datetime | None = None) -> None:
    """They wrote: a Wait for their answer step sees it."""
    ctx = Ctx(contact, now)
    ctx.run()
    if not stopped(contact):
        ctx.answered()
        ctx.run()
    ctx.write()


def legacy_kind(contact: WaContact, t: WaTemplate) -> str | None:
    """A converted touches campaign's message, as the slots code records it (so --revert finds the contact where the
    slots code expects it): first / followup / touch3 / touch4, or None."""
    c = contact.campaign
    for kind, tid, step in (("first", c.first_id, 0), ("followup", c.followup_id, 1), ("touch3", c.touch3_id, 2), ("touch4", c.touch4_id, 3)):
        if tid and (t.pk == tid or t.twin_id == tid) and contact.step == step:
            return kind
    return None


def record_sent(contact: WaContact, kind: str, text: str, actor: str, picked: WaTemplate | None) -> WaContact:
    """whatsapp.mark_sent for a path contact: record it as the slots code would (so its fields stay right), then move
    the path when it is the step's message. Called inside mark_sent's transaction."""
    from . import whatsapp as W

    now = timezone.now()
    ctx = Ctx(contact, now)
    out = ctx.run()
    due = out.template if out.kind in ("due", "wait") else None
    t = picked
    if t is None:
        if out.kind == "hold" and out.note == "set_track":
            raise W.WaError("Set the contact's Track first.")
        if due is None or out.kind != "due":
            raise W.WaError("Nothing is due for this contact.")
        t = due
    lane, step = ctx.lane, ctx.step()
    if t.category:
        msg = W._record_flow(contact, t, text, actor)
    else:
        legacy = legacy_kind(contact, t)
        msg = W._record_legacy(contact, legacy, text, actor) if legacy else W._record_older(contact, t, text, actor)
        msg.template = t
    matched = due is not None and same_template(t, due)
    if lane == "main" and contact.first_sent is None:
        contact.first_sent, contact.step = W.today(), max(contact.step, 1)
        if contact.status == WaContact.Status.READY:
            contact.status = WaContact.Status.MESSAGED
        contact.save(update_fields=["first_sent", "step", "status"])
    if matched:
        ctx.complete(t, now)
    msg.campaign, msg.lane = ctx.camp, lane
    msg.step_id = step["id"] if matched and step else ""
    msg.save(update_fields=["campaign", "lane", "step_id", "template"])
    ctx.run()
    ctx.write()
    return contact


# --- The schedule and the numbers ----------------------------------------------------------------------------------


def simulate(campaign: WaCampaign, contact: WaContact | None = None, now: datetime | None = None, limit: int = 120,
             days: int = 400, path: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every message from the contact's position on, if each one goes on its due day and nobody replies. Without a
    contact: someone who joins now. [{step_id, lane, day, date, moved_from, template, template_id, label}]"""
    now = now or timezone.now()
    who = contact if contact is not None else WaContact(pk=0, campaign=campaign, firm="", phone="")
    if contact is not None and contact.campaign_id != campaign.pk:
        who = copy.copy(contact)
        who.campaign, who.lane, who.step_id, who.step_due, who.path_state = campaign, "", "", None, {}
    ctx = Ctx(who, now, sim=True, path=path)
    start = ctx.today
    last: date | None = None
    if contact is not None and contact.pk and WaMessage.objects.filter(contact=contact, at__date=start).exclude(kind=Kind.THEIRS).exists():
        last = start
    first_main = who.first_sent
    out_rows: list[dict[str, Any]] = []
    for _ in range(limit * 6):
        if len(out_rows) >= limit or (ctx.today - start).days > days:
            break
        out = ctx.run()
        if out.kind == "wait":
            if out.when is None or local(out.when).date() <= ctx.today:
                break
            ctx.now = max(ctx.now, at_open(local(out.when).date()))
            continue
        if out.kind == "hold" and out.note == "no_code":
            ctx.now = at_open(ctx.today + timedelta(days=1))
            continue
        if out.kind != "due":
            break
        if last is not None and ctx.today <= last:
            ctx.now = at_open(last + timedelta(days=1))
            continue
        d = ctx.today
        if ctx.lane == "main" and first_main is None:
            first_main = d
        if ctx.lane == "main":
            day = (d - first_main).days + 1
        else:
            entered = _dt(ctx.st.get("lane_at")) or ctx.entered or ctx.now
            day = (d - local(entered).date()).days
        t = out.template
        out_rows.append({
            "step_id": ctx.step_id, "lane": ctx.lane, "day": day, "date": d.isoformat(),
            "moved_from": day_text(local(out.moved_from).date()) if out.moved_from else None,
            "template_id": t.pk, "template": t.name, "label": f"{'Day ' + str(day) if ctx.lane == 'main' else LANE_SHORT[ctx.lane]} · {type_of(t)}",
        })
        ctx.complete(t, ctx.now)
        last = d
    return out_rows


def first_due(campaign: WaCampaign, contact: WaContact, now: datetime | None = None,
              path: dict[str, Any] | None = None) -> tuple[Any, date] | None:
    """(template A id or "set_track", date) of the next message the path engine would queue, or None."""
    now = now or timezone.now()
    ctx = Ctx(contact, now, sim=True, path=path)
    for _ in range(400):
        out = ctx.run()
        if out.kind == "due":
            return a_id(out.template), max(local(out.when).date(), ctx.today)
        if out.kind == "wait":
            if out.when is None or local(out.when).date() <= ctx.today:
                return None
            ctx.now = at_open(local(out.when).date())
            continue
        if out.kind == "hold" and out.note == "set_track":
            return "set_track", max(local(out.when).date(), ctx.today) if out.when else ctx.today
        if out.kind == "hold" and out.note == "no_code" and (ctx.today - local(now).date()).days < 60:
            ctx.now = at_open(ctx.today + timedelta(days=1))
            continue
        return None
    return None


def step_stats(campaign: WaCampaign) -> dict[str, dict[str, int]]:
    """{step id: {here, sent, replies, reply_rate}}: people at the step now, messages it sent, and how many of those
    got an answer within 3 days (as the Templates page counts them)."""
    from .wa_templates import REPLY_WITHIN, rate

    path = campaign.path or {}
    idx = index(path)
    main = lanes(path)["main"]
    start = main[0]["id"] if main else ""
    stop_end = next((s["id"] for s in lanes(path)["stop"] if s["k"] == "end"), "")
    out: dict[str, dict[str, int]] = {sid: {"here": 0, "sent": 0, "replies": 0, "reply_rate": 0} for sid in idx}
    for sid, status, cs, state in campaign.contacts.values_list("step_id", "status", "campaign_status", "path_state"):
        if status == WaContact.Status.STOP or cs == CS.OPTED_OUT:
            sid = stop_end or sid
        elif not sid or (state or {}).get("c") != campaign.pk:
            sid = start
        if sid in out:
            out[sid]["here"] += 1
    sends = list(WaMessage.objects.filter(campaign=campaign).exclude(step_id="").exclude(kind=Kind.THEIRS).values("step_id", "contact_id", "at"))
    theirs: dict[int, list[Any]] = {}
    for m in WaMessage.objects.filter(contact_id__in={s["contact_id"] for s in sends}, kind=Kind.THEIRS).values("contact_id", "at"):
        theirs.setdefault(m["contact_id"], []).append(m["at"])
    for s in sends:
        row = out.get(s["step_id"])
        if row is None:
            continue
        row["sent"] += 1
        row["replies"] += int(any(s["at"] <= t <= s["at"] + REPLY_WITHIN for t in theirs.get(s["contact_id"], [])))
    for row in out.values():
        row["reply_rate"] = rate(row["replies"], row["sent"])
    return out


def steps_view(campaign: WaCampaign) -> list[dict[str, Any]]:
    """The path in order with each step's numbers, for Analytics."""
    stats = step_stats(campaign)
    days = plan_days(campaign.path or {})
    cache: dict[Any, Any] = {}
    out = []
    for lane, steps in lanes(campaign.path or {}).items():
        for s in steps:
            title = step_title(s, None, days, cache)
            if s["k"] == "send":
                title = (f"Day {days[s['id']]['day']} · " if lane == "main" and s["id"] in days else f"{LANE_SHORT[lane]} · ") + title
            out.append({"step_id": s["id"], "lane": lane, "k": s["k"], "label": title, **stats.get(s["id"], {})})
    return out


def contact_view(contact: WaContact, cache: dict[Any, Any] | None = None) -> dict[str, Any]:
    """{step_label, next_due} from the stored position (no walk: cheap enough for the contact list)."""
    if not on_path(contact) or not contact.step_id or (contact.path_state or {}).get("c") != contact.campaign_id:
        return {"step_label": "", "next_due": None}
    hit = index(contact.campaign.path).get(contact.step_id)
    if hit is None:
        return {"step_label": "", "next_due": None}
    lane, _, s = hit
    st = contact.path_state or {}
    due = contact.step_due
    when = (shift(due) if st.get("win") else due) if due else None
    if s["k"] in ("send", "group") and lane == "main":
        day = 1 if contact.first_sent is None or when is None else max(1, (local(when).date() - contact.first_sent).days + 1)
        label = f"Day {day} · {step_title(s, cache=cache)}"
    else:
        label = f"{LANE_SHORT.get(lane, lane)} · {step_title(s, cache=cache)}"
    next_due = local(when).date().isoformat() if when and s["k"] in ("send", "group", "wait", "reply") and not st.get("answered") else None
    if stopped(contact) or st.get("finished") or st.get("ended"):
        next_due = None
    return {"step_label": label, "next_due": next_due}


# --- Editing a live path -------------------------------------------------------------------------------------------


def _next_surviving(old_steps: list[dict[str, Any]], new_steps: list[dict[str, Any]], sid: str, lane: str) -> dict[str, Any] | None:
    """Where a contact at removed step `sid` goes: the step after the last step before it that is still there (a step
    put in its place, or the one that followed it); the lane's first step when none before it is left."""
    if not new_steps:
        return None
    new_ids = [s["id"] for s in new_steps]
    at = next((i for i, s in enumerate(old_steps) if s["id"] == sid), None)
    prev = None
    if at is not None:
        prev = next((old_steps[j]["id"] for j in range(at - 1, -1, -1) if old_steps[j]["id"] in new_ids), None)
    if prev is None:
        skip_start = lane == "main" and new_steps[0]["k"] == "start" and len(new_steps) > 1
        return new_steps[1] if skip_start else new_steps[0]
    k = new_ids.index(prev)
    return new_steps[k + 1] if k + 1 < len(new_steps) else new_steps[-1]


def apply_edit(campaign: WaCampaign, old: dict[str, Any], new: dict[str, Any], now: datetime | None = None) -> int:
    """After the path changed (campaign.path is already `new`): contacts at a removed step move to the next step, a
    step whose kind changed starts again for those at it, and 'back to the Intro path' pointers follow. Returns how
    many contacts moved. Contacts at a step that is still there keep their place and their due day."""
    now = now or timezone.now()
    old_L, new_L = lanes(old), lanes(new)
    old_idx, new_idx = index(old), index(new)
    moved = 0
    for contact in campaign.contacts.exclude(step_id="").select_related("campaign"):
        st = contact.path_state or {}
        if st.get("c") != campaign.pk:
            continue
        ctx = Ctx(contact, now)
        sid, lane = contact.step_id, contact.lane
        hit = new_idx.get(sid)
        resume = (ctx.st.get("resume") or {}).get("step")
        if resume and (resume not in new_idx or new_idx[resume][0] != "main"):
            target = _next_surviving(old_L["main"], new_L["main"], resume, "main")
            ctx.st["resume"] = {"step": target["id"]} if target else {}
        if hit and hit[0] == lane:
            before = old_idx.get(sid)
            if before and before[2]["k"] != hit[2]["k"]:
                ctx.enter(hit[2], now, cal=False)
            ctx.write()
            continue
        if stopped(contact) or lane == "stop":
            ctx.to_stop()
            ctx.write()
            continue
        target = _next_surviving(old_L.get(lane, []), new_L.get(lane, []), sid, lane)
        if target is None:
            target = new_L["main"][1] if len(new_L["main"]) > 1 else new_L["main"][0]
        ctx.enter(target, now, cal=False)
        ctx.write()
        moved += 1
    return moved
