"""
The 100 Reviews WhatsApp campaign's starting rows (z_marketing/wa_seed.py).

    manage.py seed_100_reviews --templates   the 86 template rows, as drafts (production only)
    manage.py seed_100_reviews --coupons     the 8 coupon codes, linked to the Offer rows (production only)
    manage.py seed_100_reviews --sample      dev and local: the drafts, test copy on a few, one test code

Real rows go to production only (owner, 10 Nov 2026). Running it again keeps what exists.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from z_marketing import wa_seed


class Command(BaseCommand):
    help = "Seed the 100 Reviews WhatsApp campaign: --templates and --coupons on prod, --sample on dev and local."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--templates", action="store_true", help="The 86 template rows as drafts (prod only).")
        parser.add_argument("--coupons", action="store_true", help="The 8 coupon codes (prod only).")
        parser.add_argument("--sample", action="store_true", help="Test rows for dev and local (never prod).")

    def handle(self, *args: Any, **opts: Any) -> None:
        env = getattr(settings, "ZINWE_ENV", "") or "local"
        if not (opts["templates"] or opts["coupons"] or opts["sample"]):
            raise CommandError("Say what to seed: --templates, --coupons (prod) or --sample (dev, local).")
        if (opts["templates"] or opts["coupons"]) and env != "prod":
            raise CommandError(f"The real rows go to production only; this is {env}. Use --sample here.")
        if opts["sample"] and env == "prod":
            raise CommandError("--sample is for dev and local only.")
        if opts["templates"]:
            self.stdout.write(f"Templates: {wa_seed.templates()}")
        if opts["coupons"]:
            self.stdout.write(f"Coupons: {wa_seed.coupons()}")
        if opts["sample"]:
            self.stdout.write(f"Sample: {wa_seed.sample()}")
