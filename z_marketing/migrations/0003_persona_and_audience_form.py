"""Add Persona and the new target-audience fields. Filled in 0004 and tidied in 0005, each in its own transaction:
PostgreSQL refuses to ALTER a table in the same transaction that just updated its foreign keys."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0002_first_campaign")]

    operations = [
        migrations.CreateModel(
            name="Persona",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                (
                    "confidence",
                    models.CharField(
                        choices=[("Guess", "Guess"), ("Heard once", "Heard once"), ("Confirmed", "Confirmed")],
                        default="Guess",
                        max_length=12,
                    ),
                ),
                ("profile", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.AddField(
            model_name="audience",
            name="persona",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="audiences", to="z_marketing.persona"),
        ),
        migrations.AddField(model_name="audience", name="state", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="audience", name="city", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="audience", name="business_category", field=models.CharField(default="", max_length=120), preserve_default=False),
        migrations.AddField(model_name="audience", name="business_segment", field=models.CharField(default="", max_length=120), preserve_default=False),
        migrations.AddField(
            model_name="audience",
            name="business_type",
            field=models.CharField(choices=[("B2B", "B2B"), ("B2C", "B2C"), ("Both", "Both")], default="B2B", max_length=4),
        ),
        migrations.AddField(model_name="audience", name="primary_problem", field=models.CharField(default="", max_length=240), preserve_default=False),
        migrations.AddField(model_name="audience", name="secondary_problem", field=models.CharField(blank=True, max_length=240)),
        migrations.AddField(model_name="audience", name="product_service", field=models.CharField(default="", max_length=120), preserve_default=False),
        migrations.AddField(model_name="audience", name="how_solves", field=models.TextField(default=""), preserve_default=False),
        migrations.AddField(model_name="audience", name="key_benefits", field=models.JSONField(default=list)),
        migrations.AddField(model_name="audience", name="primary_offer", field=models.CharField(default="", max_length=240), preserve_default=False),
        migrations.AddField(model_name="audience", name="pricing", field=models.CharField(blank=True, max_length=240)),
        migrations.AddField(
            model_name="audience",
            name="objective",
            field=models.CharField(
                choices=[("Leads", "Leads"), ("Sales", "Sales"), ("Enquiries", "Enquiries"), ("Brand Awareness", "Brand Awareness")],
                default="Leads",
                max_length=16,
            ),
        ),
        migrations.AddField(model_name="audience", name="cta", field=models.CharField(default="", max_length=120), preserve_default=False),
        migrations.AddField(model_name="audience", name="job_titles", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="audience", name="decision_makers", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="audience", name="industry_keywords", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="audience", name="competitors", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="audience", name="notes", field=models.TextField(blank=True)),
    ]
