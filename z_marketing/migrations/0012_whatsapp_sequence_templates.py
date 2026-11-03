"""
Templates for the 3-month IT firm and agency campaign: six first messages (one per value point), a day-3 tip and a
day-7 close for people who never reply (then stop), and the after-YES sequences (every YES, data safety, team's day,
team chat).
Also drops the PC count from the older traders template: plan limits are read from /plans, never written into copy.
"""
from django.db import migrations

HELLO = "Hi {{FirstName}}, Vishal from Zinwe here."
CHERRY_DATA = "Cherry on top: manage team activity, chat and share tasks without switching apps. Free forever."
CHERRY_CHAT = "Cherry on top: protect client files and manage team activity, without switching apps. Free forever."
CHERRY_DAY = "Cherry on top: chat, share tasks and protect client files, without switching apps. Free forever."
CHERRY_ALL = (
    "Cherry on top: protect company data, manage team activity, chat and share tasks, all without switching apps. "
    "Free forever."
)
SIGNUP = "zinwe.io/signup?utm_source=whatsapp&utm_campaign=2026-11-kelash"


def five(moment: str, fit: str, cherry: str, question: str) -> str:
    """A value message: hello, the moment, the fit, cherry on top, a one-word question. One line each."""
    return "\n\n".join([HELLO, moment, fit, cherry, question + " Reply STOP to opt out."])


TEMPLATES = [
    # name, kind, track, day, text
    ("V1 remote access, IT", "first", "", 0, five(
        "In IT teams AnyDesk opens all day, and every session looks the same.",
        "Zinwe alerts you the moment a remote tool opens on any Windows PC.", CHERRY_DATA, "Useful for {{Business}}?")),
    ("V2 files leave, IT", "first", "", 0, five(
        "When a developer moves on, client code can leave with them.",
        "Zinwe alerts you when files go to a pen drive or email.", CHERRY_DATA, "Worth a look for {{Business}}?")),
    ("V2 client files, agency", "first", "", 0, five(
        "Client files often go to a pen drive \"just for this project\".",
        "Zinwe alerts you and can set USB to read-only.", CHERRY_DATA, "How does {{Business}} handle this?")),
    ("V3 new software, IT", "first", "", 0, five(
        "New software turns up on team PCs and nobody knows who installed it.",
        "Zinwe tells you the moment something new is installed.", CHERRY_DATA, "Do you track this at {{Business}}?")),
    ("V4 team chat, agency", "first", "", 0, five(
        "Briefs and client changes get lost in busy WhatsApp groups.",
        "Zinwe puts team chat and tasks in one window on every PC.", CHERRY_CHAT, "Does that happen at {{Business}}?")),
    ("V5 team's day, agency", "first", "", 0, five(
        "In a busy studio, who started on time and who is on a break?",
        "Your team logs in, takes breaks and logs out from one Zinwe widget.", CHERRY_DAY, "Useful for {{Business}}?")),
    ("Day 3 tip, IT", "followup", "", 0, "A free tip, {{FirstName}}: open Settings, Apps on two team PCs and remove "
     "remote tools nobody uses any more.\n\nCherry on top: Zinwe flags new ones for you, plus team chat and tasks in the "
     "same app. Free forever."),
    ("Day 3 tip, agency", "followup", "", 0, "A free tip, {{FirstName}}: open a few old client folders in Google Drive "
     "and check which are still shared as \"Anyone with the link\".\n\nCherry on top: Zinwe flags client files going to "
     "pen drives, plus team chat and tasks in the same app. Free forever."),
    ("Day 7 polite close", "touch3", "", 0, "I'll leave it here, {{FirstName}}.\n\nCherry on top, if you ever need it: "
     "protect company data, manage team activity, chat and share tasks, all without switching apps. Free forever.\n\n"
     "I'm one message away."),
    ("Every YES, day 0: two questions + link", "nurture", "", 0, "Thanks {{FirstName}}! Two quick questions so I send the right "
     "steps:\n1. How many PCs are in the {{Business}} office?\n2. What matters most: company files, the team's day, or "
     "team chat?\n\nMeanwhile, sign up free here: " + SIGNUP),
    ("Data track, day 1: setup + first alert", "nurture", "data", 1, "Here are the steps, {{FirstName}}:\n1. Sign up: " + SIGNUP
     + "\n2. Press Download for Windows and install on one PC\n3. Plug in a pen drive and see the alert arrive\n\n"
     "60-second video: [video link]. I am here if anything is unclear."),
    ("Team's day track, day 1: setup + log in", "nurture", "day", 1, "Here are the steps, {{FirstName}}:\n1. Sign up: " + SIGNUP
     + "\n2. Press Download for Windows and install on one PC\n3. Log in from the Zinwe widget and take a break\n\n"
     "60-second video: [video link]. I am here if anything is unclear."),
    ("Chat track, day 1: setup + first task", "nurture", "chat", 1, "Here are the steps, {{FirstName}}:\n1. Sign up: " + SIGNUP
     + "\n2. Press Download for Windows and install on one PC\n3. Send a Task for today note from your dashboard\n\n"
     "60-second video: [video link]. I am here if anything is unclear."),
    ("Every YES, day 2: install check", "nurture", "", 2, "Hi {{FirstName}}, did the Zinwe agent install fine? If you are "
     "stuck, send me a screenshot, or shall we do it together on a 10-minute call?"),
    ("Data track, day 4: USB read-only", "nurture", "data", 4, "Try this, {{FirstName}}: set USB to read-only on one PC, plug in "
     "a pen drive and watch the alert arrive. That is your company files protected in under a minute."),
    ("Team's day track, day 4: approve a break", "nurture", "day", 4, "Try this, {{FirstName}}: ask one teammate to log in and take a "
     "break from the widget, then approve it from your dashboard."),
    ("Chat track, day 4: Task for today", "nurture", "chat", 4, "Try this, {{FirstName}}: send one Task for today note and watch "
     "it go from Sent to Seen. No more instructions lost in groups."),
    ("Data track, day 7: add the PCs with client files", "nurture", "data", 7, "{{FirstName}}, time to add the rest of the "
     "{{Business}} PCs: the same installer on each. Start with the PCs that hold client files."),
    ("Team's day track, day 7: add the team", "nurture", "day", 7, "{{FirstName}}, time to add the rest of the team: the same "
     "installer on each PC. You will then see each person's day in one place."),
    ("Chat track, day 7: add the team", "nurture", "chat", 7, "{{FirstName}}, time to add the rest of the team: the same "
     "installer on each PC. Then every instruction goes out in one window."),
    ("Every YES, day 10: plan check", "nurture", "", 10, "Hi {{FirstName}}, quick check on plans: how many PCs will run Zinwe, "
     "and how far back do you want to see alerts? If Free forever fits {{Business}}, you are all set. If not, I will "
     "suggest the plan from zinwe.io/plans and send a quotation (UPI or bank transfer)."),
    ("Every YES, day 14: feedback + referral", "nurture", "", 14, "Is Zinwe useful so far, {{FirstName}}? One thing you would "
     "change? And if you know one owner who would like it too, I would be glad of an introduction."),
    ("Every YES, day 30: one more tip", "nurture", "", 30, "Hi {{FirstName}}, one tip for this month: [a feature they have not "
     "used yet]. Reply if you want me to show you."),
]


def seed(apps, schema_editor):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    for name, kind, track, day, text in TEMPLATES:
        WaTemplate.objects.get_or_create(name=name, defaults={"kind": kind, "track": track, "day": day, "text": text})
    old = WaTemplate.objects.filter(name="Pen drive check, traders").first()
    if old and "Free on 3 PCs." in old.text:
        old.text = old.text.replace("Free on 3 PCs.", "Free forever plan, no card needed.")
        old.save(update_fields=["text"])


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0011_whatsapp_sequences")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
