# v1.4: how many days of team chat each plan keeps (Free 30, Startups 90, Teams 180, Businesses 365).

from django.db import migrations, models

DAYS = {"free": 30, "startups": 90, "teams": 180, "businesses": 365}


def set_days(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    for code, days in DAYS.items():
        Plan.objects.filter(code=code).update(chat_history_days=days)


class Migration(migrations.Migration):

    dependencies = [
        ("z_auth", "0043_user_interactions"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="chat_history_days",
            field=models.PositiveIntegerField(
                db_default=0, default=0,
                help_text="Days of team chat kept (0 = no limit). Older lines are hidden at once and deleted 30 days later.",
            ),
        ),
        migrations.RunPython(set_days, migrations.RunPython.noop),
    ]
