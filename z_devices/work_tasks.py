"""
FR-20 v2 + v1.4 team tasks: work the owner hands one or more members, worked from each member's Zinwe window.

Contract: D:/Programming/zinwe-wt/_v14/BRIEF.md section 1a and _v14/api-v14.md (older: .claude/dev/docs/15-api-reference.md).
Views stay thin; every rule about who may do what, and which status follows, lives here.

Status flow: sent -> seen -> (accepted) -> progress -> review -> approved; side states help, cantdo and cancelled.

People (v1.4): every assignee is a TaskPerson (the legacy `Task.member` is the first one, for Zinwe windows older than
1.4). Each person has their own seen / started / done times and state (sent, seen, progress, done, help, cantdo).
- mode "all": everyone has to finish. mode "any": the first to start takes it, the others are taken off it.
- Steps have an owner (null = anyone on the task); only the owner ticks it. With `in_order` a step needs every earlier
  step done first. When every step is done (or, with no steps, every person said done) the task waits for the
  owner's OK (review). On approve a repeating task makes its next copy (status sent), once.

- The member (device) can: seen, accept, start, tick / untick, help, cantdo, finish, and the pre-1.4 done / note.
- The owner can: approve, reopen, cancel, restore, nudge (once an hour per task), reply (help -> progress), reassign,
  move_due. The member can't approve; the owner can't start.
- Asking for the status a task already has is a no-op (the Windows app's outbox may send an action twice).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from django.db import transaction
from django.db.models import Count, F, Prefetch, Q, QuerySet, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from z_analytics.services import track
from z_auth.models import User

from . import web_push
from .models import Device, Member, Message, Task, TaskActivity, TaskChatRead, TaskPerson, TaskStep
from .owner_feed import TASK_ACTIONS

S = Task.Status
BY_OWNER = Task.By.OWNER
BY_MEMBER = Task.By.MEMBER

MAX_STEPS = 20
MAX_PEOPLE = 50
# The member's window keeps approved tasks this long, then they drop off its list.
DEVICE_APPROVED_DAYS = 7
NUDGE_EVERY = timedelta(hours=1)
OWNER_LIST_LIMIT = 300
CHAT_LINE_MAX = 2000

OWNER_ACTIONS = ("approve", "reopen", "cancel", "nudge", "reply", "reassign", "move_due", "restore")
MEMBER_ACTIONS = ("seen", "accept", "start", "help", "done", "note", "tick", "untick", "cantdo", "finish")

# Still being worked on (not waiting for the OK, not closed).
WORKING = (S.SENT, S.SEEN, S.ACCEPTED, S.PROGRESS, S.HELP, S.CANTDO)
NUDGEABLE = (S.SENT, S.SEEN, S.ACCEPTED, S.PROGRESS, S.HELP)

# owner action -> (statuses it may start from, status it leads to).
_OWNER_MOVES: dict[str, tuple[tuple[str, ...], str]] = {
    "approve": ((*WORKING, S.REVIEW), S.APPROVED),
    "reopen": ((S.REVIEW, S.APPROVED, S.CANCELLED, S.HELP, S.CANTDO), S.PROGRESS),
    "cancel": ((*WORKING, S.REVIEW), S.CANCELLED),
}
_ACTIVITY = {"approve": "approved", "reopen": "reopened", "cancel": "cancelled"}


class TaskRefused(Exception):
    """The action isn't allowed for this task's status (or the request is incomplete): a 400 for `field`."""

    def __init__(self, message: str, field: str = "action") -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def first_name(name: str) -> str:
    return (name or "").split(" ")[0] or name or ""


def names_list(names: list[str]) -> str:
    """["Priya", "Karan", "Emily"] -> "Priya, Karan and Emily"."""
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


# --- Reading ----------------------------------------------------------------


def with_details(qs: QuerySet) -> QuerySet:
    """Everything the Task JSON needs, in a few queries however many tasks (+ `talk_count`, v1.3.5 F-17)."""
    return qs.select_related("member", "owner").prefetch_related(
        Prefetch("steps", queryset=TaskStep.objects.order_by("position", "id")),
        Prefetch("activity", queryset=TaskActivity.objects.select_related("member").order_by("at", "id")),
        Prefetch("people", queryset=TaskPerson.objects.select_related("member").order_by("position", "id")),
    ).annotate(talk_count=Count("talk", distinct=True))


def _for_member(member_id: int) -> Q:
    """Tasks this member is on (a TaskPerson, or the legacy single member)."""
    return Q(member_id=member_id) | Q(pk__in=TaskPerson.objects.filter(member_id=member_id).values("task_id"))


def tasks_for_owner(owner: User, status: str = "open", member_id: int | None = None) -> QuerySet:
    """GET tasks/ (owner): open = not approved/cancelled, done = approved or cancelled, all. Newest activity first."""
    qs = Task.objects.filter(owner=owner)
    if status == "open":
        qs = qs.exclude(status__in=Task.CLOSED)
    elif status == "done":
        qs = qs.filter(status__in=Task.CLOSED)
    if member_id is not None:
        qs = qs.filter(_for_member(member_id))
    return with_details(qs.order_by("-updated_at", "-id"))[:OWNER_LIST_LIMIT]


def tasks_for_device(device: Device, now: Any = None) -> QuerySet:
    """GET tasks/ (device): the tasks the PC's member is on, open + the ones approved in the last 7 days."""
    if device.member_id is None:
        return Task.objects.none()
    now = now or timezone.now()
    recent = Q(status=S.APPROVED, status_changed_at__gte=now - timedelta(days=DEVICE_APPROVED_DAYS))
    qs = Task.objects.filter(owner_id=device.owner_id).filter(_for_member(device.member_id)).filter(
        ~Q(status__in=Task.CLOSED) | recent
    )
    return with_details(qs.order_by("-updated_at", "-id"))


def task_for_owner(owner: User, task_id: int) -> Task | None:
    return with_details(Task.objects.filter(owner=owner, pk=task_id)).first()


def task_for_device(device: Device, task_id: int) -> Task | None:
    """Only a task this PC's member is on (anything else is "not found")."""
    if device.member_id is None:
        return None
    return with_details(
        Task.objects.filter(pk=task_id, owner_id=device.owner_id).filter(_for_member(device.member_id))
    ).first()


def owner_needs_count(owner: User) -> int:
    """Open tasks waiting on the owner: help asked, can't do it, or finished and waiting for the OK."""
    return Task.objects.filter(owner=owner, status__in=(S.HELP, S.CANTDO, S.REVIEW)).count()


# --- People and steps (computed) ------------------------------------------------


def people_of(task: Task) -> list[TaskPerson]:
    """The task's people, first person first. A task made by a pre-1.4 server has none yet: its member, unsaved."""
    rows = list(task.people.all())
    if rows:
        return rows
    started = task.status in (S.PROGRESS, S.HELP, S.REVIEW, S.APPROVED)
    return [TaskPerson(
        task=task, member=task.member, position=0,
        seen_at=task.status_changed_at if task.status != S.SENT else None,
        started_at=task.status_changed_at if started else None,
        done_at=task.status_changed_at if task.status in (S.REVIEW, S.APPROVED) else None,
        flag="help" if task.status == S.HELP else "",
        flag_note=task.help_note if task.status == S.HELP else "",
    )]


def steps_of(task: Task) -> list[TaskStep]:
    return list(task.steps.all())


def current_step(steps: list[TaskStep]) -> int:
    """Index of the first step not done (-1 when all are)."""
    for i, s in enumerate(steps):
        if not s.done:
            return i
    return -1


def _anyone_steps(steps: list[TaskStep]) -> bool:
    return not any(s.owner_id for s in steps)


def person_steps(steps: list[TaskStep], member_id: int) -> tuple[int, int]:
    """(done, total) of this person's steps. When no step has an owner, everyone shares all of them."""
    if _anyone_steps(steps):
        return sum(1 for s in steps if s.done), len(steps)
    mine = [s for s in steps if s.owner_id == member_id]
    return sum(1 for s in mine if s.done), len(mine)


def person_state(task: Task, person: TaskPerson, steps: list[TaskStep]) -> str:
    """sent | seen | progress | done | help | cantdo."""
    if person.flag == "help" and task.status == S.HELP:
        return "help"
    if person.flag == "cantdo" and task.status == S.CANTDO:
        return "cantdo"
    if task.status in (S.REVIEW, S.APPROVED) or person.done_at:
        return "done"
    if steps and not _anyone_steps(steps):
        mine = [s for s in steps if s.owner_id == person.member_id]
        if mine and all(s.done for s in mine):
            return "done"
    if person.started_at or any(s.done_by_id == person.member_id for s in steps):
        return "progress"
    if person.seen_at:
        return "seen"
    return "sent"


def can_tick(task: Task, member_id: int, steps: list[TaskStep] | None = None) -> list[int]:
    """The step ids this member may tick right now (their own or anyone's, and in order when the task is)."""
    if task.status not in WORKING or task.status == S.CANTDO:
        return []
    steps = steps if steps is not None else steps_of(task)
    mine = lambda s: s.owner_id in (None, member_id)  # noqa: E731
    if task.in_order:
        cur = current_step(steps)
        return [steps[cur].pk] if cur >= 0 and mine(steps[cur]) else []
    return [s.pk for s in steps if not s.done and mine(s)]


def me_block(task: Task, member_id: int) -> dict[str, Any]:
    """The device's own view: is it their turn, which steps they can tick, their state, and "start again" after a reopen."""
    steps = steps_of(task)
    person = next((p for p in people_of(task) if p.member_id == member_id), None)
    if person is None:
        return {"turn": False, "can_tick": [], "state": "sent", "restart": False}
    state = person_state(task, person, steps)
    ticks = can_tick(task, member_id, steps)
    turn = bool(ticks) or (not steps and task.status in WORKING and state not in ("done", "help", "cantdo"))
    return {
        "turn": turn, "can_tick": ticks, "state": state,
        "restart": bool(task.reopen_note) and task.status == S.PROGRESS and person.started_at is None,
    }


# --- Writing helpers ----------------------------------------------------------


def _log(task: Task, by: str, action: str, note: str = "", at: Any = None, member: Member | None = None) -> TaskActivity:
    return TaskActivity.objects.create(
        task=task, by=by, action=action, note=(note or "")[:1000], at=at or timezone.now(), member=member
    )


def _move(task: Task, status: str, by: str, now: Any) -> None:
    task.status = status
    task.status_changed_at = now
    task.status_changed_by = by


def _clean_title(value: Any) -> str:
    return " ".join(str(value or "").split())[:200]


def _clean_titles(values: Iterable[Any]) -> list[str]:
    return [t for t in (_clean_title(v) for v in values) if t]


def _ensure_people(task: Task) -> list[TaskPerson]:
    """Saved TaskPerson rows (a task made by a pre-1.4 server gets its member's row now)."""
    rows = list(TaskPerson.objects.filter(task=task).select_related("member").order_by("position", "id"))
    if rows:
        return rows
    person = people_of(Task.objects.get(pk=task.pk))[0]
    person.pk = None
    person.save()
    return [person]


def add_interval(when: datetime | date, repeat: str) -> Any:
    """+1 day / week / month (the same day next month, or its last day)."""
    if repeat == Task.Repeat.DAY:
        return when + timedelta(days=1)
    if repeat == Task.Repeat.WEEK:
        return when + timedelta(days=7)
    if repeat == Task.Repeat.MONTH:
        year, month = (when.year + 1, 1) if when.month == 12 else (when.year, when.month + 1)
        day = min(when.day, calendar.monthrange(year, month)[1])
        return when.replace(year=year, month=month, day=day)
    return when


def _next_on(due_at: datetime | None, repeat: str, now: datetime) -> date | None:
    if not repeat:
        return None
    from .work_time import work_zone

    base = timezone.localtime(due_at or now, work_zone())
    return add_interval(base, repeat).date()


def _derived_status(task: Task, people: list[TaskPerson], steps: list[TaskStep]) -> str:
    """Where an open task stands from its people: progress when anyone started, seen when anyone saw it, else sent."""
    if any(p.started_at for p in people) or any(s.done for s in steps):
        return S.PROGRESS
    if any(p.seen_at for p in people):
        return S.SEEN
    return S.SENT


# --- Creating -------------------------------------------------------------------


def _group_card(task: Task, group: Any) -> None:
    """The task's card line in its group chat (z_chat)."""
    from z_chat.services import post_task_card

    post_task_card(group, task)


@transaction.atomic
def create_team_task(
    owner: User,
    members: list[Member],
    title: str,
    note: str = "",
    due_at: Any = None,
    priority: str = Task.Priority.NORMAL,
    steps: list[Any] | None = None,
    mode: str = Task.Mode.ALL,
    in_order: bool = False,
    repeat: str = "",
    group: Any = None,
    post_in_group: bool = True,
    repeat_of: Task | None = None,
) -> Task:
    """
    POST tasks/ (v1.4) and POST members/<id>/tasks/: the task, its people, steps (title + owner), the task line in the
    first person's chat (kind "task", so a Zinwe window older than 1.3.1 shows it as an ordinary message) and, when
    picked, its card in the group chat.
    """
    seen_ids: set[int] = set()
    people: list[Member] = []
    for m in members:
        if m.pk not in seen_ids:
            seen_ids.add(m.pk)
            people.append(m)
    if not people:
        raise TaskRefused("Pick at least one person.", field="member_ids")
    if len(people) > MAX_PEOPLE:
        raise TaskRefused(f"A task goes to at most {MAX_PEOPLE} people.", field="member_ids")
    for m in people:
        if m.owner_id != owner.id:
            raise ValueError("Member does not belong to this owner.")
        if not m.is_active:
            raise TaskRefused(
                "This person is switched off. Switch them back on to send a task." if len(people) == 1
                else f"{m.name} is switched off. Take them off the task or switch them back on.",
                field="member",
            )
    if group is not None and group.owner_id != owner.id:
        raise ValueError("Group does not belong to this owner.")
    step_rows: list[tuple[str, int | None]] = []
    for item in (steps or [])[:MAX_STEPS]:
        if isinstance(item, dict):
            title_ = _clean_title(item.get("title"))
            owner_id = item.get("owner_id")
        else:
            title_, owner_id = _clean_title(item), None
        if not title_:
            continue
        if owner_id is not None and owner_id not in seen_ids:
            raise TaskRefused("A step's person must be one of the people on the task.", field="steps")
        step_rows.append((title_, owner_id))
    if len(people) == 1:
        mode = Task.Mode.ALL

    first = people[0]
    message = Message.objects.create(
        owner=owner, member=first, sender=Message.Sender.OWNER, channel=Message.Channel.CHAT,
        kind=Message.Kind.TASK, title=title, body=note, severity=Message.Severity.INFO, read_by_owner=True,
    )
    now = timezone.now()
    task = Task.objects.create(
        owner=owner, member=first, message=message, title=title, note=note, due_at=due_at,
        priority=priority, status=S.SENT, status_changed_at=now, status_changed_by=BY_OWNER,
        mode=mode, in_order=bool(in_order), repeat=repeat or "", next_on=_next_on(due_at, repeat or "", now),
        group=group if (group is not None and post_in_group) else None, repeat_of=repeat_of,
    )
    TaskPerson.objects.bulk_create([TaskPerson(task=task, member=m, position=i) for i, m in enumerate(people)])
    TaskStep.objects.bulk_create(
        [TaskStep(task=task, title=t, owner_id=o, position=i) for i, (t, o) in enumerate(step_rows)]
    )
    _log(task, BY_OWNER, "created", "Repeat made a new copy." if repeat_of else "", at=now)
    if task.group_id:
        _group_card(task, group)
    transaction.on_commit(
        lambda: track(
            "task.created",
            {"member_id": first.id, "people": len(people), "steps": len(step_rows), "priority": priority,
             "due": due_at is not None, "mode": mode, "in_order": bool(in_order), "repeat": repeat or "",
             "group": task.group_id is not None},
            actor_email=owner.email,
            ref=task,
        )
    )
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


def create_task(
    owner: User,
    member: Member,
    title: str,
    note: str = "",
    due_at: Any = None,
    priority: str = Task.Priority.NORMAL,
    steps: list[str] | None = None,
) -> Task:
    """POST members/<id>/tasks/ (FR-20): a task for one person."""
    return create_team_task(owner, [member], title, note, due_at, priority, steps or [])


# --- Editing ----------------------------------------------------------------------


def _replace_steps(task: Task, raw: list[Any], people_ids: set[int]) -> None:
    """
    PATCH steps: a list of titles and/or {id?, title, done?, owner_id?}. A step keeps its tick when it is sent back by
    id, or by the same title; steps left out are removed. owner_id must be one of the task's people (null = anyone).
    """
    existing = list(task.steps.all())
    by_id = {s.pk: s for s in existing}
    used: set[int] = set()
    keep: list[TaskStep] = []
    for position, item in enumerate(raw[:MAX_STEPS]):
        owner_given, owner_id = False, None
        if isinstance(item, dict):
            title = _clean_title(item.get("title"))
            step = by_id.get(item.get("id")) if isinstance(item.get("id"), int) else None
            done = item.get("done")
            if "owner_id" in item:
                owner_given, owner_id = True, item.get("owner_id")
        else:
            title, step, done = _clean_title(item), None, None
        if not title:
            continue
        if owner_given and owner_id is not None and owner_id not in people_ids:
            raise TaskRefused("A step's person must be one of the people on the task.", field="steps")
        if step is None or step.pk in used:
            step = next((s for s in existing if s.title == title and s.pk not in used), None)
        if step is None:
            step = TaskStep(task=task, title=title, done=bool(done), owner_id=owner_id if owner_given else None)
            if step.done:
                step.done_at = timezone.now()
        else:
            step.title = title
            if done is not None and bool(done) != step.done:
                step.done = bool(done)
                step.done_at = timezone.now() if step.done else None
                if not step.done:
                    step.done_by = None
            if owner_given:
                step.owner_id = owner_id
            used.add(step.pk)
        step.position = position
        keep.append(step)
    task.steps.exclude(pk__in=[s.pk for s in keep if s.pk]).delete()
    for step in keep:
        step.save()


@transaction.atomic
def edit_task(owner: User, task: Task, data: dict[str, Any]) -> Task:
    """PATCH tasks/<id>/: title, note, due date, priority, repeat, in order, steps. A cancelled task must be restored first."""
    task = Task.objects.select_for_update().get(pk=task.pk)
    if task.status == S.CANCELLED:
        raise TaskRefused("This task is cancelled. Reopen it to change it.", field="status")
    fields: list[str] = []
    for name in ("title", "note", "due_at", "priority", "repeat", "in_order"):
        if name in data and getattr(task, name) != data[name]:
            setattr(task, name, data[name])
            fields.append(name)
    if {"repeat", "due_at"} & set(fields):
        task.next_on = _next_on(task.due_at, task.repeat, timezone.now())
    if "steps" in data:
        people_ids = {p.member_id for p in _ensure_people(task)}
        before = [(s.title, s.done, s.owner_id) for s in task.steps.all()]
        _replace_steps(task, data["steps"], people_ids)
        if [(s.title, s.done, s.owner_id) for s in task.steps.all()] != before:
            fields.append("steps")
    if not fields:
        return task_for_owner(owner, task.pk)  # type: ignore[return-value]
    task.save()  # bumps updated_at even when only the steps changed
    if task.message_id and ({"title", "note"} & set(fields)):
        Message.objects.filter(pk=task.message_id).update(title=task.title, body=task.note)
    _log(task, BY_OWNER, "edited", ", ".join(fields))
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


# --- Owner actions ------------------------------------------------------------------


def _member_on(owner: User, member_id: int | None) -> Member | None:
    if member_id is None:
        return None
    return Member.objects.filter(owner=owner, pk=member_id).first()


@transaction.atomic
def owner_action(owner: User, task: Task, action: str, note: str = "", member_id: int | None = None,
                 due_at: Any = None, from_member_id: int | None = None) -> Task:
    """POST tasks/<id>/action/ (owner): approve, reopen, cancel, restore, nudge, reply, reassign, move_due."""
    task = Task.objects.select_for_update().get(pk=task.pk)
    now = timezone.now()
    note = (note or "").strip()
    people = _ensure_people(task)
    steps = list(task.steps.all())

    if action == "nudge":
        return _nudge(owner, task, people, steps, member_id, note, now)
    if action == "reply":
        return _reply(owner, task, people, note, now)
    if action == "reassign":
        return _reassign(owner, task, people, steps, member_id, from_member_id, now)
    if action == "move_due":
        if task.status in Task.CLOSED:
            raise TaskRefused("This task is closed.")
        if due_at is None:
            raise TaskRefused("Pick the new due date.", field="due_at")
        task.due_at = due_at
        if task.repeat:
            task.next_on = _next_on(due_at, task.repeat, now)
        task.save()
        _log(task, BY_OWNER, "due_moved", timezone.localtime(due_at).strftime("%a %d %b, %I:%M %p"), at=now)
        return task_for_owner(owner, task.pk)  # type: ignore[return-value]
    if action == "restore":
        if task.status != S.CANCELLED:
            raise TaskRefused("Only a cancelled task can be restored.")
        _move(task, _derived_status(task, people, steps), BY_OWNER, now)
        task.save()
        _log(task, BY_OWNER, "restored", at=now)
        return task_for_owner(owner, task.pk)  # type: ignore[return-value]

    move = _OWNER_MOVES[action]
    if task.status == move[1]:
        return task_for_owner(owner, task.pk)  # type: ignore[return-value]
    allowed_from, target = move
    if task.status not in allowed_from:
        raise TaskRefused(_owner_refusal(action, task.status))
    _move(task, target, BY_OWNER, now)
    if action == "approve":
        for p in people:
            p.done_at = p.done_at or now
            p.flag, p.flag_note = "", ""
            p.save(update_fields=["done_at", "flag", "flag_note"])
    elif action == "reopen":
        task.reopen_note = note[:1000]
        task.help_note = ""
        task.cantdo_note = ""
        TaskPerson.objects.filter(task=task).update(done_at=None, started_at=None, flag="", flag_note="")
    task.save()
    _log(task, BY_OWNER, _ACTIVITY[action], note, at=now)
    if action == "approve" and task.repeat and not task.repeats.exists():
        _repeat_copy(owner, task, people, steps)
    transaction.on_commit(lambda: track(f"task.{_ACTIVITY[action]}", {"by": "owner"}, actor_email=owner.email, ref=task))
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


def _owner_refusal(action: str, status: str) -> str:
    if status == S.CANCELLED:
        return "This task is cancelled. Reopen it first."
    if action == "reopen":
        return "This task is still open."
    if action == "cancel" and status == S.APPROVED:
        return "This task is already approved. Reopen it first."
    return "That can't be done to this task now."


def _nudge(owner: User, task: Task, people: list[TaskPerson], steps: list[TaskStep], member_id: int | None,
           note: str, now: datetime) -> Task:
    if task.status not in NUDGEABLE:
        raise TaskRefused("Only a task they still have to finish can be nudged.")
    if task.nudged_at and now - task.nudged_at < NUDGE_EVERY:
        raise TaskRefused("You nudged them less than an hour ago.")
    target: Member | None = None
    if member_id is not None:
        person = next((p for p in people if p.member_id == member_id), None)
        if person is None:
            raise TaskRefused("That person isn't on this task.", field="member_id")
        target = person.member
    elif task.in_order and steps:
        cur = current_step(steps)
        if cur >= 0 and steps[cur].owner_id:
            target = next((p.member for p in people if p.member_id == steps[cur].owner_id), None)
    if target is not None:
        who = first_name(target.name)
    else:
        who = names_list([first_name(p.member.name) for p in people if person_state(task, p, steps) != "done"])
    task.nudged_at = now
    task.save(update_fields=["nudged_at", "updated_at"])
    _log(task, BY_OWNER, "nudged", note or who, at=now, member=target)
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


def _reply(owner: User, task: Task, people: list[TaskPerson], note: str, now: datetime) -> Task:
    if not note:
        raise TaskRefused("Write your reply.", field="note")
    if task.status in Task.CLOSED:
        raise TaskRefused("This task is closed.")
    post_task_chat(task, note, member=None)
    if task.status == S.HELP:
        _move(task, S.PROGRESS, BY_OWNER, now)
        for p in people:
            if p.flag == "help":
                p.flag, p.flag_note = "", ""
                p.started_at = p.started_at or now
                p.save(update_fields=["flag", "flag_note", "started_at"])
    task.save()
    _log(task, BY_OWNER, "replied", note, at=now)
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


def _reassign(owner: User, task: Task, people: list[TaskPerson], steps: list[TaskStep], member_id: int | None,
              from_member_id: int | None, now: datetime) -> Task:
    if task.status in Task.CLOSED:
        raise TaskRefused("This task is closed.")
    new = _member_on(owner, member_id)
    if new is None:
        raise TaskRefused("Pick who gets it.", field="member_id")
    if not new.is_active:
        raise TaskRefused(f"{new.name} is switched off.", field="member_id")
    if any(p.member_id == new.pk for p in people):
        raise TaskRefused(f"{first_name(new.name)} is already on this task.", field="member_id")
    if from_member_id is not None:
        old = next((p for p in people if p.member_id == from_member_id), None)
    else:
        flagged = [p for p in people if p.flag == "cantdo"]
        old = flagged[0] if flagged else (people[0] if len(people) == 1 else None)
    if old is None:
        raise TaskRefused("Say who to take it from.", field="from_member_id")
    old_member = old.member
    old.member = new
    old.seen_at = old.started_at = old.done_at = None
    old.flag, old.flag_note = "", ""
    old.save()
    TaskStep.objects.filter(task=task, owner_id=old_member.pk, done=False).update(owner=new)
    if task.member_id == old_member.pk:
        task.member = new
        if task.message_id:
            Message.objects.filter(pk=task.message_id).update(member=new)
    task.cantdo_note = ""
    if task.status in (S.CANTDO, S.HELP) and not any(p.flag for p in people if p.pk != old.pk):
        _move(task, _derived_status(task, [p for p in people if p.pk != old.pk], steps), BY_OWNER, now)
    task.save()
    _log(task, BY_OWNER, "reassigned", f"{first_name(old_member.name)} to {first_name(new.name)}", at=now, member=new)
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


def _repeat_copy(owner: User, task: Task, people: list[TaskPerson], steps: list[TaskStep]) -> None:
    """On approve: the next copy of a repeating task, dated by its repeat, sent to the same people."""
    now = timezone.now()
    due = add_interval(task.due_at, task.repeat) if task.due_at else None
    members = [p.member for p in people if p.member.is_active]
    if not members:
        return
    ids = {m.pk for m in members}
    create_team_task(
        owner, members, task.title, task.note, due, task.priority,
        [{"title": s.title, "owner_id": s.owner_id if s.owner_id in ids else None} for s in steps],
        mode=task.mode, in_order=task.in_order, repeat=task.repeat, group=task.group,
        post_in_group=task.group_id is not None, repeat_of=task,
    )
    if due is None:
        Task.objects.filter(repeat_of=task).update(next_on=_next_on(now, task.repeat, now))


# --- Member actions -------------------------------------------------------------------


def _settle(task: Task, people: list[TaskPerson], steps: list[TaskStep], member: Member, now: datetime) -> bool:
    """Every step done (or, with no steps, every person done) -> waiting for the owner's OK. True when it moved."""
    if task.status not in WORKING:
        return False
    done = all(s.done for s in steps) if steps else all(p.done_at for p in people)
    if not done:
        return False
    _move(task, S.REVIEW, BY_MEMBER, now)
    for p in people:
        if not p.done_at:
            p.done_at = now
            p.save(update_fields=["done_at"])
    return True


def _touch(person: TaskPerson, now: datetime, start: bool = True) -> None:
    person.seen_at = person.seen_at or now
    if start:
        person.started_at = person.started_at or now


def _take(task: Task, people: list[TaskPerson], me: TaskPerson, now: datetime) -> list[TaskPerson]:
    """Any-mode: the first to start takes it; the others are taken off it."""
    if task.mode != Task.Mode.ANY or len(people) <= 1:
        return people
    others = [p for p in people if p.pk != me.pk]
    TaskPerson.objects.filter(pk__in=[p.pk for p in others]).delete()
    TaskStep.objects.filter(task=task, owner_id__in=[p.member_id for p in others]).update(owner=None)
    if task.member_id != me.member_id:
        task.member = me.member
        if task.message_id:
            Message.objects.filter(pk=task.message_id).update(member=me.member)
    me.position = 0
    _log(task, BY_MEMBER, "took", f"{first_name(me.member.name)} took it", at=now, member=me.member)
    return [me]


def _announce(task: Task, activity: TaskActivity) -> None:
    if activity.action in TASK_ACTIONS:
        web_push.announce(task.owner_id, "task", [activity.pk])


@transaction.atomic
def member_action(
    device: Device,
    task: Task,
    action: str,
    note: str = "",
    step_id: int | None = None,
    step_done: bool | None = None,
) -> Task:
    """POST tasks/<id>/action/ (device): seen, accept, start, tick, untick, help, cantdo, finish, done, or note."""
    task = Task.objects.select_for_update().get(pk=task.pk)
    now = timezone.now()
    note = (note or "").strip()
    member = device.member
    people = _ensure_people(task)
    me = next((p for p in people if p.member_id == member.pk), None)
    if me is None:
        raise TaskRefused("You're not on this task.")
    steps = list(task.steps.select_for_update().order_by("position", "id"))
    result = _member_do(task, people, me, steps, member, action, note, step_id, step_done, now)
    member_email = member.email
    if result:
        transaction.on_commit(lambda: track(f"task.{result}", {"by": "member"}, actor_email=member_email, ref=task))
    return task_for_device(device, task.pk)  # type: ignore[return-value]


def _member_do(task: Task, people: list[TaskPerson], me: TaskPerson, steps: list[TaskStep], member: Member,
               action: str, note: str, step_id: int | None, step_done: bool | None, now: datetime) -> str:
    """Does the action; returns the activity word for analytics ("" when nothing happened)."""
    closed = task.status in Task.CLOSED

    if action == "seen":
        if me.seen_at is None:
            me.seen_at = now
            me.save(update_fields=["seen_at"])
        if task.status != S.SENT:
            return ""
        _move(task, S.SEEN, BY_MEMBER, now)
        task.save()
        _log(task, BY_MEMBER, "seen", at=now, member=member)
        if task.message_id:
            Message.objects.filter(pk=task.message_id, member=member, read_by_member=False).update(
                read_by_member=True, read_by_member_at=now, delivered_at=Coalesce(F("delivered_at"), Value(now))
            )
        return "seen"

    if action == "accept":
        _touch(me, now, start=False)
        me.save(update_fields=["seen_at"])
        if task.status == S.ACCEPTED:
            return ""
        if task.status not in (S.SENT, S.SEEN):
            raise TaskRefused("This task is closed." if closed else "That can't be done to this task now.")
        _move(task, S.ACCEPTED, BY_MEMBER, now)
        task.save()
        _log(task, BY_MEMBER, "accepted", at=now, member=member)
        return "accepted"

    if action == "start":
        if closed:
            raise TaskRefused("This task is closed.")
        if task.status == S.PROGRESS and me.started_at is not None:
            return ""
        if task.status == S.CANTDO and me.flag != "cantdo":
            raise TaskRefused("That can't be done to this task now.")
        people = _take(task, people, me, now)
        _touch(me, now)
        me.flag, me.flag_note = "", ""
        if task.status == S.REVIEW:  # pre-1.4: the member reopens their own finished task
            me.done_at = None
        me.save()
        if task.status == S.CANTDO:
            task.cantdo_note = ""
        if task.status != S.PROGRESS:
            _move(task, S.PROGRESS, BY_MEMBER, now)
        task.save()
        _log(task, BY_MEMBER, "started", at=now, member=member)
        return "started"

    if action in ("tick", "untick") or (action == "note" and step_id is not None):
        if closed:
            raise TaskRefused("This task is closed.")
        step = next((s for s in steps if s.pk == step_id), None)
        if step is None:
            raise TaskRefused("That step isn't on this task." if step_id is not None else "Pick a step.", field="step_id")
        if action == "tick":
            done = True
        elif action == "untick":
            done = False
        else:
            done = (not step.done) if step_done is None else bool(step_done)
        if step.done != done:
            _tick(task, people, me, steps, step, done, member, now)
        if action == "note" and note:
            _log(task, BY_MEMBER, "note", note, at=now, member=member)
        task.save()
        return "step_done" if done else "step_undone"

    if action == "note":
        if closed:
            raise TaskRefused("This task is closed.")
        if not note:
            raise TaskRefused("Write a note or pick a step.", field="note")
        _log(task, BY_MEMBER, "note", note, at=now, member=member)
        task.save(update_fields=["updated_at"])
        return "note"

    if action in ("help", "cantdo"):
        if not note:
            raise TaskRefused(
                "Say what you need help with." if action == "help" else "Say why you can't do it.", field="note"
            )
        if task.status not in WORKING:
            raise TaskRefused("This task is closed." if closed else "That can't be done to this task now.")
        _touch(me, now, start=False)
        me.flag, me.flag_note = action, note[:1000]
        me.save()
        if action == "help":
            task.help_note = note[:1000]
        else:
            task.cantdo_note = note[:1000]
        _move(task, S.HELP if action == "help" else S.CANTDO, BY_MEMBER, now)
        task.save()
        activity = _log(task, BY_MEMBER, action, note, at=now, member=member)
        _announce(task, activity)
        return action

    if action in ("finish", "done"):
        if action == "finish" and steps:
            raise TaskRefused("Tick your steps instead.")
        if task.status == S.REVIEW:
            return ""
        if task.status not in WORKING:
            raise TaskRefused("This task is closed." if closed else "That can't be done to this task now.")
        if len(people) == 1:
            # One person: their "done" is the task's (as before 1.4, steps or not).
            _touch(me, now)
            me.done_at = now
            me.flag, me.flag_note = "", ""
            me.save()
            _move(task, S.REVIEW, BY_MEMBER, now)
            task.save()
            _announce(task, _log(task, BY_MEMBER, "done", note, at=now, member=member))
            return "done"
        if me.done_at:
            return ""
        _touch(me, now)
        me.done_at = now
        me.flag, me.flag_note = "", ""
        me.save()
        if _settle(task, people, steps, member, now):
            task.save()
            _announce(task, _log(task, BY_MEMBER, "done", note, at=now, member=member))
            return "done"
        if task.status in (S.SENT, S.SEEN, S.ACCEPTED):
            _move(task, S.PROGRESS, BY_MEMBER, now)
        task.save()
        _log(task, BY_MEMBER, "finished", note, at=now, member=member)
        return "finished"

    raise TaskRefused("That isn't something you can do to a task.")


def _tick(task: Task, people: list[TaskPerson], me: TaskPerson, steps: list[TaskStep], step: TaskStep, done: bool,
          member: Member, now: datetime) -> None:
    index = steps.index(step)
    if step.owner_id not in (None, member.pk):
        owner = next((p.member for p in people if p.member_id == step.owner_id), None)
        raise TaskRefused(f"Only {first_name(owner.name) if owner else 'its person'} can tick this.", field="step_id")
    if done:
        if task.status in (S.CANTDO,) and me.flag != "cantdo":
            raise TaskRefused("That can't be done to this task now.")
        if task.in_order:
            before = next((i for i, s in enumerate(steps[:index]) if not s.done), None)
            if before is not None:
                raise TaskRefused(f"Step {before + 1} comes first.", field="step_id")
        people = _take(task, people, me, now)
        step.done, step.done_by, step.done_at = True, member, now
    else:
        if task.in_order and any(s.done for s in steps[index + 1:]):
            raise TaskRefused("Untick the later steps first.", field="step_id")
        step.done, step.done_by, step.done_at = False, None, None
    step.save(update_fields=["done", "done_by", "done_at"])
    _touch(me, now)
    # Their own part: done when every step of theirs is (only when steps have people).
    if not _anyone_steps(steps):
        mine = [s for s in steps if s.owner_id == member.pk]
        me.done_at = (me.done_at or now) if mine and all(s.done for s in mine) else None
    if me.flag == "cantdo":
        me.flag, me.flag_note = "", ""
        task.cantdo_note = ""
    me.save()
    _log(task, BY_MEMBER, "step_done" if done else "step_undone", step.title, at=now, member=member)
    if done:
        if task.status in (S.SENT, S.SEEN, S.ACCEPTED, S.CANTDO):
            _move(task, S.PROGRESS, BY_MEMBER, now)
        if _settle(task, people, steps, member, now):
            _announce(task, _log(task, BY_MEMBER, "done", "", at=now, member=member))
    elif task.status == S.REVIEW:
        _move(task, S.PROGRESS, BY_MEMBER, now)
        TaskPerson.objects.filter(task=task, member=member).update(done_at=None)
        me.done_at = None


# --- Task chat (v1.4): Message rows about the task, read marks per person -----------------------


def chat_lines(task: Task, after_id: int | None = None, limit: int = 200) -> list[Message]:
    qs = Message.objects.filter(about_task=task).select_related("member").order_by("created_at", "id")
    if after_id:
        qs = qs.filter(pk__gt=after_id)
    return list(qs[:limit]) if after_id else list(qs.order_by("-created_at", "-id")[:limit])[::-1]


@transaction.atomic
def post_task_chat(task: Task, body: str, member: Member | None) -> Message:
    """A line in the task's chat: by a member (member=them, sender=member) or the owner (member=first person)."""
    body = (body or "").strip()[:CHAT_LINE_MAX]
    if not body:
        raise TaskRefused("Write a message.", field="body")
    mine = member is not None
    line = Message.objects.create(
        owner_id=task.owner_id, member=member if mine else task.member,
        sender=Message.Sender.MEMBER if mine else Message.Sender.OWNER, channel=Message.Channel.CHAT,
        kind=Message.Kind.CHAT, body=body, severity=Message.Severity.INFO,
        read_by_owner=not mine, read_by_member=mine, about_task=task,
    )
    mark_task_chat_read(task, member, at=line.created_at)
    Task.objects.filter(pk=task.pk).update(updated_at=timezone.now())
    if mine:
        web_push.announce(task.owner_id, "chat", [line.pk])
    return line


def mark_task_chat_read(task: Task, member: Member | None, at: datetime | None = None) -> None:
    at = at or timezone.now()
    row = TaskChatRead.objects.filter(task=task, member=member).first()
    if row is None:
        TaskChatRead.objects.create(task=task, member=member, last_read_at=at)
    elif row.last_read_at < at:
        row.last_read_at = at
        row.save(update_fields=["last_read_at"])
    if member is None:
        Message.objects.filter(about_task=task, sender=Message.Sender.MEMBER, read_by_owner=False).update(read_by_owner=True)


def owner_first_name(owner: User) -> str:
    return first_name(getattr(owner, "full_name", "") or "") or "Admin"


def chat_summaries(tasks: list[Task], viewer_member_id: int | None) -> None:
    """Sets `task._chat` = {count, unread, last} for each task, in two queries (viewer None = the owner)."""
    ids = [t.pk for t in tasks]
    if not ids:
        return
    reads = dict(
        TaskChatRead.objects.filter(task_id__in=ids, member_id=viewer_member_id).values_list("task_id", "last_read_at")
    )
    lines: dict[int, list[tuple]] = {}
    for row in (
        Message.objects.filter(about_task_id__in=ids)
        .order_by("created_at", "id")
        .values_list("about_task_id", "created_at", "sender", "member_id", "member__name", "body")
    ):
        lines.setdefault(row[0], []).append(row)
    for t in tasks:
        rows = lines.get(t.pk, [])
        read = reads.get(t.pk)
        unread = 0
        for _, at, sender, mid, _, _ in rows:
            mine = (sender == Message.Sender.OWNER) if viewer_member_id is None else (
                sender == Message.Sender.MEMBER and mid == viewer_member_id
            )
            if not mine and (read is None or at > read):
                unread += 1
        last = None
        if rows:
            _, at, sender, mid, mname, body = rows[-1]
            by = owner_first_name(t.owner) if sender == Message.Sender.OWNER else first_name(mname or "")
            last = {"text": (body or "")[:140], "by": by, "at": at}
        t._chat = {"count": len(rows), "unread": unread, "last": last}  # type: ignore[attr-defined]
