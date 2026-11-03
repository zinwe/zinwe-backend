"""
WhatsApp templates for the owner's 90-day plan, "WhatsApp Outreach Plan: IT Firms and Agencies" (16 Nov 2026 to
13 Feb 2027):
- the six first messages take the plan's codes (IT-1..IT-3, AG-1..AG-3); their text already matches the plan;
- sign-up links carry utm_campaign=2026-11-it-agency, the plan's sign-up measure;
- a YES is answered with the plan's day-0 "Qualify" message as the Interested reply (it was also an after-YES day-0
  message, so a YES got two messages), and the after-YES sequence gains days 21, 45, 60 and 90;
- the plan's reply answers become templates: not now (check back in a month?), not interested, and four approved
  answers to common questions.
Runs on every flavour; campaigns and contacts are not touched here.
"""
from __future__ import annotations

from django.db import migrations

OLD_UTM, NEW_UTM = "utm_campaign=2026-11-kelash", "utm_campaign=2026-11-it-agency"
SIGNUP = "zinwe.io/signup?utm_source=whatsapp&" + NEW_UTM

RENAME = {
    "V2 files leave, IT": "IT-1 Developer leaves with client code",
    "V1 remote access, IT": "IT-2 Remote tool opens unseen",
    "V3 new software, IT": "IT-3 Unknown software installed",
    "V2 client files, agency": "AG-1 Client files on a pen drive",
    "V4 team chat, agency": "AG-2 Briefs lost in WhatsApp groups",
    "V5 team's day, agency": "AG-3 Who started, who is on a break",
}

QUALIFY_OLD = "Every YES, day 0: two questions + link"
QUALIFY = "YES reply: two questions + sign-up link"
QUALIFY_TEXT = (
    "Thanks {{FirstName}}! Two quick questions so I send the right steps:\n1. How many PCs are in the {{Business}} "
    "office?\n2. What matters most: company files, the team's day, or team chat?\n\nSign up free here: " + SIGNUP
)

UPDATE = {
    "Every YES, day 14: feedback + referral": (
        "Is Zinwe useful so far, {{FirstName}}? One thing you would change?\n\nIf you know one owner who would like it "
        "too, an introduction gets you both 10% off a paid plan."
    ),
    "Every YES, day 30: one more tip": (
        "Hi {{FirstName}}, one tip from a part of Zinwe you haven't tried yet: [feature and one line on how]. Reply if "
        "you want me to show you."
    ),
}

NEW = [
    # name, kind, track, day, text
    ("Every YES, day 21: ready for a paid plan?", "nurture", "", 21,
     "Hi {{FirstName}}, is Free forever covering {{Business}} well, or do you need more PCs or longer history?\n\n"
     "If you're ready to move up, I'll set up a personal offer on your dashboard, with an end date. Happy either way."),
    ("Every YES, day 45: check-in", "nurture", "", 45,
     "Hi {{FirstName}}, how is Zinwe going at {{Business}}? Anything I can set up for you?"),
    ("Every YES, day 60: anything changed?", "nurture", "", 60,
     "Hi {{FirstName}}, two months in. Has anything changed at {{Business}}: more PCs, older history to look back on, or "
     "PDF reports? If so, I'll suggest the plan that fits from zinwe.io/plans."),
    ("Every YES, day 90: thank you + feedback", "nurture", "", 90,
     "Thank you for three months with Zinwe, {{FirstName}}. What is one thing we should improve? An honest answer helps "
     "us most."),
    ("Not now: check back in a month?", "notnow", "", 0,
     "No problem, {{FirstName}}, thanks for replying. Shall I check back in a month?"),
    ("Not interested: close kindly", "notnow", "", 0,
     "Thanks for replying, {{FirstName}}. I won't message again. All the best."),
    ("Answer: is this spyware?", "question", "", 0,
     "No. Zinwe protects company data. It flags things like files copied to a pen drive or remote-access sessions. It "
     "does not read messages, record screens or log keystrokes, and your team sees the Zinwe widget on their PC. More: "
     "zinwe.io/legal/privacy"),
    ("Answer: what does it cost?", "question", "", 0,
     "There is a Free forever plan; paid plans are on zinwe.io/plans. How many PCs do you have, {{FirstName}}? I'll tell "
     "you which fits."),
    ("Answer: do you support Mac?", "question", "", 0, "Windows only today. I'll tell you when that changes."),
    ("Answer: we have antivirus", "question", "", 0,
     "Antivirus stops viruses. Zinwe shows what happens to company files and tools on each PC. They work side by side."),
]


def forward(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    for old, new in RENAME.items():
        WaTemplate.objects.filter(name=old).update(name=new)
    for t in WaTemplate.objects.filter(text__contains=OLD_UTM):
        t.text = t.text.replace(OLD_UTM, NEW_UTM)
        t.save(update_fields=["text"])
    q = WaTemplate.objects.filter(name=QUALIFY_OLD, kind="nurture").first()
    if q is not None:
        q.name, q.kind, q.track, q.day, q.text = QUALIFY, "interested", "", 0, QUALIFY_TEXT
        q.save(update_fields=["name", "kind", "track", "day", "text"])
    else:
        WaTemplate.objects.get_or_create(name=QUALIFY, defaults={"kind": "interested", "text": QUALIFY_TEXT})
    for name, text in UPDATE.items():
        WaTemplate.objects.filter(name=name).update(text=text)
    for name, kind, track, day, text in NEW:
        WaTemplate.objects.get_or_create(name=name, defaults={"kind": kind, "track": track, "day": day, "text": text})


def backward(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    for old, new in RENAME.items():
        WaTemplate.objects.filter(name=new).update(name=old)
    q = WaTemplate.objects.filter(name=QUALIFY, kind="interested").first()
    if q is not None:
        q.name, q.kind = QUALIFY_OLD, "nurture"
        q.save(update_fields=["name", "kind"])
    for t in WaTemplate.objects.filter(text__contains=NEW_UTM):
        t.text = t.text.replace(NEW_UTM, OLD_UTM)
        t.save(update_fields=["text"])
    # The new templates may be in use by a campaign; leave them (PROTECT/SET_NULL would otherwise bite).


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0015_wa_how_we_know")]

    operations = [migrations.RunPython(forward, backward)]
