"""
Conditions in WhatsApp template text (owner, 10 Nov 2026): `{{#if FirstName}}Hi {{FirstName}}{{else}}Hello {{Business}} team{{/if}}`.

`{{else}}` is optional and an If never sits inside another If. The details an If can test are FirstName, OwnerName,
Business (also written Firm), City and HowWeKnow. "Known" means:

- FirstName: a real first name by wa_names.address(), the same rule as the greeting;
- OwnerName: the contact's owner name is set;
- City and HowWeKnow: not blank (HowWeKnow is the line fill() would write);
- Business: always.

whatsapp.fill() resolves the conditions first, then fills in the details. The admin turns this text into blocks and back.
"""
from __future__ import annotations

import re
from typing import Any

DETAILS = ("FirstName", "OwnerName", "Business", "City", "HowWeKnow")
ALIASES = {"Firm": "Business"}
TOKEN = re.compile(r"\{\{\s*(?:#if\s+([A-Za-z]+)|(else)|(/if))\s*\}\}")

NO_END = "An If has no matching end."
NESTED = "An If can't sit inside another If."
TWO_ELSE = "An If has only one Otherwise."
STRAY_ELSE = "An Otherwise has no If."


def unknown_detail(name: str) -> str:
    return f"{name} isn't a detail. Use FirstName, OwnerName, Business, City or HowWeKnow."


def detail(name: str) -> str | None:
    """The canonical detail name, or None."""
    name = ALIASES.get(name, name)
    return name if name in DETAILS else None


def problem(text: str) -> str | None:
    """Why the conditions in this text can't be used, or None when they are fine (or there are none)."""
    open_if = False
    has_else = False
    for m in TOKEN.finditer(text or ""):
        name, is_else, is_end = m.groups()
        if name is not None:
            if open_if:
                return NESTED
            if detail(name) is None:
                return unknown_detail(name)
            open_if, has_else = True, False
        elif is_else:
            if not open_if:
                return STRAY_ELSE
            if has_else:
                return TWO_ELSE
            has_else = True
        elif is_end:
            if not open_if:
                return NO_END
            open_if = False
    return NO_END if open_if else None


def apply(text: str, known: dict[str, bool]) -> str:
    """The text with each If replaced by its Then or Otherwise part. Text that fails problem() comes back unchanged
    (it can't be saved, so only older text could hold it)."""
    if not text or "{{" not in text or problem(text):
        return text
    out: list[str] = []
    at = 0
    keep = True  # are we inside the part being kept?
    for m in TOKEN.finditer(text):
        if keep:
            out.append(text[at:m.start()])
        name, is_else, _ = m.groups()
        if name is not None:
            keep = bool(known.get(detail(name) or "", False))
        elif is_else:
            keep = not keep
        else:
            keep = True
        at = m.end()
    out.append(text[at:])
    return "".join(out)


PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z]+)\s*\}\}")


def outside(text: str) -> list[str]:
    """The details filled in ({{City}}...) outside any If, in order of first use: the ones that read oddly when the
    contact doesn't have them."""
    if not text or problem(text):
        return []
    found: list[str] = []
    kept: list[str] = []
    at, inside = 0, False
    for m in TOKEN.finditer(text):
        if not inside:
            kept.append(text[at:m.start()])
        inside = m.group(1) is not None or (inside and m.group(3) is None)
        at = m.end()
    kept.append(text[at:] if not inside else "")
    for m in PLACEHOLDER.finditer("\n".join(kept)):
        d = detail(m.group(1))
        if d and d not in found:
            found.append(d)
    return found


def used(text: str) -> set[str]:
    """The details an If tests in this text."""
    return {d for d in (detail(m.group(1)) for m in TOKEN.finditer(text or "") if m.group(1)) if d}


def known(contact: Any, a: dict[str, str] | None = None) -> dict[str, bool]:
    """Which details are known for this contact (`a`: wa_names.address(contact), when already worked out)."""
    if a is None:
        from .wa_names import address

        a = address(contact)
    return {
        "FirstName": bool(a["first_name"]),
        "OwnerName": bool((getattr(contact, "owner_name", "") or "").strip()),
        "Business": True,
        "City": bool((contact.city or "").strip()),
        "HowWeKnow": bool((a["how_we_know"] or "").strip()),
    }
