"""
F-22 User interactions in the admin app (Marketing -> User interactions): every pop-up and bar with its numbers.

GET /api/admin/announcements/?view=interactions ->
    {interactions: [row + stats], rows: (the same list), totals: {live, scheduled, paused, shown_this_month,
     feedback_count, feedback_avg, paid_after}}
stats = {shown (owners who saw it), views, closed, clicks, feedback_count, feedback_avg, paid_after (owners who paid
within 7 days after pressing its button)}. Bars aren't counted per owner: their seen numbers are 0.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db.models import Avg, Count
from django.utils import timezone

from z_auth.models import Announcement, AnnouncementView, Feedback, ManualPayment

from .serializers import AnnouncementSerializer

PAID_WITHIN = timedelta(days=7)


def _avg(value: Any) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _paid_after(announcement_ids: list[int]) -> dict[int, int]:
    """Per interaction: owners who paid (an approved payment) within 7 days after their last press of its button."""
    clicked = list(
        AnnouncementView.objects.filter(announcement_id__in=announcement_ids, clicked_at__isnull=False)
        .values_list("announcement_id", "owner_id", "clicked_at")
    )
    if not clicked:
        return {}
    owners = {owner for _, owner, _ in clicked}
    paid_at: dict[int, list] = defaultdict(list)
    for owner, at, submitted in ManualPayment.objects.filter(
        subscription__owner_id__in=owners, status=ManualPayment.Status.VERIFIED,
    ).values_list("subscription__owner_id", "reviewed_at", "submitted_at"):
        paid_at[owner].append(at or submitted)
    out: dict[int, int] = defaultdict(int)
    for announcement, owner, click in clicked:
        if any(click <= when <= click + PAID_WITHIN for when in paid_at[owner] if when):
            out[announcement] += 1
    return out


def interactions(rows: list[Announcement]) -> dict[str, Any]:
    ids = [a.pk for a in rows]
    feedback = {
        r["announcement_id"]: r
        for r in Feedback.objects.filter(announcement_id__in=ids).values("announcement_id")
        .annotate(n=Count("id"), avg=Avg("rating"))
    }
    paid = _paid_after(ids)
    data = AnnouncementSerializer(rows, many=True).data
    out = []
    for announcement, row in zip(rows, data):
        fb = feedback.get(announcement.pk) or {}
        row = dict(row)
        row["stats"] = {
            "shown": int(getattr(announcement, "stat_owners", 0) or 0),
            "views": int(getattr(announcement, "stat_views", 0) or 0),
            "closed": int(getattr(announcement, "stat_closed", 0) or 0),
            "clicks": int(getattr(announcement, "stat_clicks", 0) or 0),
            "feedback_count": int(fb.get("n") or 0),
            "feedback_avg": _avg(fb.get("avg")),
            "paid_after": paid.get(announcement.pk, 0),
        }
        out.append(row)
    now = timezone.now()
    month_start = timezone.localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    statuses = [r["status"] for r in out]
    all_feedback = Feedback.objects.filter(announcement__isnull=False).aggregate(n=Count("id"), avg=Avg("rating"))
    totals = {
        "live": statuses.count("live"),
        "scheduled": statuses.count("scheduled"),
        "paused": statuses.count("off"),
        "shown_this_month": AnnouncementView.objects.filter(
            announcement_id__in=ids, last_seen_at__gte=month_start
        ).count(),
        "feedback_count": int(all_feedback["n"] or 0),
        "feedback_avg": _avg(all_feedback["avg"]),
        "paid_after": sum(paid.values()),
    }
    return {"interactions": out, "rows": out, "totals": totals}
