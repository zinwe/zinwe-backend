"""
Relay tickets (v1.5 round 2): short signed tokens that let the relay (z_relay.app) pair a receiver with the sender's
device WITHOUT touching the database. Django makes them after its own access checks (z_chat.files); the relay checks
only the signature, the age and the scope.

- A GET ticket (scope "get") is for one receiver and one file: file id, size, name, the holder that has the file, the
  sender's first name (for "<first name>'s PC is offline") and the inline type (an image / video / PDF type the relay
  may show inline, or "" when it must always be an attachment). It lives 15 minutes.
- A SERVE ticket (scope "serve") is for the device or browser that holds files: just its holder id. It lives 24 hours,
  so clients refresh it.

A ticket of one scope never works for the other (a get ticket can't wait or push). Signed with Django's
TimestampSigner (SECRET_KEY) and the salt "zinwe.relay". This module imports nothing that needs the app registry.
"""
from __future__ import annotations

import re
from typing import Any

from django.core import signing

SALT = "zinwe.relay"
GET_MAX_AGE = 15 * 60  # seconds
SERVE_MAX_AGE = 24 * 60 * 60

GET = "get"
SERVE = "serve"

# d<device id> (a Windows PC) or b<owner id>.<browser id> (the owner's browser; the browser id is random, kept in
# that browser's localStorage).
HOLDER_RE = re.compile(r"^(d[1-9][0-9]{0,18}|b[1-9][0-9]{0,18}\.[A-Za-z0-9_-]{8,64})$")
BROWSER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=SALT)


def make_get(file_id: str, size: int, name: str, holder: str, first_name: str = "", inline_type: str = "") -> str:
    return _signer().sign_object({
        "k": GET, "f": str(file_id), "s": int(size), "n": name, "h": holder, "fn": first_name, "i": inline_type,
    }, compress=True)


def make_serve(holder: str) -> str:
    return _signer().sign_object({"k": SERVE, "h": holder})


def read(token: str | None, scope: str, max_age: int | None = None) -> dict[str, Any] | None:
    """The ticket's payload when it is genuine, young enough and of `scope`; None otherwise."""
    if not token or len(token) > 4096:
        return None
    if max_age is None:
        max_age = GET_MAX_AGE if scope == GET else SERVE_MAX_AGE
    try:
        data = _signer().unsign_object(token, max_age=max_age)
    except (signing.BadSignature, ValueError, TypeError):  # SignatureExpired is a BadSignature
        return None
    if not isinstance(data, dict) or data.get("k") != scope or not HOLDER_RE.match(str(data.get("h", ""))):
        return None
    if scope == GET and not (isinstance(data.get("f"), str) and isinstance(data.get("s"), int)):
        return None
    return data
