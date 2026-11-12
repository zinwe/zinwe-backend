"""
Move the old WhatsApp campaigns onto step-by-step paths, safely (z_marketing/wa_paths_convert.py).

    manage.py wa_paths --check               read-only: build each slots campaign's path, place every contact on it, and
                                             compare the next message and its day under both engines; fails on any difference
    manage.py wa_paths --convert             the same, then switches only the campaigns with no differences
    manage.py wa_paths --revert <id>         puts a campaign back on the slots engine
    --campaign <id>                          only that campaign (with --check or --convert)
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from z_marketing import wa_path_model, wa_paths_convert as conv
from z_marketing.models import WaCampaign


class Command(BaseCommand):
    help = "Check, convert or revert WhatsApp campaigns between the slots engine and step-by-step paths."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--check", action="store_true", help="Read-only: compare both engines for every contact.")
        parser.add_argument("--convert", action="store_true", help="Check, then switch the campaigns with no differences.")
        parser.add_argument("--revert", type=int, metavar="ID", help="Put this campaign back on the slots engine.")
        parser.add_argument("--campaign", type=int, metavar="ID", help="Only this campaign.")

    def handle(self, *args: Any, **opts: Any) -> None:
        picked = [opts["check"], opts["convert"], opts["revert"] is not None]
        if sum(picked) != 1:
            raise CommandError("Say what to do: --check, --convert or --revert <id>.")
        if opts["revert"] is not None:
            c = WaCampaign.objects.filter(pk=opts["revert"]).first()
            if c is None:
                raise CommandError(f"There is no campaign {opts['revert']}.")
            if c.engine == WaCampaign.Engine.SLOTS:
                self.stdout.write(f"Campaign {c.pk} \"{c.name}\" is already on slots.")
                return
            conv.revert(c)
            self.stdout.write(f"Campaign {c.pk} \"{c.name}\" is back on slots. Its path is kept; the slots code runs it again.")
            return
        campaigns = WaCampaign.objects.filter(engine=WaCampaign.Engine.SLOTS).order_by("id")
        if opts["campaign"]:
            campaigns = campaigns.filter(pk=opts["campaign"])
            if not campaigns.exists():
                raise CommandError(f"Campaign {opts['campaign']} isn't on slots (or doesn't exist).")
        items = list(campaigns)
        total_diff = 0
        ready = converted = 0
        for c in items:
            report = conv.check(c)
            diff = report.differences
            total_diff += len(diff)
            s = wa_path_model.summary(report.path)
            self.stdout.write(
                f"Campaign {c.pk} \"{c.name}\" ({c.plan}, {c.status}): {len(report.rows)} contacts, "
                f"{len(report.rows) - len(diff)} same, {len(diff)} different. Path: {s['steps']} steps, {s['days']} days, {s['lanes']} lanes."
            )
            for row in report.rows:
                mark = "  same" if row.same else "  DIFF"
                where = f"{row.place.lane}/{row.place.step_id}" if row.place else "-"
                line = (f"{mark} #{row.contact.pk} {row.contact.firm} ({row.contact.phone}) at {where}: "
                        f"slots -> {conv.describe(row.slots)} | path -> {conv.describe(row.path)}")
                if row.problem:
                    line += f" [{row.problem}]"
                if not row.same or opts["verbosity"] >= 2:
                    self.stdout.write(line)
            if not diff:
                ready += 1
                if opts["convert"]:
                    n = conv.convert(report)
                    converted += 1
                    self.stdout.write(f"  Converted: campaign {c.pk} now runs on its path ({n} contacts placed).")
            elif opts["convert"]:
                self.stdout.write(f"  Kept on slots: {len(diff)} differences.")
        self.stdout.write(
            f"Total: {len(items)} campaigns on slots checked, {ready} with no differences"
            + (f", {converted} converted" if opts["convert"] else "") + f", {total_diff} differences."
        )
        if total_diff:
            raise CommandError(f"{total_diff} differences: those campaigns stay on slots.")
