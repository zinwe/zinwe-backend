"""
100 Reviews campaign (owner, 10 Nov 2026): message categories, the Track (Data Privacy / Team Activity Monitoring /
Both Pack), the contact's campaign status and steps, and the template each message came from.

Old tracks are replaced: data -> privacy, day -> activity. The Team chat track is retired: its after-YES templates
are archived (kept for history, never sent) and contacts on it go back to Unassigned. Existing contacts get the
campaign status that matches their status.
"""
import django.db.models.deletion
from django.db import migrations, models


TRACKS = {"data": "privacy", "day": "activity"}
STATUS = {"messaged": "messaged", "replied": "replied", "notnow": "replied", "interested": "yes", "signed": "yes", "stop": "opted_out"}


def forward(apps, schema_editor):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    WaContact = apps.get_model("z_marketing", "WaContact")
    for old, new in TRACKS.items():
        WaTemplate.objects.filter(track=old).update(track=new)
        WaContact.objects.filter(track=old).update(track=new)
    WaTemplate.objects.filter(track="chat").update(track="", archived=True)
    WaContact.objects.filter(track="chat").update(track="")
    for old, new in STATUS.items():
        WaContact.objects.filter(status=old).update(campaign_status=new)


def backward(apps, schema_editor):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    WaContact = apps.get_model("z_marketing", "WaContact")
    WaTemplate.objects.filter(archived=True, track="").update(track="chat")
    for old, new in TRACKS.items():
        WaTemplate.objects.filter(track=new).update(track=old)
        WaContact.objects.filter(track=new).update(track=old)
    WaTemplate.objects.filter(track="both").update(track="")
    WaContact.objects.filter(track__in=["privacy", "activity", "both"]).update(track="")


class Migration(migrations.Migration):

    dependencies = [
        ('z_auth', '0045_plan_file_max_mb'),
        ('z_marketing', '0021_wa_hinglish_templates'),
    ]

    operations = [
        migrations.AddField(
            model_name='wacampaign',
            name='plan',
            field=models.CharField(choices=[('touches', 'First message + up to 3 touches'), ('ninety', '90 days: Outreach templates by day, then the YES path')], default='touches', max_length=8),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='campaign_status',
            field=models.CharField(blank=True, choices=[('', 'Not started'), ('messaged', 'Messaged'), ('replied', 'Replied'), ('yes', 'YES'), ('engaged', 'Engaged'), ('track_assigned', 'Track assigned'), ('demo_video_sent', 'Demo video sent'), ('setup', 'Setup'), ('demo_call', 'Demo call'), ('offer', 'Offer'), ('paid', 'Paid'), ('no_reply', 'No reply'), ('opted_out', 'Opted out')], db_index=True, default='', max_length=16),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='demo_call_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='demo_step',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='engage_step',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='offer_sent',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='out_day',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='pack',
            field=models.CharField(blank=True, choices=[('', 'Not picked'), ('startup', 'Startup pack'), ('team', 'Team pack')], default='', max_length=8),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='setup_error',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='setup_step',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='testimonial',
            field=models.CharField(blank=True, choices=[('', 'Not asked'), ('written_asked', 'Written: asked'), ('written', 'Written: got it'), ('video_asked', 'Video: asked'), ('video', 'Video: got it')], default='', max_length=14),
        ),
        migrations.AddField(
            model_name='wacontact',
            name='using_since',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wamessage',
            name='template',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sends', to='z_marketing.watemplate'),
        ),
        migrations.AddField(
            model_name='watemplate',
            name='archived',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='watemplate',
            name='category',
            field=models.CharField(blank=True, choices=[('', 'Older template'), ('outreach', 'Outreach'), ('engage', 'Engage'), ('demo_video', 'Demo video'), ('setup', 'Setup'), ('demo_call', 'Demo call'), ('offer', 'Offer & discount'), ('testimonial', 'Testimonial'), ('quick_reply', 'Quick reply')], db_index=True, default='', max_length=12),
        ),
        migrations.AddField(
            model_name='watemplate',
            name='coupon',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='wa_templates', to='z_auth.coupon'),
        ),
        migrations.AddField(
            model_name='watemplate',
            name='event',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='watemplate',
            name='stage',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='watemplate',
            name='step',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='watemplate',
            name='text_hi',
            field=models.TextField(blank=True, max_length=1000),
        ),
        migrations.AddField(
            model_name='watemplate',
            name='type_name',
            field=models.CharField(blank=True, max_length=60),
        ),
        migrations.AlterField(
            model_name='wacontact',
            name='track',
            field=models.CharField(blank=True, choices=[('', 'Unassigned'), ('privacy', 'Data Privacy'), ('activity', 'Team Activity Monitoring'), ('both', 'Both Pack')], default='', max_length=8),
        ),
        migrations.AlterField(
            model_name='wamessage',
            name='kind',
            field=models.CharField(choices=[('first', 'First message'), ('followup', 'Follow-up'), ('touch', 'No-reply touch'), ('nurture', 'After-YES message'), ('flow', 'Campaign message'), ('custom', 'One-off message'), ('reply', 'Our reply'), ('theirs', 'Their message')], max_length=10),
        ),
        migrations.AlterField(
            model_name='watemplate',
            name='text',
            field=models.TextField(blank=True, max_length=1000),
        ),
        migrations.AlterField(
            model_name='watemplate',
            name='track',
            field=models.CharField(blank=True, choices=[('', 'Unassigned'), ('privacy', 'Data Privacy'), ('activity', 'Team Activity Monitoring'), ('both', 'Both Pack')], default='', max_length=8),
        ),
        migrations.RunPython(forward, backward),
    ]
