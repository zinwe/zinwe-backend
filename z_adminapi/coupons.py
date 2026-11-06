"""
F-21 Coupons in the admin app (Marketing -> Coupons): list with plans and totals, make, change, pause, cancel,
see who used a code, and suggest a free one. Staff only; every write is audited. Rules in z_auth.coupons.

GET   /api/admin/coupons/              {coupons: [Coupon], plans: [...], totals: {live, uses, paid_inr, best}}
POST  /api/admin/coupons/              make one
PATCH /api/admin/coupons/<id>/         change (the code only while unused), pause / unpause, cancel
DELETE /api/admin/coupons/<id>/        only while unused (409 after: cancel it instead)
GET   /api/admin/coupons/<id>/uses/    {uses: [...]}
GET   /api/admin/coupons/suggest/      {code}  [?percent=25 puts the percent at the end]
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response

from z_auth import coupons as rules
from z_auth.models import Coupon, CouponUse, Plan

from .views import StaffView, _audit

ACTIVE = Q(uses__state__in=rules.ACTIVE_STATES)
CONFIRMED = Q(uses__state=CouponUse.State.CONFIRMED)


def _with_numbers(rows: Any) -> Any:
    return rows.annotate(
        n_uses=Count("uses", filter=ACTIVE, distinct=True),
        n_paid=Sum("uses__amount_inr", filter=CONFIRMED),
    )


def _money(value: Any) -> str:
    return str((Decimal(value or 0)).quantize(Decimal("0.01")))


class CouponSerializer(serializers.ModelSerializer):
    code = serializers.CharField(max_length=40)
    percent_off = serializers.IntegerField(error_messages={
        "invalid": "Give a whole number from 1 to 40.", "required": "Give a whole number from 1 to 40.",
    })
    plan_codes = serializers.JSONField(required=False, default=list)
    starts_on = serializers.DateField(required=False, error_messages={"invalid": "Enter the date as YYYY-MM-DD."})
    ends_on = serializers.DateField(
        required=False, allow_null=True, error_messages={"invalid": "Enter the date as YYYY-MM-DD."}
    )
    max_uses = serializers.IntegerField(required=False, allow_null=True, min_value=1, error_messages={
        "min_value": "At least 1, or leave it empty for no limit.", "invalid": "A whole number, or leave it empty.",
    })
    campaign = serializers.CharField(max_length=120, required=False, allow_blank=True)
    status = serializers.SerializerMethodField()
    uses = serializers.SerializerMethodField()
    paid_inr = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = Coupon
        fields = (
            "id", "code", "percent_off", "plan_codes", "starts_on", "ends_on", "max_uses", "channel", "campaign",
            "paused", "cancelled", "status", "uses", "paid_inr", "created_at", "created_by",
        )
        read_only_fields = ("id", "status", "uses", "paid_inr", "created_at", "created_by")

    def get_status(self, obj: Coupon) -> str:
        return obj.status()

    def get_uses(self, obj: Coupon) -> int:
        annotated = getattr(obj, "n_uses", None)
        return int(annotated) if annotated is not None else rules.uses_count(obj)

    def get_paid_inr(self, obj: Coupon) -> str:
        if hasattr(obj, "n_paid"):
            return _money(obj.n_paid)
        return _money(obj.uses.filter(state=CouponUse.State.CONFIRMED).aggregate(s=Sum("amount_inr"))["s"])

    def get_created_by(self, obj: Coupon) -> str:
        return obj.created_by.email if obj.created_by_id else ""

    def validate_code(self, value: str) -> str:
        code = rules.normalise(value)
        if not rules.CODE_RE.match(code):
            raise serializers.ValidationError("Use exactly 11 letters or digits (A to Z, 0 to 9).")
        if self.instance is not None and code != self.instance.code and self.instance.uses.exists():
            raise serializers.ValidationError("This code has been used, so it can't change.")
        taken = Coupon.objects.filter(code=code)
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise serializers.ValidationError("This code is taken. Pick another one.")
        return code

    def validate_percent_off(self, value: int) -> int:
        if not Coupon.PERCENT_MIN <= value <= Coupon.PERCENT_MAX:
            raise serializers.ValidationError("Give a whole number from 1 to 40.")
        return value

    def validate_plan_codes(self, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, list) or not all(isinstance(code, str) for code in value):
            raise serializers.ValidationError("A list of plan codes.")
        codes = list(dict.fromkeys(c.strip() for c in value if c.strip()))
        known = set(Plan.objects.values_list("code", flat=True))
        unknown = [code for code in codes if code not in known]
        if unknown:
            raise serializers.ValidationError(f"Unknown plan: {', '.join(unknown)}.")
        return codes

    def validate_campaign(self, value: str) -> str:
        return value.strip()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        starts = attrs.get("starts_on", self.instance.starts_on if self.instance else timezone.localdate())
        ends = attrs.get("ends_on", self.instance.ends_on if self.instance else None)
        if ends is not None and starts is not None and ends < starts:
            raise serializers.ValidationError({"ends_on": ["The end date can't be before the start date."]})
        return attrs


def _plans() -> list[dict[str, Any]]:
    plans = Plan.objects.filter(is_active=True, price_inr_monthly__gt=0).order_by("sort_order", "code")
    return [
        {
            "code": p.code, "name": p.name, "price_inr_monthly": str(p.price_inr_monthly),
            "band_min_inr": str(p.band_min_inr) if p.band_min_inr is not None else None, "pcs": p.max_devices,
        }
        for p in plans
    ]


def _totals(rows: list[Coupon]) -> dict[str, Any]:
    today = timezone.localdate()
    paid = CouponUse.objects.filter(state=CouponUse.State.CONFIRMED).aggregate(s=Sum("amount_inr"))["s"]
    best = max(rows, key=lambda c: (c.n_uses or 0, c.created_at), default=None)
    return {
        "live": sum(1 for c in rows if c.status(today) == "live"),
        "uses": CouponUse.objects.filter(state__in=rules.ACTIVE_STATES).count(),
        "paid_inr": _money(paid),
        "best": (
            {"code": best.code, "uses": int(best.n_uses), "channel": best.channel, "campaign": best.campaign}
            if best is not None and best.n_uses else None
        ),
    }


class CouponListView(StaffView):
    def get(self, request: Request) -> Response:
        rows = list(_with_numbers(Coupon.objects.select_related("created_by")).order_by("-created_at", "-id"))
        return Response({
            "coupons": CouponSerializer(rows, many=True).data, "plans": _plans(), "totals": _totals(rows),
        })

    def post(self, request: Request) -> Response:
        data = CouponSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        coupon = data.save(created_by=request.user)
        _audit(request, "coupon_created", {
            "id": coupon.pk, "code": coupon.code, "percent_off": coupon.percent_off, "plans": coupon.plan_codes,
        }, coupon)
        return Response(CouponSerializer(coupon).data, status=status.HTTP_201_CREATED)


class CouponDetailView(StaffView):
    def patch(self, request: Request, pk: int) -> Response:
        coupon = get_object_or_404(Coupon, pk=pk)
        data = CouponSerializer(coupon, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        changed = sorted(k for k, v in data.validated_data.items() if getattr(coupon, k) != v)
        coupon = data.save()
        if changed:
            _audit(request, "coupon_updated", {"id": pk, "code": coupon.code, "fields": changed}, coupon)
        return Response(CouponSerializer(coupon).data)

    def delete(self, request: Request, pk: int) -> Response:
        coupon = get_object_or_404(Coupon, pk=pk)
        if coupon.uses.exists():
            return Response(
                {"detail": "This code has been used, so it can't be deleted. Cancel it instead."},
                status=status.HTTP_409_CONFLICT,
            )
        code = coupon.code
        coupon.delete()
        _audit(request, "coupon_deleted", {"id": pk, "code": code})
        return Response(status=status.HTTP_204_NO_CONTENT)


class CouponUsesView(StaffView):
    def get(self, request: Request, pk: int) -> Response:
        coupon = get_object_or_404(Coupon, pk=pk)
        rows = coupon.uses.select_related("company", "owner").order_by("-created_at", "-id")
        return Response({"uses": [
            {
                "company": use.company.name, "owner_email": use.owner.email, "plan_code": use.plan_code,
                "plan_name": use.plan_name, "cycle": use.cycle, "state": use.state,
                "amount_inr": str(use.amount_inr), "amount_off": str(use.amount_off), "created_at": use.created_at,
                "subscriber_id": use.owner_id,
            }
            for use in rows
        ]})


class CouponSuggestView(StaffView):
    def get(self, request: Request) -> Response:
        try:
            percent = int(request.query_params.get("percent") or 0)
        except ValueError:
            percent = 0
        return Response({"code": rules.suggest_code(percent or None)})
