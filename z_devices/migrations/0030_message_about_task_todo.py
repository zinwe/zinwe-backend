# v1.3.5 (F-17): a chat line can be about one task or one of the member's own to-dos (the task's thread).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("z_devices", "0029_report_covers_start_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="about_task",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="talk", to="z_devices.task"
            ),
        ),
        migrations.AddField(
            model_name="message",
            name="about_todo",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="talk", to="z_devices.membertodo"
            ),
        ),
    ]
