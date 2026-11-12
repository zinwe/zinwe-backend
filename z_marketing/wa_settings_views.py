"""GET / PUT /api/admin/marketing/wa/settings/: Marketing -> WhatsApp -> Settings (wa_settings.py). Staff only."""
from __future__ import annotations

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from . import wa_settings
from .views import Staff


class WaSettingsView(Staff):
    """GET the settings (the defaults until staff save them) · PUT any of the fields: 400 {field: message} with
    everything to fix, else the saved settings. Live campaigns follow them from their next send."""

    def get(self, request: Request) -> Response:
        return Response(wa_settings.as_json())

    def put(self, request: Request) -> Response:
        user = request.user
        by = (getattr(user, "full_name", "") or "").strip() or user.email
        try:
            saved = wa_settings.update(request.data, by)
        except wa_settings.SettingsError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)
        self.audit(request, "wa_settings", saved, fields=sorted(k for k in request.data if isinstance(k, str))[:30])
        return Response(wa_settings.as_json(saved))

    patch = put
