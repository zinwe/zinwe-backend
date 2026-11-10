"""
The 100 Reviews campaign's starting rows (spec of 10 Nov 2026), used by `manage.py seed_100_reviews`.

- templates(): the 86 template rows as drafts (no copy yet; copy is written in a separate pass): Outreach 30,
  Engage 18 (6 per Track), Demo video 18 (6 per Track), Setup 2, Demo call 1, Offer & discount 8 (one per code),
  Testimonial 2, Quick reply 7. A draft is never sent.
- coupons(): the campaign's 8 coupon codes, linked to their Offer rows.
- sample(): a handful of rows with test copy and one test code, for dev and local only.

Real rows go to production only (owner, 10 Nov 2026). Every function can run again: what exists is kept.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from django.db import transaction

from . import wa_flow
from .models import WaTemplate

Cat = WaTemplate.Category
Track = WaTemplate.Track
TRACKS = [Track.PRIVACY, Track.ACTIVITY, Track.BOTH]
CAMPAIGN = "100 Reviews"

# The 30 Outreach messages: 10 per 30-day stage, every third day (days 1, 4, ... 28 in stage 1).
OUTREACH_TYPES = ["Intro", "Problem", "Value", "Question", "Problem", "Value", "Season or event", "Question", "Offer", "Discount"]
ENGAGE_TYPES = ["Need discovery", "Need discovery", "Qualification question", "Qualification question", "Track confirmation", "Track confirmation"]
DEMO_TYPES = ["Overview video", "Feature clip", "Feature clip", "Feature clip", "Feature clip", "Recap and next step"]
QUICK_TYPES = ["STOP / band karo", "Price question", "Is this spyware?", "Not now", "We already use something", "Send it on email", "Where did you get my number?"]

# code, pack (plan codes), % off, first day, last day, stage, festival code
COUPONS: list[tuple[str, str, list[str], int, date, date, int, bool]] = [
    ("ZWSTARTUP15", "Startup pack", ["startups"], 15, date(2026, 11, 16), date(2026, 12, 15), 1, False),
    ("ZWTEAMWIN20", "Team pack", ["teams"], 20, date(2026, 11, 16), date(2026, 12, 15), 1, False),
    ("ZWSTARTUP20", "Startup pack", ["startups"], 20, date(2026, 12, 16), date(2027, 1, 14), 2, False),
    ("ZWTEAMWIN25", "Team pack", ["teams"], 25, date(2026, 12, 16), date(2027, 1, 14), 2, False),
    ("ZWNEWYEAR30", "Any pack (event)", [], 30, date(2026, 12, 26), date(2027, 1, 5), 2, True),
    ("ZWSTARTUP25", "Startup pack", ["startups"], 25, date(2027, 1, 15), date(2027, 2, 12), 3, False),
    ("ZWTEAMWIN30", "Team pack", ["teams"], 30, date(2027, 1, 15), date(2027, 2, 12), 3, False),
    ("ZWTIRANGA40", "Team pack (event)", ["teams"], 40, date(2027, 1, 24), date(2027, 1, 28), 3, True),
]


def rows() -> list[dict[str, Any]]:
    """The 86 rows: category, type, track, stage, step (and the code for Offer rows)."""
    out: list[dict[str, Any]] = []
    for stage in (1, 2, 3):
        for i, t in enumerate(OUTREACH_TYPES):
            out.append({"category": Cat.OUTREACH, "type_name": t, "track": "", "stage": stage, "step": (stage - 1) * 30 + 1 + i * 3})
    for track in TRACKS:
        out += [{"category": Cat.ENGAGE, "type_name": t, "track": track, "stage": 0, "step": i + 1} for i, t in enumerate(ENGAGE_TYPES)]
    for track in TRACKS:
        out += [{"category": Cat.DEMO_VIDEO, "type_name": t, "track": track, "stage": 0, "step": i + 1} for i, t in enumerate(DEMO_TYPES)]
    out.append({"category": Cat.SETUP, "type_name": "Registration steps", "track": "", "stage": 0, "step": 1})
    out.append({"category": Cat.SETUP, "type_name": "Incomplete-setup nudge", "track": "", "stage": 0, "step": 2})
    out.append({"category": Cat.DEMO_CALL, "type_name": "Booking link", "track": "", "stage": 0, "step": 1})
    for code, pack, _plans, _pct, _start, _end, stage, event in COUPONS:
        out.append({"category": Cat.OFFER, "type_name": pack, "track": "", "stage": stage, "step": 0, "code": code, "event": event})
    out.append({"category": Cat.TESTIMONIAL, "type_name": "Written request", "track": "", "stage": 0, "step": 1})
    out.append({"category": Cat.TESTIMONIAL, "type_name": "Video request", "track": "", "stage": 0, "step": 2})
    out += [{"category": Cat.QUICK, "type_name": t, "track": "", "stage": 0, "step": 0} for t in QUICK_TYPES]
    return out


def _key(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("category", "type_name", "track", "stage", "step")}


@transaction.atomic
def templates() -> dict[str, int]:
    made = kept = 0
    for r in rows():
        if WaTemplate.objects.filter(**_key(r)).exists():
            kept += 1
            continue
        name = wa_flow.template_name(r["category"], r["type_name"], r["track"], r["stage"], r["step"])
        if r["category"] == Cat.OFFER:
            name = f"Offer & discount · {r['code']}"
        WaTemplate.objects.create(
            name=name, kind=wa_flow.kind_for(r["category"]), text="", text_hi="", event=r.get("event", False), **_key(r),
        )
        made += 1
    return {"made": made, "kept": kept}


@transaction.atomic
def coupons() -> dict[str, int]:
    from z_auth.models import Coupon

    made = linked = 0
    for code, pack, plans, pct, start, end, stage, event in COUPONS:
        coupon, new = Coupon.objects.get_or_create(code=code, defaults={
            "percent_off": pct, "plan_codes": plans, "starts_on": start, "ends_on": end,
            "channel": Coupon.Channel.WHATSAPP, "campaign": CAMPAIGN,
        })
        made += int(new)
        linked += WaTemplate.objects.filter(category=Cat.OFFER, type_name=pack, stage=stage, coupon__isnull=True).update(coupon=coupon, event=event)
    return {"made": made, "linked": linked}


SAMPLE_TEXT = {
    (Cat.OUTREACH, 1): ("Hi {{FirstName}}, test message from Zinwe (day 1). Reply STOP to opt out.", "Namaste {{FirstName}}, Zinwe ka test message (day 1). Reply STOP to opt out."),
    (Cat.OUTREACH, 4): ("Hi {{FirstName}}, test message from Zinwe (day 4). Reply STOP to opt out.", "Namaste {{FirstName}}, Zinwe ka test message (day 4). Reply STOP to opt out."),
    (Cat.ENGAGE, 1): ("Test Engage question 1 for {{Business}}. Reply STOP to opt out.", "{{Business}} ke liye test sawaal 1. Reply STOP to opt out."),
    (Cat.ENGAGE, 2): ("Test Engage question 2 for {{Business}}. Reply STOP to opt out.", "{{Business}} ke liye test sawaal 2. Reply STOP to opt out."),
    (Cat.DEMO_VIDEO, 1): ("Test demo video 1. Reply STOP to opt out.", "Test demo video 1. Reply STOP to opt out."),
    (Cat.SETUP, 1): ("Test setup steps. Reply STOP to opt out.", "Test setup steps. Reply STOP to opt out."),
    (Cat.TESTIMONIAL, 1): ("Test: two lines on how Zinwe went? Reply STOP to opt out.", "Test: do line likh denge? Reply STOP to opt out."),
}
SAMPLE_COUPON = "ZWTESTCODE1"


@transaction.atomic
def sample() -> dict[str, int]:
    """Dev and local only: the 86 drafts, test copy on a few of them, and one test code on the stage-1 Startup row."""
    from z_auth.models import Coupon

    templates()
    filled = 0
    for (category, step), (en, hi) in SAMPLE_TEXT.items():
        filled += WaTemplate.objects.filter(category=category, step=step, text="").update(text=en, text_hi=hi)
    WaTemplate.objects.filter(category=Cat.QUICK, type_name=QUICK_TYPES[0], text="").update(
        text="Done, {{FirstName}}. No more messages from Zinwe.", text_hi="Theek hai, {{FirstName}}. Zinwe se aur message nahi aayega.")
    coupon, _ = Coupon.objects.get_or_create(code=SAMPLE_COUPON, defaults={
        "percent_off": 15, "plan_codes": ["startups"], "starts_on": date(2026, 11, 1), "ends_on": date(2027, 2, 12),
        "channel": Coupon.Channel.WHATSAPP, "campaign": f"{CAMPAIGN} (test)",
    })
    offer = WaTemplate.objects.filter(category=Cat.OFFER, stage=1, type_name="Startup pack").first()
    if offer and not offer.text:
        offer.coupon = offer.coupon or coupon
        offer.text = f"Test offer: {coupon.code} takes {coupon.percent_off}% off the first month. Reply STOP to opt out."
        offer.text_hi = f"Test offer: {coupon.code} se pehle mahine {coupon.percent_off}% off. Reply STOP to opt out."
        offer.save()
        filled += 1
    return {"filled": filled}
