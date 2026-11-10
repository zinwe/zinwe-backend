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

from . import wa_conditions, wa_flow, wa_media, wa_structure, wa_templates, whatsapp
from .contact_file import FileProblem
from .models import WaBlock, WaCampaign, WaContact, WaMessage, WaTag, WaTemplate
from .views import Staff
from .whatsapp import WaError, WaFieldError


def _conditions_ok(value: str) -> str:
    """{{#if X}}...{{else}}...{{/if}} must be balanced, unnested and test a known detail (wa_conditions.py)."""
    problem = wa_conditions.problem(value)
    if problem:
        raise serializers.ValidationError(problem)
    return value


class WaTagSerializer(serializers.ModelSerializer):
    """A tag: name unique whatever the case; color from WaTag.COLORS (the next one in turn when left out)."""

    name = serializers.CharField(max_length=60)
    color = serializers.ChoiceField(choices=WaTag.COLORS, required=False)
    templates = serializers.SerializerMethodField()

    class Meta:
        model = WaTag
        fields = ["id", "name", "color", "templates"]

    def get_templates(self, obj: WaTag) -> int:
        """How many templates carry it (not counting version B rows)."""
        return obj.templates.filter(version_of__isnull=True).count()

    def validate_name(self, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Name the tag.")
        same = WaTag.objects.filter(name__iexact=value)
        if self.instance is not None:
            same = same.exclude(pk=self.instance.pk)
        if same.exists():
            raise serializers.ValidationError(f"There is already a tag called {same.first().name}.")
        return value

    def create(self, validated: dict[str, Any]) -> WaTag:
        validated.setdefault("color", WaTag.COLORS[WaTag.objects.count() % len(WaTag.COLORS)])
        return super().create(validated)


class WaTemplateSerializer(serializers.ModelSerializer):
    """
    `parts` set = the outreach structure (wa_structure.py) and `text` is composed from it; empty = free text.
    `category` set = a 90-day campaign row (wa_flow.py): English in `text`, Hinglish in `text_hi`, a draft until both
    are written. Only Engage and Demo video carry a Track; the kind follows the category.
    """

    media = serializers.SerializerMethodField()
    text = serializers.CharField(max_length=1000, required=False, allow_blank=True, trim_whitespace=False)
    text_hi = serializers.CharField(max_length=1000, required=False, allow_blank=True, trim_whitespace=False)
    name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    draft = serializers.SerializerMethodField()
    coupon_info = serializers.SerializerMethodField()
    performance = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    tag_ids = serializers.PrimaryKeyRelatedField(
        queryset=WaTag.objects.all(), many=True, write_only=True, required=False, source="tags",
        error_messages={"does_not_exist": "That tag no longer exists.", "incorrect_type": "Pick a tag from the list."},
    )
    version_b = serializers.SerializerMethodField()

    class Meta:
        model = WaTemplate
        fields = [
            "id", "name", "kind", "language", "twin", "text", "text_hi", "parts", "track", "day", "media", "updated_at",
            "category", "type_name", "stage", "step", "coupon", "coupon_info", "event", "archived", "draft", "performance",
            "tags", "tag_ids", "version_of", "version_b",
        ]
        read_only_fields = ["updated_at", "version_of"]

    validate_text = staticmethod(_conditions_ok)
    validate_text_hi = staticmethod(_conditions_ok)

    def _stats(self, pk: int) -> dict[str, int]:
        """The last 30 days' numbers (wa_templates.template_stats): the list passes them all in its context; a single
        template works out its own."""
        stats = self.context.get("stats")
        if stats is None:
            stats = wa_templates.template_stats(ids=[pk])
        return stats.get(pk) or wa_templates.blank_stats()

    def _b(self, obj: WaTemplate) -> WaTemplate | None:
        if obj.version_of_id:
            return None
        versions = self.context.get("versions")
        return versions.get(obj.pk) if versions is not None else wa_flow.active_b(obj)

    def get_tags(self, obj: WaTemplate) -> list[dict[str, Any]]:
        return [{"id": t.id, "name": t.name, "color": t.color} for t in obj.tags.all()]

    def get_version_b(self, obj: WaTemplate) -> dict[str, Any] | None:
        b = self._b(obj)
        if b is None:
            return None
        return {"id": b.pk, "name": b.name, "text": b.text, "text_hi": b.text_hi, "draft": wa_flow.is_draft(b),
                "updated_at": b.updated_at, **self._stats(b.pk)}

    def to_representation(self, obj: WaTemplate) -> dict[str, Any]:
        """A's own numbers sit at the top level, with the same keys as version_b's."""
        data = super().to_representation(obj)
        data.update(self._stats(obj.pk))
        data["stats_days"] = wa_templates.WINDOW_DAYS
        return data

    def get_draft(self, obj: WaTemplate) -> bool:
        return wa_flow.is_draft(obj)

    def get_coupon_info(self, obj: WaTemplate) -> dict[str, Any] | None:
        c = obj.coupon
        if c is None:
            return None
        return {"code": c.code, "percent_off": c.percent_off, "plan_codes": c.plan_codes, "starts_on": c.starts_on,
                "ends_on": c.ends_on, "status": c.status()}

    def get_performance(self, obj: WaTemplate) -> dict[str, int] | None:
        perf = self.context.get("performance")
        if perf is None:
            return None
        return perf.get(obj.pk, {"sends": 0, "replies": 0, "next": 0})

    def _category_rules(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """A campaign row: the Track only where it belongs, the day inside 90, a code that hasn't ended, a name."""
        Cat = WaTemplate.Category
        get = lambda k, d=None: attrs.get(k, getattr(self.instance, k, d) if self.instance else d)  # noqa: E731
        category = get("category", "")
        errors: dict[str, str] = {}
        if category in wa_flow.TRACKED:
            if not get("track", ""):
                errors["track"] = "Pick the Track: Data Privacy, Team Activity Monitoring or Both Pack."
        else:
            attrs["track"] = ""
        step, stage = int(get("step", 0) or 0), int(get("stage", 0) or 0)
        if category == Cat.OUTREACH:
            if not 1 <= step <= wa_flow.LAST_DAY:
                errors["step"] = "Pick a day from 1 to 90."
            else:
                attrs["stage"] = wa_flow.stage_of(step)
        elif category in (Cat.ENGAGE, Cat.DEMO_VIDEO) and not 1 <= step <= 6:
            errors["step"] = "Pick a step from 1 to 6."
        elif category in (Cat.SETUP, Cat.TESTIMONIAL) and not 1 <= step <= 2:
            errors["step"] = "Pick step 1 or 2."
        if category == Cat.OFFER and not 1 <= stage <= 3:
            errors["stage"] = "Pick stage 1, 2 or 3."
        coupon = attrs.get("coupon")
        if coupon is not None and coupon.status() in ("ended", "cancelled") and (self.instance is None or self.instance.coupon_id != coupon.pk):
            errors["coupon"] = f"{coupon.code} has {coupon.status()}. Pick a code that is live or yet to start."
        if errors:
            raise serializers.ValidationError(errors)
        attrs["kind"] = wa_flow.kind_for(category)
        attrs["parts"] = {}
        attrs["twin"] = None
        if not (attrs.get("name") or (self.instance.name if self.instance else "")).strip():
            attrs["name"] = wa_flow.template_name(category, get("type_name", ""), get("track", "") if category in wa_flow.TRACKED else "", attrs.get("stage", stage), step)
        return attrs

    def save(self, **kwargs: Any) -> WaTemplate:
        """A twin link goes both ways: the other-language version points back here."""
        obj = super().save(**kwargs)
        if obj.twin_id and obj.twin.twin_id != obj.id:
            obj.twin.twin = obj
            obj.twin.save(update_fields=["twin"])
        wa_templates.sync_versions(obj)
        return obj

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs.get("category", getattr(self.instance, "category", "") if self.instance else ""):
            return self._category_rules(attrs)
        if not (attrs.get("name") or getattr(self.instance, "name", "")).strip():
            raise serializers.ValidationError({"name": "Name the template."})
        twin = attrs.get("twin")
        if twin is not None and twin.language == attrs.get("language", getattr(self.instance, "language", "en")):
            raise serializers.ValidationError({"twin": "Pick a template in the other language."})
        if "parts" in attrs:
            attrs["parts"] = wa_structure.clean(attrs["parts"])
        parts = attrs.get("parts", getattr(self.instance, "parts", {}) or {})
        if parts:
            missing = wa_structure.problems(parts)
            if missing:
                raise serializers.ValidationError(missing)  # one message per part: {"problem": [...]}
            attrs["text"] = wa_structure.compose(parts)
        elif not (attrs.get("text") or getattr(self.instance, "text", "")).strip():
            raise serializers.ValidationError({"text": "Write the message."})
        return attrs

    def get_media(self, obj: WaTemplate) -> dict[str, Any] | None:
        return wa_media.describe(obj)

    def validate_kind(self, value: str) -> str:
        if self.instance and self.instance.media and value in wa_media.COLD and not self.instance.category:
            raise serializers.ValidationError("Remove the picture or video first. " + wa_media.COLD_REFUSAL)
        return value


class WaMessageSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()

    class Meta:
        model = WaMessage
        fields = ["id", "kind", "text", "at", "by", "template", "label"]

    def get_label(self, obj: WaMessage) -> str:
        return wa_flow.label(obj.template) if obj.template_id and obj.template.category else ""


class WaContactSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source="campaign.name", read_only=True, default="")
    last = serializers.SerializerMethodField()
    greet_as = serializers.SerializerMethodField()
    salutation = serializers.SerializerMethodField()
    salutation_hi = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()
    how_we_know = serializers.SerializerMethodField()
    # For the Templates page: which details a condition finds known for this contact (wa_conditions.known).
    known = serializers.SerializerMethodField()
    day = serializers.SerializerMethodField()
    stage = serializers.SerializerMethodField()
    plan = serializers.CharField(source="campaign.plan", read_only=True, default="")

    def get_day(self, obj: WaContact) -> int:
        return wa_flow.day_of(obj, whatsapp.today())

    def get_stage(self, obj: WaContact) -> int:
        return wa_flow.stage_of(wa_flow.day_of(obj, whatsapp.today()))

    def update(self, instance: WaContact, validated: dict[str, Any]) -> WaContact:
        """A Track set here moves the campaign status too (Track assigned), as the Set the Track buttons do."""
        track = validated.pop("track", None)
        obj = super().update(instance, validated)
        if track is not None and track != obj.track:
            if track:
                wa_flow.set_track(obj, track)
            else:
                obj.track = ""
            obj.save(update_fields=["track", "campaign_status"])
        return obj

    @staticmethod
    def _address(obj: WaContact) -> dict[str, str]:
        """wa_names.address(), worked out once per contact per response."""
        from .wa_names import address

        cached = getattr(obj, "_wa_address", None)
        if cached is None:
            cached = obj._wa_address = address(obj)
        return cached

    def get_greet_as(self, obj: WaContact) -> str:
        return self._address(obj)["greeting"]

    def get_salutation(self, obj: WaContact) -> str:
        return self._address(obj)["salutation"]

    def get_salutation_hi(self, obj: WaContact) -> str:
        return self._address(obj)["salutation_hi"]

    def get_short_name(self, obj: WaContact) -> str:
        return self._address(obj)["business"]

    def get_how_we_know(self, obj: WaContact) -> str:
        return self._address(obj)["how_we_know"]

    def get_known(self, obj: WaContact) -> dict[str, bool]:
        return wa_conditions.known(obj, self._address(obj))

    class Meta:
        model = WaContact
        fields = [
            "id", "campaign", "campaign_name", "phone", "first_name", "owner_name", "firm", "segment", "country", "city", "source",
            "info", "status", "step", "next_date", "needs_reply", "paused_until", "created_at", "last", "track", "yes_date",
            "nurture_day", "first_sent", "opted_in", "opt_in_note", "greet_as", "salutation", "salutation_hi", "short_name",
            "how_we_know", "known",
            "campaign_status", "day", "stage", "plan", "out_day", "engage_step", "demo_step", "setup_step", "demo_call_sent",
            "offer_sent", "setup_error", "using_since", "pack", "testimonial",
        ]
        read_only_fields = [
            "opted_in", "opt_in_note",
            "phone", "status", "step", "next_date", "needs_reply", "paused_until", "created_at", "yes_date", "nurture_day",
            "first_sent", "campaign_status", "out_day", "engage_step", "demo_step", "setup_step", "demo_call_sent",
            "offer_sent", "setup_error", "using_since", "testimonial",
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
            "interested", "question", "notnow", "touch3", "touch4", "touch3_days", "touch4_days", "language", "stats", "created_at",
            "plan",
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

        def row(i: dict[str, Any]) -> dict[str, Any]:
            t = i.get("template")
            if isinstance(t, WaTemplate):  # a 90-day campaign message (wa_flow.py)
                return {"kind": i["kind"], "template": t.name, "template_id": t.pk, "label": wa_flow.label(t),
                        "category": t.category, "draft": wa_flow.is_draft(t), "note": i.get("note", ""),
                        "version": "B" if t.version_of_id else "",
                        "contact": WaContactSerializer(i["contact"]).data}
            return {"kind": i["kind"], "template": t or "", "template_id": None, "label": "", "category": "",
                    "draft": False, "note": i.get("note", ""), "version": "", "contact": WaContactSerializer(i["contact"]).data}

        return Response({
            "queue": [row(i) for i in items],
            "replies": [
                {**WaContactSerializer(c).data, "messages": WaMessageSerializer(c.messages.all(), many=True).data}
                for c in whatsapp.replies()
            ],
            "sent_today": whatsapp._sent_today(),
            "stats": whatsapp.stats(),
            "campaign": wa_flow.counts(),
        })


class WaContactListView(Staff):
    """GET every contact · POST one typed in the admin (Add contact): 400 {field: message} when something's off."""

    def get(self, request: Request) -> Response:
        rows = WaContact.objects.select_related("campaign").prefetch_related("messages")
        return Response(WaContactSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        try:
            contact = whatsapp.add_one(request.data, request.user.email)
        except WaFieldError as exc:
            return Response({"detail": str(exc), **exc.errors}, status=status.HTTP_400_BAD_REQUEST)
        self.audit(request, "wa_add_contact", contact, opted_in=contact.opted_in)
        return Response(WaContactSerializer(contact).data, status=status.HTTP_201_CREATED)


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
    """draft {kind, template?} · sent {kind, text} (kind custom = a one-off that doesn't move the sequence) ·
    reply {text} (what they wrote) · act {action}."""

    def post(self, request: Request, pk: int, action: str) -> Response:
        c = _contact(pk)
        actor = request.user.email
        try:
            if action == "draft":
                template = request.data.get("template")
                return Response(whatsapp.draft(c, str(request.data.get("kind", "first")), int(template) if str(template or "").isdigit() else None))
            if action == "sent":
                template = request.data.get("template")
                whatsapp.mark_sent(c, str(request.data.get("kind", "")), str(request.data.get("text", "")), actor,
                                   int(template) if str(template or "").isdigit() else None)
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
        if action == "contacts":  # {ids}: add contacts by hand
            ids = request.data.get("ids")
            if not isinstance(ids, list) or not ids:
                return Response({"detail": "Pick at least one contact."}, status=status.HTTP_400_BAD_REQUEST)
            result = whatsapp.add_to_campaign(c, ids)
            self.audit(request, "wa_campaign_add_contacts", c, **result)
            return Response({**result, "campaign": WaCampaignSerializer(c).data})
        if action not in ("start", "pause"):
            return Response({"detail": "Unknown action."}, status=status.HTTP_404_NOT_FOUND)
        if action == "start" and not c.contacts.exclude(status=WaContact.Status.STOP).exists():
            return Response({"detail": "Add contacts to this campaign first."}, status=status.HTTP_400_BAD_REQUEST)
        c.status = WaCampaign.Status.LIVE if action == "start" else WaCampaign.Status.PAUSED
        c.save(update_fields=["status"])
        self.audit(request, f"wa_campaign_{action}", c)
        return Response(WaCampaignSerializer(c).data)


class WaTemplateListView(Staff):
    """GET every template except version B rows (each sits in its A's `version_b`), with tags and the last 30 days'
    numbers · POST a new one (tag_ids to tag it)."""

    def get(self, request: Request) -> Response:
        rows = WaTemplate.objects.filter(version_of__isnull=True).select_related("coupon").prefetch_related("tags")
        context = {"performance": wa_flow.performance(), "stats": wa_templates.template_stats(),
                   "versions": wa_templates.active_versions()}
        return Response(WaTemplateSerializer(rows, many=True, context=context).data)

    def post(self, request: Request) -> Response:
        data = WaTemplateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(WaTemplateSerializer(data.save()).data, status=status.HTTP_201_CREATED)


class WaTemplateDetailView(Staff):
    """GET one · PUT all fields · PATCH some (archived, tag_ids, text...) · DELETE (its version B goes with it)."""

    def get(self, request: Request, pk: int) -> Response:
        return Response(WaTemplateSerializer(get_object_or_404(WaTemplate, pk=pk)).data)

    def put(self, request: Request, pk: int) -> Response:
        return self._save(request, pk, partial=False)

    def patch(self, request: Request, pk: int) -> Response:
        return self._save(request, pk, partial=True)

    def _save(self, request: Request, pk: int, partial: bool) -> Response:
        t = get_object_or_404(WaTemplate, pk=pk)
        data = WaTemplateSerializer(t, data=request.data, partial=partial)
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


class WaVersionSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=1000, required=False, allow_blank=True, trim_whitespace=False, default="")
    text_hi = serializers.CharField(max_length=1000, required=False, allow_blank=True, trim_whitespace=False, default="")

    validate_text = staticmethod(_conditions_ok)
    validate_text_hi = staticmethod(_conditions_ok)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not (attrs.get("text", "").strip() or attrs.get("text_hi", "").strip()):
            raise serializers.ValidationError({"text": ["Write version B first."]})
        return attrs


class WaTemplateVersionView(Staff):
    """POST {text, text_hi}: version B of this template (201, A's JSON with version_b). One B at a time. Edit B later
    with PATCH /templates/<B's id>/."""

    def post(self, request: Request, pk: int) -> Response:
        a = get_object_or_404(WaTemplate, pk=pk)
        data = WaVersionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            b = wa_templates.add_version(a, data.validated_data["text"], data.validated_data["text_hi"])
        except wa_templates.VersionError as exc:
            return self.refused(exc)
        self.audit(request, "wa_template_version", b, of=a.pk)
        return Response(WaTemplateSerializer(WaTemplate.objects.get(pk=a.pk)).data, status=status.HTTP_201_CREATED)


class WaTemplateKeepView(Staff):
    """POST {version: "A"|"B"}: end the A/B test. B: A takes B's texts. B is archived either way; numbers stay."""

    def post(self, request: Request, pk: int) -> Response:
        a = get_object_or_404(WaTemplate, pk=pk)
        which = str(request.data.get("version", "")).upper()
        if which not in ("A", "B"):
            return Response({"version": ["Pick A or B."]}, status=status.HTTP_400_BAD_REQUEST)
        try:
            a = wa_templates.keep(a, which)
        except wa_templates.VersionError as exc:
            return self.refused(exc)
        self.audit(request, "wa_template_keep", a, version=which)
        return Response(WaTemplateSerializer(a).data)


class WaTemplateDuplicateView(Staff):
    """POST: a copy named "<name> (copy)", same texts, category, step and tags (201, the copy's JSON)."""

    def post(self, request: Request, pk: int) -> Response:
        copy = wa_templates.duplicate(get_object_or_404(WaTemplate, pk=pk))
        return Response(WaTemplateSerializer(copy).data, status=status.HTTP_201_CREATED)


class WaTemplateReachView(Staff):
    """GET {total, known: {FirstName, OwnerName, Business, City, HowWeKnow}, examples}: contacts that can be messaged."""

    def get(self, request: Request) -> Response:
        return Response(wa_templates.reach())


class WaTemplatePreviewView(Staff):
    """POST {contact, text?, text_hi?}: the texts filled in for that contact exactly as a draft would be, and the
    details used outside a condition that this contact doesn't have (missing_en, missing_hi)."""

    def post(self, request: Request) -> Response:
        contact = request.data.get("contact")
        if not str(contact or "").isdigit():
            return Response({"contact": ["Pick a contact."]}, status=status.HTTP_400_BAD_REQUEST)
        c = _contact(int(contact))
        known = wa_conditions.known(c)
        out: dict[str, Any] = {"contact": c.pk, "known": known}
        for field, lang in (("text", "en"), ("text_hi", "hi")):
            raw = str(request.data.get(field) or "")[:1000]
            problem = wa_conditions.problem(raw)
            if problem:
                return Response({field: [problem]}, status=status.HTTP_400_BAD_REQUEST)
            out[field] = whatsapp.fill(raw, c, lang) if raw else ""
            out[f"missing_{lang}"] = [d for d in wa_conditions.outside(raw) if not known[d]]
        return Response(out)


class WaTagListView(Staff):
    """GET every tag (with how many templates carry it) · POST {name, color?}."""

    def get(self, request: Request) -> Response:
        return Response(WaTagSerializer(WaTag.objects.all(), many=True).data)

    def post(self, request: Request) -> Response:
        data = WaTagSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(WaTagSerializer(data.save()).data, status=status.HTTP_201_CREATED)


class WaTagDetailView(Staff):
    """PATCH {name?, color?} · DELETE: the tag comes off every template; the templates stay."""

    def patch(self, request: Request, pk: int) -> Response:
        data = WaTagSerializer(get_object_or_404(WaTag, pk=pk), data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        return Response(WaTagSerializer(data.save()).data)

    def delete(self, request: Request, pk: int) -> Response:
        get_object_or_404(WaTag, pk=pk).delete()
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
            "languages": whatsapp.by_language(),
            "campaigns": [{"id": c.id, "name": c.name, "status": c.status, **whatsapp.stats(c)} for c in WaCampaign.objects.all()],
            "blocked": WaBlock.objects.count(),
        })
