"""
v1.5 file sharing in chat, round 2 (owner, 9 Nov 2026): FILES STAY ON THE SENDER'S DEVICE.

Our server never stores a file. The sender registers it (POST /api/chat/files/ or /api/devices/chat/files/, JSON: name,
size, type, its own pointer `local_ref`, a tiny preview), then sends a line with its `file_id` on any of the four chat
POSTs (group / direct line, owner -> member and member -> owner admin chat), exactly as in round 1. To download, a
receiver asks for a ticket (POST .../files/<id>/ticket/): Django runs the access check and signs a GET ticket; the
bytes then stream from the holder (the Windows PC, or the owner's browser tab) through the relay (z_relay.app, memory
only) to the receiver. The holder keeps a long-poll open with its SERVE ticket (GET .../files/serve-ticket/).

A file is downloadable while it isn't stopped (the sender stopped sharing it), isn't gone (the holder found the local
file changed, moved or deleted), its line isn't deleted (the row goes with it) and it is less than FILE_KEEP_DAYS old.
The size limit is the plan's `file_max_mb` (z_auth). The clean-up job only marks too-old files and deletes
registrations never attached within an hour: there is nothing on disk.

Privacy (owner decision, FINAL): the owner never reaches a file of a direct chat between members (404, like the chat).
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from z_relay import tickets

from .models import ChatFile

FILE_KEEP_DAYS = 30  # it is the sender's disk, not ours
UNATTACHED_LIFETIME = timedelta(hours=1)
MAX_WAITING = 30  # registered, not attached yet, per sender
NAME_MAX = 200
CAPTION_MAX = 4000
LOCAL_REF_MAX = 500
THUMB_MAX_BYTES = 40 * 1024
SHARED_LIST_MAX = 200

IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif", "avif", "tif", "tiff"})
VIDEO_EXTS = frozenset({"mp4", "m4v", "mov", "avi", "mkv", "webm", "wmv", "3gp", "mpg", "mpeg"})
AUDIO_EXTS = frozenset({"mp3", "wav", "m4a", "aac", "ogg", "oga", "opus", "flac", "wma"})
DOCUMENT_EXTS = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "odt", "ods", "odp", "md",
})
RISKY_EXTS = frozenset({
    "exe", "msi", "bat", "cmd", "com", "scr", "ps1", "vbs", "vbe", "js", "jse", "wsf", "hta", "lnk", "reg", "jar",
    "appx", "msix",
})
# What the relay may show inline (?inline=1): pictures and videos a browser plays, and PDFs. SVG never.
INLINE_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    "bmp": "image/bmp", "avif": "image/avif",
    "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "pdf": "application/pdf",
}

# Copy (BRIEF2 section 6 and round 1): use exactly.
TOO_BIG = "Files can be up to {limit} on your plan."
EMPTY = "That file is empty."
SHARING_OFF = "Your admin has turned off file sharing."
TOO_MANY = "Too many files waiting. Send or remove some first."
NOT_FOUND = "That file wasn't found. Attach it again."
NOT_SHARED = "This file isn't shared any more."
NO_EDIT = "A file can't be edited. Delete it and send it again."
BAD_THUMB = "The preview must be a JPEG or WebP picture of up to 40 KB."
BAD_SHA = "A SHA-256 is 64 hex characters."
SHA_CHANGED = "The file changed. Share it again."
BAD_BROWSER = "Not a valid browser id."

OWNER_ROUTE = "/api/chat/files/{id}/"
DEVICE_ROUTE = "/api/devices/chat/files/{id}/"

STATE_OK = "ok"
STATE_STOPPED = "stopped"
STATE_GONE = "gone"
STATE_OLD = "old"


class FileRefused(Exception):
    """A 400 on `field` (or a 403 when `forbidden`)."""

    def __init__(self, message: str, field: str = "file", forbidden: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.forbidden = forbidden


# --- Names, kinds, sizes -----------------------------------------------------------------------------------------


_WINDOWS_BAD = re.compile(r'[<>:"|?*]')
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")


def safe_name(raw: str | None) -> str:
    """A plain base name: no folders, no control or direction characters, nothing Windows refuses, at most 200."""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")  # control, format (RTL override), ...
    name = _WINDOWS_BAD.sub("_", name).strip().rstrip(". ")
    if name in ("", ".", ".."):
        name = "file"
    if len(name) > NAME_MAX:
        stem, ext = os.path.splitext(name)
        ext = ext[:20]
        name = stem[: NAME_MAX - len(ext)] + ext
    return name


def extension(name: str) -> str:
    return os.path.splitext(name)[1][1:].lower()


def is_risky(name: str) -> bool:
    return extension(name) in RISKY_EXTS


def kind_for(name: str, mime: str = "") -> str:
    """image | video | audio | document | file, from the extension first, then the claimed type. A program is a file."""
    ext = extension(name)
    if ext in RISKY_EXTS:
        return ChatFile.Kind.FILE
    for exts, kind in ((IMAGE_EXTS, ChatFile.Kind.IMAGE), (VIDEO_EXTS, ChatFile.Kind.VIDEO),
                       (AUDIO_EXTS, ChatFile.Kind.AUDIO), (DOCUMENT_EXTS, ChatFile.Kind.DOCUMENT)):
        if ext in exts:
            return kind
    mime = (mime or "").lower()
    if ext in ("svg", "svgz") or "svg" in mime:
        return ChatFile.Kind.FILE
    for prefix, kind in (("image/", ChatFile.Kind.IMAGE), ("video/", ChatFile.Kind.VIDEO),
                         ("audio/", ChatFile.Kind.AUDIO)):
        if mime.startswith(prefix):
            return kind
    return ChatFile.Kind.FILE


def inline_type(f: ChatFile) -> str:
    """The type the relay may show this file inline as (image / video / PDF), or "" (always an attachment)."""
    if f.kind not in (ChatFile.Kind.IMAGE, ChatFile.Kind.VIDEO, ChatFile.Kind.DOCUMENT):
        return ""
    found = INLINE_TYPES.get(extension(f.name), "")
    if f.kind == ChatFile.Kind.DOCUMENT and found != "application/pdf":
        return ""
    return found


def _one_decimal(value: float, unit: str) -> str:
    text = f"{value:.1f}"
    return f"{text[:-2] if text.endswith('.0') else text} {unit}"


def human_size(size: int) -> str:
    """The fallback text old clients show: 340 KB, 2.1 MB, 1.4 GB (".0" dropped)."""
    if size >= 1024 ** 3:
        return _one_decimal(size / 1024 ** 3, "GB")
    if size >= 1024 * 1024:
        return _one_decimal(size / (1024 * 1024), "MB")
    return f"{max(1, round(size / 1024))} KB"


def limit_text(max_mb: int) -> str:
    """The plan's limit in "Files can be up to N GB on your plan.": 2 GB, 1.5 GB, 500 MB."""
    if max_mb >= 1024:
        return _one_decimal(max_mb / 1024, "GB")
    return f"{max_mb} MB"


def fallback_body(f: ChatFile, caption: str) -> str:
    """The stored line body: what old clients (and reports) show for a file line."""
    text = f"\U0001F4CE {f.name} ({human_size(f.size)})"
    return f"{text}\n{caption}" if caption else text


def sniff_thumb(data: bytes) -> str | None:
    """image/jpeg or image/webp from the first bytes, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_thumb(raw: str | None) -> bytes | None:
    """thumb_b64 (plain base64 or a data: URL) -> the JPEG / WebP bytes. Refused when bad or over 40 KB."""
    if raw in (None, ""):
        return None
    text = str(raw)
    if text.startswith("data:"):
        text = text.partition(",")[2]
    if len(text) > (THUMB_MAX_BYTES * 4) // 3 + 8:
        raise FileRefused(BAD_THUMB, field="thumb_b64")
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise FileRefused(BAD_THUMB, field="thumb_b64") from None
    if not data or len(data) > THUMB_MAX_BYTES or sniff_thumb(data) is None:
        raise FileRefused(BAD_THUMB, field="thumb_b64")
    return data


def clean_sha(raw: str | None) -> str:
    raw = (raw or "").strip()
    if raw and not _SHA.match(raw):
        raise FileRefused(BAD_SHA, field="sha256")
    return raw.lower()


# --- Holders --------------------------------------------------------------------------------------------------------


def device_holder(device: Any) -> str:
    return f"d{device.pk}"


def browser_holder(owner_id: int, browser_id: str | None) -> str:
    if not browser_id:
        raise FileRefused("This field is required.", field="browser_id")
    if not tickets.BROWSER_ID_RE.match(browser_id):
        raise FileRefused(BAD_BROWSER, field="browser_id")
    return f"b{owner_id}.{browser_id}"


def is_owner_holder(owner_id: int, holder: str) -> bool:
    return holder.startswith(f"b{owner_id}.")


# --- Register -------------------------------------------------------------------------------------------------------


def waiting(owner_id: int, by_member_id: int | None, now: datetime | None = None) -> int:
    """Registrations by this sender not attached yet (and still attachable)."""
    now = now or timezone.now()
    qs = ChatFile.objects.filter(owner_id=owner_id, attached_at__isnull=True, created_at__gt=now - UNATTACHED_LIFETIME)
    return qs.filter(by_member__isnull=True).count() if by_member_id is None else qs.filter(by_member_id=by_member_id).count()


def register(owner_id: int, by_member_id: int | None, holder: str, max_mb: int, data: dict[str, Any]) -> ChatFile:
    """A file the sender holds: only the record (no bytes). `data` is the validated JSON of the POST."""
    size = int(data["size"])
    if size <= 0:
        raise FileRefused(EMPTY, field="size")
    if size > max_mb * 1024 * 1024:
        raise FileRefused(TOO_BIG.format(limit=limit_text(max_mb)), field="size")
    sha = clean_sha(data.get("sha256"))
    thumb = decode_thumb(data.get("thumb_b64"))
    if waiting(owner_id, by_member_id) >= MAX_WAITING:
        raise FileRefused(TOO_MANY)
    name = safe_name(data.get("name"))
    mime = (data.get("mime") or "")[:100]
    duration = data.get("duration_s")
    now = timezone.now()
    return ChatFile.objects.create(
        id=uuid.uuid4(), owner_id=owner_id, by_member_id=by_member_id, name=name, mime=mime, size=size, sha256=sha,
        kind=kind_for(name, mime), holder=holder, local_ref=data["local_ref"], thumb=thumb,
        width=data.get("width"), height=data.get("height"),
        duration_s=int(round(duration)) if duration is not None else None,
        batch_id=data.get("batch_id"), created_at=now, expires_at=now + timedelta(days=FILE_KEEP_DAYS),
    )


# --- Attach -------------------------------------------------------------------------------------------------------


def claim(owner_id: int, by_member_id: int | None, file_id: Any) -> ChatFile:
    """The sender's own registration, not attached yet, still shared and attachable, locked. Inside transaction.atomic."""
    try:
        pk = file_id if isinstance(file_id, uuid.UUID) else uuid.UUID(str(file_id))
    except (TypeError, ValueError):
        raise FileRefused(NOT_FOUND, field="file_id") from None
    qs = ChatFile.objects.select_for_update().filter(
        pk=pk, owner_id=owner_id, attached_at__isnull=True, deleted_at__isnull=True, stopped_at__isnull=True,
        gone_at__isnull=True, created_at__gt=timezone.now() - UNATTACHED_LIFETIME,
    )
    qs = qs.filter(by_member__isnull=True) if by_member_id is None else qs.filter(by_member_id=by_member_id)
    found = qs.first()
    if found is None:
        raise FileRefused(NOT_FOUND, field="file_id")
    return found


def clean_caption(text: str | None) -> str:
    return (text or "").strip()[:CAPTION_MAX]


def attach(f: ChatFile, caption: str, *, line: Any = None, message: Any = None) -> ChatFile:
    """Tie a claimed registration to the line just posted."""
    at = (line or message).created_at or timezone.now()
    values = {
        "chat_line": line, "member_message": message, "caption": caption, "attached_at": at,
        "expires_at": f.expires_at or f.created_at + timedelta(days=FILE_KEEP_DAYS),
    }
    # Conditional: a second attach of the same file (a race the row lock doesn't cover on SQLite) changes nothing.
    if ChatFile.objects.filter(pk=f.pk, attached_at__isnull=True).update(**values) != 1:
        raise FileRefused(NOT_FOUND, field="file_id")
    for k, v in values.items():
        setattr(f, k, v)
    return f


def file_of(obj: Any) -> ChatFile | None:
    """The file on a ChatMessage or z_devices.Message (reverse one-to-one), or None."""
    if obj is None:
        return None
    try:
        return obj.chat_file
    except ObjectDoesNotExist:
        return None


def drop(obj: Any) -> None:
    """The line was deleted: its file record goes too."""
    f = file_of(obj)
    if f is not None:
        f.delete()
        try:
            del obj._state.fields_cache["chat_file"]
        except (AttributeError, KeyError):
            pass


# --- State + JSON ---------------------------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def ends_at(f: ChatFile) -> datetime:
    return f.expires_at or f.created_at + timedelta(days=FILE_KEEP_DAYS)


def state(f: ChatFile, now: datetime | None = None) -> str:
    """ok | stopped | gone | old (in that order of precedence)."""
    if f.stopped_at is not None:
        return STATE_STOPPED
    if f.gone_at is not None:
        return STATE_GONE
    if f.deleted_at is not None or ends_at(f) <= (now or timezone.now()):
        return STATE_OLD
    return STATE_OK


def is_available(f: ChatFile, now: datetime | None = None) -> bool:
    return state(f, now) == STATE_OK


def sender_name(f: ChatFile) -> str:
    from .services import owner_name

    if f.by_member_id is not None:
        return f.by_member.name if f.by_member else ""
    return owner_name(f.owner)


def first_name(name: str) -> str:
    return name.split()[0] if name.split() else ""


def _route(f: ChatFile, device: bool) -> str:
    return (DEVICE_ROUTE if device else OWNER_ROUTE).format(id=f.pk)


def _abs(request: Any, path: str) -> str:
    return request.build_absolute_uri(path) if request is not None else path


def file_json(f: ChatFile | None, request: Any = None, device: bool = False,
              viewer_member_id: int | None = None) -> dict[str, Any] | None:
    """FileJSON (BRIEF2 section 2). Routes are the device ones for the Zinwe window, the owner ones otherwise.
    `mine` = the viewer shared it (the owner: by_member is null; a member: by_member is them)."""
    if f is None:
        return None
    now = timezone.now()
    st = state(f, now)
    available = st == STATE_OK
    name = sender_name(f)
    has_thumb = f.thumb is not None and len(f.thumb) > 0
    mine = (f.by_member_id is not None and f.by_member_id == viewer_member_id) if device else f.by_member_id is None
    return {
        "id": str(f.pk), "name": f.name, "size": f.size, "mime": f.mime, "kind": f.kind, "caption": f.caption,
        "risky": is_risky(f.name), "batch_id": str(f.batch_id) if f.batch_id else None,
        "width": f.width, "height": f.height, "duration_s": f.duration_s,
        "thumb_url": _abs(request, _route(f, device) + "thumb/") if has_thumb and available else None,
        "sender": {"name": name, "first_name": first_name(name)},
        "mine": mine, "available": available, "state": st, "created_at": _iso(f.created_at),
        # Round 1 fields, kept for parsing.
        "expires_at": _iso(ends_at(f)), "expired": not available,
    }


# --- Access -------------------------------------------------------------------------------------------------------


def find(file_id: Any) -> ChatFile | None:
    return (
        ChatFile.objects.filter(pk=file_id)
        .select_related("chat_line__conversation__owner", "member_message", "owner", "by_member")
        .first()
    )


def owner_may_get(user: Any, f: ChatFile) -> bool:
    """The owner: files in their groups, in their admin chats, and their own. NEVER a direct chat's file."""
    from . import services

    if f.owner_id != user.pk:
        return False
    if f.chat_line_id:
        line = f.chat_line
        conv = line.conversation
        if conv.kind != services.GROUP or conv.owner_id != user.pk:
            return False
        return services.visible_lines(conv, None).filter(pk=line.pk, deleted_at__isnull=True).exists()
    if f.member_message_id:
        return f.member_message.owner_id == user.pk
    return f.by_member_id is None


def member_may_get(member: Any, f: ChatFile) -> bool:
    """A member: files in chats they can see (same as visible_lines), their own admin-chat lines, their own files."""
    from . import services

    if member is None or f.owner_id != member.owner_id:
        return False
    if f.chat_line_id:
        line = f.chat_line
        found = services.conversation_for_member(member, line.conversation_id)
        if found is None:
            return False
        conv, row = found
        return services.visible_lines(conv, row).filter(pk=line.pk, deleted_at__isnull=True).exists()
    if f.member_message_id:
        return f.member_message.member_id == member.pk
    return f.by_member_id == member.pk


def is_sender(f: ChatFile, owner: Any, member: Any) -> bool:
    """Stop sharing: only who shared it (the owner for their own files; a member from any of their PCs)."""
    if member is not None:
        return f.owner_id == member.owner_id and f.by_member_id == member.pk
    return f.owner_id == owner.pk and f.by_member_id is None


def is_holder(f: ChatFile, owner: Any, device: Any) -> bool:
    """PATCH: only the device / browser that holds it (any browser of the owner for the owner's own files)."""
    if device is not None:
        return f.holder == device_holder(device)
    return f.owner_id == owner.pk and f.by_member_id is None and is_owner_holder(owner.pk, f.holder)


# --- Holder changes -----------------------------------------------------------------------------------------------


def patch(f: ChatFile, sha256: str | None = None, gone: bool = False) -> ChatFile:
    fields: list[str] = []
    if sha256 is not None:
        sha = clean_sha(sha256)
        if sha and f.sha256 and f.sha256 != sha:
            raise FileRefused(SHA_CHANGED, field="sha256")
        if sha and not f.sha256:
            f.sha256 = sha
            fields.append("sha256")
    if gone and f.gone_at is None:
        f.gone_at = timezone.now()
        fields.append("gone_at")
    if fields:
        f.save(update_fields=fields)
    return f


def stop(f: ChatFile) -> ChatFile:
    """Stop sharing: nobody can download it any more, and its preview goes."""
    if f.stopped_at is None:
        f.stopped_at = timezone.now()
        f.thumb = None
        f.save(update_fields=["stopped_at", "thumb"])
    return f


# --- Tickets -------------------------------------------------------------------------------------------------------


def relay_base(request: Any) -> str:
    """Where the relay is: ZINWE_RELAY_BASE when set (local dev: the relay's own port), else this host."""
    base = (getattr(settings, "ZINWE_RELAY_BASE", "") or "").rstrip("/")
    if base:
        return base
    return request.build_absolute_uri("/").rstrip("/")


def ticket_json(f: ChatFile, request: Any) -> dict[str, Any]:
    """POST .../files/<id>/ticket/ -> {url, inline_url, status_url, expires_at} (the GET ticket lives 15 minutes)."""
    t = quote(tickets.make_get(str(f.pk), f.size, f.name, f.holder, first_name(sender_name(f)), inline_type(f)),
              safe="")
    base = relay_base(request)
    url = f"{base}/relay/file/{f.pk}?t={t}"
    return {
        "url": url, "inline_url": f"{url}&inline=1", "status_url": f"{base}/relay/status/{f.pk}?t={t}",
        "expires_at": _iso(timezone.now() + timedelta(seconds=tickets.GET_MAX_AGE)),
    }


def serve_ticket_json(holder: str, request: Any) -> dict[str, Any]:
    """GET .../files/serve-ticket/ -> {ticket, wait_url, push_base, expires_at} (lives 24 hours; refresh it)."""
    ticket = tickets.make_serve(holder)
    base = relay_base(request)
    return {
        "ticket": ticket, "wait_url": f"{base}/relay/wait?t={quote(ticket, safe='')}", "push_base": f"{base}/relay/push/",
        "expires_at": _iso(timezone.now() + timedelta(seconds=tickets.SERVE_MAX_AGE)),
    }


# --- Shared by me -------------------------------------------------------------------------------------------------


def _chat_json(f: ChatFile, member: Any) -> dict[str, Any] | None:
    from . import services

    if f.chat_line_id:
        conv = f.chat_line.conversation
        if conv.kind == services.GROUP:
            return {"kind": "group", "id": conv.pk, "name": conv.name}
        other = next((p for p in conv.people.all() if member is None or p.member_id != member.pk), None)
        return {"kind": "direct", "id": conv.pk, "name": other.member.name if other else ""}
    if f.member_message_id:
        msg = f.member_message
        if member is None:
            return {"kind": "admin", "id": msg.member_id, "name": msg.member.name if msg.member else ""}
        return {"kind": "admin", "id": msg.member_id, "name": services.owner_name(f.owner)}
    return None


def shared_by(owner: Any, member: Any, request: Any, device: bool) -> list[dict[str, Any]]:
    """The sender's files still shared (sent, not stopped, not too old), newest first: FileJSON plus holder, local_ref,
    sha256 and the chat it was sent in."""
    now = timezone.now()
    qs = ChatFile.objects.filter(
        owner_id=owner.pk, attached_at__isnull=False, stopped_at__isnull=True, deleted_at__isnull=True,
        expires_at__gt=now,
    ).select_related(
        "owner", "by_member", "chat_line__conversation", "member_message__member",
    ).prefetch_related("chat_line__conversation__people__member")
    qs = qs.filter(by_member__isnull=True) if member is None else qs.filter(by_member_id=member.pk)
    out = []
    for f in qs.order_by("-created_at")[:SHARED_LIST_MAX]:
        row = file_json(f, request, device, member.pk if member is not None else None)
        row.update(holder=f.holder, local_ref=f.local_ref, sha256=f.sha256, chat=_chat_json(f, member))
        out.append(row)
    return out


# --- Clean-up (manage.py run_jobs) ----------------------------------------------------------------------------------


@dataclass
class FilePruneResult:
    expired: int = 0
    unattached: int = 0
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.expired} chat file(s) marked too old, {self.unattached} unsent registration(s) deleted, "
                f"{len(self.failed)} failed")


def prune_chat_files(now: datetime | None = None, dry_run: bool = False) -> FilePruneResult:
    """Too old (FILE_KEEP_DAYS): marked (deleted_at) and the preview dropped; the row stays so the line says "Not
    shared any more". Registrations never attached within an hour: deleted. No disk work: there is nothing on disk."""
    now = now or timezone.now()
    result = FilePruneResult()
    old = ChatFile.objects.filter(deleted_at__isnull=True, expires_at__lte=now)
    result.expired = old.count()
    stale = ChatFile.objects.filter(attached_at__isnull=True, created_at__lt=now - UNATTACHED_LIFETIME)
    result.unattached = stale.count()
    if not dry_run:
        try:
            with transaction.atomic():
                old.update(deleted_at=now, thumb=None)
                stale.delete()
        except Exception as exc:  # noqa: BLE001 -- reported, the other jobs carry on
            result.failed.append(repr(exc))
    return result
