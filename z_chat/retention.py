"""
Team chat retention (v1.4): each plan keeps `chat_history_days` of group and direct chat (z_auth.entitlements).

Reads already hide older lines (z_chat.services.visible_lines). This job deletes them for good once they are GRACE_DAYS
past the window (an owner who upgrades within a month gets them back). Reports keep their own copy of the reported
line. Runs with the other scheduled jobs (`manage.py run_jobs`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from z_auth.entitlements import entitlements_for
from z_auth.models import User

from .models import ChatMessage, Conversation

GRACE_DAYS = 30


@dataclass
class ChatPruneResult:
    owners: int = 0
    lines: int = 0
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.lines} chat line(s) deleted for {self.owners} owner(s), {len(self.failed)} failed"


def prune_chat(now: datetime | None = None, dry_run: bool = False) -> ChatPruneResult:
    now = now or timezone.now()
    result = ChatPruneResult()
    for owner in User.objects.filter(pk__in=Conversation.objects.values("owner_id")):
        try:
            ent = entitlements_for(owner)
            if not ent.chat_history_days:
                continue
            cutoff = now - timedelta(days=ent.chat_history_days + GRACE_DAYS)
            old = ChatMessage.objects.filter(conversation__owner=owner, created_at__lt=cutoff)
            count = old.count()
            if count:
                result.owners += 1
                result.lines += count
                if not dry_run:
                    # A line someone replied to goes too: the reply keeps its text, the quote becomes empty (SET_NULL).
                    old.delete()
        except Exception as exc:  # noqa: BLE001 -- one owner's failure mustn't stop the rest
            result.failed.append(f"{owner.pk}: {exc!r}")
    return result
