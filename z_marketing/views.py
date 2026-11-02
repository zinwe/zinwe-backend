"""Marketing endpoints. Staff: /api/admin/marketing/... Public: /api/marketing/unsubscribe/<token>/ (in every email)."""
from __future__ import annotations

import hmac
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from z_analytics.services import track

from . import contact_file, email_html, outreach, patterns
from .contact_file import FileProblem
from .models import Audience, Campaign, CampaignEmail, Contact, EmailTemplate, OutreachSend, Persona, Suppression
from .outreach import OutreachError


def _chips(max_items: int = 40) -> serializers.ListField:
    return serializers.ListField(child=serializers.CharField(max_length=240), required=False, max_length=max_items)


# The 12 profiling sections. Lists hold table rows; the others hold label -> text.
PROFILE_FORMS = {"business", "icp_b2b", "icp_b2c", "psychology", "journey", "keywords", "email", "ads", "market", "definition"}
PROFILE_TABLES = {"pains", "channels", "segments"}


def _clean_text(values: dict[str, Any]) -> dict[str, Any]:
    if len(values) > 40 or not all(isinstance(k, str) and isinstance(v, str) and len(v) <= 2000 for k, v in values.items()):
        raise serializers.ValidationError("Keep each answer under 2,000 characters.")
    return values


class PersonaSerializer(serializers.ModelSerializer):
    audience_count = serializers.IntegerField(source="audiences.count", read_only=True)

    class Meta:
        model = Persona
        fields = ["id", "name", "confidence", "profile", "audience_count", "updated_at"]
        read_only_fields = ["updated_at"]

    def validate_profile(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) - PROFILE_FORMS - PROFILE_TABLES:
            raise serializers.ValidationError("That profile has a section we don't know.")
        for key, section in value.items():
            if key in PROFILE_TABLES:
                if not isinstance(section, list) or len(section) > 30 or not all(isinstance(r, dict) for r in section):
                    raise serializers.ValidationError("Keep each table under 30 rows.")
                for row in section:
                    _clean_text(row)
            elif isinstance(section, dict):
                _clean_text(section)
            else:
                raise serializers.ValidationError("That profile has a section in the wrong shape.")
        return value


class AudienceSerializer(serializers.ModelSerializer):
    key_benefits = _chips()
    job_titles = _chips()
    decision_makers = _chips()
    industry_keywords = _chips()
    competitors = _chips()
    channels = _chips()
    campaign_count = serializers.IntegerField(source="campaigns.count", read_only=True)

    class Meta:
        model = Audience
        fields = [
            "id", "persona", "name", "country", "state", "city", "business_category", "business_segment", "business_type",
            "primary_problem", "secondary_problem", "product_service", "how_solves", "key_benefits", "primary_offer",
            "pricing", "objective", "cta", "job_titles", "decision_makers", "industry_keywords", "competitors", "notes",
            "channels", "campaign_count",
        ]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        for field, label in (("key_benefits", "benefit"), ("channels", "channel")):
            if not attrs.get(field, getattr(self.instance, field, None)):
                raise serializers.ValidationError({field: f"Add at least one {label}."})
        return attrs


def _clean_body(value: str) -> str:
    cleaned = email_html.clean_body(value)
    if not email_html.to_text(cleaned):
        raise serializers.ValidationError("Write the email first.")
    return cleaned


class EmailStepSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source="template.name", default="", read_only=True)

    class Meta:
        model = CampaignEmail
        fields = ["n", "stage", "when", "week", "subject", "body", "template_name"]
        read_only_fields = ["n", "stage", "when", "week"]

    def validate_body(self, value: str) -> str:
        return _clean_body(value)


class TemplateSerializer(serializers.ModelSerializer):
    built_in = serializers.SerializerMethodField()
    used_in = serializers.IntegerField(source="uses.count", read_only=True)

    class Meta:
        model = EmailTemplate
        fields = ["id", "name", "kind", "subject", "body", "built_in", "used_in"]

    def get_built_in(self, obj: EmailTemplate) -> bool:
        return obj.step is not None

    def validate_body(self, value: str) -> str:
        return _clean_body(value)

    def validate_kind(self, value: str) -> str:
        if self.instance and self.instance.step is not None and value != self.instance.kind:
            raise serializers.ValidationError("This starter template belongs to the patterns, so its type stays.")
        return value


class CampaignInput(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ["name", "audience", "goal", "theme", "problem", "offer", "daily_limit"]
        extra_kwargs = {"name": {"required": False}, "audience": {"required": False}}

    def validate_daily_limit(self, value: int) -> int:
        if value not in outreach.DAILY_LIMITS:
            raise serializers.ValidationError("Pick 10, 20 or 25 a day.")
        return value


class ContactSerializer(serializers.ModelSerializer):
    stage = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()

    class Meta:
        model = Contact
        fields = [
            "id", "first_name", "firm", "email", "city", "status", "stage", "next_email", "next_date", "last_email",
            "opted_in_at", "proof", "installed", "reason", "events",
        ]

    def get_stage(self, obj: Contact) -> str:
        return outreach.stage_of(obj, self.context["stages"])

    def get_events(self, obj: Contact) -> list[dict[str, str]]:
        return [{"at": e.at.isoformat(), "text": e.text} for e in obj.events.all()]


def _sender() -> dict[str, Any]:
    return {
        "from": settings.ZINWE_OUTREACH_FROM,
        "reply_to": settings.ZINWE_OUTREACH_REPLY_TO,
        "domain": outreach.sender_domain(),
        "mailbox_ready": outreach.mailbox_ready(),
        "smtp_host": settings.EMAIL_OUTREACH_HOST,
        "newsletter_from": settings.ZINWE_NEWSLETTER_FROM if settings.EMAIL_NEWSLETTER_HOST else settings.DEFAULT_FROM_EMAIL,
        "postal_address": settings.ZINWE_OUTREACH_POSTAL_ADDRESS,
        "limits": outreach.DAILY_LIMITS,
        "patterns": patterns.public(),
    }


def _summary(c: Campaign) -> dict[str, Any]:
    room = max(0, c.daily_limit - outreach.sent_today(c))
    funnel = {f["label"]: f["value"] for f in outreach.report(c)["funnel"]}
    counts = outreach.counts(c)
    return {
        "id": c.id, "name": c.name, "audience": c.audience_id, "audience_name": c.audience.name, "goal": c.goal,
        "theme": c.theme, "status": c.status, "counts": counts, "pattern": c.pattern, "emails_count": c.emails.count(),
        "ready_today": min(counts["due"], room), "emailed": funnel["Emailed"], "replied": funnel["Replied"],
        "signed_up": funnel["Signed up"],
    }


def _detail(c: Campaign) -> dict[str, Any]:
    return {
        **_summary(c), "problem": c.problem, "offer": c.offer, "pause_reason": c.pause_reason,
        "daily_limit": c.daily_limit, "sent_today": outreach.sent_today(c), "pattern_open": outreach.pattern_open(c),
        "emails_read_at": c.emails_read_at, "persona": c.audience.persona_id, "dns_result": c.dns_result, "dns_checked_at": c.dns_checked_at, "rules_checked_at": c.rules_checked_at,
        "rules_checked_by": c.rules_checked_by, "test_sent_at": c.test_sent_at,
        "checks": outreach.checks(c), "report": outreach.report(c),
        "emails": EmailStepSerializer(c.emails.all(), many=True).data,
    }


class Staff(APIView):
    permission_classes = [IsAdminUser]

    def refused(self, exc: Exception) -> Response:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def audit(self, request: Request, action: str, ref: object, **properties: Any) -> None:
        track(f"admin.marketing_{action}", properties, actor_email=request.user.email, ref=ref)


class PersonaListView(Staff):
    def get(self, request: Request) -> Response:
        return Response(PersonaSerializer(Persona.objects.all(), many=True).data)

    def post(self, request: Request) -> Response:
        data = PersonaSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(PersonaSerializer(data.save()).data, status=status.HTTP_201_CREATED)


class PersonaDetailView(Staff):
    def get(self, request: Request, pk: int) -> Response:
        return Response(PersonaSerializer(get_object_or_404(Persona, pk=pk)).data)

    def put(self, request: Request, pk: int) -> Response:
        data = PersonaSerializer(get_object_or_404(Persona, pk=pk), data=request.data)
        data.is_valid(raise_exception=True)
        return Response(PersonaSerializer(data.save()).data)

    def delete(self, request: Request, pk: int) -> Response:
        persona = get_object_or_404(Persona, pk=pk)
        if persona.audiences.exists():
            return self.refused(Exception("Remove its target audiences first."))
        persona.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AudienceListView(Staff):
    def get(self, request: Request) -> Response:
        rows = Audience.objects.all()
        persona = request.query_params.get("persona", "")
        if persona.isdigit():
            rows = rows.filter(persona_id=persona)
        return Response(AudienceSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        data = AudienceSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(AudienceSerializer(data.save()).data, status=status.HTTP_201_CREATED)


class AudienceDetailView(Staff):
    def get(self, request: Request, pk: int) -> Response:
        return Response(AudienceSerializer(get_object_or_404(Audience, pk=pk)).data)

    def put(self, request: Request, pk: int) -> Response:
        data = AudienceSerializer(get_object_or_404(Audience, pk=pk), data=request.data)
        data.is_valid(raise_exception=True)
        return Response(AudienceSerializer(data.save()).data)

    def delete(self, request: Request, pk: int) -> Response:
        audience = get_object_or_404(Audience, pk=pk)
        if audience.campaigns.exists():
            return self.refused(Exception("A campaign uses this audience, so it can't be removed."))
        audience.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CampaignListView(Staff):
    def get(self, request: Request) -> Response:
        rows = Campaign.objects.select_related("audience")
        return Response({"sender": _sender(), "campaigns": [_summary(c) for c in rows]})

    @transaction.atomic
    def post(self, request: Request) -> Response:
        """A new draft: an audience plus its own copy of an email pattern, or of another campaign's emails."""
        data = CampaignInput(data=request.data)
        data.is_valid(raise_exception=True)
        if "audience" not in data.validated_data or not data.validated_data.get("name"):
            return self.refused(Exception("Add a name and pick an audience."))
        pattern, source = str(request.data.get("pattern", "")), request.data.get("copy_from")
        if source:
            original = get_object_or_404(Campaign, pk=source)
            pattern = original.pattern
            steps = list(original.emails.values("n", "stage", "when", "week", "subject", "body", "template_id"))
        elif pattern in patterns.PATTERNS:
            steps = patterns.emails(pattern)
        else:
            return self.refused(Exception("Pick an email pattern."))
        campaign = data.save(pattern=pattern)
        CampaignEmail.objects.bulk_create(CampaignEmail(campaign=campaign, **step) for step in steps)
        return Response(_detail(campaign), status=status.HTTP_201_CREATED)


class CampaignDetailView(Staff):
    def get(self, request: Request, pk: int) -> Response:
        return Response(_detail(get_object_or_404(Campaign.objects.select_related("audience"), pk=pk)))

    def patch(self, request: Request, pk: int) -> Response:
        campaign = get_object_or_404(Campaign, pk=pk)
        data = CampaignInput(campaign, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        return Response(_detail(data.save()))

    def delete(self, request: Request, pk: int) -> Response:
        """Only a draft that never sent. A campaign with history stays, for its report and proof of consent."""
        campaign = get_object_or_404(Campaign, pk=pk)
        if campaign.status != Campaign.Status.DRAFT or OutreachSend.objects.filter(contact__campaign=campaign).exists():
            return self.refused(Exception("Only a draft that never sent can be removed."))
        self.audit(request, "campaign_removed", campaign, contacts=campaign.contacts.count())
        campaign.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class EmailStepView(Staff):
    def put(self, request: Request, pk: int, n: int) -> Response:
        step = get_object_or_404(CampaignEmail, campaign_id=pk, n=n)
        data = EmailStepSerializer(step, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        data.save()
        return Response(data.data)

    def delete(self, request: Request, pk: int, n: int) -> Response:
        try:
            outreach.remove_step(get_object_or_404(Campaign, pk=pk), n)
        except OutreachError as exc:
            return self.refused(exc)
        return Response(_detail(get_object_or_404(Campaign.objects.select_related("audience"), pk=pk)))


class EmailStepListView(Staff):
    def post(self, request: Request, pk: int) -> Response:
        """A new email at the end: a blank one, or a copy of `template`."""
        campaign = get_object_or_404(Campaign.objects.select_related("audience"), pk=pk)
        chosen = request.data.get("template")
        try:
            outreach.add_step(campaign, get_object_or_404(EmailTemplate, pk=chosen) if chosen else None)
        except OutreachError as exc:
            return self.refused(exc)
        return Response(_detail(campaign), status=status.HTTP_201_CREATED)


class EmailStepActionView(Staff):
    """POST .../emails/<n>/use-template/ {template} or .../emails/<n>/save-template/ {name}."""

    def post(self, request: Request, pk: int, n: int, action: str) -> Response:
        campaign = get_object_or_404(Campaign.objects.select_related("audience"), pk=pk)
        try:
            if action == "use-template":
                outreach.use_template(campaign, n, get_object_or_404(EmailTemplate, pk=request.data.get("template")))
                return Response(_detail(campaign))
            if action == "save-template":
                made = outreach.save_as_template(campaign, n, str(request.data.get("name", "")))
                return Response(TemplateSerializer(made).data, status=status.HTTP_201_CREATED)
        except (OutreachError, ValueError, TypeError) as exc:
            return self.refused(exc)
        return self.refused(Exception("That action isn't available."))


class TemplateListView(Staff):
    def get(self, request: Request) -> Response:
        return Response(TemplateSerializer(EmailTemplate.objects.all(), many=True).data)

    def post(self, request: Request) -> Response:
        data = TemplateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(TemplateSerializer(data.save()).data, status=status.HTTP_201_CREATED)


class TemplateDetailView(Staff):
    def put(self, request: Request, pk: int) -> Response:
        data = TemplateSerializer(get_object_or_404(EmailTemplate, pk=pk), data=request.data)
        data.is_valid(raise_exception=True)
        return Response(TemplateSerializer(data.save()).data)

    def delete(self, request: Request, pk: int) -> Response:
        template = get_object_or_404(EmailTemplate, pk=pk)
        if template.step is not None:
            return self.refused(Exception("The email patterns use this starter template, so it stays. You can edit it."))
        template.delete()  # campaigns keep their copies
        return Response(status=status.HTTP_204_NO_CONTENT)


class CampaignActionView(Staff):
    """POST .../campaigns/<id>/<action>/: launch, pause, test, dns, rules, emails-read, send."""

    def post(self, request: Request, pk: int, action: str) -> Response:
        campaign = get_object_or_404(Campaign.objects.select_related("audience"), pk=pk)
        actor = request.user.email
        try:
            if action == "launch":
                outreach.launch(campaign, actor)
            elif action == "pause":
                outreach.pause(campaign, "Paused by staff.")
            elif action == "test":
                outreach.send_test(campaign, int(request.data.get("n", 1)), actor)
            elif action == "dns":
                outreach.check_dns(campaign)
            elif action == "rules":
                done = bool(request.data.get("checked"))
                campaign.rules_checked_at = timezone.now() if done else None
                campaign.rules_checked_by = actor if done else ""
                campaign.save(update_fields=["rules_checked_at", "rules_checked_by"])
            elif action == "emails-read":
                campaign.emails_read_at = timezone.now() if request.data.get("checked") else None
                campaign.save(update_fields=["emails_read_at"])
            elif action == "send":
                approved = outreach.approve(campaign, int(request.data.get("expected", -1)), actor)
                self.audit(request, "outreach_approved", campaign, emails=approved)
                return Response({"approved": approved, **_detail(campaign)})
            else:
                return self.refused(Exception("That action isn't available."))
        except (OutreachError, ValueError) as exc:
            return self.refused(exc)
        self.audit(request, f"campaign_{action}", campaign)
        return Response(_detail(campaign))


class ContactListView(Staff):
    def get(self, request: Request, pk: int) -> Response:
        campaign = get_object_or_404(Campaign, pk=pk)
        stages = {e.n: e.stage for e in campaign.emails.all()}
        rows = campaign.contacts.prefetch_related("events")[:2000]
        return Response(ContactSerializer(rows, many=True, context={"stages": stages}).data)


class ContactImportView(Staff):
    def post(self, request: Request, pk: int) -> Response:
        campaign = get_object_or_404(Campaign, pk=pk)
        results = outreach.import_contacts(campaign, str(request.data.get("text", "")), request.user.email)
        self.audit(request, "contacts_added", campaign, added=sum(r["result"] == "good" for r in results))
        return Response({"results": results})


class ContactTemplateView(Staff):
    def get(self, request: Request) -> HttpResponse:
        response = HttpResponse(
            contact_file.template(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = 'attachment; filename="zinwe-contacts-template.xlsx"'
        return response


class ContactUploadView(Staff):
    """POST a file. Without `add` it only reviews each row; with add=1 it adds the rows that review as ready."""

    parser_classes = [MultiPartParser]

    def post(self, request: Request, pk: int) -> Response:
        campaign = get_object_or_404(Campaign, pk=pk)
        upload = request.FILES.get("file")
        if upload is None:
            return self.refused(Exception("Choose a file first."))
        try:
            rows = contact_file.parse(upload, upload.name, upload.size)
        except FileProblem as exc:
            return self.refused(exc)
        if request.data.get("add") == "1":
            added = outreach.add_from_file(campaign, rows, request.user.email)
            self.audit(request, "contacts_added", campaign, added=added, source="file")
            return Response({"added": added})
        return Response({"rows": outreach.review_file(campaign, rows)})


class ContactActView(Staff):
    def post(self, request: Request, pk: int) -> Response:
        contact = get_object_or_404(Contact.objects.select_related("campaign"), pk=pk)
        try:
            outreach.act(contact, str(request.data.get("action", "")), request.user.email, str(request.data.get("note", "")))
        except OutreachError as exc:
            return self.refused(exc)
        stages = {e.n: e.stage for e in contact.campaign.emails.all()}
        return Response(ContactSerializer(contact, context={"stages": stages}).data)


class BlockedView(Staff):
    def get(self, request: Request) -> Response:
        rows = Suppression.objects.all()[:2000]
        return Response([{"email": r.email, "reason": r.reason, "created_at": r.created_at} for r in rows])


class SheetImportThrottle(AnonRateThrottle):
    rate = "60/hour"


class SheetImportView(APIView):
    """POST /api/marketing/sheet-import/: the team's daily sheet run (Google Apps Script). Adds contacts only.

    Answers 404 until ZINWE_SHEET_IMPORT_TOKEN is set, and to a wrong token."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [SheetImportThrottle]

    def post(self, request: Request) -> Response:
        expected = settings.ZINWE_SHEET_IMPORT_TOKEN
        given = request.headers.get("X-Import-Token", "")
        if not expected or not hmac.compare_digest(expected, given):
            return Response(status=status.HTTP_404_NOT_FOUND)
        rows = request.data.get("rows") if isinstance(request.data, dict) else None
        if not isinstance(rows, list) or len(rows) > 500 or not all(isinstance(r, dict) for r in rows):
            return Response({"detail": "Send up to 500 rows."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"rows": outreach.import_sheet_rows(rows)})


class UnsubscribeThrottle(AnonRateThrottle):
    rate = "30/minute"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Zinwe</title></head>
<body style="font:16px/1.5 Arial,Helvetica,sans-serif;color:#16181b;max-width:34rem;margin:15vh auto;padding:0 20px;">{}</body></html>"""


class UnsubscribeView(APIView):
    """GET shows a button, so a mail scanner opening the link changes nothing. POST unsubscribes: the button, or the
    RFC 8058 one-click request a mail client sends."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    throttle_classes = [UnsubscribeThrottle]

    def get(self, request: Request, token: str) -> HttpResponse:
        contact = Contact.objects.filter(token=token).first()
        if contact is None or contact.status == Contact.Status.SUPPRESSED:
            return self._page("<p>You won't get any more emails from us.</p>")
        return self._page(
            f"<p>Stop emails to <b>{escape(contact.email)}</b>?</p>"
            '<form method="post"><button type="submit" style="font:inherit;padding:10px 16px;">Unsubscribe</button></form>'
        )

    def post(self, request: Request, token: str) -> HttpResponse:
        outreach.unsubscribe(token)
        return self._page("<p>Done. You won't get any more emails from us.</p>")

    def _page(self, body: str) -> HttpResponse:
        return HttpResponse(PAGE.format(body))


unsubscribe_view = csrf_exempt(UnsubscribeView.as_view())
