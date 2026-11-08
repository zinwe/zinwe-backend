"""
v1.4 team chat (brief §1b): groups and direct chats between members.

Privacy (owner decision, FINAL): the owner / admin can NEVER read a `direct` conversation: no owner endpoint returns
one, and the Django admin shows no direct message at all. The owner is in every group (not a ConversationMember row:
the owner's own read / pin / mute state lives on the Conversation). A member may start a direct chat only with someone
they share a group with, and only while the owner's `allow_member_dm` is on. Rules: z_chat.services.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class Conversation(models.Model):
    class Kind(models.TextChoices):
        GROUP = "group", "Group"
        DIRECT = "direct", "Direct"

    owner = models.ForeignKey("z_auth.User", on_delete=models.CASCADE, related_name="chat_conversations")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.GROUP)
    name = models.CharField(max_length=60, blank=True)
    # A picture key or an SVG path from the dashboard's group picture set ("" = initials).
    icon = models.CharField(max_length=400, blank=True)
    # Only the owner posts (news and notices).
    announce = models.BooleanField(default=False)
    # Directs only: "<lower member id>:<higher member id>", so a pair has one direct chat.
    direct_key = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    last_message_at = models.DateTimeField(null=True, blank=True)
    # The owner's own view of a group (the owner is in every group).
    owner_last_read_at = models.DateTimeField(null=True, blank=True)
    owner_pinned = models.BooleanField(default=False)
    owner_muted = models.BooleanField(default=False)

    class Meta:
        ordering = ["-last_message_at", "-id"]
        indexes = [models.Index(fields=["owner", "kind"])]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "direct_key"], condition=Q(kind="direct"), name="uniq_direct_pair_per_owner"
            ),
        ]

    def __str__(self) -> str:
        # Never the body of anything; for a direct chat not even who talks (only that it is one).
        return f"{self.name} (group {self.pk})" if self.kind == self.Kind.GROUP else f"Direct chat {self.pk}"


class ConversationMember(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="people")
    member = models.ForeignKey("z_devices.Member", on_delete=models.CASCADE, related_name="chat_memberships")
    joined_at = models.DateTimeField(default=timezone.now)
    # Removed from the group (or switched off): they keep what was said before, read-only.
    left_at = models.DateTimeField(null=True, blank=True)
    last_read_at = models.DateTimeField(null=True, blank=True)
    muted = models.BooleanField(default=False)
    pinned = models.BooleanField(default=False)

    class Meta:
        ordering = ["joined_at", "id"]
        constraints = [models.UniqueConstraint(fields=["conversation", "member"], name="uniq_conversation_member")]
        indexes = [models.Index(fields=["member", "left_at"])]

    def __str__(self) -> str:
        return f"Member {self.member_id} in conversation {self.conversation_id}"


class ChatMessage(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    # Null = the owner / admin.
    sender_member = models.ForeignKey(
        "z_devices.Member", null=True, blank=True, on_delete=models.SET_NULL, related_name="chat_messages"
    )
    body = models.TextField(blank=True, max_length=4000)
    reply_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replies")
    # A task card line (z_devices.Task).
    task = models.ForeignKey("z_devices.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="chat_cards")
    # A line Zinwe writes ("You added Karan."), not a person.
    system = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self) -> str:
        return f"Line {self.pk} in conversation {self.conversation_id}"


class ChatReport(models.Model):
    """A member reported a line (or a whole chat) to the owner. Only the reported part reaches the owner."""

    # Null once the owner deleted the group: the report stays, with the chat's kind and name kept below.
    conversation = models.ForeignKey(Conversation, null=True, blank=True, on_delete=models.SET_NULL, related_name="reports")
    conversation_kind = models.CharField(max_length=8, blank=True, default="")
    conversation_name = models.CharField(max_length=60, blank=True, default="")
    message = models.ForeignKey(ChatMessage, null=True, blank=True, on_delete=models.SET_NULL, related_name="reports")
    reporter = models.ForeignKey("z_devices.Member", on_delete=models.CASCADE, related_name="chat_reports")
    note = models.TextField(blank=True, max_length=1000)
    # What the reported line said when it was reported (the member chose to share it), and who wrote it.
    message_body = models.TextField(blank=True, max_length=4000)
    message_by = models.CharField(max_length=150, blank=True)
    message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    handled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Report {self.pk} ({'handled' if self.handled_at else 'new'})"


class ChatSettings(models.Model):
    owner = models.OneToOneField("z_auth.User", on_delete=models.CASCADE, related_name="chat_settings")
    # Members may start direct chats with teammates they share a group with.
    allow_member_dm = models.BooleanField(default=True)
    # v1.5: anyone in the company may share files in chat (the owner included). db_default too, so the previous
    # release (which doesn't know the column) can still create settings rows.
    allow_files = models.BooleanField(default=True, db_default=True)
    # The owner's own Chat list settings, the same on every browser: {"pins": ["p7", "g3"], "muted": [...],
    # "marked": [...]} ("p<member id>" = the owner's chat with that person, "g<group id>" = a group).
    owner_prefs = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Chat settings for {self.owner_id}"


class ChatFile(models.Model):
    """
    v1.5 file sharing in chat ("send and forget"): one file, uploaded once, attached to one line of a group or direct
    chat (`chat_line`) or of the owner <-> member admin chat (`member_message`). Kept FILE_KEEP_DAYS after the line was
    posted, then the disk file goes (`deleted_at`) and the row stays so the line says "no longer available". Stored at
    <CHAT_FILES_ROOT>/<owner_id>/<id> (outside MEDIA_ROOT: never in the backups). Rules: z_chat.files.

    Privacy: never registered in the Django admin, so a file of a direct chat is never visible there.
    """

    class Kind(models.TextChoices):
        IMAGE = "image", "Image"
        FILE = "file", "File"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey("z_auth.User", on_delete=models.CASCADE, related_name="chat_files")
    # Null = the owner uploaded it.
    by_member = models.ForeignKey(
        "z_devices.Member", null=True, blank=True, on_delete=models.CASCADE, related_name="chat_files"
    )
    name = models.CharField(max_length=200)
    mime = models.CharField(max_length=100, blank=True)  # as the sender claimed it; informational only
    size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.FILE)
    caption = models.TextField(blank=True, max_length=4000)
    chat_line = models.OneToOneField(
        ChatMessage, null=True, blank=True, on_delete=models.CASCADE, related_name="chat_file"
    )
    member_message = models.OneToOneField(
        "z_devices.Message", null=True, blank=True, on_delete=models.CASCADE, related_name="chat_file"
    )
    created_at = models.DateTimeField(default=timezone.now)
    attached_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # The disk file was removed (expired); the row stays so the line says "no longer available".
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "by_member", "attached_at"]),
            models.Index(fields=["expires_at", "deleted_at"]),
            models.Index(fields=["attached_at", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Chat file {self.pk}"

    @property
    def path(self) -> Path:
        return Path(settings.CHAT_FILES_ROOT) / str(self.owner_id) / str(self.pk)


def remove_chat_file(path: Path) -> None:
    """Remove a stored chat file; already gone is fine."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _chat_file_deleted(sender: type, instance: ChatFile, **kwargs: object) -> None:
    """A ChatFile row went (its line was deleted, chat history clean-up, any cascade): the disk file goes too, once the
    delete is committed (a rolled-back delete keeps it)."""
    path = instance.path
    transaction.on_commit(lambda: remove_chat_file(path))


models.signals.post_delete.connect(_chat_file_deleted, sender=ChatFile, dispatch_uid="z_chat_chat_file_deleted")
