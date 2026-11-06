"""F-21 coupon codes: the math, every refusal, one code per company, manual and Razorpay payments, the staff pages."""
from __future__ import annotations

import hashlib
import hmac
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from z_analytics.models import Event
from z_payments import razorpay_api as api
from z_payments.models import RazorpayOrder
from z_payments.services import reconcile

from . import billing, coupons, pricing
from ._testing import PortalTestCase
from .models import Announcement, Coupon, CouponUse, ManualPayment, Plan, User

SELLER = {"name": "SKYTECHNO", "address": "New Delhi, India", "email": "hello@zinwe.io", "gstin": "07ABCDE1234F1Z5"}
KEYS = dict(
    RAZORPAY_KEY_ID="rzp_test_key", RAZORPAY_KEY_SECRET="s3cret", RAZORPAY_WEBHOOK_SECRET="hook",
    ZINWE_RAZORPAY_ENABLED=True, ZINWE_APP_URL="https://dev.frontend.zinwe.io",
)
SUB_URL = "/api/auth/subscription/"
PAY_URL = "/api/auth/subscription/payment/"
CHECK_URL = "/api/auth/subscription/coupon/"
CHECKOUT_URL = "/api/auth/subscription/checkout/"
ORDER_URL = "/api/payments/razorpay/order/"
VERIFY_URL = "/api/payments/razorpay/verify/"
ADMIN = "/api/admin/coupons/"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def sign(message: str) -> str:
    return hmac.new(b"s3cret", message.encode(), hashlib.sha256).hexdigest()


def make_coupon(code: str = "ZWSTARTUP15", percent: int = 15, **fields) -> Coupon:  # type: ignore[no-untyped-def]
    fields.setdefault("starts_on", timezone.localdate() - timedelta(days=1))
    return Coupon.objects.create(code=code, percent_off=percent, **fields)


class RupeesTextTests(PortalTestCase):
    def test_copy_style(self) -> None:
        self.assertEqual(pricing.rupees(Decimal("971.00")), "₹971")
        self.assertEqual(pricing.rupees(Decimal("973.91")), "₹973.91")
        self.assertEqual(pricing.rupees(Decimal("14437.10")), "₹14,437.10")
        self.assertEqual(pricing.rupees(Decimal("100000")), "₹1,00,000")

    def test_discount_math(self) -> None:
        self.assertEqual(pricing.coupon_discount(Decimal("971.00"), 15, None), (Decimal("145.65"), False))
        self.assertEqual(pricing.coupon_discount(Decimal("971.00"), 40, Decimal("650")), (Decimal("321.00"), True))
        self.assertEqual(pricing.coupon_discount(Decimal("971.00"), 10, Decimal("971")), (Decimal("0.00"), True))


@override_settings(ZINWE_INVOICE_SELLER=SELLER)
class CouponTestCase(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.company = self.make_company(self.owner)  # Delhi: same state as the seller -> CGST + SGST
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        # The seeded band minimum would cap 15% here; each test sets the band it needs.
        Plan.objects.filter(code="startups").update(band_min_inr=None)
        self.startups = Plan.objects.get(code="startups")
        self.assertEqual(self.startups.price_inr_monthly, Decimal("971.00"))

    def subscribe(self, plan: str = "startups") -> None:
        response = self.post(SUB_URL, {"plan": plan})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def check_code(self, code: str = "ZWSTARTUP15", plan: str = "startups", cycle: str = "monthly", **extra) -> dict:  # type: ignore[no-untyped-def]
        response = self.get(CHECK_URL, data={"code": code, "plan": plan, "cycle": cycle, **extra})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()

    def submit(self, **fields):  # type: ignore[no-untyped-def]
        data = {"accept_terms": "true", "proof": SimpleUploadedFile("r.png", PNG, content_type="image/png"), **fields}
        return self.post(PAY_URL, data, fmt="multipart")

    def other_company_use(self, coupon: Coupon, state: str = "confirmed") -> CouponUse:
        other = self.make_owner(f"o{CouponUse.objects.count()}@other.in", login=False)
        company = self.make_company(other, name="Other Co")
        return CouponUse.objects.create(
            coupon=coupon, company=company, owner=other, plan_code="startups", plan_name="Startups", state=state,
            percent_off=coupon.percent_off, amount_inr=Decimal("825.35"), amount_off=Decimal("145.65"),
        )


class CheckTests(CouponTestCase):
    def test_valid_code_with_same_state_gst(self) -> None:
        self.subscribe()
        make_coupon()
        body = self.check_code("  zwstartup15 ")  # trimmed and upper-cased
        self.assertTrue(body["ok"], body)
        self.assertEqual((body["code"], body["percent_off"], body["capped"]), ("ZWSTARTUP15", 15, False))
        self.assertEqual(body["amount_off"], "145.65")
        self.assertNotIn("lowest_price", body)  # internal: never sent to the owner
        self.assertEqual(body["message"], "15% off your first month. You save ₹145.65.")
        q = body["quote"]
        self.assertEqual((q["amount"], q["total"], q["discount_amount"]), ("825.35", "973.91", q["discount_amount"]))
        self.assertEqual([t["label"] for t in q["taxes"]], ["CGST", "SGST"])
        self.assertEqual([t["amount"] for t in q["taxes"]], ["74.28", "74.28"])
        self.assertEqual(q["coupon"], {"code": "ZWSTARTUP15", "percent_off": 15, "amount_off": "145.65", "capped": False})
        # The launch discount stays the launch discount only.
        self.assertEqual(Decimal(q["list_amount"]) - Decimal(q["discount_amount"]), Decimal("971.00"))

    def test_the_price_band_does_not_limit_a_coupon(self) -> None:
        # Owner decision (6 Nov 2026): no floor; the code's own % (at most 40%) is the only limit.
        Plan.objects.filter(pk=self.startups.pk).update(band_min_inr=Decimal("900.00"))
        self.subscribe()
        make_coupon("ZWBIGSALE40", 40)
        body = self.check_code("ZWBIGSALE40")
        self.assertTrue(body["ok"], body)
        self.assertEqual((body["capped"], body["amount_off"]), (False, "388.40"))
        self.assertEqual(body["message"], "40% off your first month. You save ₹388.40.")
        self.assertEqual(body["quote"]["amount"], "582.60")
        self.assertFalse(body["quote"]["coupon"]["capped"])

    def test_without_a_subscription_prices_from_the_catalogue(self) -> None:
        make_coupon()
        body = self.check_code()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["quote"]["amount"], "825.35")

    def assert_refused(self, body: dict, reason: str, message: str) -> None:
        self.assertEqual((body["ok"], body["reason"], body["message"]), (False, reason, message), body)

    def test_every_reason(self) -> None:
        self.subscribe()
        today = timezone.localdate()
        self.assert_refused(self.check_code("ZWNOSUCHONE"), "not_found", coupons.MESSAGES["not_found"])
        self.assert_refused(self.check_code("short"), "not_found", coupons.MESSAGES["not_found"])
        make_coupon("ZWCANCELLED", cancelled=True)
        self.assert_refused(self.check_code("ZWCANCELLED"), "not_found", coupons.MESSAGES["not_found"])
        make_coupon("ZWLATER0001", starts_on=date(today.year + 1, 12, 16))
        self.assert_refused(self.check_code("ZWLATER0001"), "not_started", "This code starts on 16 Dec.")
        make_coupon("ZWENDED0001", starts_on=today - timedelta(days=30), ends_on=today - timedelta(days=1))
        ended = (today - timedelta(days=1))
        self.assert_refused(
            self.check_code("ZWENDED0001"), "ended",
            f"This code ended on {ended.day} {ended:%b}. Your price is still ₹971 + GST.",
        )
        make_coupon("ZWPAUSED001", paused=True)
        self.assert_refused(self.check_code("ZWPAUSED001"), "paused", "This code is paused. Try again later.")
        make_coupon("ZWTEAMSONLY", plan_codes=["teams"])
        self.assert_refused(
            self.check_code("ZWTEAMSONLY"), "other_plan",
            "This code is for the Teams plan. Pick Teams, or use another code.",
        )
        make_coupon()
        self.assert_refused(self.check_code(cycle="yearly"), "monthly_only", coupons.MESSAGES["monthly_only"])
        full = make_coupon("ZWFULLCODE1", max_uses=1)
        self.other_company_use(full)
        self.assert_refused(self.check_code("ZWFULLCODE1"), "limit_reached", "This code has been used up.")
        self.assert_refused(self.check_code(offer="7"), "offer", coupons.MESSAGES["offer"])
        Plan.objects.filter(pk=self.startups.pk).update(band_min_inr=Decimal("971.00"))
        self.assertTrue(self.check_code()["ok"])  # a band minimum at today's price still lets the code work

    def test_dates_are_inclusive(self) -> None:
        self.subscribe()
        today = timezone.localdate()
        make_coupon(starts_on=today, ends_on=today)
        self.assertTrue(self.check_code()["ok"])

    def test_one_code_per_company(self) -> None:
        self.subscribe()
        first = make_coupon("ZWFIRST0001")
        make_coupon()
        CouponUse.objects.create(
            coupon=first, company=self.company, owner=self.owner, plan_code="startups", plan_name="Startups",
            state="confirmed", percent_off=15, amount_inr=Decimal("825.35"), amount_off=Decimal("145.65"),
        )
        self.assert_refused(self.check_code(), "used_before", "You have used a code before. One code per company.")
        CouponUse.objects.update(state="void")
        self.assertTrue(self.check_code()["ok"])

    @override_settings(ZINWE_PAYMENT_INSTRUCTIONS={"upi_id": "skytechno@upi", "payee_name": "SKYTECHNO"})
    def test_checkout_preview_carries_the_code(self) -> None:
        self.subscribe()
        make_coupon()
        good = self.get(CHECKOUT_URL, data={"cycle": "monthly", "coupon": "zwstartup15"}).json()
        self.assertEqual((good["quote"]["amount"], good["quote"]["total"], good["coupon_error"]), ("825.35", "973.91", None))
        self.assertIn("am=973.91", good["upi_uri"])
        bad = self.get(CHECKOUT_URL, data={"cycle": "yearly", "coupon": "ZWSTARTUP15"}).json()
        self.assertIsNone(bad["quote"]["coupon"])
        self.assertEqual(bad["coupon_error"]["reason"], "monthly_only")
        plain = self.get(CHECKOUT_URL).json()
        self.assertNotIn("coupon_error", plain)
        self.assertIsNone(plain["quote"]["coupon"])


class ManualPaymentTests(CouponTestCase):
    def test_receipt_with_a_code_then_approved(self) -> None:
        self.subscribe()
        make_coupon()
        response = self.submit(coupon=" zwstartup15")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        payment = ManualPayment.objects.get()
        self.assertEqual(payment.amount_inr, Decimal("973.91"))
        self.assertEqual(payment.due["coupon"]["code"], "ZWSTARTUP15")
        use = CouponUse.objects.get()
        self.assertEqual((use.state, use.payment_id, use.amount_inr, use.amount_off), (
            "pending", payment.pk, Decimal("825.35"), Decimal("145.65")))
        # Pending already counts: no second code meanwhile.
        self.assertEqual(self.check_code()["reason"], "used_before")
        with self.captureOnCommitCallbacks(execute=True):
            billing.verify_payment(payment.pk, self.staff, "UTR1", Decimal("973.91"))
        use.refresh_from_db()
        self.assertEqual(use.state, "confirmed")
        self.assertIsNotNone(use.confirmed_at)
        invoice = ManualPayment.objects.get().invoice.data
        self.assertEqual(invoice["coupon"]["label"], "Coupon ZWSTARTUP15 −₹145.65")
        self.assertEqual(invoice["lines"][0]["coupon_code"], "ZWSTARTUP15")
        self.assertEqual((invoice["taxable_amount"], invoice["total"]), ("825.35", "973.91"))
        from .invoices import invoice_pdf

        self.assertTrue(invoice_pdf(ManualPayment.objects.get().invoice).startswith(b"%PDF"))
        # Only the first month: the plan renews at today's price.
        self.assertEqual(billing.current_subscription(self.owner).amount_inr, Decimal("971.00"))

    def test_rejected_receipt_frees_the_code(self) -> None:
        self.subscribe()
        make_coupon()
        self.submit(coupon="ZWSTARTUP15")
        with self.captureOnCommitCallbacks(execute=True):
            billing.reject_payment(ManualPayment.objects.get().pk, self.staff, "Blurry receipt")
        self.assertEqual(CouponUse.objects.get().state, "void")
        self.assertTrue(self.check_code()["ok"])

    def test_refused_code_takes_no_payment(self) -> None:
        self.subscribe()
        make_coupon(paused=True)
        response = self.submit(coupon="ZWSTARTUP15")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["reason"], "paused")
        self.assertEqual(response.json()["coupon"], ["This code is paused. Try again later."])
        self.assertFalse(ManualPayment.objects.exists())
        self.assertFalse(CouponUse.objects.exists())

    def test_yearly_refuses_a_code(self) -> None:
        self.subscribe()
        make_coupon()
        response = self.submit(coupon="ZWSTARTUP15", billing_cycle="yearly")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["reason"], "monthly_only")

    def test_no_code_unchanged(self) -> None:
        self.subscribe()
        self.assertEqual(self.submit().status_code, status.HTTP_201_CREATED)
        payment = ManualPayment.objects.get()
        self.assertEqual((payment.amount_inr, payment.due["coupon"]), (Decimal("1145.78"), None))


@override_settings(**KEYS)
class RazorpayCouponTests(CouponTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.orders = 0
        patcher = mock.patch.object(api, "create_order", side_effect=self.fake_order)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_order(self, amount, currency, receipt, notes):  # type: ignore[no-untyped-def]
        self.orders += 1
        return {"id": f"order_{self.orders}", "amount": amount, "currency": currency}

    def pay(self, order: RazorpayOrder, payment_id: str = "pay_1"):  # type: ignore[no-untyped-def]
        info = {"id": payment_id, "order_id": order.order_id, "amount": order.amount_paise, "currency": "INR",
                "status": "captured"}
        with mock.patch.object(api, "fetch_payment", return_value=info):
            return self.post(VERIFY_URL, {"razorpay_order_id": order.order_id, "razorpay_payment_id": payment_id,
                                          "razorpay_signature": sign(f"{order.order_id}|{payment_id}")})

    def test_paid_online_with_a_code(self) -> None:
        self.subscribe()
        make_coupon()
        response = self.post(ORDER_URL, {"accept_terms": True, "coupon": "zwstartup15"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(response.json()["amount"], 97391)
        order = RazorpayOrder.objects.get()
        self.assertEqual(order.due["coupon"]["code"], "ZWSTARTUP15")
        self.assertEqual(order.coupon_use.state, "pending")
        self.assertEqual(self.pay(order).json()["status"], "paid")
        use = CouponUse.objects.get()
        self.assertEqual((use.state, use.payment.method), ("confirmed", "razorpay"))
        self.assertEqual(use.payment.invoice.data["coupon"]["code"], "ZWSTARTUP15")

    def test_a_new_checkout_replaces_the_open_one(self) -> None:
        self.subscribe()
        make_coupon()
        self.post(ORDER_URL, {"accept_terms": True, "coupon": "ZWSTARTUP15"})
        self.assertTrue(self.check_code()["ok"])  # an open checkout doesn't block another try
        again = self.post(ORDER_URL, {"accept_terms": True, "coupon": "ZWSTARTUP15"})
        self.assertEqual(again.status_code, status.HTTP_201_CREATED, again.content)
        self.assertEqual(sorted(CouponUse.objects.values_list("state", flat=True)), ["pending", "void"])

    def test_never_with_an_offer(self) -> None:
        self.subscribe()
        make_coupon()
        offer = Announcement.objects.create(
            title="Try Businesses", kind="offer", audience="owners", owner_emails=[self.owner.email],
            offer_plan=Plan.objects.get(code="businesses"), offer_price_inr=Decimal("1.00"),
        )
        response = self.post(ORDER_URL, {"accept_terms": True, "offer": offer.pk, "coupon": "ZWSTARTUP15"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["reason"], "offer")
        self.assertFalse(RazorpayOrder.objects.exists())

    def test_invalid_code_refused(self) -> None:
        self.subscribe()
        response = self.post(ORDER_URL, {"accept_terms": True, "coupon": "ZWNOSUCHONE"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"detail": coupons.MESSAGES["not_found"], "reason": "not_found"})

    def test_abandoned_checkout_frees_the_code(self) -> None:
        self.subscribe()
        make_coupon()
        self.post(ORDER_URL, {"accept_terms": True, "coupon": "ZWSTARTUP15"})
        RazorpayOrder.objects.update(created_at=timezone.now() - timedelta(hours=25))
        with mock.patch.object(api, "order_payments", return_value=[]):
            self.assertEqual(reconcile().expired, 1)
        self.assertEqual(CouponUse.objects.get().state, "void")


class StaffCouponTests(CouponTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_authenticate(self.staff)

    def test_owners_are_forbidden(self) -> None:
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(ADMIN).status_code, status.HTTP_403_FORBIDDEN)

    def test_create_list_and_totals(self) -> None:
        response = self.client.post(ADMIN, {
            "code": "zwstartup15", "percent_off": 15, "plan_codes": ["startups"], "starts_on": "2026-11-01",
            "ends_on": "2099-12-15", "max_uses": 100, "channel": "whatsapp", "campaign": "IT Is OK · Stage 1",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        row = response.json()
        self.assertEqual((row["code"], row["status"], row["uses"], row["paid_inr"], row["created_by"]),
                         ("ZWSTARTUP15", "live", 0, "0.00", "ops@zinwe.local"))
        self.assertEqual(Event.objects.filter(name="admin.coupon_created", actor_email="ops@zinwe.local").count(), 1)
        coupon = Coupon.objects.get()
        self.other_company_use(coupon)
        self.other_company_use(coupon, state="pending")
        body = self.client.get(ADMIN).json()
        self.assertEqual(body["coupons"][0]["uses"], 2)
        self.assertEqual(body["coupons"][0]["paid_inr"], "825.35")
        self.assertEqual(body["totals"], {
            "live": 1, "uses": 2, "paid_inr": "825.35",
            "best": {"code": "ZWSTARTUP15", "uses": 2, "channel": "whatsapp", "campaign": "IT Is OK · Stage 1"},
        })
        codes = [p["code"] for p in body["plans"]]
        self.assertIn("startups", codes)
        self.assertNotIn("free", codes)
        self.assertEqual(set(body["plans"][0]), {"code", "name", "price_inr_monthly", "band_min_inr", "pcs"})
        uses = self.client.get(f"{ADMIN}{coupon.pk}/uses/").json()["uses"]
        self.assertEqual(len(uses), 2)
        self.assertEqual(set(uses[0]), {"company", "owner_email", "plan_code", "plan_name", "cycle", "state",
                                        "amount_inr", "amount_off", "created_at", "subscriber_id"})

    def test_validation(self) -> None:
        make_coupon("ZWTAKEN0001")
        cases = [
            ({"code": "ZW-SHORT", "percent_off": 10}, "code"),
            ({"code": "zwtaken0001", "percent_off": 10}, "code"),
            ({"code": "ZWOKCODE001", "percent_off": 41}, "percent_off"),
            ({"code": "ZWOKCODE001", "percent_off": 0}, "percent_off"),
            ({"code": "ZWOKCODE001", "percent_off": 10, "starts_on": "2026-12-10", "ends_on": "2026-12-01"}, "ends_on"),
            ({"code": "ZWOKCODE001", "percent_off": 10, "plan_codes": ["gold"]}, "plan_codes"),
        ]
        for data, field in cases:
            response = self.client.post(ADMIN, data, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, data)
            self.assertIn(field, response.json(), (data, response.json()))

    def test_patch_pause_and_code_lock(self) -> None:
        coupon = make_coupon()
        response = self.client.patch(f"{ADMIN}{coupon.pk}/", {"paused": True}, format="json")
        self.assertEqual(response.json()["status"], "paused")
        self.assertEqual(self.client.patch(f"{ADMIN}{coupon.pk}/", {"code": "ZWNEWCODE01"}, format="json").status_code, 200)
        self.other_company_use(Coupon.objects.get())
        refused = self.client.patch(f"{ADMIN}{coupon.pk}/", {"code": "ZWNEWCODE02"}, format="json")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("code", refused.json())
        self.assertEqual(self.client.delete(f"{ADMIN}{coupon.pk}/").status_code, status.HTTP_409_CONFLICT)
        cancelled = self.client.patch(f"{ADMIN}{coupon.pk}/", {"cancelled": True}, format="json").json()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(Event.objects.filter(name="admin.coupon_updated").exists())

    def test_unused_can_be_deleted(self) -> None:
        coupon = make_coupon()
        self.assertEqual(self.client.delete(f"{ADMIN}{coupon.pk}/").status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Coupon.objects.exists())

    def test_suggest(self) -> None:
        code = self.client.get(f"{ADMIN}suggest/").json()["code"]
        self.assertRegex(code, r"^ZW[A-Z0-9]{9}$")
        self.assertTrue(self.client.get(f"{ADMIN}suggest/?percent=25").json()["code"].endswith("25"))

    def test_payment_lists_show_the_code(self) -> None:
        self.client.force_authenticate(self.owner)
        self.subscribe()
        make_coupon()
        self.submit(coupon="ZWSTARTUP15")
        self.client.force_authenticate(self.staff)
        rows = self.client.get("/api/admin/payments/").json()
        rows = rows if isinstance(rows, list) else rows.get("results", rows.get("rows", []))
        self.assertEqual(rows[0]["coupon_code"], "ZWSTARTUP15")
        subs = self.client.get("/api/admin/subscriptions/").json()
        self.assertEqual(subs[0]["coupon_code"], "ZWSTARTUP15")
        self.assertEqual(subs[0]["latest_payment"]["coupon_code"], "ZWSTARTUP15")
