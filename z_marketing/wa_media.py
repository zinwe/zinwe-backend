"""
A picture or video on a WhatsApp template. Zinwe can't attach it to a wa.me link (those carry text only), so the admin
shows it beside the draft and staff paste or attach it in WhatsApp themselves.

Only messages to people who already replied or said YES may carry one. To a stranger, a first message with a picture
or video looks like spam, and WhatsApp bans numbers that get reported. Files sit in MEDIA_ROOT, which nothing serves
publicly: staff fetch them through the admin API.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .models import WaTemplate

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime"}
IMAGE_MAX = 5 * 1024 * 1024
VIDEO_MAX = 9 * 1024 * 1024  # the servers accept uploads up to 10 MB (nginx client_max_body_size)
COLD = {WaTemplate.Kind.FIRST, WaTemplate.Kind.FOLLOWUP, WaTemplate.Kind.TOUCH3, WaTemplate.Kind.TOUCH4}
COLD_REFUSAL = "Messages to people who haven't replied stay text only: to a stranger, a picture or video looks like spam."


class MediaProblem(Exception):
    """The file can't be used, with a message for staff."""


def content_type(template: WaTemplate) -> str:
    ext = Path(template.media.name).suffix.lower()
    return {**IMAGE_TYPES, **VIDEO_TYPES}.get(ext, "application/octet-stream")


def describe(template: WaTemplate) -> dict[str, Any] | None:
    """What the admin needs to show and fetch it, or None."""
    if not template.media:
        return None
    try:
        size = template.media.size
    except OSError:
        return None  # the file is gone from disk
    return {
        "kind": template.media_kind, "name": template.media_name, "size": size,
        "url": f"/api/admin/marketing/wa/templates/{template.pk}/media/",
    }


def _check(upload: Any) -> str:
    """'image' or 'video', or MediaProblem. Checks the content, not just the name."""
    ext = Path(upload.name or "").suffix.lower()
    if ext in IMAGE_TYPES:
        if upload.size > IMAGE_MAX:
            raise MediaProblem("Pictures can be up to 5 MB.")
        try:
            with Image.open(upload) as img:
                img.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise MediaProblem("This picture can't be opened. Save it again as JPG or PNG.") from exc
        finally:
            upload.seek(0)
        return "image"
    if ext in VIDEO_TYPES:
        if upload.size > VIDEO_MAX:
            raise MediaProblem("Videos can be up to 9 MB here. Shorten it, or export it at 720p.")
        head = upload.read(12)
        upload.seek(0)
        if head[4:8] != b"ftyp":
            raise MediaProblem("This video can't be read. Export it again as MP4.")
        return "video"
    raise MediaProblem("Use a JPG, PNG or WebP picture, or an MP4 or MOV video.")


def remove(template: WaTemplate) -> None:
    if template.media:
        template.media.delete(save=False)
    template.media, template.media_kind, template.media_name = "", "", ""
    template.save(update_fields=["media", "media_kind", "media_name"])


def attach(template: WaTemplate, upload: Any) -> WaTemplate:
    if template.kind in COLD:
        raise MediaProblem(COLD_REFUSAL)
    kind = _check(upload)
    if template.media:
        template.media.delete(save=False)
    name = Path(upload.name).name[:160]
    upload.name = f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"  # never the uploader's name on disk
    template.media, template.media_kind, template.media_name = upload, kind, name
    template.save(update_fields=["media", "media_kind", "media_name"])
    return template
