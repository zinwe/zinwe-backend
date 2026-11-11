# The Intro category's first type was also called "Intro" ("Intro · Intro · day 1"); it is now "Opener".

from django.db import migrations


def rename(apps, schema_editor, old="Intro", new="Opener"):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    for t in WaTemplate.objects.filter(category="outreach", type_name=old):
        t.type_name = new
        t.name = t.name.replace(f" · {old} · ", f" · {new} · ", 1)
        t.save(update_fields=["type_name", "name"])


def rename_back(apps, schema_editor):
    rename(apps, schema_editor, old="Opener", new="Intro")


class Migration(migrations.Migration):

    dependencies = [
        ("z_marketing", "0024_wa_intro_label"),
    ]

    operations = [
        migrations.RunPython(rename, rename_back),
    ]
