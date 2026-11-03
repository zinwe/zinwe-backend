"""
New version plans: once a week (Thursday 03:00 IST) the jobs loop reads the week's feedback and "Report an issue"
reports, Claude drafts what the next version should include, and the plan is kept here, shown in the admin app's
New version tab and emailed to the founder as a PDF (z_versions.services).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class VersionPlan(models.Model):
    class Kind(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        MANUAL = "manual", "Made by hand"

    class Status(models.TextChoices):
        DRAFTING = "drafting", "Drafting"
        READY = "ready", "Ready to review"
        APPROVED = "approved", "Approved"
        SET_ASIDE = "set_aside", "Set aside"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.WEEKLY)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFTING, db_index=True)
    period_start = models.DateTimeField(help_text="Feedback and issues sent from here...")
    period_end = models.DateTimeField(help_text="...up to (not including) here.")
    version = models.CharField(max_length=20, blank=True, help_text="Suggested version number, e.g. 1.4.")
    headline = models.CharField(max_length=200, blank=True)
    summary = models.TextField(blank=True)
    items = models.JSONField(default=list, blank=True, help_text="[{title, kind, decision, why, sources: [{type, id}]}]")
    counts = models.JSONField(default=dict, blank=True, help_text="{feedback, issues, average_rating}")
    drafted_by = models.CharField(max_length=40, blank=True, help_text="The Claude model, or 'list' without an API key.")
    error = models.TextField(blank=True)
    emailed_to = models.EmailField(blank=True)
    email_status = models.CharField(max_length=200, blank=True, help_text="'sent', or why it failed.")
    emailed_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One weekly plan per Thursday: a second jobs run in the same week finds it and does nothing.
            models.UniqueConstraint(fields=["period_end"], condition=Q(kind="weekly"), name="one_weekly_plan_per_week"),
        ]

    def __str__(self) -> str:
        return f"Version plan {self.version or '#' + str(self.pk)} ({timezone.localtime(self.period_end):%d %b %Y})"
