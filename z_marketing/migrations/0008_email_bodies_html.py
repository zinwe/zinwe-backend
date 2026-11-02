"""Email text moves to clean HTML for the rich editor. Going back turns it into plain text again."""
from __future__ import annotations

from django.db import migrations

from z_marketing import email_html


def to_html(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    for name in ("EmailTemplate", "CampaignEmail"):
        model = apps.get_model("z_marketing", name)
        for row in model.objects.all():
            row.body = email_html.as_html(row.body)
            row.save(update_fields=["body"])


def to_plain(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    for name in ("EmailTemplate", "CampaignEmail"):
        model = apps.get_model("z_marketing", name)
        for row in model.objects.all():
            row.body = email_html.to_text(row.body)
            row.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0007_email_templates")]

    operations = [migrations.RunPython(to_html, to_plain)]
