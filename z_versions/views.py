"""Staff side of the New version tab, mounted at /api/admin/versions/."""
from __future__ import annotations

from typing import Any

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import VersionPlan
from .pdf import plan_filename, plan_pdf

LIST_LIMIT = 60


def plan_data(plan: VersionPlan) -> dict[str, Any]:
    return {
        "id": plan.pk,
        "kind": plan.kind,
        "status": plan.status,
        "status_label": plan.get_status_display(),
        "period": services.period_label(plan),
        "period_start": plan.period_start,
        "period_end": plan.period_end,
        "version": plan.version,
        "headline": plan.headline,
        "summary": plan.summary,
        "items": plan.items,
        "counts": plan.counts,
        "drafted_by": plan.drafted_by,
        "error": plan.error,
        "emailed_to": plan.emailed_to,
        "email_status": plan.email_status,
        "emailed_at": plan.emailed_at,
        "decided_by": plan.decided_by.email if plan.decided_by else "",
        "decided_at": plan.decided_at,
        "created_at": plan.created_at,
    }


class PlansView(APIView):
    """GET /api/admin/versions/ -- plans, newest first. POST -- make a plan now from the last 7 days (takes a minute)."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request) -> Response:
        plans = VersionPlan.objects.select_related("decided_by")[:LIST_LIMIT]
        return Response({"rows": [plan_data(p) for p in plans], "next_run": services.next_run_at()})

    def post(self, request: Request) -> Response:
        if VersionPlan.objects.filter(status=VersionPlan.Status.DRAFTING).exists():
            return Response({"detail": "A plan is being drafted right now. Try again in a minute."}, status=status.HTTP_409_CONFLICT)
        return Response(plan_data(services.make_plan_now()), status=status.HTTP_201_CREATED)


class PlanView(APIView):
    """PATCH /api/admin/versions/<id>/ -- {status: approved|set_aside|ready}."""

    permission_classes = [IsAdminUser]

    def patch(self, request: Request, pk: int) -> Response:
        plan = get_object_or_404(VersionPlan.objects.select_related("decided_by"), pk=pk)
        try:
            plan = services.decide(plan, str(request.data.get("status", "")), request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(plan_data(plan))


class PlanPdfView(APIView):
    """GET /api/admin/versions/<id>/pdf/ -- the plan as a PDF."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request, pk: int) -> HttpResponse:
        plan = get_object_or_404(VersionPlan, pk=pk)
        response = HttpResponse(plan_pdf(plan), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{plan_filename(plan)}"'
        return response
