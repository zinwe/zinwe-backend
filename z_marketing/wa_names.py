"""
How a WhatsApp draft addresses a business. Sheets from Google Maps carry "first names" that are mostly scraps of the
business or WhatsApp profile name ("IT", "Sun", "digital", "The"), and business names that are SEO titles
("Webgross - Digital Marketing Agency in Delhi"). A wrong name is worse than none, so a first name is used only when it
clearly belongs to a person; otherwise the draft greets the business team by a short, clean name.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# Words that describe a business, a place or a service, never a person's first name.
GENERIC = {
    "&", "a", "ads", "advertising", "agencies", "agency", "and", "app", "apps", "bangalore", "bengaluru", "best", "brand",
    "business", "center", "centre", "certified", "co", "colab", "company", "consultancy", "consultants", "consulting",
    "coworking", "co-working", "creative", "delhi", "design", "designer", "designing", "designs", "developer",
    "developers", "development", "digi", "digital", "e-commerce", "ecommerce", "estate", "executive", "expert",
    "experts", "firm", "for", "global", "group", "gurgaon", "gurugram", "hub", "in", "india", "inc", "info", "infotech",
    "institute", "internet", "it", "lab", "labs", "limited", "llp", "local", "ltd", "marketing", "media", "mobile",
    "mumbai", "ncr", "network", "noida", "of", "office", "offices", "online", "private", "promotion", "properties",
    "pune", "pvt", "real", "seo", "service", "services", "shared", "shopify", "social", "software", "solution",
    "solutions", "space", "spaces", "studio", "system", "systems", "team", "tech", "technologies", "technology", "the",
    "top", "web", "website", "websites", "work", "working", "workspace", "workspaces",
}
PROFESSIONS = {"developer", "expert", "consultant", "freelancer", "designer", "specialist", "strategist", "coach", "trainer"}
GENERIC |= PROFESSIONS
_CUT = re.compile(r"\s[|\-–—:]\s|[|(\[,:]|\s[–—]")
_LEGAL = re.compile(r"\b(pvt\.?|private|ltd\.?|limited|llp|inc\.?)(\s|$).*$", re.I)


def _plain(text: str) -> str:
    """NFKC turns 𝐒𝐓𝐒 into STS; then only letters, digits, spaces and & ' . - stay."""
    text = unicodedata.normalize("NFKC", text or "")
    kept = "".join(ch if unicodedata.category(ch)[0] in "LN" or ch in " &'.-" else " " for ch in text)
    return " ".join(kept.split()).strip(" .-'")


def _words(text: str) -> list[str]:
    return [w.lower().strip(".'") for w in _plain(text).split() if w.strip(".'")]


def looks_like_person(name: str) -> bool:
    """'Rajesh Mehta', 'Aushi V Saxena': 2–4 words, letters only, none of them a business word."""
    words = _plain(name).split()
    return 2 <= len(words) <= 4 and all(w.replace(".", "").isalpha() for w in words) and not any(w.lower() in GENERIC for w in words)


def short_business(firm: str, wa_name: str = "") -> str:
    """'Webgross - Digital Marketing Agency in Delhi' → 'Webgross'. Falls back to the WhatsApp name when the listing is
    only generic words ('Website Design & Development Services')."""

    def clean(text: str) -> str:
        head = _CUT.split(unicodedata.normalize("NFKC", text or ""), maxsplit=1)[0]
        head = _plain(_LEGAL.sub("", _plain(head)))
        if len(head) <= 40:
            return head
        words = head.split()[:4]
        while len(words) > 1 and words[-1].lower() in {"of", "in", "for", "and", "&", "the", "at", "by", "with"}:
            words.pop()  # never end on a dangling word: "Digital Samay Institute of"
        return " ".join(words)

    name = clean(firm)
    if (not name or all(w in GENERIC for w in _words(name))) and wa_name:
        wa = clean(wa_name)
        if wa and not looks_like_person(wa) and not all(w in GENERIC for w in _words(wa)):
            return wa
    return name or _plain(firm)[:40] or "your team"


def _cap(name: str) -> str:
    return name.capitalize() if name.islower() or name.isupper() else name


def person_name(first: str, firm: str, wa_name: str = "", scraped: bool = False) -> str:
    """The contact's first name if it clearly belongs to a person, else ''.

    `scraped`: the row came from a listing (Google Maps), where the "first name" is usually a scrap of the business or
    WhatsApp profile name. Then the name needs proof: a WhatsApp profile that is a person's name starting with it
    ("Kishan Kumar Mishra"), or a business that is a person's name ("Anil Gupta")."""
    f = _plain(first).replace(".", "")
    low = f.lower()
    if len(f) < 2 or not f.isalpha() or low in GENERIC or (f.isupper() and len(f) <= 5):
        return ""
    firm_words = _words(firm)
    if not scraped and looks_like_person(firm) and firm_words and firm_words[0] == low:  # named after them
        return _cap(f)
    joined = "".join(firm_words)
    if low in joined or any(w.startswith(low) or (len(w) >= 4 and low.startswith(w)) for w in firm_words):
        return ""  # "Sun" in "Sun Info Global Services", "Cofynd" in "CoFynd"
    wa = _plain(wa_name)
    if wa:
        wa_words = set(_words(wa)) - GENERIC
        own = wa.lower().startswith(low) and looks_like_person(wa)
        brand = not wa_words.isdisjoint(set(firm_words) - GENERIC) or "".join(_words(wa)) in joined  # "Trace Presence" again
        return _cap(f) if own and not brand else ""
    return "" if scraped else _cap(f)  # typed in by staff, or from an introduction: trust it


def _owner_of(firm: str, business: str) -> str:
    """'Dhanashree Kokate - Website Developer In Pune' -> 'Dhanashree': a person's name, then their profession."""
    if not looks_like_person(business):
        return ""
    full = _plain(unicodedata.normalize("NFKC", firm))
    rest = _words(full[len(business):]) if full.startswith(business) else []
    return _plain(business).split()[0] if PROFESSIONS & set(rest[:4]) else ""  # the profession comes right after


def _rating(info: dict[str, Any]) -> str:
    try:
        value = float(str(info.get("rating", "")).strip())
    except ValueError:
        return ""
    return f"{value:.1f}" if 4.0 <= value <= 5.0 else ""


def how_we_know(source: str, info: dict[str, Any], subject: str) -> str:
    """One true line on where we found them, from their own data. '' when we can't say."""
    source = (source or "").strip()
    if not source or source.lower().startswith("uploaded"):
        return ""
    if "google" in source.lower() and "map" in source.lower():
        rating = _rating(info or {})
        return f"I found {subject} on Google Maps, where you're rated {rating}." if rating else f"I found {subject} on Google Maps."
    return f"I found {subject} through {source}."


def address(contact: Any) -> dict[str, str]:
    """Everything a draft needs to address this contact: greeting, first name (or ''), short business, how we know."""
    info = contact.info or {}
    wa_name = str(info.get("wa_business_name") or info.get("contact_name") or "")
    scraped = "google" in (contact.source or "").lower() or "map" in (contact.source or "").lower()
    first = person_name(contact.first_name, contact.firm, wa_name, scraped)
    business = short_business(contact.firm, wa_name)
    first = first or _owner_of(contact.firm, business)
    return {
        "greeting": first or f"{business} team",
        "first_name": first,
        "business": business,
        # "I found you": unless they're greeted by name and the business is someone else's name for it
        "how_we_know": how_we_know(contact.source, info, business if first and not business.lower().startswith(first.lower()) else "you"),
    }
