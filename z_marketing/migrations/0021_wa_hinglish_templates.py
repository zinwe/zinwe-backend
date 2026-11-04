"""
Hinglish twins of the 90-day plan's cold messages (owner, 4 Nov 2026): the six first messages (IT-1..AG-3, in the
outreach structure), the two day-3 tips and the day-7 close. Same structure, same one problem and one solution, same
cherry-on-top line and STOP; plain Roman Hindi with English where Indian owners use it. Each is linked to its English
template as `twin`, so a campaign set to Hinglish or to 50/50 sends it and Analytics can compare the two.
The greeting is "Namaste Rohit ji", or "Namaste Webgross team" without a real first name (wa_names.address).
"""
from __future__ import annotations

from django.db import migrations

INTRO = "Vishal, Zinwe se. {{HowWeKnow}}"
CHERRY_DATA = "Cherry on top: team activity, chat aur tasks bhi, sab ek app mein. Free forever."
CHERRY_CHAT = "Cherry on top: client files safe aur team activity bhi, sab ek app mein. Free forever."
CHERRY_DAY = "Cherry on top: chat, tasks aur client files safe, sab ek app mein. Free forever."
STOP = "Rokna ho to STOP likhiye."
NOTES = (
    "Hinglish version. Reply in the language they use: Hindi, Hinglish or English. HAAN, haan ji, ok all mean yes: "
    "send the YES reply. Same rules as the English template: no links in a first message, never invent facts."
)

# English name prefix -> (Hinglish name, parts)
FIRST = {
    "IT-1": ("IT-1 Developer leaves with client code (Hinglish)", {
        "problem": "Developer company chhode, to client ka code bhi saath ja sakta hai.",
        "solution": "Zinwe batata hai jab files pen drive ya email se bahar jaati hain.",
        "reward": CHERRY_DATA, "cta": f"{{{{Business}}}} ke liye useful hai? {STOP}"}),
    "IT-2": ("IT-2 Remote tool opens unseen (Hinglish)", {
        "problem": "IT teams mein AnyDesk din bhar khulta hai, har session ek jaisa dikhta hai.",
        "solution": "Zinwe turant batata hai jab kisi bhi PC par remote tool khulta hai.",
        "reward": CHERRY_DATA, "cta": f"{{{{Business}}}} ke liye useful hai? {STOP}"}),
    "IT-3": ("IT-3 Unknown software installed (Hinglish)", {
        "problem": "Team PCs par naya software aa jaata hai, pata nahi chalta kisne daala.",
        "solution": "Zinwe turant batata hai jab kuch naya install hota hai.",
        "reward": CHERRY_DATA, "cta": f"{{{{Business}}}} mein ye track karte hain? {STOP}"}),
    "AG-1": ("AG-1 Client files on a pen drive (Hinglish)", {
        "problem": "Client files aksar pen drive mein jaati hain, \"bas is project ke liye\".",
        "solution": "Zinwe batata hai, aur USB read-only bhi kar sakta hai.",
        "reward": CHERRY_DATA, "cta": f"{{{{Business}}}} mein ye kaise hota hai? {STOP}"}),
    "AG-2": ("AG-2 Briefs lost in WhatsApp groups (Hinglish)", {
        "problem": "Briefs aur client changes busy WhatsApp groups mein kho jaate hain.",
        "solution": "Zinwe har PC par team chat aur tasks ek hi window mein deta hai.",
        "reward": CHERRY_CHAT, "cta": f"{{{{Business}}}} mein aisa hota hai? {STOP}"}),
    "AG-3": ("AG-3 Who started, who is on a break (Hinglish)", {
        "problem": "Busy studio mein pata nahi chalta kaun time par aaya aur kaun break par hai.",
        "solution": "Team ek chhote Zinwe widget se login, break aur logout karti hai.",
        "reward": CHERRY_DAY, "cta": f"{{{{Business}}}} ke liye useful hai? {STOP}"}),
}

# English name -> (Hinglish name, kind, text)
FREE = {
    "Day 3 tip, IT": ("Day 3 tip, IT (Hinglish)", "followup",
                      "Ek free tip, {{FirstName}}: do team PCs par Settings, Apps kholiye aur jo remote tools ab use nahi "
                      "hote, unhe hata dijiye.\n\nCherry on top: Zinwe naye tools ka alert deta hai, saath mein team chat "
                      "aur tasks ek hi app mein. Free forever."),
    "Day 3 tip, agency": ("Day 3 tip, agency (Hinglish)", "followup",
                          "Ek free tip, {{FirstName}}: Google Drive mein purane client folders kholiye aur dekhiye kaun se "
                          "abhi bhi \"Anyone with the link\" par shared hain.\n\nCherry on top: Zinwe pen drive par jaati "
                          "client files ka alert deta hai, saath mein team chat aur tasks ek hi app mein. Free forever."),
    "Day 7 polite close": ("Day 7 polite close (Hinglish)", "touch3",
                           "Main yahin rukta hoon, {{FirstName}}.\n\nCherry on top, jab bhi zaroorat ho: company data safe "
                           "rakho, team activity manage karo, chat aur tasks share karo, sab ek app mein. Free forever."
                           "\n\nBas ek message ki doori par hoon."),
}


def compose(parts: dict[str, str]) -> str:
    blocks = [f"{{{{Greeting}}}}, {parts['intro']}"] + [parts[k] for k in ("problem", "solution", "reward", "cta") if parts.get(k)]
    return "\n\n".join(blocks)


def forwards(apps, schema_editor):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")

    def pair(english, name, kind, text, parts):
        if english is None or WaTemplate.objects.filter(name=name).exists():
            return
        hinglish = WaTemplate.objects.create(name=name, kind=kind, language="hi", text=text, parts=parts, twin=english)
        english.twin = hinglish
        english.save(update_fields=["twin"])

    for t in WaTemplate.objects.filter(kind="first", language="en"):
        code = t.name.split(" ")[0]
        if code not in FIRST:
            continue
        name, parts = FIRST[code]
        parts = {"creative": (t.parts or {}).get("creative", ""), "intro": INTRO, **parts, "notes": NOTES}
        pair(t, name, "first", compose(parts), parts)
    for english_name, (name, kind, text) in FREE.items():
        pair(WaTemplate.objects.filter(name=english_name, language="en").first(), name, kind, text, {})
    # Everyone messaged before languages existed got English: count them in the English vs Hinglish comparison.
    apps.get_model("z_marketing", "WaContact").objects.filter(step__gte=1, lang="").update(lang="en")


def backwards(apps, schema_editor):
    WaTemplate = apps.get_model("z_marketing", "WaTemplate")
    names = [n for n, _ in FIRST.values()] + [n for n, _, _ in FREE.values()]
    WaTemplate.objects.filter(twin__name__in=names).update(twin=None)
    WaTemplate.objects.filter(name__in=names).delete()


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0020_wa_languages")]
    operations = [migrations.RunPython(forwards, backwards)]
