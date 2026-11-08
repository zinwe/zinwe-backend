"""
v1.5 file sharing in chat, the "flavour" version: send and forget.

The sender uploads a file once (POST /api/chat/files/ or /api/devices/chat/files/), then sends a line with its
`file_id` on any of the four chat POSTs (group / direct line, owner -> member and member -> owner admin chat). Everyone
in that chat can download it for FILE_KEEP_DAYS after the line was posted; then `prune_chat_files` (manage.py
run_jobs) removes the disk file and keeps the row, so the line says "no longer available". Uploads never attached are
deleted after UNATTACHED_LIFETIME.

Privacy (owner decision, FINAL): the owner never reaches a file of a direct chat between members (404, like the chat).
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import FileResponse
from django.utils import timezone

from .models import ChatFile, remove_chat_file

FILE_MAX_BYTES = 25 * 1024 * 1024  # 26 214 400, on every plan
FILE_KEEP_DAYS = 7
UNATTACHED_LIFETIME = timedelta(hours=1)
MAX_WAITING = 30  # unattached uploads per sender
NAME_MAX = 200
CAPTION_MAX = 4000
# Refuse a body this far over the limit before reading it (the multipart wrapping adds a little).
BODY_SLACK = 1024 * 1024

IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})
RISKY_EXTS = frozenset({
    "exe", "msi", "bat", "cmd", "com", "scr", "ps1", "vbs", "vbe", "js", "jse", "wsf", "hta", "lnk", "reg", "jar",
    "appx", "msix",
})

# Copy (brief section 4): use exactly.
TOO_BIG = "Files can be up to 25 MB."
EMPTY = "That file is empty."
NO_FILE = "Pick a file."
SHARING_OFF = "Your admin has turned off file sharing."
TOO_MANY = "Too many files waiting. Send or remove some first."
NOT_FOUND = "That file wasn't found. Attach it again."
GONE = "This file is no longer available. Files are kept for 7 days."
NO_EDIT = "A file can't be edited. Delete it and send it again."

OWNER_ROUTE = "/api/chat/files/{id}/"
DEVICE_ROUTE = "/api/devices/chat/files/{id}/"


class FileRefused(Exception):
    """A 400 on `field` (or a 403 when `forbidden`)."""

    def __init__(self, message: str, field: str = "file", forbidden: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.forbidden = forbidden


# --- Names, kinds, sizes -----------------------------------------------------------------------------------------


_WINDOWS_BAD = re.compile(r'[<>:"|?*]')


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


def sniff_image(head: bytes) -> str | None:
    """The real image type from the first bytes (jpg, png, gif, webp), or None. SVG is never an image here."""
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def human_size(size: int) -> str:
    """2202009 -> "2.1 MB", 348160 -> "340 KB" (the fallback text old clients show)."""
    if size >= 1024 * 1024:
        text = f"{size / (1024 * 1024):.1f}"
        return f"{text[:-2] if text.endswith('.0') else text} MB"
    return f"{max(1, round(size / 1024))} KB"


def fallback_body(f: ChatFile, caption: str) -> str:
    """The stored line body: what old clients (and reports) show for a file line."""
    text = f"\U0001F4CE {f.name} ({human_size(f.size)})"
    return f"{text}\n{caption}" if caption else text


# --- Upload --------------------------------------------------------------------------------------------------------


def body_too_big(content_length: Any) -> bool:
    try:
        return int(content_length or 0) > FILE_MAX_BYTES + BODY_SLACK
    except (TypeError, ValueError):
        return False


def waiting(owner_id: int, by_member_id: int | None, now: datetime | None = None) -> int:
    """Uploads by this sender not attached yet (and still attachable)."""
    now = now or timezone.now()
    qs = ChatFile.objects.filter(owner_id=owner_id, attached_at__isnull=True, created_at__gt=now - UNATTACHED_LIFETIME)
    return qs.filter(by_member__isnull=True).count() if by_member_id is None else qs.filter(by_member_id=by_member_id).count()


def store_upload(upload: Any, owner_id: int, by_member_id: int | None) -> ChatFile:
    """Write the upload to <CHAT_FILES_ROOT>/<owner_id>/<uuid> in chunks while hashing it; then the row (not attached)."""
    if upload is None:
        raise FileRefused(NO_FILE)
    size = getattr(upload, "size", None) or 0
    if size > FILE_MAX_BYTES:
        raise FileRefused(TOO_BIG)
    if size <= 0:
        raise FileRefused(EMPTY)
    if waiting(owner_id, by_member_id) >= MAX_WAITING:
        raise FileRefused(TOO_MANY)
    name = safe_name(getattr(upload, "name", ""))
    row = ChatFile(id=uuid.uuid4(), owner_id=owner_id, by_member_id=by_member_id, name=name,
                   mime=(getattr(upload, "content_type", "") or "")[:100])
    path = row.path
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    head = b""
    written = 0
    out = open(path, "xb")  # noqa: SIM115 -- "x": never overwrite; closed below
    try:
        with out:
            for chunk in upload.chunks():
                written += len(chunk)
                if written > FILE_MAX_BYTES:
                    raise FileRefused(TOO_BIG)
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                digest.update(chunk)
                out.write(chunk)
        if written == 0:
            raise FileRefused(EMPTY)
        row.size = written
        row.sha256 = digest.hexdigest()
        row.kind = ChatFile.Kind.IMAGE if extension(name) in IMAGE_EXTS and sniff_image(head) else ChatFile.Kind.FILE
        row.created_at = timezone.now()
        row.save(force_insert=True)
    except BaseException:
        remove_chat_file(path)
        raise
    return row


# --- Attach -------------------------------------------------------------------------------------------------------


def claim(owner_id: int, by_member_id: int | None, file_id: Any) -> ChatFile:
    """The sender's own upload, not attached yet and still attachable, locked. Call inside transaction.atomic."""
    try:
        pk = file_id if isinstance(file_id, uuid.UUID) else uuid.UUID(str(file_id))
    except (TypeError, ValueError):
        raise FileRefused(NOT_FOUND, field="file_id") from None
    qs = ChatFile.objects.select_for_update().filter(
        pk=pk, owner_id=owner_id, attached_at__isnull=True, deleted_at__isnull=True,
        created_at__gt=timezone.now() - UNATTACHED_LIFETIME,
    )
    qs = qs.filter(by_member__isnull=True) if by_member_id is None else qs.filter(by_member_id=by_member_id)
    found = qs.first()
    if found is None:
        raise FileRefused(NOT_FOUND, field="file_id")
    return found


def clean_caption(text: str | None) -> str:
    return (text or "").strip()[:CAPTION_MAX]


def attach(f: ChatFile, caption: str, *, line: Any = None, message: Any = None) -> ChatFile:
    """Tie a claimed upload to the line just posted. Kept FILE_KEEP_DAYS from when the line was posted."""
    at = (line or message).created_at or timezone.now()
    values = {
        "chat_line": line, "member_message": message, "caption": caption, "attached_at": at,
        "expires_at": at + timedelta(days=FILE_KEEP_DAYS),
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
    """The line was deleted: its file goes too (the row now, the disk file on commit)."""
    f = file_of(obj)
    if f is not None:
        f.delete()
        try:
            del obj._state.fields_cache["chat_file"]
        except (AttributeError, KeyError):
            pass


# --- JSON ---------------------------------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def is_gone(f: ChatFile, now: datetime | None = None) -> bool:
    now = now or timezone.now()
    return f.deleted_at is not None or (f.expires_at is not None and f.expires_at <= now)


def file_json(f: ChatFile | None, request: Any = None, device: bool = False) -> dict[str, Any] | None:
    """FileJSON (brief 2e). `url` is the device route for the Zinwe window, the owner route otherwise; null once gone."""
    if f is None:
        return None
    gone = is_gone(f)
    url = None
    if not gone:
        path = (DEVICE_ROUTE if device else OWNER_ROUTE).format(id=f.pk)
        url = request.build_absolute_uri(path) if request is not None else path
    return {
        "id": str(f.pk), "name": f.name, "size": f.size, "mime": f.mime, "kind": f.kind, "caption": f.caption,
        "risky": is_risky(f.name), "expires_at": _iso(f.expires_at), "expired": gone, "url": url,
    }


# --- Download -----------------------------------------------------------------------------------------------------


def find(file_id: Any) -> ChatFile | None:
    return (
        ChatFile.objects.filter(pk=file_id)
        .select_related("chat_line__conversation__owner", "member_message")
        .first()
    )


def owner_may_get(user: Any, f: ChatFile) -> bool:
    """The owner: files in their groups, in their admin chats, and their own uploads. NEVER a direct chat's file."""
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
    """A member: files in chats they can see (same as visible_lines), their own admin-chat lines, their own uploads."""
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


def download(f: ChatFile, inline: bool = False) -> FileResponse | None:
    """The streamed file, or None when its disk file is gone."""
    try:
        handle = open(f.path, "rb")  # noqa: SIM115 -- FileResponse closes it
    except FileNotFoundError:
        return None
    content_type, disposition = "application/octet-stream", "attachment"
    if inline and f.kind == ChatFile.Kind.IMAGE:
        real = sniff_image(handle.read(16))
        handle.seek(0)
        if real:
            content_type, disposition = real, "inline"
    response = FileResponse(handle, content_type=content_type)
    response["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(f.name, safe='')}"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


# --- Clean-up (manage.py run_jobs) ----------------------------------------------------------------------------------


@dataclass
class FilePruneResult:
    expired: int = 0
    unattached: int = 0
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.expired} expired chat file(s) removed, {self.unattached} unsent upload(s) deleted, "
                f"{len(self.failed)} failed")


def prune_chat_files(now: datetime | None = None, dry_run: bool = False) -> FilePruneResult:
    """Expired files: the disk file goes, the row stays (deleted_at). Uploads never attached within an hour: deleted."""
    now = now or timezone.now()
    result = FilePruneResult()
    for f in ChatFile.objects.filter(deleted_at__isnull=True, expires_at__lte=now).iterator():
        try:
            result.expired += 1
            if not dry_run:
                remove_chat_file(f.path)
                ChatFile.objects.filter(pk=f.pk).update(deleted_at=now)
        except Exception as exc:  # noqa: BLE001 -- one file's failure mustn't stop the rest
            result.failed.append(f"{f.pk}: {exc!r}")
    stale = ChatFile.objects.filter(attached_at__isnull=True, created_at__lt=now - UNATTACHED_LIFETIME)
    for f in list(stale):
        try:
            result.unattached += 1
            if not dry_run:
                with transaction.atomic():
                    f.delete()  # the post_delete handler removes the disk file on commit
        except Exception as exc:  # noqa: BLE001
            result.failed.append(f"{f.pk}: {exc!r}")
    return result
