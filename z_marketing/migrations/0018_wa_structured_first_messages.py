"""
The 90-day plan's six first messages (IT-1..IT-3, AG-1..AG-3) move to the outreach structure (wa_structure.py, owner
4 Nov 2026): their paragraphs become intro, problem, solution, reward and CTA, each gains a creative suggestion and
notes for replies, and the text opens with {{Greeting}} ("Hi Rohit", or "Hello Webgross team" without a real name).
A template staff already restructured, or whose text no longer has the five paragraphs, is left alone.
"""
from __future__ import annotations

from django.db import migrations

COMMON_NOTES = (
    "Cold first message: text only, no links. If they reply YES or HAAN, send the YES reply (two questions + sign-up "
    "link). Questions: use the approved answers (spyware, price, Mac, antivirus). Not now: ask if we may check back in "
    "a month. STOP, band karo, mat bhejo: no reply, block the number. Never invent numbers, clients or features."
)
EXTRA = {
    "IT-1": ("After they reply: a 10-second screen clip of a pen-drive alert arriving on the dashboard.",
             "Value point: files leaving with a developer (V2). Good follow-up: USB read-only on one PC."),
    "IT-2": ("After they reply: a 10-second clip of the remote-access alert when AnyDesk opens.",
             "Value point: remote tools (V1). Good follow-up: the alert shows the PC and the tool by name."),
    "IT-3": ("After they reply: a screenshot of a 'new software installed' alert.",
             "Value point: unknown software (V3). Good follow-up: the alert names the app and the PC."),
    "AG-1": ("After they reply: a short clip of USB set to read-only, then a pen drive refused.",
             "Value point: client files on pen drives (V2). Good follow-up: large transfers by email or LAN share too."),
    "AG-2": ("After they reply: a clip of a Task for today note going from Sent to Seen.",
             "Value point: briefs lost in WhatsApp groups (V4). Good follow-up: ready notes like Quick question, Call me."),
    "AG-3": ("After they reply: a clip of a teammate taking a break and the owner approving it.",
             "Value point: the team's day (V5). Good follow-up: the day per person, without screenshots of their screen."),
}
OPENERS = ("Hi {{FirstName}}, ", "Hello {{FirstName}}, ")


def compose(parts: dict[str, str]) -> str:
    blocks = [f"{{{{Greeting}}}}, {parts['intro']}"] + [parts[k] for k in ("problem", "solution", "reward", "cta", "details") if parts.get(k)]
    return "\n\n".join(blocks)


def forwards(apps, schema_editor):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    for t in WaTemplate.objects.filter(kind="first"):
        code = t.name.split(" ")[0]
        if code not in EXTRA or t.parts:
            continue
        blocks = [b.strip() for b in t.text.replace("\r\n", "\n").split("\n\n") if b.strip()]
        opener = next((o for o in OPENERS if blocks and blocks[0].startswith(o)), None)
        if len(blocks) != 5 or opener is None:
            continue
        creative, notes = EXTRA[code]
        parts = {
            "creative": creative,
            "intro": blocks[0][len(opener):],
            "problem": blocks[1], "solution": blocks[2], "reward": blocks[3], "cta": blocks[4],
            "notes": f"{notes} {COMMON_NOTES}",
        }
        t.parts, t.text = parts, compose(parts)
        t.save(update_fields=["parts", "text"])


def backwards(apps, schema_editor):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    for t in WaTemplate.objects.filter(kind="first").exclude(parts={}):
        if t.name.split(" ")[0] in EXTRA:
            t.text = t.text.replace("{{Greeting}}, ", "Hi {{FirstName}}, ", 1)
            t.parts = {}
            t.save(update_fields=["parts", "text"])


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0017_wa_template_parts")]
    operations = [migrations.RunPython(forwards, backwards)]
