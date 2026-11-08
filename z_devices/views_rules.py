"""v1.4 Rules: work hours by scope, holidays, leave, and per-department / per-PC protection rules. Thin HTTP layer."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.http import Http404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from z_auth import entitlements as plan

from . import policy_scopes, services, work_rules
from .authentication import DeviceScopedThrottle, DeviceTokenAuthentication, IsOwner
from .models import Holiday, Leave
from .serializers import PolicySerializer, PolicyUpdateSerializer
from .views import IsOwnerOrDevice, _is_device


def _scope(request: Request) -> tuple[str, str]:
    try:
        return work_rules.parse_scope(request.user, request.query_params.get("scope"))
    except work_rules.BadScope as exc:
        raise serializers.ValidationError({"scope": [str(exc)]}) from None


def _refused(exc: work_rules.RulesRefused) -> Response:
    return Response({exc.field: [exc.message]}, status=status.HTTP_400_BAD_REQUEST)


class RulesHoursView(APIView):
    """
    GET /api/devices/rules/hours/?scope=company|department:<role>|device:<id> -> {scope, own, inherits_from,
    effective, fields}. PUT (fields; null = inherit) -> same. DELETE -> reset to inherited (company: the defaults).
    """

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        plan.require(request.user, plan.WORK_TIME)
        return Response(work_rules.hours_payload(request.user, *_scope(request)))

    def put(self, request: Request) -> Response:
        plan.require(request.user, plan.WORK_TIME)
        kind, key = _scope(request)
        if not isinstance(request.data, dict):
            return Response({"detail": "Send the rules as an object."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(work_rules.save_hours(request.user, kind, key, dict(request.data)))
        except work_rules.RulesRefused as exc:
            return _refused(exc)

    patch = put

    def delete(self, request: Request) -> Response:
        plan.require(request.user, plan.WORK_TIME)
        return Response(work_rules.reset_hours(request.user, *_scope(request)))


class RulesScopesView(APIView):
    """GET /api/devices/rules/scopes/ -> {departments: [...], devices: [...]} with own_hours / own_policy flags."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        return Response(work_rules.scopes_overview(request.user))


class HolidayInSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80, trim_whitespace=True)
    date = serializers.DateField()


def _holiday_json(h: Holiday) -> dict[str, Any]:
    return {"id": h.pk, "name": h.name, "date": h.date.isoformat()}


class HolidaysView(APIView):
    """GET /api/devices/rules/holidays/ -> [{id, name, date}]; POST {name, date} -> 201 holiday."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        return Response([_holiday_json(h) for h in Holiday.objects.filter(owner=request.user)])

    def post(self, request: Request) -> Response:
        plan.require(request.user, plan.WORK_TIME)
        data = HolidayInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            holiday = work_rules.add_holiday(request.user, data.validated_data["name"], data.validated_data["date"])
        except work_rules.RulesRefused as exc:
            return _refused(exc)
        return Response(_holiday_json(holiday), status=status.HTTP_201_CREATED)

    def delete(self, request: Request) -> Response:
        raw = request.query_params.get("id", "")
        if not raw.isdigit():
            return Response({"id": ["Which holiday?"]}, status=status.HTTP_400_BAD_REQUEST)
        return HolidayDetailView().delete(request, int(raw))


class HolidayDetailView(APIView):
    """DELETE /api/devices/rules/holidays/<id>/ -> 204."""

    permission_classes = [IsOwner]

    def delete(self, request: Request, holiday_id: int) -> Response:
        deleted, _ = Holiday.objects.filter(owner=request.user, pk=holiday_id).delete()
        if not deleted:
            raise Http404
        return Response(status=status.HTTP_204_NO_CONTENT)


class LeaveInSerializer(serializers.Serializer):
    date = serializers.DateField()
    half = serializers.BooleanField(required=False, default=False)
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class LeaveView(APIView):
    """
    GET /api/devices/leave/ -- owner: ?status=requested|approved|declined|all (default requested) -> [Leave];
    device: the member's own (last 90 days and ahead).
    POST /api/devices/leave/ (device) -- {date, half?, note?} -> Leave (201).
    """

    authentication_classes = [DeviceTokenAuthentication, *APIView.authentication_classes]
    permission_classes = [IsOwnerOrDevice]
    throttle_scope = "device_tasks"

    def get_throttles(self):  # type: ignore[no-untyped-def]
        return [DeviceScopedThrottle()] if _is_device(self.request) else []

    def get(self, request: Request) -> Response:
        if _is_device(request):
            member_id = request.auth.member_id
            if member_id is None:
                return Response([])
            since = timezone.localdate() - timedelta(days=90)
            rows = Leave.objects.filter(member_id=member_id, date__gte=since).select_related("member")
            return Response([work_rules.leave_json(lv) for lv in rows])
        wanted = request.query_params.get("status", "requested")
        rows = Leave.objects.filter(owner=request.user).select_related("member")
        if wanted != "all":
            if wanted not in Leave.Status.values:
                return Response({"status": ["requested, approved, declined or all."]}, status=status.HTTP_400_BAD_REQUEST)
            rows = rows.filter(status=wanted)
        if wanted == "requested":
            rows = rows.order_by("date", "id")
        return Response([work_rules.leave_json(lv) for lv in rows[:500]])

    def post(self, request: Request) -> Response:
        if not _is_device(request):
            return Response({"detail": "A member asks for leave from their Zinwe app."}, status=status.HTTP_403_FORBIDDEN)
        member = request.auth.member
        if member is None:
            return Response({"detail": "This PC has no member."}, status=status.HTTP_400_BAD_REQUEST)
        data = LeaveInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        if data.validated_data["date"] < timezone.localdate() - timedelta(days=30):
            return Response({"date": ["Pick a day in the last month or ahead."]}, status=status.HTTP_400_BAD_REQUEST)
        try:
            leave = work_rules.ask_leave(member, data.validated_data["date"], data.validated_data["half"],
                                         data.validated_data["note"])
        except work_rules.RulesRefused as exc:
            return _refused(exc)
        return Response(work_rules.leave_json(leave), status=status.HTTP_201_CREATED)


class LeaveDecisionView(APIView):
    """POST /api/devices/leave/<id>/decision/ -- {decision: approve|decline} -> Leave."""

    permission_classes = [IsOwner]

    def post(self, request: Request, leave_id: int) -> Response:
        leave = Leave.objects.filter(owner=request.user, pk=leave_id).select_related("member").first()
        if leave is None:
            raise Http404
        decision = request.data.get("decision") or request.data.get("action") or ""
        try:
            leave = work_rules.decide_leave(leave, decision)
        except work_rules.RulesRefused as exc:
            return _refused(exc)
        return Response(work_rules.leave_json(leave))


def policy_scope_response(request: Request, kind: str, key: str, policy: Any = None) -> Response:
    policy = policy if policy is not None else policy_scopes.effective_policy(request.user, kind, key)
    return Response({**PolicySerializer(policy).data, **policy_scopes.scope_extra(request.user, kind, key)})


def policy_get(request: Request) -> Response:
    """GET policy/?scope=...: the effective policy at that scope."""
    kind, key = _scope(request)
    return policy_scope_response(request, kind, key)


def policy_patch(request: Request) -> Response:
    """PATCH policy/?scope=...: company = the old PUT; a department / PC keeps only what differs."""
    kind, key = _scope(request)
    serializer = PolicyUpdateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if kind == "company":
        policy = services.update_policy(request.user, serializer.validated_data)
    else:
        policy = policy_scopes.update_scope(request.user, kind, key, serializer.validated_data)
    return policy_scope_response(request, kind, key, policy)


def policy_delete(request: Request) -> Response:
    kind, key = _scope(request)
    if kind == "company":
        return Response({"scope": ["The company rules can't be reset here."]}, status=status.HTTP_400_BAD_REQUEST)
    return policy_scope_response(request, kind, key, policy_scopes.reset_scope(request.user, kind, key))

