"""
Plans & pricing in the admin app (2026-10-23): what each plan costs, by region and billing cycle.

- **Regions**: India (INR, GST on top) and International (USD, no GST). The customer's country picks the region.
- **Cycles**: monthly and yearly. A month is a list price and a discount (the price is the list price minus the
  discount, rounded to a whole unit); a year is its own price (empty = 12 months), shown against 12 x the
  monthly list price.

F-12: each paid plan has a price band for its monthly INR price (D1: list prices stay real, any discount is
allowed as long as the price stays in the band). A change that takes the price outside it is refused, and each
plan carries `allowed_discounts`, the whole-number discounts that keep it inside.

GET /api/admin/pricing/ lists every plan with its prices; GET/PATCH /api/admin/pricing/<code>/ one plan. Owners
already paying keep the price they signed up at (z_auth.pricing.quote_for_subscription); a change applies to new
subscriptions, upgrades and changes of cycle.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count, Q
from rest_framework import serializers

from z_auth import pricing
from z_auth.models import Plan, Subscription


def _pct(list_price: Decimal, price: Decimal) -> int:
    if list_price <= 0 or price >= list_price:
        return 0
    return int(((1 - price / list_price) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _region(plan: Plan, currency: str) -> dict[str, Any]:
    list_month, price_month = pricing.cycle_prices(plan, currency, pricing.MONTHLY)
    list_year, price_year = pricing.cycle_prices(plan, currency, pricing.YEARLY)
    yearly = plan.price_usd_yearly if currency == pricing.USD else plan.price_inr_yearly
    return {
        "currency": currency,
        "monthly": {
            "list": str(list_month),
            "price": str(price_month),
            "discount_percent": (plan.discount_percent or 0) if currency == pricing.INR else _pct(list_month, price_month),
        },
        "yearly": {
            "list": str(list_year),
            "price": str(price_year),
            # True when a yearly price is set; False = 12 x the monthly price.
            "custom": yearly is not None and yearly > 0,
            "saving_percent": pricing.yearly_saving_percent(plan, currency),
            "months_charged": str((price_year / price_month).quantize(Decimal("0.1"))) if price_month > 0 else "0",
        },
    }


def _rupees(value: Decimal) -> str:
    return f"₹{value:,.0f}"


def band_error(plan: Plan, price: Decimal) -> str:
    """ "Puts the price at ₹2,834, outside ₹2,899–₹3,000." """
    low = _rupees(plan.band_min_inr) if plan.band_min_inr is not None else "₹0"
    high = _rupees(plan.band_max_inr) if plan.band_max_inr is not None else "any price"
    return f"Puts the price at {_rupees(price)}, outside {low}–{high}."


def allowed_discounts(plan: Plan) -> list[int]:
    """The whole-number discounts (0-90) on the INR monthly list price that keep the price in the plan's band."""
    if not plan.requires_payment or not plan.list_price_inr_monthly or plan.list_price_inr_monthly <= 0:
        return [] if plan.requires_payment else [0]
    return [pct for pct in range(91) if plan.in_band(Plan.discounted(plan.list_price_inr_monthly, pct))]


def _band(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def plan_pricing(plan: Plan, subscribers: int = 0) -> dict[str, Any]:
    return {
        "code": plan.code,
        "name": plan.name,
        "tagline": plan.tagline,
        "max_devices": plan.max_devices,
        "history_days": plan.history_days,
        "chat_history_days": plan.chat_history_days,
        "is_recommended": plan.is_recommended,
        "is_free": not plan.requires_payment,
        "subscribers": subscribers,
        "regions": {"INR": _region(plan, pricing.INR), "USD": _region(plan, pricing.USD)},
        "band_min_inr": _band(plan.band_min_inr),
        "band_max_inr": _band(plan.band_max_inr),
        "allowed_discounts": allowed_discounts(plan),
    }


def pricing_overview() -> dict[str, Any]:
    paying = Q(subscriptions__status=Subscription.Status.ACTIVE, subscriptions__is_trial=False)
    plans = (
        Plan.objects.filter(is_active=True)
        .annotate(paying=Count("subscriptions", filter=paying))
        .order_by("sort_order", "code")
    )
    gstin = (settings.ZINWE_INVOICE_SELLER.get("gstin") or "").strip()
    return {
        "plans": [plan_pricing(p, p.paying) for p in plans],
        "gst_rate": settings.ZINWE_GST["rate"] if gstin else 0,
    }


class RegionPriceInput(serializers.Serializer):
    """One region's prices: the monthly list price and discount, and the yearly price (null = 12 months)."""

    list_monthly = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))
    discount_percent = serializers.IntegerField(min_value=0, max_value=90)
    yearly_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("1"), required=False, allow_null=True,
    )


class PricingUpdateSerializer(serializers.Serializer):
    """
    PATCH /api/admin/pricing/<code>/: {INR: {...}, USD: {...}, is_recommended?, band_min_inr?, band_max_inr?}, each
    part optional. Refused (400 on `discount_percent`) when the INR monthly price would leave the plan's band.
    """

    INR = RegionPriceInput(required=False)
    USD = RegionPriceInput(required=False)
    is_recommended = serializers.BooleanField(required=False)
    # v1.4: days of team chat the plan keeps (0 = no limit).
    chat_history_days = serializers.IntegerField(min_value=0, max_value=3650, required=False)
    band_min_inr = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True,
    )
    band_max_inr = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True,
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        plan: Plan = self.context["plan"]
        if not plan.requires_payment and (attrs.get("INR") or attrs.get("USD")):
            raise serializers.ValidationError("The free plan has no price.")
        for currency in ("INR", "USD"):
            region = attrs.get(currency)
            if not region:
                continue
            name = "India" if currency == "INR" else "International"
            if currency == "INR" and region["list_monthly"] <= 0:
                raise serializers.ValidationError("India: a paid plan needs a monthly price in rupees.")
            month = Plan.discounted(region["list_monthly"], region["discount_percent"])
            yearly = region.get("yearly_price")
            if yearly is not None and month > 0 and yearly > month * 12:
                raise serializers.ValidationError(f"{name}: a year can't cost more than 12 months.")
        self._check_band(plan, attrs)
        return attrs

    @staticmethod
    def _check_band(plan: Plan, attrs: dict[str, Any]) -> None:
        """F-12: the INR monthly price this change leads to must stay in the (new) band. The free plan has none."""
        if not plan.requires_payment:
            if attrs.get("band_min_inr") is not None or attrs.get("band_max_inr") is not None:
                raise serializers.ValidationError("The free plan has no price band.")
            return
        banded = Plan(
            band_min_inr=attrs["band_min_inr"] if "band_min_inr" in attrs else plan.band_min_inr,
            band_max_inr=attrs["band_max_inr"] if "band_max_inr" in attrs else plan.band_max_inr,
        )
        if banded.band_min_inr is not None and banded.band_max_inr is not None and banded.band_min_inr > banded.band_max_inr:
            raise serializers.ValidationError({"band_max_inr": ["The band's top can't be below its bottom."]})
        region = attrs.get("INR")
        price = (
            Plan.discounted(region["list_monthly"], region["discount_percent"]) if region else plan.price_inr_monthly
        )
        if not banded.in_band(price):
            raise serializers.ValidationError({"discount_percent": [band_error(banded, price)]})

    def save(self, **kwargs: Any) -> Plan:
        plan: Plan = self.context["plan"]
        data = self.validated_data
        fields: list[str] = []
        if "INR" in data:
            region = data["INR"]
            plan.list_price_inr_monthly = region["list_monthly"]
            plan.discount_percent = region["discount_percent"]
            plan.price_inr_monthly = Plan.discounted(region["list_monthly"], region["discount_percent"])
            plan.price_inr_yearly = region.get("yearly_price")
            fields += ["list_price_inr_monthly", "discount_percent", "price_inr_monthly", "price_inr_yearly"]
        if "USD" in data:
            region = data["USD"]
            plan.list_price_usd_monthly = region["list_monthly"]
            plan.price_usd_monthly = Plan.discounted(region["list_monthly"], region["discount_percent"])
            plan.price_usd_yearly = region.get("yearly_price") if region["list_monthly"] > 0 else None
            fields += ["list_price_usd_monthly", "price_usd_monthly", "price_usd_yearly"]
        for field in ("band_min_inr", "band_max_inr", "chat_history_days"):
            if field in data:
                setattr(plan, field, data[field])
                fields.append(field)
        if "is_recommended" in data:
            if data["is_recommended"]:
                Plan.objects.exclude(pk=plan.pk).update(is_recommended=False)
            plan.is_recommended = data["is_recommended"]
            fields.append("is_recommended")
        if fields:
            plan.save(update_fields=fields)
        return plan
