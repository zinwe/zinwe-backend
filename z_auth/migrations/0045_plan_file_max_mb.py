# v1.5 round 2: the largest file each plan may share in chat, in MB (Free 1 GB, Startups 2 GB, Teams 3 GB,
# Businesses 5 GB). Any other plan keeps the 1 GB default.

from django.db import migrations, models

SIZES_MB = {"free": 1024, "startups": 2048, "teams": 3072, "businesses": 5120}


def set_sizes(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    for code, mb in SIZES_MB.items():
        Plan.objects.filter(code=code).update(file_max_mb=mb)


class Migration(migrations.Migration):

    dependencies = [
        ("z_auth", "0044_plan_chat_history_days"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="file_max_mb",
            field=models.PositiveIntegerField(
                db_default=1024, default=1024, verbose_name="file sharing, max size (MB)",
                help_text="The largest file anyone may share in chat, in MB (1024 = 1 GB).",
            ),
        ),
        migrations.RunPython(set_sizes, migrations.RunPython.noop),
    ]
