"""Create the Zinwe base persona and move every audience onto the new form. Old text with no new field goes to notes."""
from __future__ import annotations

from django.db import migrations

from z_marketing import persona_seed, seed


def _old_notes(a) -> str:  # type: ignore[no-untyped-def]
    lines = []
    for label, value in (
        ("Who sets it up", a.setup),
        ("What they do today", a.today),
        ("How sure we are", a.confidence),
        ("Trigger moments", "; ".join(a.triggers)),
        ("Objections", "; ".join(a.objections)),
        ("Not for", "; ".join(a.not_for)),
        ("More problems", "; ".join(a.problems[2:])),
        ("More offers", "; ".join(a.offers[1:])),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if a.summary and a.summary != seed.AUDIENCE["summary"]:
        lines.append(f"Earlier summary: {a.summary}")
    return "\n".join(lines)


def move(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    Persona = apps.get_model("z_marketing", "Persona")
    Audience = apps.get_model("z_marketing", "Audience")
    persona = Persona.objects.create(name=persona_seed.NAME, profile=persona_seed.PROFILE)
    for a in Audience.objects.all():
        a.persona = persona
        a.business_category = (a.segments[0] if a.segments else "")[:120]
        a.business_segment = ", ".join(a.segments[1:])[:120]
        a.primary_problem = a.problems[0] if a.problems else ""
        a.secondary_problem = a.problems[1] if len(a.problems) > 1 else ""
        a.how_solves = "\n".join(a.solutions)
        a.key_benefits = list(a.solutions)
        a.primary_offer = a.offers[0] if a.offers else ""
        a.decision_makers = [a.decider[:240]] if a.decider else []
        a.industry_keywords = list(a.words)
        a.notes = _old_notes(a)
        if a.name == seed.AUDIENCE["name"]:
            for field, value in persona_seed.FIRST_AUDIENCE.items():
                if field in ("key_benefits", "how_solves") or not getattr(a, field):
                    setattr(a, field, value)
        a.product_service = a.product_service or "Zinwe"
        a.save()


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0003_persona_and_audience_form")]

    operations = [migrations.RunPython(move, migrations.RunPython.noop)]
