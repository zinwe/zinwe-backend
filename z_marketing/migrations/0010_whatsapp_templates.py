"""Starter WhatsApp templates: one of each kind for traders and clinics. No contacts and no campaigns."""
from django.db import migrations

TEMPLATES = [
    ("Pen drive check, traders", "first",
     "Hi {{FirstName}}, this is Vishal from Zinwe in New Delhi.\n\nQuick question for {{Firm}}: if a staff member copied "
     "your customer list to a pen drive today, would you know?\n\nZinwe tells you in plain English when files leave a PC. "
     "Free on 3 PCs.\n\nReply YES and I'll send a 2-minute setup guide. Reply STOP and I won't message again."),
    ("Remote access, clinics", "first",
     "Hi {{FirstName}}, Vishal from Zinwe here.\n\nMany clinics get calls asking the front desk to install AnyDesk. Zinwe "
     "alerts you the moment a remote tool opens on your PC.\n\nCan I send you the free setup for one PC? Reply YES, or "
     "STOP to opt out."),
    ("One gentle follow-up", "followup",
     "Hi {{FirstName}}, just checking you saw my note about {{Firm}}'s PCs. If now isn't a good time, reply LATER and "
     "I'll leave it. This is my last message unless you reply."),
    ("Send the setup steps", "interested",
     "Great, thanks {{FirstName}}! Here's the free plan: zinwe.io/signup\n\n1. Create the account\n2. Install on one PC\n"
     "3. Plug in a pen drive and see the alert\n\nI'm here if anything is unclear."),
    ("Answer a question", "question",
     "Good question, {{FirstName}}. [Your answer]. Happy to show you on a quick call if that's easier."),
    ("Not now, close kindly", "notnow",
     "No problem at all, {{FirstName}}. I won't message again. If it's ever useful, zinwe.io has everything. Thanks for "
     "replying!"),
]


def seed(apps, schema_editor):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    if not WaTemplate.objects.exists():
        WaTemplate.objects.bulk_create(WaTemplate(name=n, kind=k, text=t) for n, k, t in TEMPLATES)


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0009_whatsapp")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
