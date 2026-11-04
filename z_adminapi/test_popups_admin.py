"""The admin's Pop-ups page: per pop-up numbers (seen, views, closed, button presses) and the owner search."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_auth.models import Announcement, AnnouncementView, Company, User

ADMIN = "/api/admin/announcements/"
SEARCH = "/api/admin/owners/search/"


def popup(**fields):  # type: ignore[no-untyped-def]
    return Announcement.objects.create(**{
        "title": "Happy Diwali", "kind": "wish", "placement": "popup", "cta_label": "Say hi", "cta_href": "/app/chat",
        "starts_at": timezone.now() - timedelta(hours=1), **fields,
    })


class PopupAdminTests(APITestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        self.client.force_authenticate(self.staff)

    def owner(self, email: str, **fields) -> User:  # type: ignore[no-untyped-def]
        return User.objects.create_user(email, "pw-12345678", email_verified_at=timezone.now(), **fields)

    def seen(self, owner: User, announcement: Announcement, **body):  # type: ignore[no-untyped-def]
        token, _ = Token.objects.get_or_create(user=owner)
        self.client.force_authenticate(None)
        response = self.client.post(
            f"/api/auth/announcements/{announcement.pk}/seen/", body, format="json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        self.client.force_authenticate(self.staff)
        return response

    def test_click_is_counted_on_the_account(self) -> None:
        a = popup()
        owner = self.owner("owner@acme.in")
        self.assertEqual(self.seen(owner, a).json()["clicks"], 0)
        body = self.seen(owner, a, clicked=True, closed=True).json()
        self.assertEqual((body["views"], body["clicks"], body["closed"]), (1, 1, True))
        view = AnnouncementView.objects.get(owner=owner, announcement=a)
        self.assertIsNotNone(view.clicked_at)
        # A click alone doesn't close it, nor use up a view the same day.
        other = self.owner("b@acme.in")
        body = self.seen(other, a, clicked=True).json()
        self.assertEqual((body["views"], body["clicks"], body["closed"]), (1, 1, False))
        self.assertEqual(self.seen(other, a, clicked="yes").json()["clicks"], 1)  # only true counts

    def test_stats_per_popup_in_the_list_and_on_one_row(self) -> None:
        a, quiet = popup(), popup(title="Nobody yet")
        bar = Announcement.objects.create(title="A bar", placement="dashboard")
        one, two, three = self.owner("a@x.in"), self.owner("b@x.in"), self.owner("c@x.in")
        AnnouncementView.objects.create(owner=one, announcement=a, views=3, closed=False, clicks=2)
        AnnouncementView.objects.create(owner=two, announcement=a, views=1, closed=True)
        AnnouncementView.objects.create(owner=three, announcement=a, views=2, closed=True, clicks=1)

        rows = {r["id"]: r for r in self.client.get(ADMIN).json()}
        expected = {"owners": 3, "views": 6, "closed": 2, "clicks": 3, "clicked_owners": 2}
        self.assertEqual(rows[a.pk]["stats"], expected)
        self.assertEqual(rows[quiet.pk]["stats"], {"owners": 0, "views": 0, "closed": 0, "clicks": 0, "clicked_owners": 0})
        self.assertIsNone(rows[bar.pk]["stats"])

        only = self.client.get(ADMIN, {"placement": "popup"}).json()
        self.assertEqual({r["id"] for r in only}, {a.pk, quiet.pk})
        edited = self.client.patch(f"{ADMIN}{a.pk}/", {"title": "Happy Diwali!"}, format="json").json()
        self.assertEqual(edited["stats"], expected)

    def test_owner_search_by_email_name_or_company(self) -> None:
        acme = self.owner("ravi@acme.in", full_name="Ravi Kumar")
        Company.objects.create(owner=acme, name="Acme Tools")
        self.owner("meera@globex.in", full_name="Meera Shah")
        self.assertEqual(self.client.get(SEARCH, {"q": "a"}).json(), [])  # too short
        self.assertEqual([o["email"] for o in self.client.get(SEARCH, {"q": "acme"}).json()], ["ravi@acme.in"])
        self.assertEqual([o["email"] for o in self.client.get(SEARCH, {"q": "TOOLS"}).json()], ["ravi@acme.in"])
        hit = self.client.get(SEARCH, {"q": "meera"}).json()
        self.assertEqual(hit, [{"id": hit[0]["id"], "email": "meera@globex.in", "full_name": "Meera Shah",
                                "company": "", "plan": ""}])
        self.assertEqual(self.client.get(SEARCH, {"q": "zinwe.local"}).json(), [])  # never staff

    def test_staff_only(self) -> None:
        self.client.force_authenticate(self.owner("owner@acme.in"))
        self.assertEqual(self.client.get(SEARCH, {"q": "acme"}).status_code, status.HTTP_403_FORBIDDEN)
