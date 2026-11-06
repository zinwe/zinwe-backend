"""Owner portal endpoints: company, subscription, manual payment. Thin; logic in company.py / billing.py."""
from __future__ import annotations

import mimetypes
import os
from typing import Any

from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import billing
from .company import CompanyExists, get_company, register_company, update_company
from .models import Feedback, Issue, ManualPayment
from .portal_serializers import (
    IssueSerializer,
    CompanySerializer,
    FeedbackSerializer,
    PaymentSerializer,
    PaymentSubmitSerializer,
    QuoteEmailSerializer,
    SubscribeSerializer,
    SubscriptionSerializer,
)
from .coupons import CouponRefused
from .services import PlanNotAllowed

NO_COMPANY = {"detail": "No company yet."}


def coupon_refused(exc: CouponRefused) -> Response:
    """F-21: a code refused at submit: 400 {detail, reason, coupon: [message]}."""
    return Response(
        {"detail": exc.message, "reason": exc.reason, "coupon": [exc.message]}, status=status.HTTP_400_BAD_REQUEST
    )


class CompanyView(APIView):
    """GET / POST / PATCH /api/auth/company/ -- the owner's single company."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        company = get_company(request.user)
        if company is None:
            return Response(NO_COMPANY, status=status.HTTP_404_NOT_FOUND)
        return Response(CompanySerializer(company).data)

    def post(self, request: Request) -> Response:
        if get_company(request.user) is not None:
            return Response({"detail": CompanyExists.message}, status=status.HTTP_409_CONFLICT)
        serializer = CompanySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            company = register_company(request.user, serializer.validated_data)
        except CompanyExists as exc:
            return Response({"detail": exc.message}, status=status.HTTP_409_CONFLICT)
        return Response(CompanySerializer(company).data, status=status.HTTP_201_CREATED)

    def patch(self, request: Request) -> Response:
        company = get_company(request.user)
        if company is None:
            return Response(NO_COMPANY, status=status.HTTP_404_NOT_FOUND)
        serializer = CompanySerializer(company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        company = update_company(company, serializer.validated_data)
        return Response(CompanySerializer(company).data)


class SubscriptionView(APIView):
    """GET / POST /api/auth/subscription/ -- current subscription + payment instructions; subscribe."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        subscription = billing.current_subscription(request.user)
        return Response(
            {
                "subscription": (
                    SubscriptionSerializer(subscription, context={"user": request.user}).data
                    if subscription else None
                ),
                "instructions": billing.payment_instructions(subscription, company=get_company(request.user)),
            }
        )

    def post(self, request: Request) -> Response:
        serializer = SubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            subscription = billing.subscribe(request.user, serializer.validated_data["plan"])
        except billing.CompanyRequired as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except PlanNotAllowed as exc:
            return Response({"plan": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        except (billing.AlreadyActive, billing.PaymentUnderReview) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(
            SubscriptionSerializer(subscription, context={"user": request.user}).data,
            status=status.HTTP_201_CREATED,
        )


class PaymentSubmitView(APIView):
    """POST /api/auth/subscription/payment/ (multipart) -- report a manual payment for review."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_submit"

    def post(self, request: Request) -> Response:
        serializer = PaymentSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        proof = data.pop("proof", None)
        try:
            payment = billing.submit_payment(request.user, data, proof)
        except (billing.NothingToPay, billing.PaymentUnderReview) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except billing.UpgradeNotAllowed as exc:
            return Response({"upgrade_plan": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        except CouponRefused as exc:
            return coupon_refused(exc)
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


class InvoiceView(APIView):
    """
    GET /api/auth/subscription/payments/<id>/invoice/ -- the invoice for one confirmed payment
    (seller, customer, plan, period, amount). The dashboard renders it as a printable page.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, payment_id: int) -> Response:
        payment = get_object_or_404(
            ManualPayment.objects.select_related("subscription__plan", "subscription__company", "upgrade_plan"),
            pk=payment_id, subscription__owner=request.user,
        )
        if payment.status != ManualPayment.Status.VERIFIED:
            return Response(
                {"detail": "An invoice is ready once the payment is confirmed."}, status=status.HTTP_409_CONFLICT
            )
        return Response(billing.invoice_for(payment))


def invoice_pdf_response(payment: ManualPayment) -> HttpResponse:
    """The confirmed payment's invoice as a PDF download (issued now if it was confirmed before invoices)."""
    from .invoices import invoice_for_payment, invoice_pdf

    invoice = invoice_for_payment(payment)
    response = HttpResponse(invoice_pdf(invoice), content_type="application/pdf")
    filename = invoice.number.replace("/", "-")
    response["Content-Disposition"] = f'attachment; filename="Zinwe-invoice-{filename}.pdf"'
    return response


class InvoicePdfView(APIView):
    """GET /api/auth/subscription/payments/<id>/invoice/pdf/ -- the owner's own invoice as a PDF (409 until confirmed)."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, payment_id: int) -> HttpResponse:
        payment = get_object_or_404(
            ManualPayment.objects.select_related("subscription__plan", "subscription__company", "upgrade_plan"),
            pk=payment_id, subscription__owner=request.user,
        )
        if payment.status != ManualPayment.Status.VERIFIED:
            return Response(
                {"detail": "An invoice is ready once the payment is confirmed."}, status=status.HTTP_409_CONFLICT
            )
        return invoice_pdf_response(payment)


def proof_file_response(payment: ManualPayment) -> FileResponse:
    """Stream a payment's proof as an attachment. Raises Http404 if there is none."""
    if not payment.proof:
        raise Http404("No proof file.")
    try:
        handle = payment.proof.open("rb")
    except FileNotFoundError as exc:
        raise Http404("The proof file is missing.") from exc
    filename = f"payment-{payment.pk}-proof{os.path.splitext(payment.proof.name)[1]}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(handle, as_attachment=True, filename=filename, content_type=content_type)


class PaymentHistoryView(APIView):
    """GET /api/auth/subscription/payments/ -- every payment the owner reported, newest first."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        payments = (
            ManualPayment.objects.filter(subscription__owner=request.user)
            .select_related("subscription__plan")
            .order_by("-submitted_at", "-id")[:50]
        )
        return Response(PaymentSerializer(payments, many=True).data)


class PaymentProofView(APIView):
    """GET /api/auth/subscription/payment/<id>/proof/ -- the owner's own proof file (staff: any)."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, payment_id: int) -> FileResponse:
        payments = ManualPayment.objects.all()
        if not request.user.is_staff:
            payments = payments.filter(subscription__owner=request.user)
        payment = payments.filter(pk=payment_id).first()
        if payment is None:
            raise Http404("No such payment.")
        return proof_file_response(payment)


class FeedbackView(APIView):
    """
    GET  /api/auth/feedback/ -- the owner's own feedback, newest first (last 10).
    POST /api/auth/feedback/ -- {rating 1-10, message?, page?}; the team reads it in the admin app.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "feedback"

    def get_throttles(self):  # type: ignore[no-untyped-def]
        # Only sending is rate-limited; reading your own history is not.
        return [ScopedRateThrottle()] if self.request.method == "POST" else []

    def get(self, request: Request) -> Response:
        # Only what the owner sent: their members' feedback from the Zinwe window goes to the Zinwe team (FR-14).
        rows = Feedback.objects.filter(owner=request.user, source=Feedback.Source.OWNER)[:10]
        return Response(FeedbackSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = FeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        feedback = serializer.save(owner=request.user)
        return Response(FeedbackSerializer(feedback).data, status=status.HTTP_201_CREATED)


class IssueView(APIView):
    """
    GET  /api/auth/issues/ -- the owner's own reports, newest first (last 20), with status and the team's reply.
    POST /api/auth/issues/ -- {description, page?, context?, contact_ok?} as JSON, or multipart with a `screenshot`
         (PNG, JPG or WebP, up to 5 MB). The team works it in the admin app's Issues inbox.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    throttle_scope = "issues"

    def get_throttles(self):  # type: ignore[no-untyped-def]
        return [ScopedRateThrottle()] if self.request.method == "POST" else []

    def get(self, request: Request) -> Response:
        # Only what the owner sent: their members' reports from the Zinwe window go to the Zinwe team (F-13).
        rows = Issue.objects.filter(owner=request.user, source=Issue.Source.OWNER)[:20]
        return Response(IssueSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = IssueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        issue = serializer.save(owner=request.user)
        return Response(IssueSerializer(issue).data, status=status.HTTP_201_CREATED)


def screenshot_response(file: Any, name: str) -> FileResponse:
    """Stream a stored screenshot inline (it was checked to be PNG, JPG or WebP on upload). 404 if it's gone."""
    try:
        handle = file.open("rb")
    except FileNotFoundError as exc:
        raise Http404("The screenshot file is missing.") from exc
    ext = os.path.splitext(file.name)[1].lower()
    content_type = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    response = FileResponse(handle, filename=f"{name}{ext}", content_type=content_type)
    response["Content-Security-Policy"] = "default-src 'none'"
    response["Cache-Control"] = "private, no-store"
    return response


class IssueScreenshotView(APIView):
    """GET /api/auth/issues/<id>/screenshot/ -- the screenshot sent with a report: its owner, or staff."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, issue_id: int) -> FileResponse:
        rows = Issue.objects.all() if request.user.is_staff else Issue.objects.filter(
            owner=request.user, source=Issue.Source.OWNER
        )
        issue = rows.filter(pk=issue_id).first()
        if issue is None or not issue.screenshot:
            raise Http404("No screenshot.")
        return screenshot_response(issue.screenshot, f"issue-{issue.pk}-screenshot")


# --- Shared quotes (z_auth.quotes) ------------------------------------------------------------

NO_QUOTE = {"detail": "This quote link has expired or isn't valid. Ask for a new one from the Zinwe payment page."}


def _quote_pdf_response(doc: dict) -> HttpResponse:
    from .quotes import quote_filename, quote_pdf

    response = HttpResponse(quote_pdf(doc), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{quote_filename(doc)}"'
    return response


class CheckoutPreviewView(APIView):
    """
    GET /api/auth/subscription/checkout/[?plan=<code>][&cycle=monthly|yearly][&coupon=CODE] -- what the checkout shows
    for one billing cycle: {quote, covers_from, covers_until, carried_days, upi_uri, yearly_saving_percent}; with
    `coupon` (F-21) the quote has it applied when it's valid, and `coupon_error` {reason, message} says why not. `plan` = an
    upgrade from an active plan, or another plan to pay for instead of the one waiting for payment; none = what's
    due (first payment or renewal). Read only. 404 unknown plan; 409 when there's nothing to pay / an upgrade
    isn't possible / a payment is being checked.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        from . import pricing
        from .models import Plan

        code = request.query_params.get("plan", "")
        plan = Plan.objects.filter(code=code[:50], is_active=True).first() if code else None
        if code and plan is None:
            return Response({"detail": "That plan doesn't exist or isn't available."}, status=status.HTTP_404_NOT_FOUND)
        cycle = pricing.valid_cycle(request.query_params.get("cycle"))
        coupon = (request.query_params.get("coupon") or "").strip()[:40] or None
        try:
            return Response(billing.checkout_preview(request.user, plan, cycle, coupon=coupon))
        except (billing.NothingToPay, billing.UpgradeNotAllowed, billing.PaymentUnderReview) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)


class UpgradePreviewView(APIView):
    """
    GET /api/auth/subscription/upgrade/<plan_code>/[?cycle=] -- before paying for an upgrade from an active plan:
    {quote, covers_from, covers_until, carried_days, upi_uri, yearly_saving_percent}. 404 unknown plan; 409 when
    the upgrade isn't possible (no active plan, a payment under review, or not a higher plan).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, plan_code: str) -> Response:
        from .models import Plan

        plan = Plan.objects.filter(code=plan_code, is_active=True).first()
        if plan is None:
            return Response({"detail": "That plan doesn't exist or isn't available."}, status=status.HTTP_404_NOT_FOUND)
        from . import pricing

        cycle = pricing.valid_cycle(request.query_params.get("cycle"))
        coupon = (request.query_params.get("coupon") or "").strip()[:40] or None
        try:
            return Response(billing.upgrade_preview(request.user, plan, cycle=cycle, coupon=coupon))
        except (billing.UpgradeNotAllowed, billing.PaymentUnderReview) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)


NO_UPI_QR = {"detail": "There's no UPI payment to make here. Use another way to pay, or email hello@zinwe.io."}


def _upi_qr_response(qr: dict | None) -> Response:
    """The QR (or 404), never cached: it's made from the server's UPI ID each time it's asked for."""
    response = Response(qr) if qr else Response(NO_UPI_QR, status=status.HTTP_404_NOT_FOUND)
    response["Cache-Control"] = "no-store"
    return response


class UpiQrView(APIView):
    """
    GET /api/auth/subscription/upi-qr/[?upgrade=<plan_code>] -- the UPI QR for what's due (or for that upgrade),
    made on request when the owner chooses "Show QR code" (FR-06): {svg, upi_id, payee_name, amount, note, upi_uri}.
    Built only from the server's own UPI ID and the server's quote; nothing in the request changes who is paid or
    how much. 404 when there's no UPI payment (not INR, nothing due, UPI not set up); 409 for an upgrade that
    isn't possible.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "upi_qr"

    def get(self, request: Request) -> Response:
        from . import pricing
        from .models import Plan

        subscription = billing.current_subscription(request.user)
        code = request.query_params.get("upgrade", "")
        cycle = pricing.valid_cycle(request.query_params.get("cycle"))
        coupon = (request.query_params.get("coupon") or "").strip()[:40]
        if not code:
            due = billing.subscription_due(subscription)
            if due is not None and cycle:
                due = billing.amount_due(subscription, None, cycle)
            if due is not None and coupon:
                due = _with_valid_coupon(coupon, subscription, subscription.plan, due)
            return _upi_qr_response(billing.upi_qr(due, subscription.company) if due else None)
        plan = Plan.objects.filter(code=code[:50], is_active=True).first()
        if plan is None:
            return Response({"detail": "That plan doesn't exist or isn't available."}, status=status.HTTP_404_NOT_FOUND)
        try:
            subscription = billing.check_upgrade(subscription, plan)
        except (billing.UpgradeNotAllowed, billing.PaymentUnderReview) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        due = billing.amount_due(subscription, plan, cycle)
        if coupon:
            due = _with_valid_coupon(coupon, subscription, plan, due)
        return _upi_qr_response(billing.upi_qr(due, subscription.company))


def _with_valid_coupon(code: str, subscription: Any, plan: Any, due: Any) -> Any:
    """The quote with the coupon (F-21) when it's valid for this owner, else unchanged."""
    from .coupons import check

    checked = check(code, subscription.company, plan, due)
    return checked.quote if checked.ok else due


class CouponCheckView(APIView):
    """
    GET /api/auth/subscription/coupon/?code=ZWSTARTUP15&plan=startups&cycle=monthly[&offer=<id>] -- F-21: can this
    owner use the code on that plan and cycle? 200 {ok: true, code, percent_off, capped, lowest_price, amount_off,
    message, quote} or {ok: false, code, reason, message}. `offer` (an admin offer price) always refuses a code.
    The checkout and the payment check it again.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "coupon_check"

    def get(self, request: Request) -> Response:
        from . import coupons, pricing
        from .models import Plan

        params = request.query_params
        code = coupons.normalise(params.get("code"))[:40]
        cycle = pricing.valid_cycle(params.get("cycle"))
        subscription = billing.current_subscription(request.user)
        plan_code = (params.get("plan") or "")[:50]
        plan = Plan.objects.filter(code=plan_code, is_active=True).first() if plan_code else (
            subscription.plan if subscription else None
        )
        if plan is None:
            return Response({"detail": "That plan doesn't exist or isn't available."}, status=status.HTTP_404_NOT_FOUND)
        company = get_company(request.user)
        if params.get("offer"):
            base = pricing.quote(plan, company, cycle=cycle)
            return Response(coupons.check(code, company, plan, base, offer=True).as_dict())
        base = None
        try:
            other = subscription is None or plan.pk != subscription.plan_id
            preview = billing.checkout_preview(request.user, plan if other else None, cycle)
            if preview["quote"]["plan_code"] == plan.code:
                base = _quote_from_dict(preview["quote"])
        except billing.BillingError:
            pass
        if base is None:  # no payment can start here yet (e.g. before choosing a plan): today's catalogue price
            currency = subscription.currency if subscription else None
            base = pricing.quote(plan, company, currency, cycle)
        return Response(coupons.check(code, company, plan, base).as_dict())


def _quote_from_dict(data: dict) -> Any:
    """A Quote back from its as_dict() form (the checkout preview's quote)."""
    from decimal import Decimal

    from .pricing import Quote, TaxLine

    fields = {k: v for k, v in data.items() if k != "taxes"}
    for key in ("list_amount", "discount_amount", "amount", "tax_total", "total"):
        fields[key] = Decimal(fields[key])
    q = Quote(**fields)
    q.taxes = [TaxLine(t["label"], Decimal(t["rate"]), Decimal(t["amount"])) for t in data.get("taxes", [])]
    return q


class QuoteView(APIView):
    """GET /api/auth/subscription/quote/ -- the owner's quote (what's due, how to pay) and its public link."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        from .quotes import QuoteNotAvailable, quote_document

        try:
            return Response(quote_document(billing.current_subscription(request.user)))
        except QuoteNotAvailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)


class QuotePdfView(APIView):
    """GET /api/auth/subscription/quote/pdf/ -- the owner's quote as a PDF."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> HttpResponse:
        from .quotes import QuoteNotAvailable, quote_document

        try:
            return _quote_pdf_response(quote_document(billing.current_subscription(request.user)))
        except QuoteNotAvailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)


class QuoteEmailView(APIView):
    """
    POST /api/auth/subscription/quote/email/ `{email?}` -- email the quote PDF (attached) and its public
    link, to the owner or to whoever pays (an accounts team). -> `{sent_to, public_url}`.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "quote_email"

    def post(self, request: Request) -> Response:
        from .quotes import QuoteNotAvailable, email_quote

        serializer = QuoteEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recipient = serializer.validated_data.get("email") or request.user.email
        try:
            doc = email_quote(billing.current_subscription(request.user), recipient)
        except QuoteNotAvailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"sent_to": recipient, "public_url": doc["public_url"]})


class PublicQuoteView(APIView):
    """
    GET /api/auth/quotes/<token>/ -- the shared quote, for anyone with the link (the website's /quote page): the
    copy as issued, with `status` "open" / "paid" / "replaced" (the page only offers ways to pay while open).
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "quote_public"

    def get(self, request: Request, token: str) -> Response:
        from .quotes import public_quote

        doc = public_quote(token)
        if doc is None:
            return Response(NO_QUOTE, status=status.HTTP_404_NOT_FOUND)
        return Response(doc)


class PublicQuotePdfView(PublicQuoteView):
    """GET /api/auth/quotes/<token>/pdf/ -- the shared quote as a PDF, only while it stands (404 once paid/replaced)."""

    def get(self, request: Request, token: str) -> HttpResponse:  # type: ignore[override]
        from .quotes import public_quote

        doc = public_quote(token)
        if doc is None or doc["status"] != "open":
            return Response(NO_QUOTE, status=status.HTTP_404_NOT_FOUND)
        return _quote_pdf_response(doc)


class PublicQuoteUpiQrView(PublicQuoteView):
    """GET /api/auth/quotes/<token>/upi-qr/ -- the shared quote's UPI QR, on request and only while it's open (FR-06)."""

    def get(self, request: Request, token: str) -> Response:
        from .quotes import public_upi_qr

        return _upi_qr_response(public_upi_qr(token))
