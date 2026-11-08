"""v1.4 rules: work hours by scope (company <- department <- PC), holidays, leave, day counting, per-PC protection rules."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from django.utils import timezone
from rest_framework import status

from .models import Device, Holiday, Leave, Policy, PolicyOverride, WorkLog
from .tests import CURRENT_POLICY_URL, HEARTBEAT_URL, POLICY_URL, DeviceTestCase
from .work_rules import DEFAULTS, day_status
from .work_time import DayWork

HOURS = "/api/devices/rules/hours/"
HOLIDAYS = "/api/devices/rules/holidays/"
LEAVE = "/api/devices/leave/"


class RulesCase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.member.role = "developer"
        self.member.save(update_fields=["role"])
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()
        self.as_owner()

    def beat(self) -> dict[str, Any]:
        self.as_device(self.token)
        body = self.client.post(HEARTBEAT_URL, {"agent_version": "1.4.0"}, format="json").json()
        self.as_owner()
        return body


class WorkHoursTests(RulesCase):
    def test_company_defaults_are_made_lazily(self) -> None:
        body = self.client.get(HOURS).json()
        self.assertEqual(body["scope"], "company")
        self.assertTrue(body["own"])
        self.assertIsNone(body["inherits_from"])
        self.assertEqual(body["effective"]["days"], "1111110")
        self.assertEqual(body["effective"]["start"], "10:00")
        self.assertEqual(body["effective"]["full_day_hours"], 8)
        self.assertEqual(body["fields"]["break_allowed_min"], 75)

    def test_department_and_pc_override_and_reset(self) -> None:
        r = self.client.put(f"{HOURS}?scope=department:developer", {"start": "11:00", "end": "20:00", "days": "1111100"},
                            format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["own"], body["inherits_from"]), (True, "company"))
        self.assertEqual(body["fields"]["start"], "11:00")
        self.assertIsNone(body["fields"]["late_grace_min"])
        self.assertEqual(body["effective"]["late_grace_min"], 15)
        pc = f"{HOURS}?scope=device:{self.device.pk}"
        body = self.client.get(pc).json()
        self.assertEqual((body["own"], body["inherits_from"]), (False, "department:developer"))
        self.assertEqual(body["effective"]["start"], "11:00")
        self.client.put(pc, {"start": "12:00", "break_allowed_min": 90}, format="json")
        rules = self.beat()["work_rules"]
        self.assertEqual((rules["start"], rules["end"], rules["days"], rules["break_allowed_min"]),
                         ("12:00", "20:00", "1111100", 90))
        # null = inherit again
        self.client.put(pc, {"start": None}, format="json")
        self.assertEqual(self.beat()["work_rules"]["start"], "11:00")
        self.client.delete(f"{HOURS}?scope=department:developer")
        self.client.delete(pc)
        self.assertEqual(self.beat()["work_rules"]["start"], "10:00")
        scopes = self.client.get("/api/devices/rules/scopes/").json()
        dev = next(d for d in scopes["departments"] if d["role"] == "developer")
        self.assertEqual((dev["own_hours"], dev["people"]), (False, 1))
        self.assertEqual(scopes["devices"][0]["device_id"], self.device.pk)

    def test_validation_and_bad_scope(self) -> None:
        self.assertEqual(self.client.get(f"{HOURS}?scope=department:astronaut").status_code, 400)
        self.assertEqual(self.client.get(f"{HOURS}?scope=device:99999").status_code, 400)
        for body, field in (({"start": "9am"}, "start"), ({"days": "11111"}, "days"), ({"late_grace_min": -1}, "late_grace_min"),
                            ({"full_day_hours": 30}, "full_day_hours"), ({"start": None}, "start"),
                            ({"start": "20:00", "end": "09:00"}, "end"), ({"bogus": 1}, "bogus")):
            r = self.client.put(HOURS, body, format="json")
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(field, r.json())
        self.assertEqual(self.client.get(HOURS).json()["effective"]["start"], "10:00")


class HolidayLeaveTests(RulesCase):
    def test_holidays(self) -> None:
        r = self.client.post(HOLIDAYS, {"name": "Holi", "date": "2027-03-04"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.client.post(HOLIDAYS, {"name": "Again", "date": "2027-03-04"}, format="json").json(),
                         {"date": ["That day is already a holiday."]})
        self.assertEqual(self.client.get(HOLIDAYS).json(), [{"id": r.json()["id"], "name": "Holi", "date": "2027-03-04"}])
        self.assertEqual(self.client.delete(f"{HOLIDAYS}{r.json()['id']}/").status_code, 204)
        self.assertEqual(self.client.get(HOLIDAYS).json(), [])

    def test_holiday_today_on_the_heartbeat(self) -> None:
        Holiday.objects.create(owner=self.owner, name="Diwali", date=timezone.localdate())
        rules = self.beat()["work_rules"]
        self.assertEqual((rules["holiday_today"], rules["working_today"]), ("Diwali", False))

    def test_leave_request_and_decision(self) -> None:
        day = timezone.localdate() + timedelta(days=3)
        self.as_device(self.token)
        r = self.client.post(LEAVE, {"date": day.isoformat(), "half": True, "note": "Doctor"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        leave = r.json()
        self.assertEqual((leave["status"], leave["half"], leave["member_name"]), ("requested", True, "Asha Rao"))
        self.assertEqual(len(self.client.get(LEAVE).json()), 1)
        self.as_owner()
        self.assertEqual(self.client.post(LEAVE, {"date": day.isoformat()}, format="json").status_code, 403)
        self.assertEqual([x["id"] for x in self.client.get(LEAVE).json()], [leave["id"]])
        r = self.client.post(f"{LEAVE}{leave['id']}/decision/", {"decision": "approve"}, format="json")
        self.assertEqual(r.json()["status"], "approved")
        self.assertEqual(self.client.get(LEAVE).json(), [])
        self.assertEqual(len(self.client.get(f"{LEAVE}?status=approved").json()), 1)
        self.assertEqual(self.client.post(f"{LEAVE}{leave['id']}/decision/", {"decision": "maybe"}, format="json").status_code, 400)
        # Asking again for an approved day is refused.
        self.as_device(self.token)
        r = self.client.post(LEAVE, {"date": day.isoformat()}, format="json")
        self.assertEqual(r.json(), {"date": ["That day is already approved."]})

    def test_approved_leave_today_on_the_heartbeat(self) -> None:
        Leave.objects.create(owner=self.owner, member=self.member, date=timezone.localdate(), status="approved")
        rules = self.beat()["work_rules"]
        self.assertEqual((rules["leave_today"], rules["working_today"]), ("full", False))


def _work(worked_h: float, start: tuple[int, int] | None, end: tuple[int, int] | None, day: date,
          break_min: int = 0) -> DayWork:
    tz = timezone.get_current_timezone()
    mk = lambda hm: timezone.make_aware(datetime.combine(day, datetime.min.time()) + timedelta(hours=hm[0], minutes=hm[1]), tz)  # noqa: E731
    return DayWork(worked=int(worked_h * 3600), on_break=break_min * 60, logged_in=start is not None,
                   first_in=mk(start) if start else None, last_out=mk(end) if end else None, has_entries=True)


class DayStatusTests(RulesCase):
    rules = {**DEFAULTS, "start": "10:00", "end": "19:00"}

    def status_of(self, work: DayWork | None, day: date, holiday: str | None = None, leave: Leave | None = None):  # type: ignore[no-untyped-def]
        now = timezone.make_aware(datetime.combine(day + timedelta(days=2), datetime.min.time()) + timedelta(hours=12))
        return day_status(self.rules, day, work, holiday, leave, timezone.get_current_timezone(), now)

    def test_each_status(self) -> None:
        monday = date(2026, 11, 2)
        sunday = date(2026, 11, 8)
        self.assertEqual(self.status_of(_work(8.5, (10, 0), (19, 0), monday), monday)["status"], "full")
        row = self.status_of(_work(8.5, (10, 20), (19, 30), monday), monday)
        self.assertEqual((row["status"], row["late"]), ("full", True))
        self.assertEqual(self.status_of(_work(5, (10, 0), (15, 0), monday), monday)["status"], "half")
        self.assertEqual(self.status_of(_work(8, (13, 30), (22, 0), monday), monday)["status"], "half")  # late start
        self.assertEqual(self.status_of(_work(8.5, (5, 0), (13, 30), monday), monday)["status"], "half")  # early end
        self.assertEqual(self.status_of(_work(2, (10, 0), (12, 0), monday), monday)["status"], "absent")
        self.assertEqual(self.status_of(None, monday)["status"], "absent")
        self.assertEqual(self.status_of(None, sunday)["status"], "off")
        self.assertEqual(self.status_of(None, monday, holiday="Diwali")["status"], "holiday")
        full_leave = Leave(member=self.member, date=monday, half=False, status="approved")
        self.assertEqual(self.status_of(None, monday, leave=full_leave)["status"], "leave")
        half_leave = Leave(member=self.member, date=monday, half=True, status="approved")
        self.assertEqual(self.status_of(_work(4, (14, 0), (18, 0), monday), monday, leave=half_leave)["status"], "half")
        self.assertTrue(self.status_of(_work(8, (10, 0), (19, 0), monday, break_min=90), monday)["break_over"])

    def test_report_attendance(self) -> None:
        today = timezone.localdate()
        day = today - timedelta(days=1)
        while day.weekday() == 6:
            day -= timedelta(days=1)
        at = lambda h, m=0: timezone.make_aware(datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=m))  # noqa: E731
        for state, when in (("working", at(10, 30)), ("on_break", at(13)), ("working", at(13, 30)), ("ended", at(19, 30))):
            WorkLog.objects.create(owner=self.owner, device=self.device, member=self.member, state=state, at=when)
        body = self.client.get("/api/devices/reports/", {"from": day.isoformat(), "to": day.isoformat()}).json()
        person = body["attendance"]["people"][0]
        self.assertEqual(person["member_id"], self.member.id)
        self.assertEqual(person["days"], [{"date": day.isoformat(), "status": "full", "late": True, "worked_min": 510,
                                           "break_min": 30, "break_over": False}])
        self.assertEqual(person["counts"]["full"], 1)
        self.assertEqual(body["attendance"]["totals"]["late"], 1)
        one = self.client.get("/api/devices/reports/", {"from": day.isoformat(), "to": day.isoformat(),
                                                        "member": self.member.id}).json()
        self.assertEqual((one["person_days"][0]["status"], one["person_days"][0]["late"]), ("full", True))


class PolicyScopeTests(RulesCase):
    def usb_body(self, mode: str) -> dict[str, Any]:
        return {"usb": {"mode": mode}}

    def device_policy(self) -> dict[str, Any]:
        self.as_device(self.token)
        body = self.client.get(CURRENT_POLICY_URL).json()
        self.as_owner()
        return body

    def test_override_for_a_department_and_a_pc(self) -> None:
        version = Policy.objects.get_or_create(owner=self.owner)[0].version
        r = self.client.patch(f"{POLICY_URL}?scope=department:developer", self.usb_body("read_only"), format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["usb"]["mode"], body["scope"], body["own"], body["own_fields"]),
                         ("read_only", "department:developer", True, ["usb.mode"]))
        self.assertEqual(Policy.objects.get(owner=self.owner).version, version + 1)
        self.assertEqual(self.device_policy()["usb"]["mode"], "read_only")
        # The company rules are unchanged; plain GET keeps its old shape.
        plain = self.client.get(POLICY_URL).json()
        self.assertEqual(plain["usb"]["mode"], "allow")
        self.assertNotIn("scope", plain)
        # The PC overrides the department.
        pc = f"{POLICY_URL}?scope=device:{self.device.pk}"
        self.assertFalse(self.client.get(pc).json()["own"])
        self.client.patch(pc, self.usb_body("block"), format="json")
        self.assertEqual(self.device_policy()["usb"]["mode"], "block")
        # Setting the PC back to its department's value drops its own rule.
        self.client.patch(pc, self.usb_body("read_only"), format="json")
        self.assertFalse(PolicyOverride.objects.filter(scope_kind="device").exists())
        # Reset the department.
        r = self.client.delete(f"{POLICY_URL}?scope=department:developer")
        self.assertEqual((r.json()["usb"]["mode"], r.json()["own"]), ("allow", False))
        self.assertEqual(self.device_policy()["usb"]["mode"], "allow")
        self.assertEqual(self.client.delete(POLICY_URL).status_code, 400)  # the company can't be "reset"

    def test_other_departments_are_untouched(self) -> None:
        self.client.patch(f"{POLICY_URL}?scope=department:sales", self.usb_body("block"), format="json")
        self.assertEqual(self.device_policy()["usb"]["mode"], "allow")

    def test_version_moves_on_the_heartbeat(self) -> None:
        before = self.beat()["policy_version"]
        self.client.patch(f"{POLICY_URL}?scope=device:{self.device.pk}", self.usb_body("read_only"), format="json")
        self.assertNotEqual(self.beat()["policy_version"], before)

    def test_company_patch_with_scope_is_the_old_put(self) -> None:
        r = self.client.patch(f"{POLICY_URL}?scope=company", self.usb_body("read_only"), format="json")
        self.assertEqual((r.json()["usb"]["mode"], r.json()["scope"]), ("read_only", "company"))
        self.assertEqual(Policy.objects.get(owner=self.owner).usb_mode, "read_only")

    def test_plan_still_clamps_an_override(self) -> None:
        from z_auth.models import Plan

        self.client.patch(f"{POLICY_URL}?scope=device:{self.device.pk}", self.usb_body("block"), format="json")
        Plan.objects.filter(code="businesses").update(modules=["usb_alerts"])
        self.assertEqual(self.device_policy()["usb"]["mode"], "allow")
        self.assertEqual(
            self.client.patch(f"{POLICY_URL}?scope=device:{self.device.pk}", self.usb_body("block"), format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )
