"""
F-21 coupon codes: checking a code for an owner, holding it on a payment, and confirming or voiding it.

Rules (owner decisions, 6 Nov 2026):
- A code takes its % off today's price (after the launch discount), for the first month of MONTHLY billing only.
  Yearly takes no code; nor does an admin offer price (an Announcement offer).
- It never takes the monthly price below the plan's price band minimum (`Plan.band_min_inr`): the discount is capped
  there. No floor when the band minimum is empty; nothing to give when the price is already at it (`lowest_price`).
- One code per company, ever: a company with a pending or confirmed use can't use another (CouponUse).
- Dates are Indian dates, both inclusive.

The server checks the code again whenever money is asked for (receipt upload, Razorpay order): `reserve` makes a
pending CouponUse, `confirm_for_payment` / `void_for_payment` / `void_for_order` settle it.
"""
from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from . import pricing
from .models import Company, Coupon, CouponUse, ManualPayment, Plan, User

State = CouponUse.State
ACTIVE_STATES = (State.PENDING, State.CONFIRMED)
CODE_RE = re.compile(r"^[A-Z0-9]{11}$")

MESSAGES = {
    "not_found": "We could not find this code. Check the letters and try again.",
    "paused": "This code is paused. Try again later.",
    "monthly_only": "Coupon codes are for monthly billing. Yearly already gives you 2 months free.",
    "used_before": "You have used a code before. One code per company.",
    "limit_reached": "This code has been used up.",
    "lowest_price": "This plan is already at its lowest price, so the code gives nothing.",
    "offer": "This is an offer price. A coupon code can't be added to it.",
}


def normalise(code: Any) -> str:
    """What the owner typed, trimmed and upper-cased (inner spaces dropped)."""
    return re.sub(r"\s+", "", str(code or "")).upper()


def day_text(day: date) -> str:
    """16 Dec"""
    return f"{day.day} {day:%b}"


def _plan_names(codes: list[str]) -> list[str]:
    names = dict(Plan.objects.filter(code__in=codes).values_list("code", "name"))
    return [names.get(c, c) for c in codes]


def _or(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


class CouponRefused(Exception):
    """The code can't be used here; `reason` is one of the contract's reasons, str(exc) the owner's sentence."""

    def __init__(self, reason: str, message: str, code: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.code = code


@dataclass
class CouponCheck:
    code: str
    ok: bool = False
    reason: str = ""
    message: str = ""
    coupon: Coupon | None = None
    capped: bool = False
    amount_off: Decimal = Decimal("0.00")
    lowest_price: Decimal | None = None
    quote: pricing.Quote | None = None  # the quote with the coupon applied (ok only)

    def error(self) -> dict[str, str] | None:
        return None if self.ok else {"reason": self.reason, "message": self.message}

    def as_dict(self) -> dict[str, Any]:
        if not self.ok:
            return {"ok": False, "code": self.code, "reason": self.reason, "message": self.message}
        return {
            "ok": True,
            "code": self.code,
            "percent_off": self.coupon.percent_off if self.coupon else 0,
            "capped": self.capped,
            "lowest_price": str(pricing.money(self.lowest_price)) if self.lowest_price is not None else None,
            "amount_off": str(self.amount_off),
            "message": self.message,
            "quote": self.quote.as_dict() if self.quote else None,
        }

    def refuse(self) -> CouponRefused:
        return CouponRefused(self.reason, self.message, self.code)


def _open_order_uses(company: Company) -> QuerySet[CouponUse]:
    """The company's pending uses held by a Razorpay checkout that is still open: a new attempt replaces them."""
    return CouponUse.objects.filter(
        company=company, state=State.PENDING, payment__isnull=True, razorpay_orders__status="created",
    )


def blocking_uses(company: Company) -> QuerySet[CouponUse]:
    """Uses that stop this company using a code now: confirmed, or pending on a payment (not an open checkout)."""
    return CouponUse.objects.filter(company=company, state__in=ACTIVE_STATES).exclude(
        pk__in=_open_order_uses(company).values("pk")
    )


def uses_count(coupon: Coupon, company: Company | None = None) -> int:
    """Pending + confirmed uses of a code (leaving out `company`'s own open checkout, which a new try replaces)."""
    rows = coupon.uses.filter(state__in=ACTIVE_STATES)
    if company is not None:
        rows = rows.exclude(pk__in=_open_order_uses(company).values("pk"))
    return rows.count()


def check(
    code: Any,
    company: Company | None,
    plan: Plan,
    base: pricing.Quote,
    offer: bool = False,
    today: date | None = None,
) -> CouponCheck:
    """
    Can `company` use `code` on `base` (the quote for `plan`, without a coupon)? Every refusal carries the
    contract's reason and the owner's sentence; success carries the discount and the quote with it applied.
    """
    today = today or timezone.localdate()
    code = normalise(code)
    result = CouponCheck(code=code)

    def no(reason: str, message: str | None = None) -> CouponCheck:
        result.reason, result.message = reason, message or MESSAGES[reason]
        return result

    if offer:
        return no("offer")
    coupon = Coupon.objects.filter(code=code).first() if CODE_RE.match(code) else None
    if coupon is None or coupon.cancelled:
        return no("not_found")
    result.coupon = coupon
    if coupon.ends_on is not None and today > coupon.ends_on:
        price = pricing.rupees(base.amount) if base.currency == pricing.INR else pricing.money_text(base.amount, base.currency)
        tax = " + GST" if base.currency == pricing.INR else ""
        return no("ended", f"This code ended on {day_text(coupon.ends_on)}. Your price is still {price}{tax}.")
    if coupon.paused:
        return no("paused")
    if today < coupon.starts_on:
        return no("not_started", f"This code starts on {day_text(coupon.starts_on)}.")
    if coupon.plan_codes and plan.code not in coupon.plan_codes:
        names = _or(_plan_names(list(coupon.plan_codes)))
        return no("other_plan", f"This code is for the {names} plan. Pick {names}, or use another code.")
    if base.billing_cycle != pricing.MONTHLY:
        return no("monthly_only")
    if company is None or blocking_uses(company).exists():
        return no("used_before")
    if coupon.max_uses is not None and uses_count(coupon, company) >= coupon.max_uses:
        return no("limit_reached")
    lowest = plan.band_min_inr if base.currency == pricing.INR else None
    result.lowest_price = lowest
    off, capped = pricing.coupon_discount(base.amount, coupon.percent_off, lowest)
    if off <= 0:
        return no("lowest_price")
    result.ok, result.capped, result.amount_off = True, capped, off
    result.quote = pricing.with_coupon(base, coupon, plan, company)
    if capped:
        result.message = (
            f"This code gives {coupon.percent_off}%, but {plan.name} has a lowest price of "
            f"{pricing.rupees(lowest)} a month. You get {pricing.rupees(off)} off."
        )
    else:
        result.message = f"{coupon.percent_off}% off your first month. You save {pricing.rupees(off)}."
    return result


def reserve(
    code: Any, user: User, company: Company | None, plan: Plan, base: pricing.Quote, offer: bool = False,
) -> tuple[CouponUse, pricing.Quote]:
    """
    At submit (receipt upload or Razorpay order), inside the payment's transaction: check the code again and hold it
    as a pending use. An open Razorpay checkout's earlier hold is replaced. Raises `CouponRefused`.
    """
    if company is not None:
        _open_order_uses(company).update(state=State.VOID, voided_at=timezone.now())
    result = check(code, company, plan, base, offer=offer)
    if not result.ok:
        raise result.refuse()
    # Lock the code's row so two companies racing for its last use can't both get it.
    coupon = Coupon.objects.select_for_update().get(pk=result.coupon.pk)
    if coupon.max_uses is not None and uses_count(coupon) >= coupon.max_uses:
        raise CouponRefused("limit_reached", MESSAGES["limit_reached"], result.code)
    due = result.quote
    use = CouponUse.objects.create(
        coupon=coupon, company=company, owner=user, plan_code=plan.code, plan_name=plan.name,
        cycle=due.billing_cycle, currency=due.currency, percent_off=coupon.percent_off, capped=result.capped,
        amount_inr=due.amount, amount_off=result.amount_off,
    )
    return use, due


def attach(use: CouponUse | None, payment: ManualPayment) -> None:
    """The pending use belongs to this payment (receipt or paid Razorpay order). A use voided meanwhile (replaced
    checkout) comes back when the company has no other code in use; the money was taken with it."""
    if use is None:
        return
    use.payment = payment
    fields = ["payment"]
    if use.state == State.VOID and not blocking_uses(use.company).exclude(pk=use.pk).exists():
        use.state, use.voided_at = State.PENDING, None
        fields += ["state", "voided_at"]
    use.save(update_fields=fields)


def confirm_for_payment(payment: ManualPayment) -> None:
    """The payment was approved: its coupon use counts."""
    CouponUse.objects.filter(payment=payment, state=State.PENDING).update(
        state=State.CONFIRMED, confirmed_at=timezone.now()
    )


def void_for_payment(payment: ManualPayment) -> None:
    """The payment was turned down: the company may use a code again."""
    CouponUse.objects.filter(payment=payment, state=State.PENDING).update(state=State.VOID, voided_at=timezone.now())


def void_for_orders(order_ids: Any) -> None:
    """Razorpay orders failed or were abandoned: their held codes are free again."""
    CouponUse.objects.filter(
        razorpay_orders__pk__in=order_ids, state=State.PENDING, payment__isnull=True
    ).update(state=State.VOID, voided_at=timezone.now())


def coupon_code(due: dict[str, Any] | None) -> str | None:
    """The code on a frozen quote (`ManualPayment.due`), or None."""
    block = (due or {}).get("coupon") or {}
    return block.get("code") or None


# --- Staff ------------------------------------------------------------------------------


SUGGEST_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL")


def suggest_code(percent: int | None = None) -> str:
    """A free 11-character code: ZW + easy-to-read letters (+ the percent at the end when given)."""
    tail = str(percent) if percent and 1 <= percent <= 40 else ""
    for _ in range(50):
        middle = "".join(random.choices(SUGGEST_ALPHABET, k=Coupon.CODE_LENGTH - 2 - len(tail)))
        code = f"ZW{middle}{tail}"
        if not Coupon.objects.filter(code=code).exists():
            return code
    raise RuntimeError("Couldn't find a free code; try again.")


def live_q(today: date | None = None) -> Q:
    today = today or timezone.localdate()
    return Q(cancelled=False, paused=False, starts_on__lte=today) & (Q(ends_on__isnull=True) | Q(ends_on__gte=today))
