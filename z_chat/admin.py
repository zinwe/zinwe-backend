"""
Django admin for team chat. Privacy (owner decision, FINAL): a direct chat between members is never readable here.
Conversations list shows only which kind they are; lines of direct chats are not listed at all, and no line body is
shown for one. Reports show only the line the member chose to report.
"""
from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import ChatMessage, ChatReport, ChatSettings, Conversation, ConversationMember


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "kind", "group_name", "announce", "created_at", "last_message_at")
    list_filter = ("kind", "announce")
    search_fields = ("owner__email",)
    raw_id_fields = ("owner",)
    fields = ("owner", "kind", "name", "icon", "announce", "created_at", "last_message_at")
    readonly_fields = ("kind", "created_at", "last_message_at")

    @admin.display(description="name")
    def group_name(self, obj: Conversation) -> str:
        return obj.name if obj.kind == Conversation.Kind.GROUP else "(direct chat)"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(ConversationMember)
class ConversationMemberAdmin(admin.ModelAdmin):
    list_display = ("conversation", "member", "joined_at", "left_at")
    raw_id_fields = ("conversation", "member")

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).filter(conversation__kind=Conversation.Kind.GROUP)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    """Group lines only: a direct chat's lines never appear here, by id or by search."""

    list_display = ("id", "conversation", "sender_member", "system", "created_at", "deleted_at")
    list_filter = ("system",)
    raw_id_fields = ("conversation", "sender_member", "reply_to", "task")
    readonly_fields = ("conversation", "sender_member", "body", "reply_to", "task", "system", "edited_at",
                       "deleted_at", "created_at")

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).filter(conversation__kind=Conversation.Kind.GROUP)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: ChatMessage | None = None) -> bool:
        return False


@admin.register(ChatReport)
class ChatReportAdmin(admin.ModelAdmin):
    list_display = ("id", "reporter", "kind", "created_at", "handled_at")
    raw_id_fields = ("reporter",)
    # The reported line itself (shared by the member who reported it), never the conversation around it.
    fields = ("kind", "reporter", "note", "message_body", "message_by", "message_at", "created_at", "handled_at")
    readonly_fields = ("kind", "reporter", "note", "message_body", "message_by", "message_at", "created_at")

    @admin.display(description="chat")
    def kind(self, obj: ChatReport) -> str:
        return obj.conversation.name if obj.conversation.kind == Conversation.Kind.GROUP else "(direct chat)"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(ChatSettings)
class ChatSettingsAdmin(admin.ModelAdmin):
    list_display = ("owner", "allow_member_dm", "updated_at")
    raw_id_fields = ("owner",)
