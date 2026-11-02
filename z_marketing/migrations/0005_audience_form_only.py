"""Every audience now has a persona, and its old text lives in the new fields, so the old columns go."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0004_seed_base_persona")]

    operations = [
        migrations.AlterField(
            model_name="audience",
            name="persona",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="audiences", to="z_marketing.persona"),
        ),
        migrations.AlterField(model_name="audience", name="channels", field=models.JSONField(default=list)),
        migrations.RemoveField(model_name="audience", name="decider"),
        migrations.RemoveField(model_name="audience", name="setup"),
        migrations.RemoveField(model_name="audience", name="today"),
        migrations.RemoveField(model_name="audience", name="confidence"),
        migrations.RemoveField(model_name="audience", name="segments"),
        migrations.RemoveField(model_name="audience", name="triggers"),
        migrations.RemoveField(model_name="audience", name="words"),
        migrations.RemoveField(model_name="audience", name="objections"),
        migrations.RemoveField(model_name="audience", name="not_for"),
        migrations.RemoveField(model_name="audience", name="problems"),
        migrations.RemoveField(model_name="audience", name="solutions"),
        migrations.RemoveField(model_name="audience", name="offers"),
        migrations.RemoveField(model_name="audience", name="summary"),
    ]
