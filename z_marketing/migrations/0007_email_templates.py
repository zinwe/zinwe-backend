"""The generic email library, starting with the 12 emails of the first campaign. Campaigns keep their own copies."""
from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion

from z_marketing import seed


def fill(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    EmailTemplate = apps.get_model("z_marketing", "EmailTemplate")
    EmailTemplate.objects.bulk_create(
        EmailTemplate(name=seed.TEMPLATE_NAMES[n], kind=stage, subject=subject, body=body, step=n)
        for n, stage, _when, _week, subject, body in seed.EMAILS
    )


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0006_campaign_pattern")]

    operations = [
        migrations.CreateModel(
            name="EmailTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                (
                    "kind",
                    models.CharField(
                        choices=[("Cold", "Cold"), ("Setup", "Setup"), ("Tips", "Tips"), ("Final", "Final")],
                        default="Tips",
                        max_length=12,
                    ),
                ),
                ("subject", models.CharField(max_length=200)),
                ("body", models.TextField()),
                ("step", models.PositiveSmallIntegerField(blank=True, null=True, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.AddField(
            model_name="campaignemail",
            name="template",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uses", to="z_marketing.emailtemplate"),
        ),
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
