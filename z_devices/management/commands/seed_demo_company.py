"""`manage.py seed_demo_company [--password P] [--days 21] [--clear] [--keep-alive MINUTES] [--force]`

Builds the demo account used for product videos, screenshots and blog posts: **Kitecrest Digital**,
a made-up 11-25 person web & app studio in Bengaluru with Indian and US team members, on the
Businesses plan, with 10 people and their PCs, three weeks of work days, alerts, usage charts and
team chat that read like a real office.

- Every address is @kitecrestdigital.com, a fictional domain listed in ZINWE_DEMO_EMAIL_DOMAINS, so no
  email to it is ever delivered and the admin dashboard's numbers leave the account out (z_main/demo.py).
- Nothing is billed: the plan is switched on directly; no payment, invoice or invoice number is made.
- Re-running keeps the login, password and Member IDs and rebuilds the activity up to "now" (do it
  right before recording). Today: most people working, one on a break waiting for approval, one
  logged out early, one on leave. Run between 11:30 and 18:30 IST for real clock times; outside those
  hours today's day is shifted so "now" reads as mid-afternoon.
- `--keep-alive 60` keeps the working PCs "online" for an hour (they send no real heartbeats).
- `--clear` deletes the whole demo account.
- v1.4: groups Design / Development / Everyone (with a direct chat between Priya and Emily), the team tasks of the
  prototype (the Lumen relay: Priya steps 1-2 done, Karan's turn on 3-4, Emily 5-6), work hours (company + Arjun's
  PC), holidays and leave.

DEBUG off (dev / prod) needs --force.
"""
from __future__ import annotations

import hashlib
import random
import secrets
import time as time_module
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from z_auth.models import Company, CustomerRemark, Plan, PlanActivation, Subscription, User
from z_devices.models import Device, DeviceEvent, Member, Message, Policy, UsageMetric, WorkLog
from z_main.demo import is_demo_email

DOMAIN = "kitecrestdigital.com"
OWNER_EMAIL = f"ananya.rao@{DOMAIN}"
OWNER_NAME = "Ananya Rao"
COMPANY = {
    "name": "Kitecrest Digital",
    "industry": Company.Industry.IT_SERVICES,
    "team_size": "11-25",
    "phone": "+91 80 4718 2290",
    "city": "Bengaluru",
    "state": "Karnataka",
    "country": "India",
}
MACHINE_PREFIX = "demo-kc-"
AGENT_VERSION = "1.0.4"
CONSENT_VERSION = "2026-10-17"
PLAN_CODE = "businesses"
REMARK = "DEMO ACCOUNT (Kitecrest Digital) for product videos and screenshots, not a customer. Built by manage.py seed_demo_company."

State = WorkLog.State
Approval = WorkLog.Approval
MB = 1_000_000


@dataclass(frozen=True)
class Person:
    name: str
    role: str
    hostname: str
    os: str
    start: tuple[int, int]
    apps: tuple[tuple[str, int], ...]  # foreground app key, weight
    sites: tuple[tuple[str, int], ...]  # domain, weight
    today: str = "working"  # working | break | left_early | leave
    extra_break_odds: float = 0.3

    @property
    def email(self) -> str:
        first, _, last = self.name.lower().partition(" ")
        return f"{first}.{last}@{DOMAIN}"


DEV_APPS = (("code", 45), ("chrome", 25), ("windowsterminal", 10), ("slack", 12), ("teams", 8))
DEV_SITES = (("github.com", 30), ("stackoverflow.com", 14), ("developer.mozilla.org", 8), ("kitecrest.atlassian.net", 16),
             ("chatgpt.com", 10), ("mail.google.com", 8), ("youtube.com", 4))
PEOPLE: tuple[Person, ...] = (
    Person("Arjun Mehta", "Lead developer", "KC-DEV-01", "Windows 11 Pro 24H2 (26100)", (9, 30), DEV_APPS, DEV_SITES),
    Person("Priya Nair", "Accounts", "KC-ACCOUNTS-01", "Windows 11 Pro 23H2 (22631)", (9, 15),
           (("excel", 40), ("tallyprime", 25), ("outlook", 15), ("chrome", 15), ("teams", 5)),
           (("mail.google.com", 20), ("incometax.gov.in", 8), ("gst.gov.in", 14), ("onlinesbi.sbi", 10),
            ("docs.google.com", 14), ("hdfcbank.com", 8))),
    Person("Emily Carter", "Project manager", "KC-PM-LAPTOP-01", "Windows 11 Pro 24H2 (26100)", (10, 0),
           (("chrome", 40), ("teams", 20), ("outlook", 15), ("excel", 10), ("zoom", 15)),
           (("kitecrest.atlassian.net", 30), ("docs.google.com", 18), ("mail.google.com", 16), ("calendar.google.com", 10),
            ("notion.so", 12), ("linkedin.com", 4)), today="break"),
    Person("Karan Malhotra", "Sales", "KC-SALES-LAPTOP-02", "Windows 11 Home 24H2 (26100)", (9, 45),
           (("chrome", 45), ("outlook", 20), ("zoom", 15), ("powerpnt", 10), ("whatsapp", 10)),
           (("linkedin.com", 26), ("hubspot.com", 22), ("mail.google.com", 16), ("docs.google.com", 12),
            ("canva.com", 8), ("youtube.com", 6)), extra_break_odds=0.5),
    Person("Jake Thompson", "Client success", "KC-CS-LAPTOP-03", "Windows 11 Pro 24H2 (26100)", (10, 15),
           (("chrome", 40), ("zoom", 20), ("outlook", 20), ("slack", 15), ("teams", 5)),
           (("mail.google.com", 22), ("zendesk.com", 20), ("kitecrest.atlassian.net", 14), ("calendar.google.com", 10),
            ("loom.com", 8)), today="left_early"),
    Person("Sneha Reddy", "UI designer", "KC-DESIGN-02", "Windows 11 Pro 24H2 (26100)", (9, 40),
           (("figma", 50), ("photoshop", 15), ("chrome", 20), ("slack", 10), ("illustrator", 5)),
           (("figma.com", 34), ("dribbble.com", 10), ("behance.net", 8), ("fonts.google.com", 6), ("unsplash.com", 6),
            ("mail.google.com", 6))),
    Person("Michael Chen", "Developer", "KC-DEV-03", "Windows 11 Pro 24H2 (26100)", (10, 5), DEV_APPS, DEV_SITES),
    Person("Olivia Martinez", "QA engineer", "KC-QA-01", "Windows 10 Pro 22H2 (19045)", (9, 50),
           (("chrome", 40), ("postman", 20), ("code", 15), ("slack", 15), ("excel", 10)),
           (("kitecrest.atlassian.net", 28), ("browserstack.com", 14), ("github.com", 12), ("docs.google.com", 10),
            ("mail.google.com", 6))),
    Person("Vikram Singh", "DevOps engineer", "KC-DEV-04", "Windows 11 Pro 24H2 (26100)", (9, 20),
           (("windowsterminal", 30), ("code", 30), ("chrome", 25), ("slack", 15)),
           (("console.aws.amazon.com", 24), ("github.com", 20), ("grafana.com", 10), ("cloudflare.com", 8),
            ("stackoverflow.com", 8))),
    Person("Fatima Sheikh", "HR & office admin", "KC-FRONT-DESK", "Windows 11 Home 23H2 (22631)", (9, 0),
           (("outlook", 30), ("chrome", 30), ("excel", 20), ("winword", 20)),
           (("mail.google.com", 22), ("keka.com", 18), ("naukri.com", 10), ("linkedin.com", 10), ("docs.google.com", 10)),
           today="leave"),
)

LOGOUT_NOTES = {
    "Arjun Mehta": ["Checkout API fix merged, PR #482", "Code review for Michael, payments webhook", "Release 3.2 deployed to staging"],
    "Priya Nair": ["GST filing for September done", "Vendor payments cleared", "Reconciled HDFC statement"],
    "Emily Carter": ["Sprint review with the Denver client", "Updated the roadmap for Q4", "Kickoff notes shared"],
    "Karan Malhotra": ["Pitch deck v3 sent to Austin lead", "3 demos booked for next week", "Proposal for the Pune clinic"],
    "Jake Thompson": ["Onboarding call with BluePeak", "Closed 6 tickets", "QBR prep for Monday"],
    "Sneha Reddy": ["Dashboard screens for review", "Mobile onboarding flow v2", "Icon set exported"],
    "Michael Chen": ["Webhook retries done", "Fixed flaky tests", "Search filters shipped"],
    "Olivia Martinez": ["Regression pass on 3.2", "Logged 9 bugs", "Payment flow test cases"],
    "Vikram Singh": ["Backups moved to S3 Glacier", "CI pipeline 40% faster", "Staging certs renewed"],
    "Fatima Sheikh": ["Interviews scheduled", "Payroll inputs sent", "Laptop handover for new joiner"],
}
EXTRA_BREAKS = [("Personal", "Bank errand"), ("Meeting", "Client call outside"), ("Personal", "School pickup"),
                ("Other", "Doctor appointment"), ("Meeting", "Vendor meeting downstairs")]


class Command(BaseCommand):
    help = "Create or refresh the Kitecrest Digital demo account (or --clear it)."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--password", default="", help="Owner password (set on create, or changed when given).")
        parser.add_argument("--days", type=int, default=21, help="Days of history, including today (default 21).")
        parser.add_argument("--clear", action="store_true", help="Delete the demo account and stop.")
        parser.add_argument("--keep-alive", type=int, default=0, metavar="MINUTES",
                            help="Afterwards keep the working PCs online for this many minutes.")
        parser.add_argument("--force", action="store_true", help="Allow running with DEBUG off (dev / prod).")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG and not options["force"]:
            raise CommandError("This writes a demo account. Pass --force to run with DEBUG off.")
        if not is_demo_email(OWNER_EMAIL):
            raise CommandError(f"{DOMAIN} must be listed in ZINWE_DEMO_EMAIL_DOMAINS, so no email to the demo is delivered.")

        if options["clear"]:
            deleted, _ = User.objects.filter(email=OWNER_EMAIL).delete()
            self.stdout.write(f"Removed the demo account ({deleted} rows)." if deleted else "No demo account to remove.")
            return

        password = options["password"]
        owner, created = self._owner(password)
        self._account(owner)
        members = self._members(owner)
        self._activity(owner, members, max(2, min(options["days"], 60)))
        self._report(owner, members, password if (created or password) else "")
        if options["keep_alive"] > 0:
            self._keep_alive(owner, options["keep_alive"])

    # --- account --------------------------------------------------------------

    def _owner(self, password: str) -> tuple[User, bool]:
        owner = User.objects.filter(email=OWNER_EMAIL).first()
        created = owner is None
        if created:
            if not password:
                password = secrets.token_urlsafe(12)
                self._generated = password
            owner = User.objects.create_user(OWNER_EMAIL, password, full_name=OWNER_NAME, company=COMPANY["name"])
            User.objects.filter(pk=owner.pk).update(date_joined=timezone.now() - timedelta(days=46))
            owner.refresh_from_db()
        elif password:
            owner.set_password(password)
            owner.save(update_fields=["password"])
        now = timezone.now()
        owner.full_name = OWNER_NAME
        owner.phone = "+91 98450 31276"
        owner.email_verified_at = owner.email_verified_at or owner.date_joined
        owner.terms_accepted_at = owner.terms_accepted_at or owner.date_joined
        owner.terms_version = settings.ZINWE_LEGAL_VERSION
        owner.tour_done_at = owner.tour_done_at or now
        owner.save(update_fields=["full_name", "phone", "email_verified_at", "terms_accepted_at", "terms_version", "tour_done_at"])
        return owner, created

    @transaction.atomic
    def _account(self, owner: User) -> None:
        company, _ = Company.objects.update_or_create(owner=owner, defaults=COMPANY)
        Company.objects.filter(pk=company.pk).update(created_at=owner.date_joined)

        plan = Plan.objects.filter(code=PLAN_CODE).first()
        if plan is None:
            raise CommandError(f'No "{PLAN_CODE}" plan in this database.')
        starts = owner.date_joined + timedelta(hours=2)
        ends = timezone.now() + timedelta(days=365)  # far enough that it never lapses mid-demo
        Subscription.objects.filter(owner=owner).delete()
        PlanActivation.objects.filter(user=owner).delete()
        activation = PlanActivation.objects.create(user=owner, plan=plan, starts_at=starts, ends_at=ends, source="demo")
        Subscription.objects.create(
            owner=owner, company=company, plan=plan, status=Subscription.Status.ACTIVE,
            amount_inr=plan.price_inr_monthly, currency=Subscription.Currency.INR,
            billing_cycle=Subscription.BillingCycle.MONTHLY, starts_at=starts, ends_at=ends,
            activation=activation, created_at=starts,
        )
        Policy.objects.update_or_create(owner=owner, defaults={
            "usb_mode": Policy.UsbMode.ALLOW,
            "web_enabled": True, "web_mode": Policy.WebMode.BLOCK,
            "web_categories": ["adult", "gambling", "malware", "phishing"],
            "web_custom_domains": ["thepiratebay.org", "1337x.to"],
            "data_transfer_enabled": True,
            "remote_software_enabled": True, "remote_software_blocking_enabled": True,
            "remote_software_blocked_tools": ["AnyDesk"],
        })
        CustomerRemark.objects.update_or_create(owner=owner, defaults={"text": REMARK})

    def _members(self, owner: User) -> list[tuple[Person, Member, Device]]:
        out = []
        for index, person in enumerate(PEOPLE):
            member, _ = Member.objects.get_or_create(owner=owner, email=person.email, defaults={"name": person.name})
            if member.name != person.name or not member.is_active:
                member.name, member.is_active = person.name, True
                member.save(update_fields=["name", "is_active"])
            joined = owner.date_joined + timedelta(days=1, minutes=17 * index)
            Member.objects.filter(pk=member.pk).update(created_at=joined)
            device, _ = Device.objects.get_or_create(
                owner=owner, machine_id=f"{MACHINE_PREFIX}{index + 1:02d}",
                defaults={"hostname": person.hostname, "token_hash": hashlib.sha256(secrets.token_bytes(32)).hexdigest()},
            )
            device.member = member
            device.hostname = person.hostname
            device.os_version = person.os
            device.agent_version = AGENT_VERSION
            device.enrolled_at = joined + timedelta(minutes=25)
            device.consent_version = CONSENT_VERSION
            device.consent_at = device.enrolled_at + timedelta(minutes=2)
            device.consent_declined_at = None
            device.last_ip = f"10.20.1.{31 + index}"
            device.is_revoked = False
            device.save()
            out.append((person, member, device))
        return out

    # --- activity ---------------------------------------------------------------

    @transaction.atomic
    def _activity(self, owner: User, members: list[tuple[Person, Member, Device]], days: int) -> None:
        devices = [d for _, _, d in members]
        WorkLog.objects.filter(device__in=devices).delete()
        DeviceEvent.objects.filter(device__in=devices).delete()
        UsageMetric.objects.filter(device__in=devices).delete()
        Message.objects.filter(owner=owner).delete()

        rng = random.Random("kitecrest-demo")
        now = timezone.localtime()
        today = now.date()
        # Today runs on the real clock during office hours; outside them it's a normal day shifted so "now"
        # is 15:20, so the dashboard always looks like a working afternoon.
        office = _local(today, 11, 30) <= now <= _local(today, 18, 30)
        shift = timedelta(0) if office else now - _local(today, 15, 20)
        metrics: dict[tuple[int, datetime, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
        events: list[DeviceEvent] = []

        for index, (person, member, device) in enumerate(members):
            last_state = ""
            for back in range(days - 1, -1, -1):
                day = today - timedelta(days=back)
                is_today = back == 0
                if not is_today and day.weekday() >= 5:
                    continue  # weekends off
                if is_today and person.today == "leave":
                    continue
                plan = _today_plan(rng, person, today, now, shift) if is_today else _day_plan(rng, person, day)
                previous = ""
                for position, (state, at, reason, note) in enumerate(plan):
                    approval, decided = "", None
                    if state == State.ON_BREAK:
                        if is_today and position == len(plan) - 1:
                            approval = Approval.PENDING  # the break they're on now waits for the owner
                        elif is_today:
                            approval, decided = Approval.APPROVED, at + timedelta(minutes=rng.randint(3, 25))
                        else:
                            approval = Approval.DENIED if (reason == "Personal" and index == 3 and back in (6, 7, 8)) else Approval.APPROVED
                            decided = at + timedelta(minutes=rng.randint(4, 40))
                    WorkLog.objects.create(owner=owner, device=device, member=member, state=state, previous_state=previous,
                                           at=at, reason=reason, note=note, approval=approval, decided_at=decided)
                    previous = state
                    if is_today:
                        last_state = state
                _usage(rng, person, device, plan, now, metrics)

            worked, on_break = _today_totals(device, now)
            online = last_state in (State.WORKING, State.ON_BREAK)
            device.work_state = last_state or Device.WorkState.NOT_STARTED
            device.worked_seconds, device.break_seconds = worked, on_break
            if online:
                device.last_seen_at = now - timedelta(seconds=rng.randint(3, 40))
            elif person.today == "leave":
                device.last_seen_at = _local(_previous_workday(today), 18, 41)
            else:
                device.last_seen_at = now - timedelta(minutes=rng.randint(48, 75))
            device.work_updated_at = device.last_seen_at
            device.save(update_fields=["work_state", "worked_seconds", "break_seconds", "last_seen_at", "work_updated_at"])

            # The confirmation every PC sends when it gets its rules.
            events.append(_event(device, "usb.policy_applied", "info", device.enrolled_at + timedelta(minutes=3), {"mode": "allow", "dry_run": False}))
            events.append(_event(device, "web.blocklist_applied", "info", device.enrolled_at + timedelta(minutes=3), {
                "summary": "Website blocking active: 6 domain(s) across 5 categories", "blocked_count": 6,
                "categories": ["adult", "custom", "gambling", "malware", "phishing"], "dry_run": False}))

        events += _story(rng, members, now, today, days, metrics)
        DeviceEvent.objects.bulk_create(events, batch_size=500)
        UsageMetric.objects.bulk_create(
            [UsageMetric(device_id=d, bucket_start=b, category=c, key=k, count=v[0], bytes=v[1], seconds=v[2])
             for (d, b, c, k), v in metrics.items()],
            batch_size=1000,
        )
        _chat(owner, members, now)
        _team(owner, members, now)  # v1.4: groups, team tasks, work hours, holidays, leave

    # --- output -----------------------------------------------------------------

    def _report(self, owner: User, members: list[tuple[Person, Member, Device]], password: str) -> None:
        password = password or getattr(self, "_generated", "")
        self.stdout.write(f"Demo account ready: {COMPANY['name']} ({PLAN_CODE}), owner {OWNER_NAME} <{owner.email}>")
        self.stdout.write(f"  password: {password}" if password else "  password: unchanged")
        for person, member, device in members:
            self.stdout.write(f"  {person.name:16} {person.role:18} {member.email:34} {member.member_code}  {device.hostname}")

    def _keep_alive(self, owner: User, minutes: int) -> None:
        self.stdout.write(f"Keeping the working PCs online for {minutes} min (Ctrl+C to stop)...")
        stop = time_module.monotonic() + minutes * 60
        step = 45
        while time_module.monotonic() < stop:
            moment = timezone.now()
            live = Device.objects.filter(owner=owner, machine_id__startswith=MACHINE_PREFIX,
                                         work_state__in=[Device.WorkState.WORKING, Device.WorkState.ON_BREAK])
            for device in live:
                field = "worked_seconds" if device.work_state == Device.WorkState.WORKING else "break_seconds"
                setattr(device, field, getattr(device, field) + step)
                device.last_seen_at = device.work_updated_at = moment
                device.save(update_fields=[field, "last_seen_at", "work_updated_at"])
            time_module.sleep(step)


# --- helpers -----------------------------------------------------------------------


def _local(day: date, hour: int, minute: int) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


def _previous_workday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _day_plan(rng: random.Random, person: Person, day: date, is_today: bool = False) -> list[tuple[str, datetime, str, str]]:
    """One work day: start, tea, lunch, maybe one more break, log out with a note (today: no log-out)."""
    def at(h: int, m: int, jitter: int = 10) -> datetime:
        return _local(day, h, m) + timedelta(minutes=rng.randint(-jitter, jitter))

    h, m = person.start
    plan: list[tuple[str, datetime, str, str]] = [(State.WORKING, at(h, m, 12), "", "")]
    plan.append((State.ON_BREAK, at(11, 15, 15), "Tea / coffee", ""))
    plan.append((State.WORKING, plan[-1][1] + timedelta(minutes=rng.randint(8, 18)), "", ""))
    plan.append((State.ON_BREAK, at(13, 10, 15), "Lunch", ""))
    plan.append((State.WORKING, plan[-1][1] + timedelta(minutes=rng.randint(30, 50)), "", ""))
    if not is_today and rng.random() < person.extra_break_odds:
        reason, note = rng.choice(EXTRA_BREAKS)
        plan.append((State.ON_BREAK, at(15, 40, 30), reason, note))
        plan.append((State.WORKING, plan[-1][1] + timedelta(minutes=rng.randint(15, 40)), "", ""))
    if not is_today:
        plan.append((State.ENDED, at(18, 35, 35), "Done for today", rng.choice(LOGOUT_NOTES[person.name])))
    return plan


def _today_plan(rng: random.Random, person: Person, today: date, now: datetime,
                shift: timedelta) -> list[tuple[str, datetime, str, str]]:
    """Today up to now, plus the person's status for the demo: on a break, or logged out early."""
    plan = [(s, at + shift, r, n) for s, at, r, n in _day_plan(rng, person, today, is_today=True)]
    if person.today == "break":
        cut = now - timedelta(minutes=14)
        plan = [p for p in plan if p[1] <= cut]
        if plan and plan[-1][0] == State.WORKING:
            plan.append((State.ON_BREAK, cut, "Tea / coffee", "Quick chai, back in 10"))
    elif person.today == "left_early":
        cut = now - timedelta(minutes=70)
        plan = [p for p in plan if p[1] <= cut]
        if plan and plan[-1][0] == State.WORKING:
            plan.append((State.ENDED, cut, "Leaving early", "Client meeting at BluePeak, back online tomorrow"))
    plan = [p for p in plan if p[1] <= now]
    if person.today == "working":
        while plan and plan[-1][0] == State.ON_BREAK:
            plan.pop()  # back at their desk: only the demo's one break is going on now
    return plan


def _usage(rng: random.Random, person: Person, device: Device, plan: list, now: datetime,
           metrics: dict[tuple[int, datetime, str, str], list[int]]) -> None:
    """Hourly foreground-app time and site visits for the day's working stretches."""
    stretches = []
    for i, (state, at, _, _) in enumerate(plan):
        if state != State.WORKING:
            continue
        end = plan[i + 1][1] if i + 1 < len(plan) else now
        stretches.append((at, min(end, now)))
    for start, end in stretches:
        bucket = start.astimezone(dt_timezone.utc).replace(minute=0, second=0, microsecond=0)
        while bucket < end:
            nxt = bucket + timedelta(hours=1)
            seconds = int((min(end, nxt) - max(start, bucket)).total_seconds())
            if seconds > 60:
                active = int(seconds * rng.uniform(0.78, 0.94))
                picks = _weighted_sample(rng, person.apps, 3)
                split = _split(rng, active, len(picks))
                for key, part in zip(picks, split):
                    row = metrics[(device.pk, bucket, "app.foreground", key)]
                    row[0] += rng.randint(2, 18)
                    row[2] += part
                for key in _weighted_sample(rng, person.sites, 2):
                    row = metrics[(device.pk, bucket, "web.domain", key)]
                    visits = rng.randint(2, 14)
                    row[0] += visits
                    row[2] += min(visits * rng.randint(40, 180), seconds)
            bucket = nxt


def _weighted_sample(rng: random.Random, items: tuple[tuple[str, int], ...], k: int) -> list[str]:
    pool = list(items)
    out: list[str] = []
    while pool and len(out) < k:
        key = rng.choices([p[0] for p in pool], weights=[p[1] for p in pool])[0]
        out.append(key)
        pool = [p for p in pool if p[0] != key]
    return out


def _split(rng: random.Random, total: int, parts: int) -> list[int]:
    weights = [rng.uniform(0.2, 1.0) for _ in range(parts)]
    weights.sort(reverse=True)
    s = sum(weights)
    out = [int(total * w / s) for w in weights]
    out[0] += total - sum(out)
    return out


def _today_totals(device: Device, now: datetime) -> tuple[int, int]:
    entries = list(WorkLog.objects.filter(device=device, at__date=now.date()).order_by("at"))
    worked = breaks = 0
    for i, entry in enumerate(entries):
        end = entries[i + 1].at if i + 1 < len(entries) else (now if entry.state != State.ENDED else entry.at)
        seconds = max(0, int((end - entry.at).total_seconds()))
        if entry.state == State.WORKING:
            worked += seconds
        elif entry.state == State.ON_BREAK:
            breaks += seconds
    return worked, breaks


def _event(device: Device, kind: str, severity: str, at: datetime, data: dict[str, Any],
           handled_at: datetime | None = None) -> DeviceEvent:
    return DeviceEvent(device=device, client_id=uuid.uuid4(), type=kind, severity=severity, occurred_at=at, data=data,
                       handled_at=handled_at, handled_by=OWNER_NAME if handled_at else "")


def _size(n: int) -> str:
    return f"{n / 1_000_000_000:.1f} GB" if n >= 1_000_000_000 else f"{n / MB:.0f} MB"


def _add(metrics: dict, device: Device, at: datetime, category: str, key: str, count: int = 1, size: int = 0) -> None:
    bucket = at.astimezone(dt_timezone.utc).replace(minute=0, second=0, microsecond=0)
    row = metrics[(device.pk, bucket, category, key)]
    row[0] += count
    row[1] += size


def _story(rng: random.Random, members: list[tuple[Person, Member, Device]], now: datetime,
           today: date, days: int, metrics: dict) -> list[DeviceEvent]:
    """The alerts: a handful of everyday ones each work day, plus today's few that make the demo."""
    by_name = {p.name: d for p, _, d in members}
    out: list[DeviceEvent] = []

    def usb_copy(device: Device, at: datetime, files: int, size: int, drive: str, handled: datetime | None = None) -> None:
        sev = "warning" if size >= 52_428_800 else "info"
        out.append(_event(device, "usb.connected", "info", at - timedelta(minutes=3),
                          {"device_name": drive.split(" (")[0], "drive_letter": drive[-3:-1], "vendor": drive.split()[0]}))
        out.append(_event(device, "data.usb_copy", sev, at, {
            "summary": f"{files} files ({_size(size)}) to {drive}", "channel": "usb", "device": drive,
            "file_count": files, "total_bytes": size}, handled))
        _add(metrics, device, at, "transfer.usb", drive, files, size)

    def share_copy(device: Device, at: datetime, files: int, size: int, share: str, handled: datetime | None = None) -> None:
        sev = "warning" if size >= 52_428_800 else "info"
        out.append(_event(device, "data.share_copy", sev, at, {
            "summary": f"{files} files ({_size(size)}) to {share}", "channel": "lan", "share": share,
            "file_count": files, "total_bytes": size}, handled))
        _add(metrics, device, at, "transfer.lan", share, files, size)

    def email(device: Device, at: datetime, count: int, size: int, handled: datetime | None = None) -> None:
        sev = "warning" if size >= 20_971_520 else "info"
        out.append(_event(device, "data.email_attachment", sev, at, {
            "summary": f"{count} {'file' if count == 1 else 'files'} ({_size(size)}) emailed via Outlook",
            "channel": "email", "attachment_count": count, "total_bytes": size}, handled))
        _add(metrics, device, at, "transfer.email", "Outlook", count, size)

    def upload(device: Device, at: datetime, host: str, size: int, handled: datetime | None = None) -> None:
        out.append(_event(device, "net.upload_over_limit", "warning", at, {
            "summary": f"Large upload to {host}: over the 100 MB limit", "host": host,
            "total_bytes": size, "limit_bytes": 100 * MB}, handled))
        _add(metrics, device, at, "net.upload", host, 1, size)

    def remote(device: Device, at: datetime, tool: str, handled: datetime | None = None) -> None:
        if tool == "AnyDesk":  # blocked by the company's rule
            out.append(_event(device, "remote.blocked", "warning", at, {
                "summary": "AnyDesk was closed: blocked by your company's rule", "tool": tool, "process_count": 1,
                "dry_run": False}, handled))
        else:
            out.append(_event(device, "remote.tool_detected", "warning", at, {
                "summary": f"Remote-access tool detected: {tool} (running process)", "tool": tool,
                "detected_by": "process"}, handled))
        _add(metrics, device, at, "remote.tool", tool)  # what the agent counts per sighting

    def software(device: Device, at: datetime, product: str, publisher: str, handled: datetime | None = None) -> None:
        out.append(_event(device, "software.installed", "info", at, {
            "summary": f"New software installed: {product} ({publisher})", "product": product, "publisher": publisher},
            handled))
        _add(metrics, device, at, "software.installed", product)

    def blocked_site(device: Device, at: datetime, domain: str, category: str, handled: datetime | None = None) -> None:
        out.append(_event(device, "web.blocked_attempt", "warning", at, {
            "summary": f"Blocked website attempt: {domain}", "domain": domain, "dry_run": False, "category": category},
            handled))

    # Everyday activity on past work days (all handled or harmless).
    drives = ["SanDisk Ultra (E:)", "Kingston DataTraveler (F:)", "HP v236w (E:)", "Samsung T7 (G:)"]
    shares = [r"\\KC-NAS\Projects", r"\\KC-NAS\Design-Assets", r"\\KC-NAS\Finance"]
    for back in range(days - 1, 0, -1):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        for _ in range(rng.randint(1, 3)):
            person, _, device = rng.choice(members)
            at = _local(day, rng.randint(10, 17), rng.randint(0, 59))
            seen = at + timedelta(hours=rng.randint(1, 20))
            kind = rng.choices(["usb", "share", "email", "software", "site", "upload"], weights=[30, 25, 25, 8, 7, 5])[0]
            if kind == "usb":
                usb_copy(device, at, rng.randint(2, 40), rng.randint(4, 140) * MB, rng.choice(drives), seen)
            elif kind == "share":
                share_copy(device, at, rng.randint(3, 60), rng.randint(10, 300) * MB,
                           shares[2] if person.name == "Priya Nair" else rng.choice(shares[:2]), seen)
            elif kind == "email":
                email(device, at, rng.randint(1, 4), rng.randint(2, 28) * MB, seen)
            elif kind == "software":
                product, publisher = rng.choice([("Zoom Workplace", "Zoom Video Communications, Inc."),
                                                 ("Notion", "Notion Labs, Inc."), ("Postman", "Postman, Inc."),
                                                 ("WinRAR 7.01 (64-bit)", "win.rar GmbH"), ("Grammarly", "Grammarly")])
                software(device, at, product, publisher, seen)
            elif kind == "site":
                blocked_site(device, at, rng.choice(["bet365.com", "1xbet.com", "thepiratebay.org"]),
                             rng.choice(["gambling", "custom"]), seen)
            else:
                upload(device, at, rng.choice(["wetransfer.com", "drive.google.com"]), rng.randint(110, 480) * MB, seen)

    # The few things that need the owner's eye (today and yesterday), unhandled.
    ago = lambda minutes: now - timedelta(minutes=minutes)  # noqa: E731
    yesterday = _previous_workday(today)
    y = lambda h, m: _local(yesterday, h, m)  # noqa: E731
    remote(by_name["Priya Nair"], ago(212), "UltraViewer")
    usb_copy(by_name["Karan Malhotra"], ago(174), 214, 1_840 * MB, "SanDisk Ultra (E:)")
    blocked_site(by_name["Karan Malhotra"], ago(78), "bet365.com", "gambling")
    remote(by_name["Michael Chen"], ago(253), "AnyDesk")
    email(by_name["Priya Nair"], ago(86), 3, 26 * MB)
    software(by_name["Olivia Martinez"], ago(308), "uTorrent", "BitTorrent Inc.")
    upload(by_name["Sneha Reddy"], y(17, 20), "wetransfer.com", 640 * MB)
    share_copy(by_name["Vikram Singh"], y(16, 5), 1_206, 3_400 * MB, r"\\KC-NAS\Projects")
    return out


def _chat(owner: User, members: list[tuple[Person, Member, Device]], now: datetime) -> None:
    """A few real-sounding threads; two replies still unread by the owner (the Chat badge)."""
    member = {p.name: m for p, m, _ in members}
    O, M = Message.Sender.OWNER, Message.Sender.MEMBER
    threads = [
        ("Priya Nair", [
            (O, 205, "Did someone ask you to install UltraViewer?", "Priya, Zinwe flagged UltraViewer on your PC. Did someone call asking you to install it? Please don't share any codes.", "warning", "chat_email"),
            (M, 196, "", "Yes, a caller said he was from the bank about our GST refund. I closed it and didn't share anything.", "info", "chat"),
            (O, 191, "", "Good call. Banks never ask for that. Please send me his number so I can report it.", "info", "chat"),
            (M, 188, "", "Sent on WhatsApp. Thanks Ananya!", "info", "chat"),
        ]),
        ("Karan Malhotra", [
            (O, 64, "About the pen drive copy today", "Hi Karan, Zinwe shows 1.8 GB copied to a SanDisk pen drive from your laptop earlier today. What was it for?", "info", "chat"),
            (M, 41, "", "Hi Ananya, the demo videos and pitch deck for the Austin meeting tomorrow. The hotel Wi-Fi is bad, so I'm carrying them. I'll wipe the drive after.", "info", "chat"),
        ]),
        ("Emily Carter", [
            (O, 1440 + 300, "Sprint review", "Emily, can you share the sprint board with the Denver team before tomorrow's 9 PM IST call?", "info", "chat"),
            (M, 1440 + 285, "", "Done, shared it in Slack and on the invite.", "info", "chat"),
            (O, 95, "Team call at 4:30", "Quick sync with the Denver client at 4:30 PM IST. 15 minutes.", "info", "chat"),
        ]),
        ("Arjun Mehta", [
            (O, 2880 + 200, "Task for today", "Please get the checkout fix live on staging today, the client is testing tomorrow morning.", "info", "chat"),
            (M, 2880 + 150, "", "On it, staging by 6.", "info", "chat"),
            (M, 2880 - 40, "", "Done: release 3.2 on staging, PR #482 merged.", "info", "chat"),
        ]),
        ("Jake Thompson", [
            (M, 52, "", "Heading out for the BluePeak meeting, I'll log in early tomorrow. Notes in the CRM tonight.", "info", "chat"),
        ]),
        ("Olivia Martinez", [
            (O, 230, "uTorrent on your PC", "Hi Olivia, uTorrent showed up on your PC this morning. Can you remove it? We use the NAS for big files.", "info", "chat_email"),
            (M, 214, "", "Sorry, it came with a test build download. Uninstalled now.", "info", "chat"),
        ]),
    ]
    unread_by_owner = {("Karan Malhotra", 41), ("Jake Thompson", 52)}
    for name, rows in threads:
        for sender, minutes_ago, title, body, severity, channel in rows:
            at = now - timedelta(minutes=minutes_ago)
            mine = sender == O
            msg = Message.objects.create(
                owner=owner, member=member[name], sender=sender, channel=channel if mine else Message.Channel.CHAT,
                title=title, body=body, severity=severity,
                read_by_owner=mine or (name, minutes_ago) not in unread_by_owner,
                read_by_member=True,
                delivered_at=at + timedelta(seconds=20) if mine else None,
                read_by_member_at=at + timedelta(minutes=3) if mine else None,
            )
            Message.objects.filter(pk=msg.pk).update(created_at=at)


# --- v1.4: groups, team tasks, work hours ----------------------------------------------------------------------

GROUPS = (
    ("Design", "M12 19l7-7 3 3-7 7-3-3zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5zM2 2l7.586 7.586", ("Priya Nair", "Emily Carter", "Fatima Sheikh")),
    ("Development", "M16 18l6-6-6-6M8 6l-6 6 6 6", ("Karan Malhotra", "Arjun Mehta", "Jake Thompson", "Vikram Singh")),
    ("Everyone", "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8", tuple(p.name for p in PEOPLE)),
)
HOLIDAYS = (("Guru Nanak Jayanti", date(2026, 11, 24)), ("Christmas", date(2026, 12, 25)), ("Republic Day", date(2027, 1, 26)))


def _team(owner: User, members: list[tuple[Person, Member, Device]], now: datetime) -> None:
    """Groups Design / Development / Everyone with a few lines each, the team tasks (the Lumen relay first), work
    hours and holidays. Rebuilt from scratch on every run, like the chat."""
    from z_chat.models import ChatMessage, Conversation, ConversationMember
    from z_devices import work_tasks
    from z_devices.models import Holiday, Leave, Task, TaskActivity, TaskPerson, WorkSchedule
    from z_devices.work_rules import DEFAULTS

    member = {p.name: m for p, m, _ in members}
    device = {p.name: d for p, _, d in members}
    Task.objects.filter(owner=owner).delete()
    Conversation.objects.filter(owner=owner).delete()
    WorkSchedule.objects.filter(owner=owner).delete()
    Holiday.objects.filter(owner=owner).delete()
    Leave.objects.filter(owner=owner).delete()

    ago = lambda minutes: now - timedelta(minutes=minutes)  # noqa: E731

    # Work hours: the company's (Mon to Fri, 10 to 7, 7 hours make a full day), and Arjun's PC starts later (he
    # covers the US evening).
    WorkSchedule.objects.create(owner=owner, scope_kind="company", scope_key="",
                                **{**DEFAULTS, "days": "1111100", "full_day_hours": 7})
    WorkSchedule.objects.create(owner=owner, scope_kind="device", scope_key=str(device["Arjun Mehta"].pk),
                                start="11:00", end="20:00", break_allowed_min=90)
    for name, day in HOLIDAYS:
        Holiday.objects.create(owner=owner, name=name, date=day)
    Leave.objects.create(owner=owner, member=member["Fatima Sheikh"], date=now.date(), half=False,
                         status=Leave.Status.APPROVED, note="Family function", decided_at=ago(60 * 26))
    Leave.objects.create(owner=owner, member=member["Sneha Reddy"], date=now.date() + timedelta(days=4), half=True,
                         note="Doctor's appointment in the morning")

    groups = {}
    for index, (name, icon, people) in enumerate(GROUPS):
        made = ago(60 * 24 * (12 - index))
        conv = Conversation.objects.create(owner=owner, kind="group", name=name, icon=icon, created_at=made,
                                           last_message_at=made, owner_last_read_at=now)
        ConversationMember.objects.bulk_create([
            ConversationMember(conversation=conv, member=member[n], joined_at=made, last_read_at=now) for n in people
        ])
        ChatMessage.objects.create(conversation=conv, system=True, created_at=made,
                                   body=f"You made the group and added {_names([n.split()[0] for n in people])}.")
        groups[name] = conv

    def say(group: str, who: str | None, minutes: int, body: str) -> None:
        at = ago(minutes)
        ChatMessage.objects.create(conversation=groups[group], sender_member=member[who] if who else None, body=body,
                                   created_at=at)
        Conversation.objects.filter(pk=groups[group].pk).update(last_message_at=at)

    say("Everyone", None, 60 * 24 * 3, "Diwali party on Monday from 5 PM in the main hall. Family welcome!")
    say("Everyone", "Sneha Reddy", 60 * 24 * 3 - 20, "The caterer needs the final count by Sunday, please reply to Fatima.")
    say("Development", "Vikram Singh", 300, "Staging is back up, the certificate is renewed.")
    say("Development", "Karan Malhotra", 120, "Lumen hero after lunch, checkout fix right after.")
    say("Design", "Emily Carter", 180, "Both hero options are in the Design folder: A is the office photo, B the illustration.")
    say("Design", "Priya Nair", 95, "Copy and images for Lumen are in the Lumen folder.")

    # A direct chat between two members (the owner never sees it; it shows in their Zinwe windows).
    direct = Conversation.objects.create(owner=owner, kind="direct", direct_key=":".join(
        str(i) for i in sorted((member["Priya Nair"].pk, member["Emily Carter"].pk))), created_at=ago(400))
    for n in ("Priya Nair", "Emily Carter"):
        ConversationMember.objects.create(conversation=direct, member=member[n], joined_at=ago(400), last_read_at=now)
    for who, minutes, body in (("Emily Carter", 70, "Option B for the hero?"), ("Priya Nair", 66, "Yes, B. The photo is too dark on phones.")):
        ChatMessage.objects.create(conversation=direct, sender_member=member[who], body=body, created_at=ago(minutes))
    Conversation.objects.filter(pk=direct.pk).update(last_message_at=ago(66))

    today = timezone.localtime(now)

    def due(days: int, hour: int = 18) -> datetime:
        return _local((today + timedelta(days=days)).date(), hour, 0)

    def task(title: str, people: tuple[str, ...], days: int, steps: list[tuple[str, str]], sent_min: int, *,
             note: str = "", high: bool = False, in_order: bool = False, group: str | None = None,
             repeat: str = "", hour: int = 18) -> Task:
        t = work_tasks.create_team_task(
            owner, [member[n] for n in people], title, note, due(days, hour), "high" if high else "normal",
            [{"title": s, "owner_id": member[o].pk} for s, o in steps], in_order=in_order, repeat=repeat,
            group=groups.get(group) if group else None, post_in_group=bool(group),
        )
        sent = ago(sent_min)
        Task.objects.filter(pk=t.pk).update(created_at=sent, status_changed_at=sent)
        TaskActivity.objects.filter(task=t).update(at=sent)
        Message.objects.filter(pk=t.message_id).update(created_at=sent, delivered_at=sent + timedelta(seconds=20))
        ChatMessage.objects.filter(task=t).update(created_at=sent)
        return Task.objects.get(pk=t.pk)

    def progress(t: Task, status: str, minutes: int, seen: int | None = None, done_steps: int = 0,
                 people_done: tuple[str, ...] = (), flag: tuple[str, str, str] | None = None) -> None:
        at = ago(minutes)
        steps = list(t.steps.order_by("position", "id"))
        for i, s in enumerate(steps[:done_steps]):
            when = at - timedelta(minutes=10 * (done_steps - i))
            s.done, s.done_by_id, s.done_at = True, s.owner_id, when
            s.save(update_fields=["done", "done_by", "done_at"])
            TaskActivity.objects.create(task=t, by="member", member_id=s.owner_id, action="step_done", note=s.title, at=when)
        for p in TaskPerson.objects.filter(task=t).select_related("member"):
            p.seen_at = ago(seen) if seen is not None else None
            if status not in ("sent", "seen"):
                p.started_at = p.seen_at
            if p.member.name in people_done or status in ("review", "approved"):
                p.done_at = at
            if flag and p.member.name == flag[0]:
                p.flag, p.flag_note = flag[1], flag[2]
            p.save()
        if seen is not None:
            TaskActivity.objects.create(task=t, by="member", member=t.member, action="seen", at=ago(seen))
        fields = {"status": status, "status_changed_at": at, "status_changed_by": "member" if status != "approved" else "owner"}
        if flag:
            fields["help_note" if flag[1] == "help" else "cantdo_note"] = flag[2]
            TaskActivity.objects.create(task=t, by="member", member=member[flag[0]], action=flag[1], note=flag[2], at=at)
        if status == "review":
            TaskActivity.objects.create(task=t, by="member", member=t.member, action="done", at=at)
        if status == "approved":
            TaskActivity.objects.create(task=t, by="owner", action="approved", at=at)
        Task.objects.filter(pk=t.pk).update(**fields, updated_at=at)

    def talk(t: Task, rows: list[tuple[str | None, int, str]]) -> None:
        for who, minutes, body in rows:
            m = member[who] if who else t.member
            line = Message.objects.create(
                owner=owner, member=m, sender=Message.Sender.MEMBER if who else Message.Sender.OWNER, body=body,
                read_by_owner=True, read_by_member=True, about_task=t,
            )
            Message.objects.filter(pk=line.pk).update(created_at=ago(minutes))

    # The Lumen relay: Priya's steps 1-2 done, Karan's turn (3-4), then Emily (5-6). Posted in the Design chat.
    lumen = task("Launch the Lumen landing page", ("Priya Nair", "Karan Malhotra", "Emily Carter"), 0, [
        ("Write the page copy", "Priya Nair"), ("Pick the hero images", "Priya Nair"),
        ("Build the hero section", "Karan Malhotra"), ("Connect the signup form", "Karan Malhotra"),
        ("Check it on phones", "Emily Carter"), ("Final check with Ananya", "Emily Carter"),
    ], 60 * 30, note="Client demo on Monday morning. Use option B for the hero.", high=True, in_order=True, group="Design")
    progress(lumen, "progress", 220, seen=60 * 30 - 20, done_steps=2, people_done=("Priya Nair",))
    TaskPerson.objects.filter(task=lumen, member__name__in=["Emily Carter"]).update(started_at=None)
    talk(lumen, [("Priya Nair", 218, "Copy and images are in the Lumen folder. Karan, over to you."),
                 ("Karan Malhotra", 190, "Thanks! Starting after lunch."), (None, 185, "Great. Let's aim for 6.")])

    party = task("Prepare the Diwali office party", ("Sneha Reddy", "Fatima Sheikh", "Arjun Mehta"), 2, [
        ("Book the caterer for 40 people", "Sneha Reddy"), ("Order the sweets boxes", "Fatima Sheikh"),
        ("Put up the lights and rangoli", "Sneha Reddy"), ("Set up music and speakers", "Arjun Mehta"),
        ("Send the invite to everyone", "Fatima Sheikh"),
    ], 60 * 24 * 3, note="Party on Monday from 5 PM in the main hall. Budget Rs 40,000. Veg menu only.", group="Everyone")
    progress(party, "review", 40, seen=60 * 24 * 3 - 20, done_steps=5)
    talk(party, [("Sneha Reddy", 60 * 24 * 2, "Caterer booked, they need the final count by Sunday."),
                 (None, 60 * 24 * 2 - 25, "Count is 38. Thanks Sneha!")])

    invoices = task("Send the October invoices", ("Priya Nair",), 1, [
        ("Make the invoices in Tally", "Priya Nair"), ("Check the GST line on each", "Priya Nair"),
        ("Email them to the 12 clients", "Priya Nair"),
    ], 60 * 30)
    progress(invoices, "review", 65, seen=60 * 30 - 7, done_steps=3)

    hero = task("Design the new landing page hero", ("Emily Carter",), 1, [
        ("Make two image options", "Emily Carter"), ("Pick one with Ananya", "Emily Carter"),
        ("Export it for web and phone", "Emily Carter"),
    ], 60 * 28, note="Brand colours only.")
    progress(hero, "help", 12, seen=60 * 28 - 12, done_steps=1, flag=("Emily Carter", "help", "Which hero image, A or B?"))

    ssl = task("Renew the SSL certificates", ("Arjun Mehta",), 2, [
        ("Get the new certificates from the provider", "Arjun Mehta"), ("Install them on the web server", "Arjun Mehta"),
        ("Check the site shows the lock", "Arjun Mehta"),
    ], 60 * 29, note="They expire on Wed 11 Nov.", high=True)
    progress(ssl, "cantdo", 110, seen=60 * 29 - 5, flag=("Arjun Mehta", "cantdo", "No admin access to the server."))

    links = task("Fix the broken links on the Lumen site", ("Jake Thompson",), -1, [
        ("Run the link checker", "Jake Thompson"), ("Fix or remove each broken link", "Jake Thompson"),
    ], 60 * 47)
    progress(links, "seen", 60 * 30, seen=60 * 30)

    checkout = task("Get the checkout fix live on staging", ("Karan Malhotra",), 0, [
        ("Fix the coupon rounding", "Karan Malhotra"), ("Deploy to staging", "Karan Malhotra"),
        ("Ask the client to test", "Karan Malhotra"),
    ], 60 * 28, note="The client tests tomorrow morning.", high=True)
    progress(checkout, "progress", 60 * 22, seen=60 * 28 - 7, done_steps=1)

    case = task("Write the case study for Northwind", ("Priya Nair",), 1, [], 400, note="One page.")
    progress(case, "seen", 394, seen=394)

    task("Pay the office rent", ("Fatima Sheikh",), 3, [], 15, note="The landlord's account is in the Accounts sheet.",
         repeat="month", hour=12)

    nas = task("Back up the design files to the NAS", ("Vikram Singh",), -4, [
        ("Copy the 2026 folders", "Vikram Singh"), ("Check the copy opens", "Vikram Singh"),
    ], 60 * 24 * 6)
    progress(nas, "approved", 60 * 24 * 5, seen=60 * 24 * 6 - 4, done_steps=2)

    # Each group's newest line (the task cards were back-dated above).
    for conv in groups.values():
        newest = ChatMessage.objects.filter(conversation=conv).order_by("-created_at").values_list("created_at", flat=True).first()
        Conversation.objects.filter(pk=conv.pk).update(last_message_at=newest)
    # Lines the owner hasn't read yet (the Chat badge): Emily's and Priya's in Design.
    Conversation.objects.filter(pk=groups["Design"].pk).update(owner_last_read_at=ago(200))


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
