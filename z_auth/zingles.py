"""
F-15: Zingles, our animated logo for special days.

- The team uploads one in the admin app (Publish -> Zingles: /api/admin/zingles/): an animated GIF or WebP (up to
  2 MB), a still PNG for reduced motion, alt text, when it runs and where (website and sign-in, dashboard, or both).
- GET /api/auth/zingle/ (public, cached 5 minutes) answers the one live now on the web (places is not `none`), the
  latest `starts_at`, or null. `?for=app` answers the latest live one with `show_app` for the Windows app (plus
  `updated_at`). Both carry `app` (it also shows in the Windows app). Its images are served from
  /api/auth/zingle/<id>/<animated|still>/ (public; `?v=` changes when the file does).
"""
from __future__ import annotations

from typing import Any

from django.core.cache import cache
from django.db.models import Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone
from rest_framework import serializers

from .models import Zingle

CACHE_KEY = "zingle:live"
APP_CACHE_KEY = "zingle:live:app"
CACHE_SECONDS = 300
MAX_BYTES = 2 * 1024 * 1024

# extension -> leading bytes (a WebP is RIFF....WEBP: checked below)
ANIMATED_TYPES = {".gif": (b"GIF87a", b"GIF89a"), ".webp": (b"RIFF",)}
STILL_TYPES = {".png": (b"\x89PNG\r\n\x1a\n",)}
CONTENT_TYPES = {
    ".gif": "image/gif", ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}


def file_url(zingle: Zingle, which: str) -> str:
    """Public path (relative to the API origin), versioned so a new upload isn't served from a stale cache."""
    return f"/api/auth/zingle/{zingle.pk}/{which}/?v={int(zingle.updated_at.timestamp())}"


def live_zingle(now: Any = None, *, app: bool = False) -> Zingle | None:
    """The latest live Zingle on the web (`places` not `none`), or with `app` the latest live one for the Windows app."""
    now = now or timezone.now()
    live = Zingle.objects.filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now), is_active=True, starts_at__lte=now)
    live = live.filter(show_app=True) if app else live.exclude(places=Zingle.Places.NONE)
    return live.order_by("-starts_at", "-id").first()


def public_zingle(app: bool = False) -> dict[str, Any] | None:
    """The live Zingle as the website and dashboard (or the Windows app) read it, or None (cached 5 minutes)."""
    key = APP_CACHE_KEY if app else CACHE_KEY
    cached = cache.get(key, "miss")
    if cached != "miss":
        return cached
    zingle = live_zingle(app=app)
    data = None if zingle is None else {
        "id": zingle.pk,
        "animated_url": file_url(zingle, "animated"),
        "still_url": file_url(zingle, "still"),
        "alt": zingle.alt,
        "places": zingle.places,
        "app": zingle.show_app,
    }
    if data is not None and app:
        data["updated_at"] = zingle.updated_at.isoformat()
    cache.set(key, data, CACHE_SECONDS)
    return data


@receiver(post_save, sender=Zingle)
@receiver(post_delete, sender=Zingle)
def _forget_live_zingle(**kwargs: Any) -> None:
    cache.delete_many([CACHE_KEY, APP_CACHE_KEY])


def _check_image(value: Any, types: dict[str, tuple[bytes, ...]], wanted: str) -> Any:
    import os

    if value.size > MAX_BYTES:
        raise serializers.ValidationError("The file is too large. The limit is 2 MB.")
    ext = os.path.splitext(value.name or "")[1].lower()
    head = value.read(16)
    value.seek(0)
    signatures = types.get(ext)
    if not signatures or not any(head.startswith(sig) for sig in signatures):
        raise serializers.ValidationError(wanted)
    if ext == ".webp" and head[8:12] != b"WEBP":
        raise serializers.ValidationError(wanted)
    return value


class ZingleSerializer(serializers.ModelSerializer):
    """GET/POST /api/admin/zingles/ and GET/PATCH /<id>/ (multipart): one Zingle plus its live status."""

    animated = serializers.FileField(write_only=True, required=True)
    still = serializers.FileField(write_only=True, required=True)
    # On by default: a multipart form that leaves it out would otherwise read as "off".
    is_active = serializers.BooleanField(required=False, default=True)
    animated_url = serializers.SerializerMethodField()
    still_url = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = Zingle
        fields = (
            "id", "name", "animated", "still", "animated_url", "still_url", "alt", "starts_at", "ends_at", "places",
            "show_app", "is_active", "status", "created_at", "updated_at",
        )
        read_only_fields = ("id", "status", "created_at", "updated_at")

    def get_animated_url(self, obj: Zingle) -> str:
        return file_url(obj, "animated")

    def get_still_url(self, obj: Zingle) -> str:
        return file_url(obj, "still")

    def get_status(self, obj: Zingle) -> str:
        """live · scheduled · ended · off; "waiting" when a newer one that's live takes its place."""
        now = timezone.now()
        if not obj.is_active:
            return "off"
        if obj.starts_at > now:
            return "scheduled"
        if obj.ends_at is not None and obj.ends_at <= now:
            return "ended"
        web = live_zingle(now) if obj.places != Zingle.Places.NONE else None
        app = live_zingle(now, app=True) if obj.show_app else None
        return "live" if obj.pk in {z.pk for z in (web, app) if z is not None} else "waiting"

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Give it a name, like Diwali 2026.")
        return value.strip()

    def validate_alt(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Describe the logo for screen readers.")
        return value.strip()

    def validate_animated(self, value: Any) -> Any:
        return _check_image(value, ANIMATED_TYPES, "Upload a GIF or WebP.")

    def validate_still(self, value: Any) -> Any:
        return _check_image(value, STILL_TYPES, "Upload a PNG.")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": "The end must be after the start."})
        places = attrs.get("places", getattr(self.instance, "places", Zingle.Places.BOTH))
        show_app = attrs.get("show_app", getattr(self.instance, "show_app", False))
        if places == Zingle.Places.NONE and not show_app:
            raise serializers.ValidationError(
                {"places": "Choose where it shows: the website, the dashboard or the Windows app."}
            )
        return attrs

    def update(self, instance: Zingle, validated_data: dict[str, Any]) -> Zingle:
        # A new file replaces the old one: the old one is deleted once the new one is saved.
        old = [getattr(instance, f) for f in ("animated", "still") if f in validated_data]
        old_names = [(f.storage, f.name) for f in old if f]
        instance = super().update(instance, validated_data)
        for storage, name in old_names:
            storage.delete(name)
        return instance
