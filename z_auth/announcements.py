"""
Notification-bar items: the slim bars on the owner dashboard and the public website.

- **Pushed messages** (offer, discount, news, new version, wish, maintenance) are `Announcement` rows the
  Zinwe team writes in the admin panel (Announcements page), each with a placement (dashboard,
  website, both), an audience (everyone / free / paid / chosen plans — dashboard only) and a time window.
- **Pop-ups** (F-11, placement "popup") are the same rows shown as a moments pop-up on the dashboard, in their
  own list. The server picks at most one a day per owner and follows a rhythm kept on the account
  (AnnouncementView): first visit, again after 2 days, then after a week, at most 3 times; closing it stops it.
  Free plans get every kind; paid plans only news, new versions, wishes and maintenance (D5).
- **Configured banners** (``settings.ZINWE_ANNOUNCEMENTS``) still work, for an environment-wide note
  without the admin panel.
- **The upgrade bar** (dashboard only) is derived live from the owner's plan and entitlements: what the
  next plan adds, or — when every PC slot is taken — that a new PC won't connect until they upgrade.

Each item is a plain dict the frontend renders and can remember-dismiss by ``id`` (an edited message
gets a new id, so it shows again).
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .billing import current_subscription
from .entitlements import Entitlements, devices_used, entitlements_for
from .models import Announcement, AnnouncementView, Plan, Subscription, User

ALLOWED_KINDS = {"version", "offer", "discount", "news", "maintenance", "wish", "upgrade"}

# F-11 / D5: what a pop-up may be about for an owner on a paid plan. Free plans get every kind.
PAID_POPUP_KINDS = {
    Announcement.Kind.NEWS, Announcement.Kind.VERSION, Announcement.Kind.WISH, Announcement.Kind.MAINTENANCE,
}
# Show it again this long after the 1st view, then after the 2nd; never after the 3rd.
POPUP_GAPS = (timedelta(days=2), timedelta(days=7))
POPUP_MAX_VIEWS = 3

# Short names for "Businesses adds work hours, app usage and team chat".
SHORT_MODULE_NAMES: dict[str, str] = {
    "usb_control": "USB blocking",
    "data_transfer": "file-transfer alerts",
    "remote_access": "remote-access control",
    "software_installs": "new software alerts",
    "web": "website protection",
    "work_time": "work hours",
    "app_usage": "app usage",
    "team_chat": "team chat",
    "alert_to_employee": "alerts to employees",
}


def _item(
    ident: str, kind: str, title: str, body: str = "", cta_label: str = "", cta_href: str = "",
    dismissible: bool = True,
) -> dict[str, Any]:
    return {
        "id": ident,
        "kind": kind,
        "title": title,
        "body": body,
        "cta_label": cta_label,
        "cta_href": cta_href,
        "dismissible": dismissible,
    }


# --- Pushed from the admin panel ------------------------------------------------------


def _live(placements: tuple[str, ...]):  # type: ignore[no-untyped-def]
    now = timezone.now()
    return Announcement.objects.filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=now),
        is_active=True,
        starts_at__lte=now,
        placement__in=placements,
    )


def _reaches(announcement: Announcement, ent: Entitlements, email: str = "") -> bool:
    """Does this dashboard message's audience include an owner with these entitlements (and this email)?"""
    audience = announcement.audience
    if audience == Announcement.Audience.OWNERS:
        wanted = {e.strip().lower() for e in announcement.owner_emails or [] if isinstance(e, str)}
        return bool(email) and email.strip().lower() in wanted
    if audience == Announcement.Audience.EVERYONE:
        return True
    paid = _is_paid(ent)
    if audience == Announcement.Audience.FREE:
        return not paid
    if audience == Announcement.Audience.PAID:
        return paid
    return bool(ent.on_active_plan and ent.plan and ent.plan.code in (announcement.plan_codes or []))


def offer_link(announcement: Announcement) -> str:
    """Where an offer's button goes: the upgrade page with the offer applied (checked again at payment)."""
    return f"/app/upgrade?plan={announcement.offer_plan.code}&offer={announcement.pk}" if announcement.offer_plan_id else ""


def _as_item(announcement: Announcement) -> dict[str, Any]:
    label = announcement.cta_label if announcement.cta_href else ""
    href = announcement.cta_href if announcement.cta_label else ""
    if announcement.offer_plan_id and announcement.offer_price_inr and not href:
        # An offer with no button of its own gets one that applies it.
        label = f"Upgrade for ₹{announcement.offer_price_inr:,.2f} + GST"[:40]
        href = offer_link(announcement)
    return _item(
        f"a{announcement.pk}-{int(announcement.updated_at.timestamp())}",
        announcement.kind,
        announcement.title,
        announcement.body,
        label,
        href,
        announcement.dismissible,
    )


def _is_paid(ent: Entitlements) -> bool:
    return bool(ent.on_active_plan and ent.plan and ent.plan.requires_payment)


def popup_reaches(announcement: Announcement, ent: Entitlements, email: str = "") -> bool:
    """A pop-up's audience, and on a paid plan only the kinds that aren't selling anything (D5)."""
    if _is_paid(ent) and announcement.kind not in PAID_POPUP_KINDS:
        return False
    return _reaches(announcement, ent, email)


def _popup_due(view: AnnouncementView | None, now: Any) -> bool:
    """Never seen; or seen once, 2+ days ago; or twice, 7+ days ago. Closed or seen 3 times: never again."""
    if view is None:
        return True
    if view.closed or view.views >= POPUP_MAX_VIEWS:
        return False
    if view.views == 0 or view.last_seen_at is None:
        return True
    return now - view.last_seen_at >= POPUP_GAPS[view.views - 1]


def popups_for(user: User, ent: Entitlements | None = None) -> list[dict[str, Any]]:
    """
    F-11: the pop-up to show this owner now: none, or one (the highest priority that's due). Nothing when they
    already saw a pop-up today.
    """
    ent = ent or entitlements_for(user)
    now = timezone.now()
    today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if AnnouncementView.objects.filter(owner=user, last_seen_at__gte=today).exists():
        return []
    live = [a for a in _live((Announcement.Placement.POPUP,)).select_related("offer_plan")
            if popup_reaches(a, ent, user.email)]
    views = {v.announcement_id: v for v in AnnouncementView.objects.filter(owner=user, announcement__in=live)}
    for announcement in live:  # already in priority order
        if _popup_due(views.get(announcement.pk), now):
            return [{**_as_item(announcement), "announcement_id": announcement.pk}]
    return []


def record_popup_view(user: User, announcement: Announcement, closed: bool = False) -> AnnouncementView:
    """
    POST .../announcements/<id>/seen/: the dashboard showed the pop-up (one view a day at most, so a reload or a
    repeat doesn't use up its 3 views), or `closed` it for good.
    """
    now = timezone.now()
    view, _ = AnnouncementView.objects.get_or_create(owner=user, announcement=announcement)
    seen_today = view.last_seen_at is not None and timezone.localdate(view.last_seen_at) == timezone.localdate(now)
    if not seen_today:
        view.views += 1
        view.last_seen_at = now
    if closed:
        view.closed = True
    view.save(update_fields=["views", "last_seen_at", "closed"])
    return view


def pushed_for_dashboard(ent: Entitlements, email: str = "") -> list[dict[str, Any]]:
    placements = (Announcement.Placement.DASHBOARD, Announcement.Placement.BOTH)
    return [_as_item(a) for a in _live(placements).select_related("offer_plan") if _reaches(a, ent, email)]


def website_announcements() -> list[dict[str, Any]]:
    """The bars for the public website, sign-up and onboarding (every visitor sees the same). Messages for
    specific owners and offers are dashboard-only: they never show to the public."""
    placements = (Announcement.Placement.WEBSITE, Announcement.Placement.BOTH)
    live = _live(placements).exclude(audience=Announcement.Audience.OWNERS).filter(offer_plan__isnull=True)
    return [_as_item(a) for a in live]


# --- Configured per environment ---------------------------------------------------------


def _config_announcements() -> list[dict[str, Any]]:
    """Validate the configured version/offer banners, dropping malformed entries."""
    out: list[dict[str, Any]] = []
    for raw in settings.ZINWE_ANNOUNCEMENTS or []:
        if not isinstance(raw, dict):
            continue
        ident = str(raw.get("id", "")).strip()
        kind = str(raw.get("kind", "offer")).strip()
        title = str(raw.get("title", "")).strip()
        # The upgrade bar is derived, never configured.
        if not ident or not title or kind not in ALLOWED_KINDS - {"upgrade"}:
            continue
        out.append(
            _item(
                ident, kind, title, str(raw.get("body", "")).strip(),
                str(raw.get("cta_label", "")).strip(), str(raw.get("cta_href", "")).strip(),
                bool(raw.get("dismissible", True)),
            )
        )
    return out


# --- The upgrade bar (dashboard) ------------------------------------------------------------


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _upgrade_announcement(user: User, ent: Entitlements) -> dict[str, Any] | None:
    """
    The next paid plan up and what it adds — or, when every PC slot on the plan is taken, that a new
    PC won't connect until they upgrade. Nothing while a payment is waiting (that has its own banner)
    or on the top plan.
    """
    subscription = current_subscription(user)
    if subscription is not None and subscription.status == Subscription.Status.PENDING_PAYMENT:
        return None

    current = ent.plan
    floor = current.price_inr_monthly if current else 0
    candidates = list(
        Plan.objects.filter(is_active=True, price_inr_monthly__gt=floor).order_by("price_inr_monthly", "sort_order")
    )
    if not candidates:
        return None

    current_name = current.name if current else "the free plan"
    used = devices_used(user)
    if ent.max_devices and used >= ent.max_devices:
        # The smallest plan that fits the team the owner told us about at sign-up (e.g. "11-25" → 25 PCs), and at
        # least one more PC than today: never suggest a plan that can't hold their team.
        need = max(ent.max_devices + 1, team_size_upper(user))
        target = next((p for p in candidates if not p.max_devices or p.max_devices >= need), None)
        if target is None:
            return None
        more = f"up to {target.max_devices} PCs" if target.max_devices else "more PCs"
        return _item(
            f"upgrade-limit:{current.code if current else 'none'}:{target.code}:{ent.max_devices}",
            "upgrade",
            f"All {ent.max_devices} PCs on {current_name} are in use",
            f"A new PC won't connect yet. Upgrade to {target.name} for {more}; your PCs keep working meanwhile.",
            "Upgrade",
            f"/app/upgrade?plan={target.code}",
        )

    target = candidates[0]
    gained = [SHORT_MODULE_NAMES[m] for m in (target.modules or []) if m not in ent.modules and m in SHORT_MODULE_NAMES]
    more_pcs = bool(target.max_devices and target.max_devices > (ent.max_devices or 0))
    body = f"You're on {current_name}."
    if gained:
        shown = gained[:3] + (["more"] if len(gained) > 3 else [])
        body += f" {target.name} adds {_join(shown)}"
        body += f", for up to {target.max_devices} PCs." if more_pcs else "."
    else:
        # Every plan has every feature since 2026-10-17: say what's bigger (PCs, history kept).
        more_history = bool(ent.history_days and (not target.history_days or target.history_days > ent.history_days))
        bigger = ([f"up to {target.max_devices} PCs"] if more_pcs else []) + (
            [f"{history_label(target.history_days)} of history"] if more_history else []
        )
        if bigger:
            body += f" {target.name} covers {_join(bigger)}."
    return _item(
        f"upgrade:{current.code if current else 'none'}:{target.code}",
        "upgrade",
        f"Upgrade to {target.name}",
        body,
        "Upgrade",
        f"/app/upgrade?plan={target.code}",
    )


def team_size_upper(user: User) -> int:
    """The top of the company's team-size range ("11-25" → 25, "50+" → 51), or 0 when unknown."""
    company = getattr(user, "owned_company", None)
    raw = (getattr(company, "team_size", "") or "").strip()
    numbers = [int(n) for n in re.findall(r"\d+", raw)]
    if not numbers:
        return 0
    return numbers[-1] + (1 if raw.endswith("+") else 0)


def history_label(days: int) -> str:
    """365 -> "1 year", 90 -> "90 days", 0 -> "unlimited"."""
    if not days:
        return "unlimited"
    if days % 365 == 0:
        years = days // 365
        return f"{years} year" + ("s" if years > 1 else "")
    return f"{days} days"


def announcements_for(user: User) -> list[dict[str, Any]]:
    """The owner's dashboard bars: pushed messages, configured banners, then the upgrade bar."""
    ent = entitlements_for(user)
    items = pushed_for_dashboard(ent, user.email) + _config_announcements()
    upgrade = _upgrade_announcement(user, ent)
    if upgrade is not None:
        items.append(upgrade)
    return items
