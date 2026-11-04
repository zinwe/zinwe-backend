"""The admin app's Plans & pricing page: prices by region (India / International) and cycle (monthly / yearly)."""
from __future__ import annotations

from rest_framework import status

from z_auth.models import Plan

from .tests import AdminApiTestCase

URL = "/api/admin/pricing/"


class PricingTests(AdminApiTestCase):
    def test_overview(self) -> None:
        body = self.client.get(URL).json()
        plans = {p["code"]: p for p in body["plans"]}
        self.assertEqual(list(plans), ["free", "startups", "teams", "businesses"])
        inr = plans["startups"]["regions"]["INR"]
        self.assertEqual(inr["monthly"], {"list": "1349.00", "price": "971.00", "discount_percent": 28})
        self.assertEqual((inr["yearly"]["price"], inr["yearly"]["custom"], inr["yearly"]["saving_percent"]), ("9710.00", True, 17))
        self.assertEqual(inr["yearly"]["months_charged"], "10.0")
        self.assertEqual(plans["startups"]["regions"]["USD"]["monthly"]["discount_percent"], 25)
        self.assertTrue(plans["free"]["is_free"])

    def test_change_prices(self) -> None:
        response = self.client.patch(
            f"{URL}startups/",
            {
                "INR": {"list_monthly": "1250", "discount_percent": 20, "yearly_price": "12000"},
                "USD": {"list_monthly": "20", "discount_percent": 25, "yearly_price": None},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        plan = Plan.objects.get(code="startups")
        self.assertEqual((plan.price_inr_monthly, plan.price_inr_yearly), (1000, 12000))
        self.assertEqual((plan.price_usd_monthly, plan.list_price_usd_monthly, plan.price_usd_yearly), (15, 20, None))
        usd = response.json()["regions"]["USD"]["yearly"]
        self.assertEqual((usd["price"], usd["custom"], usd["saving_percent"]), ("180.00", False, 0))

    def test_rules(self) -> None:
        too_dear = self.client.patch(
            f"{URL}startups/", {"INR": {"list_monthly": "1000", "discount_percent": 0, "yearly_price": "12001"}}, format="json",
        )
        self.assertEqual(too_dear.status_code, status.HTTP_400_BAD_REQUEST)
        free = self.client.patch(f"{URL}free/", {"INR": {"list_monthly": "100", "discount_percent": 0}}, format="json")
        self.assertEqual(free.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Plan.objects.get(code="free").price_inr_monthly, 0)

    def test_one_recommended_plan(self) -> None:
        self.client.patch(f"{URL}teams/", {"is_recommended": True}, format="json")
        self.assertEqual(list(Plan.objects.filter(is_recommended=True).values_list("code", flat=True)), ["teams"])

    def test_staff_only(self) -> None:
        self.as_user(self.make_owner())
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_403_FORBIDDEN)


class PriceBandTests(AdminApiTestCase):  # F-12
    def test_seeded_bands_and_businesses_at_28_percent(self) -> None:
        bands = {p.code: (p.band_min_inr, p.band_max_inr) for p in Plan.objects.filter(is_active=True)}
        self.assertEqual(bands, {"free": (None, None), "startups": (900, 1000), "teams": (1799, 2000),
                                 "businesses": (2899, 3000)})
        businesses = Plan.objects.get(code="businesses")
        self.assertEqual((businesses.discount_percent, businesses.price_inr_monthly), (28, 2915))

    def test_detail_carries_the_band_and_allowed_discounts(self) -> None:
        body = self.client.get(f"{URL}businesses/").json()
        self.assertEqual((body["band_min_inr"], body["band_max_inr"]), ("2899.00", "3000.00"))
        # 4,049 list: 26% -> 2,996 ... 28% -> 2,915; 29% -> 2,875 is below the floor.
        self.assertEqual(body["allowed_discounts"], [26, 27, 28])
        free = self.client.get(f"{URL}free/").json()
        self.assertEqual((free["band_min_inr"], free["band_max_inr"], free["allowed_discounts"]), (None, None, [0]))
        overview = {p["code"]: p for p in self.client.get(URL).json()["plans"]}
        self.assertEqual(overview["startups"]["allowed_discounts"], [26, 27, 28, 29, 30, 31, 32, 33])

    def test_a_discount_outside_the_band_is_refused(self) -> None:
        response = self.client.patch(f"{URL}businesses/", {"INR": {"list_monthly": "4049", "discount_percent": 30}},
                                     format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"discount_percent": ["Puts the price at ₹2,834, outside ₹2,899–₹3,000."]})
        self.assertEqual(Plan.objects.get(code="businesses").price_inr_monthly, 2915)
        ok = self.client.patch(f"{URL}businesses/", {"INR": {"list_monthly": "4049", "discount_percent": 27}},
                               format="json")
        self.assertEqual(ok.status_code, status.HTTP_200_OK, ok.content)
        self.assertEqual(ok.json()["regions"]["INR"]["monthly"]["price"], "2956.00")

    def test_staff_change_the_band(self) -> None:
        widened = self.client.patch(f"{URL}businesses/", {"band_min_inr": "2800", "band_max_inr": "3000",
                                                          "INR": {"list_monthly": "4049", "discount_percent": 30}},
                                    format="json")
        self.assertEqual(widened.status_code, status.HTTP_200_OK, widened.content)
        self.assertEqual((widened.json()["band_min_inr"], widened.json()["regions"]["INR"]["monthly"]["price"]),
                         ("2800.00", "2834.00"))
        # Narrowing it past today's price is refused; so is an upside-down band, and a band on the free plan.
        self.assertEqual(self.client.patch(f"{URL}businesses/", {"band_min_inr": "2900"}, format="json").status_code,
                         status.HTTP_400_BAD_REQUEST)
        upside_down = self.client.patch(f"{URL}teams/", {"band_min_inr": "2000", "band_max_inr": "1799"}, format="json")
        self.assertIn("band_max_inr", upside_down.json())
        self.assertEqual(self.client.patch(f"{URL}free/", {"band_min_inr": "1"}, format="json").status_code,
                         status.HTTP_400_BAD_REQUEST)
        cleared = self.client.patch(f"{URL}teams/", {"band_min_inr": None, "band_max_inr": None}, format="json").json()
        self.assertEqual(cleared["allowed_discounts"], list(range(91)))
