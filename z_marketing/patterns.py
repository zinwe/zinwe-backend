"""Email patterns: ready-made sets of templates a new campaign can start from.

Email 1 and 2 are the two cold emails and stay in every pattern (context.md §3 rule 6). Everything after them goes only
to people who said yes or signed up. A campaign takes a copy, so editing either side never changes the other.
"""
from __future__ import annotations

from typing import Any

from . import seed
from .models import EmailTemplate

PATTERNS: dict[str, dict[str, Any]] = {
    "full": {
        "name": "Full flow",
        "text": "2 cold emails. After a yes: setup help, then a tip about once a week for 3 months.",
        "steps": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    },
    "setup": {
        "name": "Setup help",
        "text": "2 cold emails. After a yes: 3 emails to get Zinwe onto one PC, then it stops.",
        "steps": [1, 2, 3, 4, 5],
    },
    "cold": {
        "name": "Two cold emails",
        "text": "One email and one follow-up. After a yes you take over and reply yourself.",
        "steps": [1, 2],
    },
}


def public() -> list[dict[str, Any]]:
    return [{"key": k, "name": p["name"], "text": p["text"], "emails": len(p["steps"])} for k, p in PATTERNS.items()]


def emails(key: str) -> list[dict[str, Any]]:
    """The pattern's emails as they read today: timing from the pattern, words from the templates."""
    templates = {t.step: t for t in EmailTemplate.objects.filter(step__in=PATTERNS[key]["steps"])}
    return [
        {
            "n": n, "stage": stage, "when": when, "week": week, "subject": templates[n].subject, "body": templates[n].body,
            "template": templates[n],
        }
        for n, stage, when, week, _subject, _body in seed.EMAILS if n in templates
    ]
