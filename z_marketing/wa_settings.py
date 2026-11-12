"""
WhatsApp Settings (Marketing -> WhatsApp -> Settings, owner's board of 12 Nov 2026): the rules staff change in the
admin instead of in code. One row, WaSettings pk 1; until staff save it the defaults apply, and they are what the code
did before (Mon-Sat 10:00-19:00, 15 new people a day per new campaign, one message per person a day, STOP / band karo
/ mat bhejo, 40 % at most).

Where each one is used:

    send_days, send_from, send_to, skip_holidays   wa_paths.at_open / shift (a wait that keeps to the window)
    new_per_day, language                          a new campaign's daily limit and language (WaCampaignListView.post)
    per_number_day                                 whatsapp.queue: Intro messages a day from the number, all campaigns
    per_person_day                                 wa_paths.queue, wa_flow.queue: messages a day to one person
    min_intro_wait                                 wa_path_model.issues: a shorter wait between Intro messages is an error
    max_days                                       wa_paths.Ctx.run (no reply by then = No reply), wa_flow.queue, the
                                                   touches serializer and the path's "past day N" warning
    stop_words                                     whatsapp.record_reply: a reply that is one of them stops the number
    stop_pause_pct                                 whatsapp._stop: a live campaign whose stops pass it pauses
    discount_max                                   Offer templates, wa_flow.check_sendable / offer_for, wa_paths._offer_ok
    claude_draft                                   whatsapp._claude: off = the template as written
    slots, switch_en, claude_read, claude_track,   stored for Today and the number card; nothing in the engine reads them
    wa_number, wa_name

current() reads the row once per request (the cache is cleared when a request starts and when the row is saved) and
at most every 30 seconds elsewhere (commands, tests).
"""
from __future__ import annotations

import re
import time as _time
from datetime import date, time
from typing import Any

from django.core.signals import request_started
from django.utils import timezone

from .models import WaSettings

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SLOTS = ["10-12", "12-14", "14-17", "17-19"]
LANGUAGES = [c for c, _ in WaSettings.Language.choices]
PER_PERSON = (1, 2)
MIN_WAITS = (1, 2, 3)
MAX_DAYS = (30, 60, 90)
STOP_PAUSE = (0, 2, 3, 5)
DISCOUNT_MAX = 50
STOP_WORDS_MAX = 30
# Public holidays a send day can skip (skip_holidays): fixed dates, then Holi and Diwali by year (add each new year).
FIXED_HOLIDAYS = {(1, 26), (8, 15), (10, 2)}
FESTIVALS = {
    date(2026, 3, 4), date(2026, 11, 8),  # Holi, Diwali
    date(2027, 3, 22), date(2027, 10, 29),
    date(2028, 3, 11), date(2028, 10, 17),
}
TTL = 30.0

_cache: dict[str, Any] = {}


def clear(**_: Any) -> None:
    _cache.clear()


request_started.connect(clear, dispatch_uid="z_marketing.wa_settings.clear")


def current() -> WaSettings:
    """The settings now: the saved row, or the defaults while nothing is saved (never written by reading)."""
    hit = _cache.get("row")
    if hit is not None and _time.monotonic() - hit[1] < TTL:
        return hit[0]
    row = WaSettings.objects.filter(pk=1).first() or WaSettings(pk=1)
    _cache["row"] = (row, _time.monotonic())
    return row


# --- What the engine asks ------------------------------------------------------------------------------------------


def is_holiday(d: date) -> bool:
    return (d.month, d.day) in FIXED_HOLIDAYS or d in FESTIVALS


def is_send_day(d: date, s: WaSettings | None = None) -> bool:
    s = s or current()
    return d.weekday() in (s.send_days or []) and not (s.skip_holidays and is_holiday(d))


def opens(s: WaSettings | None = None) -> time:
    return (s or current()).send_from


def closes(s: WaSettings | None = None) -> time:
    return (s or current()).send_to


def stop_pattern(s: WaSettings | None = None) -> re.Pattern[str]:
    """A reply that is only a stop word (any case, punctuation around it, any spaces inside it)."""
    words = [w for w in ((s or current()).stop_words or []) if str(w).strip()] or ["stop"]
    alts = "|".join(r"\s+".join(re.escape(p) for p in str(w).split()) for w in words)
    return re.compile(rf"\W*({alts})\W*", re.I)


def is_stop(text: str, s: WaSettings | None = None) -> bool:
    return bool(stop_pattern(s).fullmatch(text or ""))


# --- The API -------------------------------------------------------------------------------------------------------


def _hm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def as_json(s: WaSettings | None = None) -> dict[str, Any]:
    s = s or current()
    return {
        "send_days": sorted(s.send_days or []), "send_from": _hm(s.send_from), "send_to": _hm(s.send_to),
        "skip_holidays": s.skip_holidays,
        "new_per_day": s.new_per_day, "per_number_day": s.per_number_day, "per_person_day": s.per_person_day,
        "min_intro_wait": s.min_intro_wait, "max_days": s.max_days,
        "slots": [x for x in SLOTS if x in (s.slots or [])],
        "stop_words": list(s.stop_words or []), "stop_pause_pct": s.stop_pause_pct,
        "language": s.language, "switch_en": s.switch_en,
        "claude_read": s.claude_read, "claude_draft": s.claude_draft, "claude_track": s.claude_track,
        "wa_number": s.wa_number, "wa_name": s.wa_name,
        "discount_max": s.discount_max,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None, "updated_by": s.updated_by,
    }


class SettingsError(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__(next(iter(errors.values())))


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _time_of(value: Any) -> time | None:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)(:\d\d)?", str(value or "").strip())
    return time(int(m.group(1)), int(m.group(2))) if m else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def update(data: dict[str, Any], by: str) -> WaSettings:
    """Save the fields given (the rest stay), or SettingsError {field: message} with everything to fix."""
    if not isinstance(data, dict):
        raise SettingsError({"detail": "Send the settings as an object."})
    s = WaSettings.objects.filter(pk=1).first() or WaSettings(pk=1)
    errors: dict[str, str] = {}

    def number(key: str, low: int, high: int, message: str, allowed: tuple[int, ...] = ()) -> None:
        if key not in data:
            return
        n = _int(data[key])
        if n is None or not low <= n <= high or (allowed and n not in allowed):
            errors[key] = message
        else:
            setattr(s, key, n)

    if "send_days" in data:
        days = data["send_days"]
        if not isinstance(days, list) or any(_int(d) is None or not 0 <= _int(d) <= 6 for d in days):  # type: ignore[operator]
            errors["send_days"] = "Pick the days from Monday to Sunday."
        elif not days:
            errors["send_days"] = "Pick at least one day."
        else:
            s.send_days = sorted({_int(d) for d in days})  # type: ignore[type-var]
    for key in ("send_from", "send_to"):
        if key in data:
            t = _time_of(data[key])
            if t is None:
                errors[key] = "Use a time like 10:00."
            else:
                setattr(s, key, t)
    if "send_from" not in errors and "send_to" not in errors and s.send_from >= s.send_to:
        errors["send_to"] = "The end comes after the start."
    number("new_per_day", 1, 50, "Keep it between 1 and 50 a day.")
    number("per_number_day", 1, 500, "Keep it between 1 and 500 a day.")
    number("per_person_day", 1, 2, "Pick 1 or 2 messages a day.", PER_PERSON)
    number("min_intro_wait", 1, 3, "Pick 1, 2 or 3 days.", MIN_WAITS)
    number("max_days", 30, 90, "Pick 30, 60 or 90 days.", MAX_DAYS)
    number("stop_pause_pct", 0, 5, "Pick never, 2 %, 3 % or 5 %.", STOP_PAUSE)
    number("discount_max", 1, DISCOUNT_MAX, f"Keep it between 1 % and {DISCOUNT_MAX} %.")
    if "slots" in data:
        slots = data["slots"]
        if not isinstance(slots, list) or any(x not in SLOTS for x in slots):
            errors["slots"] = "Pick from 10-12, 12-14, 14-17 and 17-19."
        elif not slots:
            errors["slots"] = "Keep at least one slot."
        else:
            s.slots = [x for x in SLOTS if x in slots]
    if "stop_words" in data:
        words = data["stop_words"]
        clean: list[str] = []
        if not isinstance(words, list) or any(not isinstance(w, str) for w in words):
            errors["stop_words"] = "Send the stop words as a list."
        else:
            for w in words:
                w = " ".join(w.split())
                if not w:
                    continue
                if len(w) > 40:
                    errors["stop_words"] = "Keep each stop word under 40 letters."
                    break
                if w.lower() not in {x.lower() for x in clean}:
                    clean.append(w)
            if "stop_words" not in errors:
                if "stop" not in {w.lower() for w in clean}:
                    errors["stop_words"] = "STOP always stays: every Intro message ends with “Reply STOP to opt out.”"
                elif len(clean) > STOP_WORDS_MAX:
                    errors["stop_words"] = f"Keep it to {STOP_WORDS_MAX} stop words."
                else:
                    s.stop_words = clean
    if "language" in data:
        if data["language"] not in LANGUAGES:
            errors["language"] = "Pick Hinglish first, English or half and half."
        else:
            s.language = data["language"]
    for key in ("skip_holidays", "switch_en", "claude_read", "claude_draft", "claude_track"):
        if key in data:
            b = _bool(data[key])
            if b is None:
                errors[key] = "Send true or false."
            else:
                setattr(s, key, b)
    for key, size in (("wa_number", 24), ("wa_name", 80)):
        if key in data:
            text = " ".join(str(data[key] or "").split())
            if len(text) > size:
                errors[key] = f"Keep it to {size} characters."
            elif key == "wa_number" and text and not re.fullmatch(r"\+?[\d ()-]{8,24}", text):
                errors[key] = "Use the number with its country code, like +91 98100 00000."
            else:
                setattr(s, key, text)
    if errors:
        raise SettingsError(errors)
    s.updated_at, s.updated_by = timezone.now(), by[:120]
    s.save()
    clear()
    return s
