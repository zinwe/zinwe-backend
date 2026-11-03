"""First messages gain {{HowWeKnow}} after the hello, as in the sheet's WhatsApp Sequence ("Vishal from Zinwe here.
{{HowWeKnow}}."). It fills from the contact's own data: "I found you on Google Maps, where you're rated 4.8."
Templates that already have it, or that don't open with Vishal's hello, are left alone."""
from __future__ import annotations

import re

from django.db import migrations

HELLO = re.compile(r"(Vishal from Zinwe[^.\n]*\.)")


def add(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    for t in WaTemplate.objects.filter(kind="first").exclude(text__contains="{{HowWeKnow}}"):
        text, n = HELLO.subn(r"\1 {{HowWeKnow}}", t.text, count=1)
        if n:
            t.text = text
            t.save(update_fields=["text"])


def remove(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    for t in WaTemplate.objects.filter(text__contains=" {{HowWeKnow}}"):
        t.text = t.text.replace(" {{HowWeKnow}}", "")
        t.save(update_fields=["text"])


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0014_wa_contact_opt_in")]

    operations = [migrations.RunPython(add, remove)]
