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
gets a new id, so it shows again). ``image_url`` is the message's optional picture
(``/api/auth/announcements/<id>/image/?v=<updated>``, public) or null.

F-22 **User interactions**: pop-ups gain kinds (feedback_ask, upgrade_ask), a moment (`trigger`: sign in, sign out,
N days after sign-up, after the first PC, N days before the plan ends, a date), new audiences (new in the last 7 days,
plan ending, quiet for N days) and a `frequency` (rhythm, once, every time, every 14 days, until closed). Still at
most one a day per owner, across every kind (bars aside). `{first name}` in the text becomes the owner's first name
(or is dropped with the comma/space before it). Pop-up items also carry `trigger` and `coupon` (null or
{code, plan_code, ends_on, note}); a bar with a coupon carries `coupon` too.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .billing import current_subscription, days_left
from .entitlements import Entitlements, devices_used, entitlements_for
from .models import Announcement, AnnouncementView, Feedback, Plan, Subscription, User

Trigger = Announcement.Trigger
Frequency = Announcement.Frequency
Audience = Announcement.Audience

ALLOWED_KINDS = {"version", "offer", "discount", "news", "maintenance", "wish", "upgrade"}

# F-11 / D5: what a pop-up may be about for an owner on a paid plan. Free plans get every kind.
PAID_POPUP_KINDS = {
    Announcement.Kind.NEWS, Announcement.Kind.VERSION, Announcement.Kind.WISH, Announcement.Kind.MAINTENANCE,
    Announcement.Kind.FEEDBACK_ASK,  # F-22: asking what they think sells nothing
}
# Show it again this long after the 1st view, then after the 2nd; never after the 3rd.
POPUP_GAPS = (timedelta(days=2), timedelta(days=7))
POPUP_MAX_VIEWS = 3
EVERY_14D = timedelta(days=14)
NEW_DAYS = 7
ENDING_WITHIN = timedelta(days=7)
DEFAULT_PLAN_END_DAYS = 7

FIRST_NAME = re.compile(r"\{\s*first name\s*\}", re.IGNORECASE)
FIRST_NAME_WITH_GAP = re.compile(r"[,\s]*\{\s*first name\s*\}", re.IGNORECASE)


def first_name(user: User | None) -> str:
    """The owner's first name from their full name ('' when they never gave one: never the email)."""
    name = (getattr(user, "full_name", "") or "").strip()
    return name.split()[0] if name else ""


def personalise(text: str, user: User | None) -> str:
    """`{first name}` -> the owner's first name; without one the token goes with the comma/space before it."""
    if not text or "{" not in text:
        return text
    name = first_name(user)
    if name:
        return FIRST_NAME.sub(name, text)
    out = FIRST_NAME_WITH_GAP.sub("", text)
    return out.lstrip(" ,") if FIRST_NAME.match(text.lstrip()) else out

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
    dismissible: bool = True, image_url: str | None = None,
) -> dict[str, Any]:
    return {
        "id": ident,
        "kind": kind,
        "title": title,
        "body": body,
        "cta_label": cta_label,
        "cta_href": cta_href,
        "dismissible": dismissible,
        "image_url": image_url,
    }


def image_url(announcement: Announcement) -> str | None:
    """The picture's public path (relative to the API origin), versioned so a new upload isn't served stale."""
    if not announcement.image:
        return None
    return f"/api/auth/announcements/{announcement.pk}/image/?v={int(announcement.updated_at.timestamp())}"


# --- Pushed from the admin panel ------------------------------------------------------


def _live(placements: tuple[str, ...]):  # type: ignore[no-untyped-def]
    now = timezone.now()
    return Announcement.objects.filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=now),
        is_active=True,
        starts_at__lte=now,
        placement__in=placements,
    )


def plan_ending(user: User, now: Any = None) -> bool:
    """F-22: an active plan or trial with an end date within the next 7 days."""
    now = now or timezone.now()
    sub = current_subscription(user)
    return bool(
        sub is not None and sub.status == Subscription.Status.ACTIVE and sub.ends_at is not None
        and now < sub.ends_at <= now + ENDING_WITHIN
    )


def is_quiet(user: User, days: int, now: Any = None) -> bool:
    """
    F-22: the owner hadn't signed in for `days` days: their last sign-in is that old, or they just came back after
    such a gap (sign-ins from the audit trail, else `last_login`). Never signed in: their account is that old.
    """
    from z_analytics.models import Event

    now = now or timezone.now()
    gap = timedelta(days=days)
    logins = list(
        Event.objects.filter(name="auth.login", actor_email=user.email)
        .order_by("-created_at").values_list("created_at", flat=True)[:2]
    )
    latest = logins[0] if logins else user.last_login
    if latest is None:
        return user.date_joined <= now - gap
    if now - latest >= gap:
        return True
    return len(logins) > 1 and latest - logins[1] >= gap


def _reaches(announcement: Announcement, ent: Entitlements, email: str = "", user: User | None = None) -> bool:
    """Does this dashboard message's audience include an owner with these entitlements (and this email)?
    The F-22 audiences (new in 7 days, plan ending, quiet) need the owner (`user`)."""
    audience = announcement.audience
    if audience == Audience.NEW7:
        return user is not None and user.date_joined >= timezone.now() - timedelta(days=NEW_DAYS)
    if audience == Audience.PLAN_ENDING:
        return user is not None and plan_ending(user)
    if audience == Audience.QUIET:
        return user is not None and is_quiet(user, announcement.audience_days or Announcement.QUIET_DAYS)
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


def coupon_link(announcement: Announcement) -> str:
    """F-22: "Use the code" opens the plan page with the code, which the pay page fills in and checks."""
    return f"/onboarding/plan?coupon={announcement.coupon.code}" if announcement.coupon_id else ""


def _as_item(announcement: Announcement, user: User | None = None) -> dict[str, Any]:
    label = announcement.cta_label if announcement.cta_href else ""
    href = announcement.cta_href if announcement.cta_label else ""
    if announcement.coupon_id and not href:
        label = (announcement.cta_label or "Use the code")[:40]
        href = coupon_link(announcement)
    if announcement.offer_plan_id and announcement.offer_price_inr and not href:
        # An offer with no link of its own gets one that applies it: the staff's button text if they wrote one,
        # else the price (whole rupees without ".00").
        price = announcement.offer_price_inr
        amount = f"{int(price):,}" if price == int(price) else f"{price:,.2f}"
        label = (announcement.cta_label or f"Upgrade for ₹{amount} + GST")[:40]
        href = offer_link(announcement)
    item = _item(
        f"a{announcement.pk}-{int(announcement.updated_at.timestamp())}",
        announcement.kind,
        personalise(announcement.title, user),
        personalise(announcement.body, user),
        label,
        href,
        announcement.dismissible,
        image_url(announcement),
    )
    if announcement.coupon_id:
        item["coupon"] = coupon_block(announcement.coupon)
    return item


def coupon_block(coupon: Any) -> dict[str, Any]:
    """
    F-22: the coupon an interaction hands out: {code, plan_code (its one plan, else null), ends_on, note}, e.g.
    "Startups ₹679.70 + GST for the first month, about ₹68 per PC. Ends 5 Jan." (the plan's monthly price after the
    code, floored at its band minimum; without one plan: "30% off your first month.").
    """
    from .coupons import day_text
    from .pricing import coupon_discount, money, rupees

    codes = list(coupon.plan_codes or [])
    plan = Plan.objects.filter(code=codes[0]).first() if len(codes) == 1 else None
    ends = f" Ends {day_text(coupon.ends_on)}." if coupon.ends_on else ""
    if plan is not None and plan.price_inr_monthly > 0:
        off, _ = coupon_discount(plan.price_inr_monthly, coupon.percent_off, plan.band_min_inr)
        price = money(plan.price_inr_monthly - off)
        per_pc = ""
        if plan.max_devices:
            per_pc = f", about {rupees((price / plan.max_devices).quantize(Decimal('1'), rounding=ROUND_HALF_UP))} per PC"
        note = f"{plan.name} {rupees(price)} + GST for the first month{per_pc}.{ends}"
    else:
        note = f"{coupon.percent_off}% off your first month.{ends}"
    return {
        "code": coupon.code,
        "plan_code": plan.code if plan is not None else None,
        "ends_on": coupon.ends_on.isoformat() if coupon.ends_on else None,
        "note": note,
    }


def _is_paid(ent: Entitlements) -> bool:
    return bool(ent.on_active_plan and ent.plan and ent.plan.requires_payment)


def popup_reaches(announcement: Announcement, ent: Entitlements, email: str = "", user: User | None = None) -> bool:
    """A pop-up's audience, and on a paid plan only the kinds that aren't selling anything (D5)."""
    if _is_paid(ent) and announcement.kind not in PAID_POPUP_KINDS:
        return False
    return _reaches(announcement, ent, email, user)


def _popup_due(view: AnnouncementView | None, now: Any) -> bool:
    """Never seen; or seen once, 2+ days ago; or twice, 7+ days ago. Closed or seen 3 times: never again."""
    if view is None:
        return True
    if view.closed or view.views >= POPUP_MAX_VIEWS:
        return False
    if view.views == 0 or view.last_seen_at is None:
        return True
    return now - view.last_seen_at >= POPUP_GAPS[view.views - 1]


def _frequency_due(frequency: str, view: AnnouncementView | None, now: Any) -> bool:
    """F-22: is it this owner's turn, by the interaction's frequency? (Closing stops all but every / every 14 days.)"""
    if frequency == Frequency.ONCE:
        return view is None or (view.views == 0 and not view.closed)
    if frequency == Frequency.EVERY:
        return True
    if frequency == Frequency.EVERY_14D:
        return view is None or view.last_seen_at is None or now - view.last_seen_at >= EVERY_14D
    if frequency == Frequency.UNTIL_CLOSED:
        return view is None or not view.closed
    return _popup_due(view, now)  # rhythm (the pop-ups' default)


def _has_pc(user: User) -> bool:
    from z_devices.models import Device

    return Device.objects.filter(owner=user).exists()


def _trigger_due(announcement: Announcement, user: User, now: Any) -> bool:
    """F-22: has this owner reached the interaction's moment? (Sign in / sign out: always; the moment itself is
    the request.)"""
    trigger = announcement.trigger
    if trigger == Trigger.DAYS_AFTER_SIGNUP:
        joined = timezone.localdate(user.date_joined)
        return (timezone.localdate(now) - joined).days >= (announcement.trigger_days or 0)
    if trigger == Trigger.FIRST_PC:
        return _has_pc(user)
    if trigger == Trigger.BEFORE_PLAN_END:
        sub = current_subscription(user)
        left = days_left(sub, now) if sub is not None else None
        return left is not None and left <= (announcement.trigger_days or DEFAULT_PLAN_END_DAYS)
    if trigger == Trigger.DATE and announcement.ends_at is None:
        return timezone.localdate(now) == timezone.localdate(announcement.starts_at)
    return True


SIGNIN_TRIGGERS = tuple(t for t in Trigger.values if t != Trigger.SIGNOUT)


def popups_for(user: User, ent: Entitlements | None = None, moment: str = "signin") -> list[dict[str, Any]]:
    """
    F-11 / F-22: the pop-up (user interaction) to show this owner now: none, or one (the highest priority that's
    due by its moment, audience and frequency). `moment` "signout" asks for a sign-out interaction; anything else is
    the dashboard visit (sign in and the other moments). Nothing when they already saw one today.
    """
    ent = ent or entitlements_for(user)
    now = timezone.now()
    today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if AnnouncementView.objects.filter(owner=user, last_seen_at__gte=today).exists():
        return []
    triggers = (Trigger.SIGNOUT,) if moment == "signout" else SIGNIN_TRIGGERS
    live = [a for a in _live((Announcement.Placement.POPUP,)).filter(trigger__in=triggers)
            .select_related("offer_plan", "coupon") if popup_reaches(a, ent, user.email, user)]
    views = {v.announcement_id: v for v in AnnouncementView.objects.filter(owner=user, announcement__in=live)}
    for announcement in live:  # already in priority order
        view = views.get(announcement.pk)
        if _frequency_due(announcement.effective_frequency, view, now) and _trigger_due(announcement, user, now):
            return [{
                **_as_item(announcement, user), "announcement_id": announcement.pk,
                "trigger": announcement.trigger, "coupon": coupon_block(announcement.coupon) if announcement.coupon_id
                else None,
            }]
    return []


def record_answer(user: User, announcement: Announcement, rating: int, message: str = "") -> Feedback:
    """
    F-22: POST .../announcements/<id>/answer/: the owner rated Zinwe in a feedback ask. Saved as their feedback
    (with the interaction and its moment, for Inbox -> Feedback), and the pop-up counts as seen and pressed.
    """
    feedback = Feedback.objects.create(
        owner=user, source=Feedback.Source.OWNER, rating=rating, message=(message or "").strip()[:2000],
        page="interaction", announcement=announcement, trigger=announcement.trigger,
    )
    record_popup_view(user, announcement, clicked=True)
    user.feedback_prompt_at = timezone.now()  # the dashboard's own feedback card waits its turn again
    user.save(update_fields=["feedback_prompt_at"])
    return feedback


def record_popup_view(
    user: User, announcement: Announcement, closed: bool = False, clicked: bool = False
) -> AnnouncementView:
    """
    POST .../announcements/<id>/seen/: the dashboard showed the pop-up (one view a day at most, so a reload or a
    repeat doesn't use up its 3 views), `closed` it for good, or the owner pressed its button (`clicked`).
    """
    now = timezone.now()
    view, _ = AnnouncementView.objects.get_or_create(owner=user, announcement=announcement)
    seen_today = view.last_seen_at is not None and timezone.localdate(view.last_seen_at) == timezone.localdate(now)
    if not seen_today:
        view.views += 1
        view.last_seen_at = now
    if closed:
        view.closed = True
    if clicked:
        view.clicks += 1
        view.clicked_at = now
    view.save(update_fields=["views", "last_seen_at", "closed", "clicks", "clicked_at"])
    return view


def pushed_for_dashboard(ent: Entitlements, email: str = "", user: User | None = None) -> list[dict[str, Any]]:
    placements = (Announcement.Placement.DASHBOARD, Announcement.Placement.BOTH)
    return [
        _as_item(a, user) for a in _live(placements).select_related("offer_plan", "coupon")
        if _reaches(a, ent, email, user)
    ]


def website_announcements() -> list[dict[str, Any]]:
    """The bars for the public website, sign-up and onboarding (every visitor sees the same). Messages for
    specific owners and offers are dashboard-only: they never show to the public."""
    placements = (Announcement.Placement.WEBSITE, Announcement.Placement.BOTH)
    personal = (Audience.OWNERS, Audience.NEW7, Audience.PLAN_ENDING, Audience.QUIET)
    live = _live(placements).exclude(audience__in=personal).filter(offer_plan__isnull=True, coupon__isnull=True)
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
    items = pushed_for_dashboard(ent, user.email, user) + _config_announcements()
    upgrade = _upgrade_announcement(user, ent)
    if upgrade is not None:
        items.append(upgrade)
    return items
