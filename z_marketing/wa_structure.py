"""
The WhatsApp outreach message structure (owner, 4 Nov 2026). Every structured template has the same parts in the same
order; only the words change:

    Creative   a picture or video to send with it, or a suggestion for one   (not in the text)
    Intro      {{Greeting}}, who we are and how we know them
    Value      one problem, then the one Zinwe answer to it
    Reward     the cherry-on-top line
    CTA        one easy ask, with how to stop
    Details    optional: one more line (a link, a P.S.)
    Notes      context for whoever replies next (Claude in the browser), never sent

{{Greeting}} is "Hi Rohit" when we know a real first name, else "Hello Webgross team" (wa_names.address).
"""
from __future__ import annotations

from typing import Any

# key: (label, required, max length)
PARTS: dict[str, tuple[str, bool, int]] = {
    "creative": ("Creative", False, 300),
    "intro": ("Intro", True, 200),
    "problem": ("Problem", True, 200),
    "solution": ("Solution", True, 200),
    "reward": ("Reward", True, 200),
    "cta": ("Call to action", True, 200),
    "details": ("Other details", False, 300),
    "notes": ("Notes for replies", False, 1000),
}
SENT = ("intro", "problem", "solution", "reward", "cta", "details")
DEFAULT_INTRO = "Vishal from Zinwe here. {{HowWeKnow}}"


def clean(parts: Any) -> dict[str, str]:
    """Known parts only, trimmed. The sent parts are one paragraph each, so blank lines inside them are closed up."""
    if not isinstance(parts, dict):
        return {}
    out: dict[str, str] = {}
    for key, (_, _, size) in PARTS.items():
        value = str(parts.get(key) or "").strip()
        if key in SENT:
            value = "\n".join(line.strip() for line in value.splitlines() if line.strip())
        if value:
            out[key] = value[:size]
    return out


def problems(parts: dict[str, str]) -> dict[str, str]:
    """{part: what's wrong} for the form; empty when the template can be saved."""
    out = {key: f"Add the {label.lower()}." for key, (label, required, _) in PARTS.items() if required and not parts.get(key)}
    for key in ("problem", "solution"):
        if "\n" in parts.get(key, ""):
            out[key] = "One line only: one problem, one solution."
    return out


def compose(parts: dict[str, str]) -> str:
    """The message text: the greeting and intro, then one paragraph per part, blank lines between."""
    intro = parts.get("intro") or DEFAULT_INTRO
    blocks = [f"{{{{Greeting}}}}, {intro}"] + [parts[k] for k in ("problem", "solution", "reward", "cta", "details") if parts.get(k)]
    return "\n\n".join(blocks)


def split(text: str) -> dict[str, str]:
    """Best guess at the parts of an older free-text template: greeting + intro, problem, solution, reward, CTA, details.
    Used to convert the 90-day plan's first messages; staff check the result in the editor."""
    blocks = [b.strip() for b in text.replace("\r\n", "\n").split("\n\n") if b.strip()]
    if len(blocks) < 5:
        return {}
    head = blocks[0]
    for opener in ("Hi {{FirstName}}, ", "Hello {{FirstName}}, ", "Hi {{FirstName}} ", "{{Greeting}}, "):
        if head.startswith(opener):
            head = head[len(opener):]
            break
    else:
        return {}
    keys = ["problem", "solution", "reward", "cta", "details"]
    parts = {"intro": head, **dict(zip(keys, blocks[1:6], strict=False))}
    if len(blocks) > 6:
        parts["details"] = "\n".join(blocks[5:])
    return clean(parts)
