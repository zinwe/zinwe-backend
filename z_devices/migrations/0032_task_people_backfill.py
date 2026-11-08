# v1.4: every task gets its people. A task from before v1.4 has one person (Task.member); their seen / started /
# done times come from the task's activity, else from its status.

from django.db import migrations

SEEN = ("seen", "accepted", "progress", "help", "review", "approved")
STARTED = ("progress", "help", "review", "approved")
DONE = ("review", "approved")


def backfill(apps, schema_editor):
    Task = apps.get_model("z_devices", "Task")
    TaskPerson = apps.get_model("z_devices", "TaskPerson")
    TaskActivity = apps.get_model("z_devices", "TaskActivity")
    have = set(TaskPerson.objects.values_list("task_id", flat=True))
    rows = []
    for task in Task.objects.exclude(pk__in=have).iterator():
        firsts = {}
        for action, at in TaskActivity.objects.filter(task_id=task.pk, by="member").order_by("at").values_list("action", "at"):
            firsts.setdefault(action, at)
        status = task.status
        if status == "cancelled":
            status = "seen" if firsts else "sent"
        seen = firsts.get("seen") or firsts.get("accepted") or firsts.get("started") or (
            task.status_changed_at if status in SEEN else None
        )
        started = firsts.get("started") or (task.status_changed_at if status in STARTED else None)
        done = firsts.get("done") or (task.status_changed_at if status in DONE else None)
        rows.append(TaskPerson(
            task_id=task.pk, member_id=task.member_id, position=0, seen_at=seen, started_at=started, done_at=done,
            flag="help" if task.status == "help" else "", flag_note=task.help_note if task.status == "help" else "",
        ))
        if len(rows) >= 500:
            TaskPerson.objects.bulk_create(rows)
            rows = []
    if rows:
        TaskPerson.objects.bulk_create(rows)


class Migration(migrations.Migration):

    dependencies = [
        ("z_devices", "0031_team_tasks_rules"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
