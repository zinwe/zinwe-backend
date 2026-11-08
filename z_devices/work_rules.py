"""
v1.4 rules (brief section 1c): work hours, breaks, holidays, leave and how a day counts.

Work hours live at three scopes: the company (WorkSchedule row with every field, made lazily with the defaults), a
department (Member.role) and one PC (Device id). Department and PC rows hold only their own values (null = inherit).
The effective rules for a PC are company <- its member's department <- the PC itself.

Day status (reports): holiday / off / leave / full / half / absent, plus late, from the work log (work_time.py).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from z_auth.models import User

from .models import Device, Holiday, Leave, Member, WorkSchedule

FIELDS = (
    "days", "start", "end", "late_grace_min", "break_allowed_min", "break_ok_over_min", "tell_member_over",
    "tell_owner_over", "ok_early_end", "full_day_hours", "half_start_after", "half_end_before",
)
DEFAULTS: dict[str, Any] = {
    "days": "1111110", "start": "10:00", "end": "19:00", "late_grace_min": 15, "break_allowed_min": 75,
    "break_ok_over_min": 45, "tell_member_over": True, "tell_owner_over": True, "ok_early_end": True,
    "full_day_hours": Decimal("8"), "half_start_after": "13:00", "half_end_before": "14:00",
}
# What the Windows app gets on every heartbeat.
WIRE_FIELDS = ("days", "start", "end", "late_grace_min", "break_allowed_min", "break_ok_over_min", "tell_member_over",
               "full_day_hours")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DAYS_RE = re.compile(r"^[01]{7}$")
ROLES = {r for r, _ in Member.Role.choices}

STATUSES = ("full", "half", "absent", "leave", "off", "holiday")


class BadScope(ValueError):
    pass


class RulesRefused(ValueError):
    def __init__(self, message: str, field: str = "detail") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


# --- Scopes ------------------------------------------------------------------------


def parse_scope(owner: User, raw: str | None) -> tuple[str, str]:
    """ "company" | "department:<role>" | "device:<id>" -> (kind, key). Raises BadScope."""
    raw = (raw or "company").strip()
    if raw in ("company", "all", ""):
        return WorkSchedule.Scope.COMPANY, ""
    kind, _, key = raw.partition(":")
    if kind == "department" and key in ROLES:
        return WorkSchedule.Scope.DEPARTMENT, key
    if kind == "device" and key.isdigit() and Device.objects.filter(owner=owner, pk=int(key)).exists():
        return WorkSchedule.Scope.DEVICE, key
    raise BadScope("Pick everyone, a department or one of your PCs.")


def scope_label(kind: str, key: str) -> str:
    return "company" if kind == WorkSchedule.Scope.COMPANY else f"{kind}:{key}"


def _parents(owner: User, kind: str, key: str) -> list[tuple[str, str]]:
    """The scopes above this one, nearest last: [company] or [company, department]."""
    if kind == WorkSchedule.Scope.COMPANY:
        return []
    if kind == WorkSchedule.Scope.DEPARTMENT:
        return [(WorkSchedule.Scope.COMPANY, "")]
    device = Device.objects.filter(owner=owner, pk=int(key)).select_related("member").first()
    role = device.member.role if device and device.member_id else ""
    out = [(WorkSchedule.Scope.COMPANY, "")]
    if role:
        out.append((WorkSchedule.Scope.DEPARTMENT, role))
    return out


def company_schedule(owner: User) -> WorkSchedule:
    row = WorkSchedule.objects.filter(owner=owner, scope_kind=WorkSchedule.Scope.COMPANY, scope_key="").first()
    if row is None:
        row, _ = WorkSchedule.objects.get_or_create(
            owner=owner, scope_kind=WorkSchedule.Scope.COMPANY, scope_key="", defaults=dict(DEFAULTS)
        )
    return row


def _rows(owner: User) -> dict[tuple[str, str], WorkSchedule]:
    company_schedule(owner)
    return {(r.scope_kind, r.scope_key): r for r in WorkSchedule.objects.filter(owner=owner)}


def _merge(rows: list[WorkSchedule | None]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for row in rows:
        if row is None:
            continue
        for f in FIELDS:
            value = getattr(row, f)
            if value is not None:
                out[f] = value
    return out


def effective(owner: User, kind: str, key: str, rows: dict | None = None) -> dict[str, Any]:
    rows = rows if rows is not None else _rows(owner)
    chain = [*_parents(owner, kind, key), (kind, key)]
    return _merge([rows.get(s) for s in chain])


def effective_for_device(device: Device, rows: dict | None = None) -> dict[str, Any]:
    """The rules that apply on this PC: company <- its member's department <- the PC."""
    rows = rows if rows is not None else _rows(device.owner)
    chain: list[tuple[str, str]] = [(WorkSchedule.Scope.COMPANY, "")]
    role = device.member.role if device.member_id and device.member else ""
    if role:
        chain.append((WorkSchedule.Scope.DEPARTMENT, role))
    chain.append((WorkSchedule.Scope.DEVICE, str(device.pk)))
    return _merge([rows.get(s) for s in chain])


def effective_for_member(owner: User, member: Member, rows: dict | None = None) -> dict[str, Any]:
    from .services import current_device_for

    device = current_device_for(member)
    if device is not None:
        return effective_for_device(device, rows)
    rows = rows if rows is not None else _rows(owner)
    chain = [(WorkSchedule.Scope.COMPANY, "")] + ([(WorkSchedule.Scope.DEPARTMENT, member.role)] if member.role else [])
    return _merge([rows.get(s) for s in chain])


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def hours_payload(owner: User, kind: str, key: str) -> dict[str, Any]:
    rows = _rows(owner)
    own_row = rows.get((kind, key))
    parents = _parents(owner, kind, key)
    return {
        "scope": scope_label(kind, key),
        "own": kind == WorkSchedule.Scope.COMPANY or (
            own_row is not None and any(getattr(own_row, f) is not None for f in FIELDS)
        ),
        "inherits_from": scope_label(*parents[-1]) if parents else None,
        "effective": {f: _wire(v) for f, v in effective(owner, kind, key, rows).items()},
        "fields": {f: _wire(getattr(own_row, f)) if own_row else None for f in FIELDS},
    }


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _clean(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "days":
        if not isinstance(value, str) or not DAYS_RE.match(value):
            raise RulesRefused("Working days are 7 digits, Monday first: 1111110.", field)
        return value
    if field in ("start", "end", "half_start_after", "half_end_before"):
        if not isinstance(value, str) or not TIME_RE.match(value):
            raise RulesRefused("Use a time like 09:30.", field)
        return value
    if field in ("late_grace_min", "break_allowed_min", "break_ok_over_min"):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 600:
            raise RulesRefused("Pick 0 to 600 minutes.", field)
        return value
    if field in ("tell_member_over", "tell_owner_over", "ok_early_end"):
        if not isinstance(value, bool):
            raise RulesRefused("true or false.", field)
        return value
    if field == "full_day_hours":
        try:
            hours = Decimal(str(value))
        except Exception:  # noqa: BLE001
            raise RulesRefused("Pick 1 to 24 hours.", field) from None
        if isinstance(value, bool) or not Decimal("1") <= hours <= Decimal("24"):
            raise RulesRefused("Pick 1 to 24 hours.", field)
        return hours.quantize(Decimal("0.01"))
    raise RulesRefused("Unknown field.", field)


@transaction.atomic
def save_hours(owner: User, kind: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
    """PUT rules/hours/: the given fields at this scope (null = inherit again, not at company)."""
    unknown = set(data) - set(FIELDS)
    if unknown:
        raise RulesRefused(f"Unknown field: {sorted(unknown)[0]}.", sorted(unknown)[0])
    company = company_schedule(owner)
    row = company if kind == WorkSchedule.Scope.COMPANY else WorkSchedule.objects.filter(
        owner=owner, scope_kind=kind, scope_key=key).first() or WorkSchedule(owner=owner, scope_kind=kind, scope_key=key)
    for field, value in data.items():
        value = _clean(field, value)
        if value is None and kind == WorkSchedule.Scope.COMPANY:
            raise RulesRefused("The company rules need a value.", field)
        setattr(row, field, value)
    row.save()
    eff = effective(owner, kind, key)
    if _minutes(eff["end"]) <= _minutes(eff["start"]):
        raise RulesRefused("The day has to end after it starts.", "end")
    return hours_payload(owner, kind, key)


@transaction.atomic
def reset_hours(owner: User, kind: str, key: str) -> dict[str, Any]:
    if kind == WorkSchedule.Scope.COMPANY:
        WorkSchedule.objects.filter(owner=owner, scope_kind=kind, scope_key="").update(**DEFAULTS)
    else:
        WorkSchedule.objects.filter(owner=owner, scope_kind=kind, scope_key=key).delete()
    return hours_payload(owner, kind, key)


def scopes_overview(owner: User) -> dict[str, Any]:
    """Which departments and PCs have their own work hours and / or protection rules."""
    from .models import PolicyOverride

    hours = {(r.scope_kind, r.scope_key) for r in WorkSchedule.objects.filter(owner=owner).exclude(scope_kind="company")
             if any(getattr(r, f) is not None for f in FIELDS)}
    policy = {(r.scope_kind, r.scope_key) for r in PolicyOverride.objects.filter(owner=owner) if r.data}
    counts: dict[str, int] = {}
    for role in Member.objects.filter(owner=owner, is_active=True).values_list("role", flat=True):
        if role:
            counts[role] = counts.get(role, 0) + 1
    labels = dict(Member.Role.choices)
    departments = [
        {"scope": f"department:{role}", "role": role, "label": labels[role], "people": counts.get(role, 0),
         "own_hours": ("department", role) in hours, "own_policy": ("department", role) in policy}
        for role, _ in Member.Role.choices
    ]
    devices = []
    for d in Device.objects.filter(owner=owner, is_revoked=False).select_related("member").order_by("hostname"):
        key = str(d.pk)
        devices.append({
            "scope": f"device:{d.pk}", "device_id": d.pk, "hostname": d.hostname, "member_id": d.member_id,
            "member_name": d.member.name if d.member_id else "", "role": d.member.role if d.member_id else "",
            "own_hours": ("device", key) in hours, "own_policy": ("device", key) in policy,
        })
    return {"departments": departments, "devices": devices}


# --- Holidays and leave ----------------------------------------------------------------


def holidays_between(owner: User, start: date, end: date) -> dict[date, str]:
    return dict(Holiday.objects.filter(owner=owner, date__gte=start, date__lte=end).values_list("date", "name"))


def add_holiday(owner: User, name: str, day: date) -> Holiday:
    name = " ".join((name or "").split())[:80]
    if not name:
        raise RulesRefused("Give the holiday a name.", "name")
    if Holiday.objects.filter(owner=owner, date=day).exists():
        raise RulesRefused("That day is already a holiday.", "date")
    return Holiday.objects.create(owner=owner, name=name, date=day)


@transaction.atomic
def ask_leave(member: Member, day: date, half: bool, note: str) -> Leave:
    """A member asks for a day (or half day) off. Asking again for the same day replaces a waiting or declined ask."""
    existing = Leave.objects.select_for_update().filter(member=member, date=day).first()
    if existing is not None:
        if existing.status == Leave.Status.APPROVED:
            raise RulesRefused("That day is already approved.", "date")
        existing.half, existing.note, existing.status, existing.decided_at = half, (note or "")[:500], Leave.Status.REQUESTED, None
        existing.save()
        return existing
    return Leave.objects.create(owner_id=member.owner_id, member=member, date=day, half=half, note=(note or "")[:500])


def decide_leave(leave: Leave, decision: str) -> Leave:
    if decision not in ("approve", "decline"):
        raise RulesRefused("Approve or decline.", "decision")
    leave.status = Leave.Status.APPROVED if decision == "approve" else Leave.Status.DECLINED
    leave.decided_at = timezone.now()
    leave.save(update_fields=["status", "decided_at"])
    return leave


def leave_json(leave: Leave) -> dict[str, Any]:
    return {
        "id": leave.pk, "member_id": leave.member_id, "member_name": leave.member.name, "date": leave.date.isoformat(),
        "half": leave.half, "status": leave.status, "note": leave.note,
        "created_at": leave.created_at.isoformat().replace("+00:00", "Z") if leave.created_at else None,
        "decided_at": leave.decided_at.isoformat().replace("+00:00", "Z") if leave.decided_at else None,
    }


# --- The PC's rules on the wire ----------------------------------------------------------


def work_rules_for_device(device: Device, now: datetime | None = None) -> dict[str, Any]:
    """`work_rules` in the heartbeat / policy response."""
    from .work_time import work_zone

    eff = effective_for_device(device)
    today = timezone.localdate(now or timezone.now(), work_zone(device.owner))
    out = {f: _wire(eff[f]) for f in WIRE_FIELDS}
    holiday = Holiday.objects.filter(owner_id=device.owner_id, date=today).values_list("name", flat=True).first()
    leave = None
    if device.member_id:
        row = Leave.objects.filter(member_id=device.member_id, date=today, status=Leave.Status.APPROVED).first()
        leave = ("half" if row.half else "full") if row else None
    out["working_today"] = eff["days"][today.weekday()] == "1" and holiday is None and leave != "full"
    out["holiday_today"] = holiday
    out["leave_today"] = leave
    return out


# --- How a day counts -----------------------------------------------------------------------


def _local_minutes(at: datetime | None, tz: Any) -> int | None:
    if at is None:
        return None
    local = at.astimezone(tz)
    return local.hour * 60 + local.minute


def day_status(rules: dict[str, Any], day: date, work: Any, holiday: str | None, leave: Leave | None, tz: Any,
               now: datetime) -> dict[str, Any] | None:
    """
    One person's day: {date, status, late, worked_min, break_min, break_over}. `work` = the day's work_time.DayWork
    (or None). None for today before it has started (and the start time + grace hasn't passed yet).
    """
    worked = int(work.worked // 60) if work else 0
    on_break = int(work.on_break // 60) if work else 0
    first_in = _local_minutes(work.first_in, tz) if work and work.logged_in else None
    last_out = _local_minutes(work.last_out, tz) if work else None
    start = _minutes(rules["start"])
    late = first_in is not None and first_in > start + int(rules["late_grace_min"] or 0)
    full = float(rules["full_day_hours"]) * 60
    working_day = rules["days"][day.weekday()] == "1"
    today = timezone.localdate(now, tz)
    row = {"date": day.isoformat(), "status": "absent", "late": late, "worked_min": worked, "break_min": on_break,
           "break_over": bool(rules["break_allowed_min"]) and on_break > int(rules["break_allowed_min"])}
    if holiday:
        row.update(status="holiday", late=False)
    elif not working_day:
        row.update(status="off", late=False)
    elif leave is not None and not leave.half:
        row.update(status="leave", late=False)
    elif leave is not None and leave.half:
        row["status"] = "half"
    elif first_in is None and worked == 0:
        if day == today and (now.astimezone(tz).hour * 60 + now.astimezone(tz).minute) <= start + int(rules["late_grace_min"] or 0):
            return None
        row["status"] = "absent"
    elif day == today and worked < full and (now.astimezone(tz).hour * 60 + now.astimezone(tz).minute) < _minutes(rules["end"]):
        # Today, still going: a full day so far unless they started after the half-day time.
        late_start = first_in is not None and first_in > _minutes(rules["half_start_after"])
        row["status"] = "half" if late_start else "full"
    elif worked < full / 2:
        row["status"] = "absent"
    else:
        early_end = last_out is not None and day < today and last_out < _minutes(rules["half_end_before"])
        late_start = first_in is not None and first_in > _minutes(rules["half_start_after"])
        row["status"] = "full" if worked >= full and not early_end and not late_start else "half"
    return row


def attendance(owner: User, start: date, end: date, per_person: dict[Any, dict[date, Any]], now: datetime,
               member: Member | None = None) -> dict[str, Any]:
    """
    Day status per person per day for the report: every active member (or `member`), from their work days
    (`per_person` = {("m", member id): {date: DayWork}}).
    """
    from .work_time import work_zone

    tz = work_zone(owner)
    rows = _rows(owner)
    holidays = holidays_between(owner, start, end)
    members = [member] if member is not None else list(
        Member.objects.filter(owner=owner, is_active=True).prefetch_related("devices").order_by("name")
    )
    leaves: dict[tuple[int, date], Leave] = {
        (lv.member_id, lv.date): lv
        for lv in Leave.objects.filter(owner=owner, date__gte=start, date__lte=end, status=Leave.Status.APPROVED)
    }
    totals = {k: 0 for k in (*STATUSES, "late")}
    people = []
    for m in members:
        rules = effective_for_member(owner, m, rows)
        days = per_person.get(("m", m.pk), {})
        counts = {k: 0 for k in (*STATUSES, "late")}
        out = []
        day = start
        while day <= end:
            row = day_status(rules, day, days.get(day), holidays.get(day), leaves.get((m.pk, day)), tz, now)
            if row is not None:
                out.append(row)
                counts[row["status"]] += 1
                counts["late"] += int(row["late"])
            day += timedelta(days=1)
        for k, v in counts.items():
            totals[k] += v
        people.append({"member_id": m.pk, "name": m.name, "days": out, "counts": counts})
    return {"people": people, "totals": totals}
