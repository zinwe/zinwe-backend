"""
The weekly new version plan (tasks.md, Task 1).

`plan_due()` is a jobs-loop job (z_main run_jobs, hourly on the server). From Thursday 03:00 IST it makes that week's
plan once: it reads the feedback and "Report an issue" reports sent since the last weekly plan, asks Claude what the
next version should include, keeps the plan (New version tab in the admin app) and emails it as a PDF to the founder.
The plan row is created first, so the unique weekly constraint stops a second run from making it twice.

Only the text people wrote, ratings and pages go to Claude: never names, emails or company names. Without
ANTHROPIC_API_KEY, or when the call fails, the plan is a plain list of what came in, marked "review".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from z_auth.models import Feedback, Issue
from z_notification.services import notify

from .models import VersionPlan
from .pdf import plan_filename, plan_pdf

logger = logging.getLogger("zinwe.versions")

RUN_WEEKDAY = 3  # Thursday
RUN_HOUR = 3  # 03:00 local (Asia/Kolkata)
MAX_ENTRIES = 300  # per kind, newest first: a busy week still fits one request

KINDS = ("bug", "feature", "improvement")
DECISIONS = ("in", "later", "planned", "review")


@dataclass
class Result:
    made: int = 0
    skipped: str = ""

    def summary(self) -> str:
        return f"{self.made} plan made" if self.made else (self.skipped or "nothing due")


def week_run_at(now: datetime) -> datetime:
    """The latest Thursday 03:00 (local) at or before `now`."""
    local = timezone.localtime(now)
    day = local.date() - timedelta(days=(local.weekday() - RUN_WEEKDAY) % 7)
    run = timezone.make_aware(datetime.combine(day, datetime.min.time()) + timedelta(hours=RUN_HOUR),
                              timezone.get_current_timezone())
    return run if run <= now else run - timedelta(days=7)


def plan_due(now: datetime | None = None) -> Result:
    """Make this week's plan if Thursday 03:00 has passed and it isn't made yet."""
    now = now or timezone.now()
    run_at = week_run_at(now)
    if VersionPlan.objects.filter(kind=VersionPlan.Kind.WEEKLY, period_end=run_at).exists():
        return Result(skipped="this week's plan is already made")
    last = VersionPlan.objects.filter(kind=VersionPlan.Kind.WEEKLY, period_end__lt=run_at).order_by("-period_end").first()
    start = last.period_end if last else run_at - timedelta(days=7)
    try:
        with transaction.atomic():
            plan = VersionPlan.objects.create(kind=VersionPlan.Kind.WEEKLY, period_start=start, period_end=run_at)
    except IntegrityError:  # another run claimed it a moment ago
        return Result(skipped="this week's plan is already made")
    build(plan)
    return Result(made=1)


def make_plan_now(days: int = 7) -> VersionPlan:
    """A plan by hand from the admin app (Make a plan now): the last `days` days up to now."""
    now = timezone.now()
    plan = VersionPlan.objects.create(kind=VersionPlan.Kind.MANUAL, period_start=now - timedelta(days=days), period_end=now)
    return build(plan)


def build(plan: VersionPlan) -> VersionPlan:
    """Collect, draft, save and email one plan. Never raises: a failure is saved on the plan and emailed."""
    try:
        feedback, issues = collect(plan.period_start, plan.period_end)
        draft, model, note = _draft(feedback, issues, _context(plan))
        rated = [f["rating"] for f in feedback]
        plan.counts = {
            "feedback": len(feedback),
            "issues": len(issues),
            "average_rating": round(sum(rated) / len(rated), 1) if rated else None,
        }
        plan.version = draft["version"][:20]
        plan.headline = draft["headline"][:200]
        plan.summary = draft["summary"]
        plan.items = _clean_items(draft["items"], {f["id"] for f in feedback}, {i["id"] for i in issues})
        plan.drafted_by = model
        plan.error = note
        plan.status = VersionPlan.Status.READY
        plan.save()
    except Exception as exc:  # noqa: BLE001 -- keep the row so the tab and the email say what went wrong
        logger.exception("Version plan %s failed", plan.pk)
        plan.status = VersionPlan.Status.FAILED
        plan.error = f"{type(exc).__name__}: {exc}"[:2000]
        plan.save()
    send(plan)
    return plan


def collect(start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The period's feedback and issue reports, without who sent them."""
    feedback = [
        {"id": f.pk, "rating": f.rating, "message": f.message, "page": f.page, "from": f.source}
        for f in Feedback.objects.filter(created_at__gte=start, created_at__lt=end).order_by("-created_at")[:MAX_ENTRIES]
    ]
    issues = [
        {"id": i.pk, "description": i.description, "page": i.page, "status": i.status}
        for i in Issue.objects.filter(created_at__gte=start, created_at__lt=end).order_by("-created_at")[:MAX_ENTRIES]
    ]
    return feedback, issues


def _context(plan: VersionPlan) -> dict[str, Any]:
    from z_devices.models import AgentRelease

    latest = AgentRelease.objects.filter(is_active=True).order_by("-published_at").first()
    earlier = VersionPlan.objects.exclude(pk=plan.pk).exclude(version="").order_by("-created_at").first()
    return {
        "latest_windows_app_release": latest.version if latest else "",
        "last_planned_version": earlier.version if earlier else "",
        "last_plan_items": [i.get("title", "") for i in (earlier.items if earlier else []) if i.get("decision") in ("in", "planned")],
    }


SYSTEM = (
    "You are the product lead for Zinwe, a work-hours and app-usage tracker for small teams (5-50 people) in India "
    "and the US: a Windows app on each PC, a web dashboard for the owner and an admin panel for the Zinwe team. "
    "Each week you read what customers sent (feedback with a 1-10 rating, and 'Report an issue' reports) and plan "
    "the next version for the founder. Group entries that are about the same thing into one item. Mark each item "
    "kind: bug, feature or improvement. Decide: 'in' (goes into the next version), 'later' (worth doing, not now) "
    "or 'planned' (already in the last plan). Put real bugs that block people first. Give one plain sentence of why "
    "for each item and list the feedback and issue ids it comes from. Suggest the next version number from the "
    "versions given (a small fix release bumps the last digit). Write simply, for a busy founder: short sentences, "
    "no jargon, no marketing words. Never invent entries, numbers or customer quotes. If nothing came in, say so "
    "and return no items."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "string"},
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "decision": {"type": "string", "enum": ["in", "later", "planned"]},
                    "why": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"type": {"type": "string", "enum": ["feedback", "issue"]}, "id": {"type": "integer"}},
                            "required": ["type", "id"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "kind", "decision", "why", "sources"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["version", "headline", "summary", "items"],
    "additionalProperties": False,
}


def _draft(feedback: list[dict[str, Any]], issues: list[dict[str, Any]], context: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """(plan, drafted_by, note). Claude when there is something to read and a key; a plain list otherwise."""
    if not feedback and not issues:
        return {"version": "", "headline": "No new feedback or issue reports this week.", "summary": "", "items": []}, "", ""
    if not settings.ANTHROPIC_API_KEY:
        return _plain(feedback, issues), "list", "No ANTHROPIC_API_KEY on this server, so this is a plain list."
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=300.0, max_retries=2)
        response = client.beta.messages.create(
            model=settings.ZINWE_PLAN_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps({"context": context, "feedback": feedback, "issues": issues})}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to draft the plan")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("the plan was too long and was cut off")
        text = "".join(block.text for block in response.content if block.type == "text")
        return json.loads(text), response.model, ""
    except Exception as exc:  # noqa: BLE001 -- the founder still gets the week's entries as a list
        logger.warning("Version plan: Claude call failed, sending a plain list", exc_info=True)
        return _plain(feedback, issues), "list", f"Claude couldn't draft the plan ({type(exc).__name__}: {exc}), so this is a plain list."[:2000]


def _plain(feedback: list[dict[str, Any]], issues: list[dict[str, Any]]) -> dict[str, Any]:
    items = [
        {"title": _short(i["description"]), "kind": "bug", "decision": "review", "why": f"Reported on {i['page'] or 'an unknown page'}.",
         "sources": [{"type": "issue", "id": i["id"]}]}
        for i in issues
    ] + [
        {"title": _short(f["message"]), "kind": "improvement", "decision": "review", "why": f"Rated {f['rating']}/10.",
         "sources": [{"type": "feedback", "id": f["id"]}]}
        for f in feedback if f["message"].strip()
    ]
    return {"version": "", "headline": f"{len(issues)} issue reports and {len(feedback)} feedback entries to review.",
            "summary": "", "items": items}


def _short(text: str, size: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def _clean_items(items: list[dict[str, Any]], feedback_ids: set[int], issue_ids: set[int]) -> list[dict[str, Any]]:
    """Keep the fields we show, and only sources that really are in this period."""
    known = {"feedback": feedback_ids, "issue": issue_ids}
    clean = []
    for item in items:
        sources = [s for s in item.get("sources", []) if s.get("id") in known.get(s.get("type"), set())]
        clean.append({
            "title": str(item.get("title", ""))[:200],
            "kind": item.get("kind") if item.get("kind") in KINDS else "improvement",
            "decision": item.get("decision") if item.get("decision") in DECISIONS else "review",
            "why": str(item.get("why", ""))[:600],
            "sources": sources,
        })
    order = {d: n for n, d in enumerate(DECISIONS)}
    return sorted(clean, key=lambda i: (order[i["decision"]], KINDS.index(i["kind"])))


def send(plan: VersionPlan) -> None:
    """Email the plan (PDF attached) to the founder; the outcome is saved on the plan."""
    to = settings.ZINWE_FOUNDER_EMAIL
    if not to:
        return
    files = [(plan_filename(plan), plan_pdf(plan), "application/pdf")] if plan.status != VersionPlan.Status.FAILED else []
    result = notify(
        "email", to, "version_plan",
        {
            "plan": plan,
            "failed": plan.status == VersionPlan.Status.FAILED,
            "period": period_label(plan),
            "going_in": [i for i in plan.items if i["decision"] == "in"],
            "admin_url": f"{settings.ZINWE_ADMIN_URL}/new-version?plan={plan.pk}",
        },
        ref=plan, attachments=files,
    )
    plan.emailed_to = to
    plan.email_status = "sent" if result is not None and result.status == "sent" else (getattr(result, "error", "") or "not sent")[:200]
    plan.emailed_at = timezone.now()
    plan.save(update_fields=["emailed_to", "email_status", "emailed_at"])


def period_label(plan: VersionPlan) -> str:
    start, end = timezone.localtime(plan.period_start), timezone.localtime(plan.period_end)
    return f"{start:%d %b} – {end:%d %b %Y}"


def decide(plan: VersionPlan, status: str, user: Any) -> VersionPlan:
    """Approve or set aside a ready plan (or put it back to ready)."""
    allowed = {VersionPlan.Status.READY, VersionPlan.Status.APPROVED, VersionPlan.Status.SET_ASIDE}
    if status not in allowed or plan.status not in allowed:
        raise ValueError("Only a finished plan can be approved or set aside.")
    plan.status = status
    plan.decided_by = user if status != VersionPlan.Status.READY else None
    plan.decided_at = timezone.now() if status != VersionPlan.Status.READY else None
    plan.save(update_fields=["status", "decided_by", "decided_at"])
    return plan


def next_run_at(now: datetime | None = None) -> datetime:
    """When the next weekly plan is made (the jobs loop runs hourly, so within the hour after this)."""
    return week_run_at(now or timezone.now()) + timedelta(days=7)
