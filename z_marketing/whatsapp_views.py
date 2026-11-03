"""WhatsApp outreach endpoints for staff: /api/admin/marketing/wa/... Nothing here sends a WhatsApp message."""
from __future__ import annotations

import json
from typing import Any

from django.db.models import ProtectedError
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from . import wa_media, whatsapp
from .contact_file import FileProblem
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTemplate
from .views import Staff
from .whatsapp import WaError


class WaTemplateSerializer(serializers.ModelSerializer):
    media = serializers.SerializerMethodField()

    class Meta:
        model = WaTemplate
        fields = ["id", "name", "kind", "text", "track", "day", "media", "updated_at"]
        read_only_fields = ["updated_at"]

    def get_media(self, obj: WaTemplate) -> dict[str, Any] | None:
        return wa_media.describe(obj)

    def validate_kind(self, value: str) -> str:
        if self.instance and self.instance.media and value in wa_media.COLD:
            raise serializers.ValidationError("Remove the picture or video first. " + wa_media.COLD_REFUSAL)
        return value


class WaMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = WaMessage
        fields = ["id", "kind", "text", "at", "by"]


class WaContactSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source="campaign.name", read_only=True, default="")
    last = serializers.SerializerMethodField()

    class Meta:
        model = WaContact
        fields = [
            "id", "campaign", "campaign_name", "phone", "first_name", "firm", "segment", "country", "city", "source", "info",
            "status", "step", "next_date", "needs_reply", "paused_until", "created_at", "last", "track", "yes_date",
            "nurture_day", "first_sent", "opted_in", "opt_in_note",
        ]
        read_only_fields = [
            "opted_in", "opt_in_note",
            "phone", "status", "step", "next_date", "needs_reply", "paused_until", "created_at", "yes_date", "nurture_day",
            "first_sent",
        ]

    def get_last(self, obj: WaContact) -> dict[str, str] | None:
        m = obj.messages.last()
        return {"kind": m.kind, "text": m.text[:160], "at": m.at.isoformat()} if m else None

    def validate_info(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or len(value) > 20 or not all(isinstance(v, str) and len(v) <= 500 for v in value.values()):
            raise serializers.ValidationError("Keep Useful Info to 20 short text fields.")
        return value


class WaCampaignSerializer(serializers.ModelSerializer):
    audience_name = serializers.CharField(source="audience.name", read_only=True, default="")
    stats = serializers.SerializerMethodField()

    class Meta:
        model = WaCampaign
        fields = [
            "id", "name", "audience", "audience_name", "goal", "status", "daily_limit", "followup_days", "first", "followup",
            "interested", "question", "notnow", "touch3", "touch4", "touch3_days", "touch4_days", "stats", "created_at",
        ]
        read_only_fields = ["status", "created_at"]

    def get_stats(self, obj: WaCampaign) -> dict[str, int]:
        return whatsapp.stats(obj)

    def validate_daily_limit(self, value: int) -> int:
        if not 1 <= value <= 50:
            raise serializers.ValidationError("Keep it between 1 and 50 a day.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        days = [attrs.get(k, getattr(self.instance, k, d)) for k, d in (("followup_days", 3), ("touch3_days", 30), ("touch4_days", 60))]
        if not days[0] < days[1] < days[2] <= 90:
            raise serializers.ValidationError(
                "Space the messages out: follow-up, then touch 3, then touch 4, all within 90 days of the first."
            )
        return attrs


def _contact(pk: int) -> WaContact:
    return get_object_or_404(WaContact.objects.select_related("campaign"), pk=pk)


class WaTodayView(Staff):
    def get(self, request: Request) -> Response:
        items = whatsapp.queue()
        return Response({
            "queue": [
                {"kind": i["kind"], "template": i.get("template", ""), "contact": WaContactSerializer(i["contact"]).data}
                for i in items
            ],
            "replies": [
                {**WaContactSerializer(c).data, "messages": WaMessageSerializer(c.messages.all(), many=True).data}
                for c in whatsapp.replies()
            ],
            "sent_today": whatsapp._sent_today(),
            "stats": whatsapp.stats(),
        })


class WaContactListView(Staff):
    def get(self, request: Request) -> Response:
        rows = WaContact.objects.select_related("campaign").prefetch_related("messages")
        return Response(WaContactSerializer(rows, many=True).data)


class WaContactDetailView(Staff):
    def get(self, request: Request, pk: int) -> Response:
        c = _contact(pk)
        return Response({**WaContactSerializer(c).data, "messages": WaMessageSerializer(c.messages.all(), many=True).data})

    def patch(self, request: Request, pk: int) -> Response:
        c = _contact(pk)
        data = WaContactSerializer(c, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        data.save()
        return self.get(request, pk)

    def delete(self, request: Request, pk: int) -> Response:
        _contact(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WaContactActionView(Staff):
    """draft {kind} · sent {kind, text} · reply {text} (what they wrote) · act {action}."""

    def post(self, request: Request, pk: int, action: str) -> Response:
        c = _contact(pk)
        actor = request.user.email
        try:
            if action == "draft":
                return Response(whatsapp.draft(c, str(request.data.get("kind", "first"))))
            if action == "sent":
                whatsapp.mark_sent(c, str(request.data.get("kind", "")), str(request.data.get("text", "")), actor)
            elif action == "reply":
                whatsapp.record_reply(c, str(request.data.get("text", "")), actor)
            elif action == "act":
                whatsapp.act(c, str(request.data.get("action", "")), str(request.data.get("note", "")))
            else:
                return Response({"detail": "Unknown action."}, status=status.HTTP_404_NOT_FOUND)
        except WaError as exc:
            return self.refused(exc)
        self.audit(request, f"wa_{action}", c, kind=request.data.get("kind", ""), outcome=request.data.get("action", ""))
        c.refresh_from_db()
        return Response({**WaContactSerializer(c).data, "messages": WaMessageSerializer(c.messages.all(), many=True).data})


class WaUploadView(Staff):
    """POST a file to check every row; add=1 with `choices` (JSON: row -> include, segment, campaign) adds them."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Choose a file."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            rows = whatsapp.parse_file(upload, upload.name, upload.size)
        except FileProblem as exc:
            return self.refused(exc)
        if request.data.get("add") != "1":
            return Response({"rows": rows})
        try:
            choices = json.loads(request.data.get("choices") or "{}")
        except ValueError:
            return Response({"detail": "Those choices can't be read."}, status=status.HTTP_400_BAD_REQUEST)
        added = whatsapp.add_rows(rows, choices if isinstance(choices, dict) else {}, request.user.email)
        self.audit(request, "wa_upload", None, added=added, rows=len(rows))
        return Response({"added": added})


class WaContactTemplateView(Staff):
    def get(self, request: Request) -> HttpResponse:
        response = HttpResponse(
            whatsapp.template_file(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = 'attachment; filename="zinwe-whatsapp-contacts.xlsx"'
        return response


class WaCampaignListView(Staff):
    def get(self, request: Request) -> Response:
        return Response(WaCampaignSerializer(WaCampaign.objects.select_related("audience"), many=True).data)

    def post(self, request: Request) -> Response:
        data = WaCampaignSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        campaign = data.save()
        self.audit(request, "wa_campaign_create", campaign)
        return Response(WaCampaignSerializer(campaign).data, status=status.HTTP_201_CREATED)


class WaCampaignDetailView(Staff):
    def get(self, request: Request, pk: int) -> Response:
        return Response(WaCampaignSerializer(get_object_or_404(WaCampaign, pk=pk)).data)

    def patch(self, request: Request, pk: int) -> Response:
        c = get_object_or_404(WaCampaign, pk=pk)
        data = WaCampaignSerializer(c, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        data.save()
        return Response(data.data)

    def delete(self, request: Request, pk: int) -> Response:
        c = get_object_or_404(WaCampaign, pk=pk)
        if c.status != WaCampaign.Status.DRAFT or WaMessage.objects.filter(contact__campaign=c).exists():
            return Response({"detail": "Only a draft with no messages can be deleted. Pause it instead."}, status=status.HTTP_400_BAD_REQUEST)
        c.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WaCampaignActionView(Staff):
    def post(self, request: Request, pk: int, action: str) -> Response:
        c = get_object_or_404(WaCampaign, pk=pk)
        if action not in ("start", "pause"):
            return Response({"detail": "Unknown action."}, status=status.HTTP_404_NOT_FOUND)
        if action == "start" and not c.contacts.exclude(status=WaContact.Status.STOP).exists():
            return Response({"detail": "Add contacts to this campaign first."}, status=status.HTTP_400_BAD_REQUEST)
        c.status = WaCampaign.Status.LIVE if action == "start" else WaCampaign.Status.PAUSED
        c.save(update_fields=["status"])
        self.audit(request, f"wa_campaign_{action}", c)
        return Response(WaCampaignSerializer(c).data)


class WaTemplateListView(Staff):
    def get(self, request: Request) -> Response:
        return Response(WaTemplateSerializer(WaTemplate.objects.all(), many=True).data)

    def post(self, request: Request) -> Response:
        data = WaTemplateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(WaTemplateSerializer(data.save()).data, status=status.HTTP_201_CREATED)


class WaTemplateDetailView(Staff):
    def put(self, request: Request, pk: int) -> Response:
        t = get_object_or_404(WaTemplate, pk=pk)
        data = WaTemplateSerializer(t, data=request.data)
        data.is_valid(raise_exception=True)
        return Response(WaTemplateSerializer(data.save()).data)

    def delete(self, request: Request, pk: int) -> Response:
        template = get_object_or_404(WaTemplate, pk=pk)
        media = template.media.name if template.media else ""
        try:
            template.delete()
            if media:
                template.media.storage.delete(media)
        except ProtectedError:
            return Response({"detail": "A campaign starts with this template. Pick another first message there first."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


class WaTemplateMediaView(Staff):
    """GET the template's picture or video (staff only; MEDIA_ROOT is never public), POST a new one, DELETE it."""

    parser_classes = [MultiPartParser]

    def get(self, request: Request, pk: int) -> FileResponse:
        template = get_object_or_404(WaTemplate, pk=pk)
        if not wa_media.describe(template):
            return Response(status=status.HTTP_404_NOT_FOUND)
        return FileResponse(template.media.open("rb"), content_type=wa_media.content_type(template), filename=template.media_name)

    def post(self, request: Request, pk: int) -> Response:
        template = get_object_or_404(WaTemplate, pk=pk)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Choose a picture or video first."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            wa_media.attach(template, upload)
        except wa_media.MediaProblem as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        self.audit(request, "wa_template_media", template, kind=template.media_kind)
        return Response(WaTemplateSerializer(template).data)

    def delete(self, request: Request, pk: int) -> Response:
        template = get_object_or_404(WaTemplate, pk=pk)
        wa_media.remove(template)
        return Response(WaTemplateSerializer(template).data)


class WaAnalyticsView(Staff):
    def get(self, request: Request) -> Response:
        return Response({
            "total": whatsapp.stats(),
            "daily": whatsapp.daily(),
            "segments": whatsapp.by_segment(),
            "campaigns": [{"id": c.id, "name": c.name, "status": c.status, **whatsapp.stats(c)} for c in WaCampaign.objects.all()],
            "blocked": WaBlock.objects.count(),
        })
