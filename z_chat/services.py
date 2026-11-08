"""
Team chat rules (brief section 1b). Views stay thin; who may read, post, edit and report lives here.

Privacy (owner decision, FINAL): nothing in this module ever hands a `direct` conversation, or a line of one, to the
owner. Owner functions take a group (`group_for_owner` filters kind="group"); the only thing of a direct chat that can
reach the owner is a line a member chose to report (ChatReport keeps a copy of just that line).
"""
from __future__ import annotations

import re

from datetime import datetime, timedelta
from typing import Any, Iterable

from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from z_auth.entitlements import TEAM_CHAT, entitlements_for
from z_auth.models import User
from z_devices.models import Member

from . import files
from .models import ChatMessage, ChatReport, ChatSettings, Conversation, ConversationMember

EDIT_WINDOW = timedelta(minutes=15)
# A removed member (or the teammate of one) keeps a read-only chat this long.
KEEP_READ_ONLY = timedelta(days=30)
PAGE = 50
MAX_PAGE = 200
MAX_PINS = 3
BODY_MAX = 4000

GROUP = Conversation.Kind.GROUP
DIRECT = Conversation.Kind.DIRECT


class ChatRefused(Exception):
    """Not allowed (a 400 on `field`, or 403 when `forbidden`)."""

    def __init__(self, message: str, field: str = "detail", forbidden: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.forbidden = forbidden


def first_name(name: str) -> str:
    return (name or "").split(" ")[0] or name or ""


def names_list(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def owner_name(owner: User) -> str:
    return getattr(owner, "full_name", "") or "Admin"


def settings_for(owner: User) -> ChatSettings:
    row = ChatSettings.objects.filter(owner=owner).first()
    return row or ChatSettings(owner=owner)


def update_settings(owner: User, data: dict[str, Any]) -> ChatSettings:
    row, _ = ChatSettings.objects.get_or_create(owner=owner)
    changed = False
    for key in ("allow_member_dm", "allow_files"):
        if key in data:
            setattr(row, key, bool(data[key]))
            changed = True
    if changed:
        row.save()
    return row


def settings_json(row: ChatSettings) -> dict[str, Any]:
    """GET|PATCH /api/chat/settings/ (file_max_bytes and file_keep_days are read-only)."""
    return {
        "allow_member_dm": row.allow_member_dm, "allow_files": row.allow_files,
        "file_max_bytes": files.FILE_MAX_BYTES, "file_keep_days": files.FILE_KEEP_DAYS,
    }


def files_allowed(owner: User) -> bool:
    """v1.5: may anyone in this company upload a chat file now (the team-chat plan and the owner's switch)."""
    return entitlements_for(owner).has(TEAM_CHAT) and settings_for(owner).allow_files


PREF_KEY = re.compile(r"^[pg][1-9][0-9]{0,9}$")
PREF_LISTS = ("pins", "muted", "marked")


def owner_prefs(owner: User) -> dict[str, list[str]]:
    """The owner's Chat list settings (pins in pin order, muted, marked as unread), kept on the server so every
    browser shows the same list. A group pinned or muted through PATCH groups/<id>/ shows here too."""
    stored = settings_for(owner).owner_prefs or {}
    out = {k: [x for x in stored.get(k, []) if isinstance(x, str) and PREF_KEY.match(x)] for k in PREF_LISTS}
    groups = Conversation.objects.filter(owner=owner, kind=GROUP).values_list("pk", "owner_pinned", "owner_muted")
    live = set()
    for pk, pinned, muted in groups:
        key = f"g{pk}"
        live.add(key)
        if pinned and key not in out["pins"] and len(out["pins"]) < MAX_PINS:
            out["pins"].append(key)
        if muted and key not in out["muted"]:
            out["muted"].append(key)
    for k in PREF_LISTS:  # a deleted group drops out
        out[k] = [x for x in out[k] if x.startswith("p") or x in live]
    return out


@transaction.atomic
def save_owner_prefs(owner: User, data: dict[str, Any]) -> dict[str, list[str]]:
    clean: dict[str, list[str]] = {}
    for k in PREF_LISTS:
        raw = data.get(k, [])
        if not isinstance(raw, list) or len(raw) > 1000 or not all(isinstance(x, str) and PREF_KEY.match(x) for x in raw):
            raise ChatRefused("A list of chat keys like p7 or g3.", field=k)
        clean[k] = list(dict.fromkeys(raw))
    if len(clean["pins"]) > MAX_PINS:
        raise ChatRefused(f"You can pin up to {MAX_PINS} chats.", field="pins")
    row, _ = ChatSettings.objects.get_or_create(owner=owner)
    row.owner_prefs = clean
    row.save(update_fields=["owner_prefs", "updated_at"])
    groups = Conversation.objects.filter(owner=owner, kind=GROUP)
    pinned = {int(x[1:]) for x in clean["pins"] if x[0] == "g"}
    muted = {int(x[1:]) for x in clean["muted"] if x[0] == "g"}
    groups.exclude(pk__in=pinned).filter(owner_pinned=True).update(owner_pinned=False)
    groups.filter(pk__in=pinned).update(owner_pinned=True)
    groups.exclude(pk__in=muted).filter(owner_muted=True).update(owner_muted=False)
    groups.filter(pk__in=muted).update(owner_muted=True)
    return owner_prefs(owner)


def chat_floor(owner: User, now: datetime | None = None) -> datetime | None:
    """The oldest line the owner's plan shows (chat_history_days); None = everything."""
    return entitlements_for(owner).chat_since(now)


# --- Groups (owner) ----------------------------------------------------------------


def groups_for_owner(owner: User) -> QuerySet:
    return Conversation.objects.filter(owner=owner, kind=GROUP).prefetch_related("people__member").order_by(
        "-owner_pinned", "-last_message_at", "-id"
    )


def group_for_owner(owner: User, group_id: int) -> Conversation | None:
    """Only ever a GROUP: a direct chat is not found for the owner, whatever its id."""
    return Conversation.objects.filter(owner=owner, kind=GROUP, pk=group_id).prefetch_related("people__member").first()


def _owner_members(owner: User, ids: Iterable[int]) -> list[Member]:
    ids = list(dict.fromkeys(ids))
    found = {m.pk: m for m in Member.objects.filter(owner=owner, pk__in=ids, is_active=True)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ChatRefused("Some of those people aren't in your team (or are switched off).", field="member_ids")
    return [found[i] for i in ids]


def _clean_name(owner: User, name: str, exclude_id: int | None = None) -> str:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ChatRefused("Give the group a name.", field="name")
    clash = Conversation.objects.filter(owner=owner, kind=GROUP, name__iexact=name)
    if exclude_id:
        clash = clash.exclude(pk=exclude_id)
    found = clash.first()
    if found:
        raise ChatRefused(f"There is already a group called {found.name}.", field="name")
    return name


def system_line(conversation: Conversation, text: str, at: datetime | None = None) -> ChatMessage:
    at = at or timezone.now()
    line = ChatMessage.objects.create(conversation=conversation, system=True, body=text, created_at=at)
    Conversation.objects.filter(pk=conversation.pk).update(last_message_at=at)
    return line


@transaction.atomic
def create_group(owner: User, name: str, icon: str = "", member_ids: list[int] | None = None,
                 announce: bool = False) -> Conversation:
    name = _clean_name(owner, name)
    members = _owner_members(owner, member_ids or [])
    if not members:
        raise ChatRefused("Pick at least one person.", field="member_ids")
    now = timezone.now()
    group = Conversation.objects.create(
        owner=owner, kind=GROUP, name=name, icon=(icon or "")[:400], announce=bool(announce), created_at=now,
        last_message_at=now, owner_last_read_at=now,
    )
    ConversationMember.objects.bulk_create([ConversationMember(conversation=group, member=m, joined_at=now) for m in members])
    text = f"You made the group and added {names_list([first_name(m.name) for m in members])}."
    if announce:
        text += " Only you can post here."
    system_line(group, text, now)
    return group_for_owner(owner, group.pk)  # type: ignore[return-value]


@transaction.atomic
def delete_group(owner: User, group: Conversation) -> None:
    """DELETE groups/<id>/: the group and everything said in it go for everyone. Its task cards' tasks stay (they
    just lose the group), and reports about it stay with the group's name."""
    key = f"g{group.pk}"
    ChatReport.objects.filter(conversation=group).update(conversation_kind=GROUP, conversation_name=group.name)
    group.delete()
    row = ChatSettings.objects.filter(owner=owner).first()
    if row and row.owner_prefs:
        prefs = {k: [x for x in v if x != key] for k, v in row.owner_prefs.items() if isinstance(v, list)}
        row.owner_prefs = prefs
        row.save(update_fields=["owner_prefs", "updated_at"])


@transaction.atomic
def update_group(owner: User, group: Conversation, data: dict[str, Any]) -> Conversation:
    """PATCH groups/<id>/: name, icon, member_ids, announce (each change is a line in the chat), pinned, muted."""
    group = Conversation.objects.select_for_update().get(pk=group.pk, kind=GROUP)
    now = timezone.now()
    lines: list[str] = []
    fields: list[str] = []
    if "name" in data:
        name = _clean_name(owner, data["name"], exclude_id=group.pk)
        if name != group.name:
            group.name = name
            fields.append("name")
            lines.append(f"You renamed the group to {name}.")
    if "icon" in data and (data["icon"] or "") != group.icon:
        group.icon = (data["icon"] or "")[:400]
        fields.append("icon")
    if "member_ids" in data:
        wanted = _owner_members(owner, data["member_ids"] or [])
        if not wanted:
            raise ChatRefused("A group needs at least one person.", field="member_ids")
        rows = {r.member_id: r for r in ConversationMember.objects.filter(conversation=group).select_related("member")}
        current = {mid for mid, r in rows.items() if r.left_at is None}
        wanted_ids = [m.pk for m in wanted]
        added = [m for m in wanted if m.pk not in current]
        removed = [rows[mid].member for mid in current if mid not in wanted_ids]
        for m in added:
            row = rows.get(m.pk)
            if row is None:
                ConversationMember.objects.create(conversation=group, member=m, joined_at=now)
            else:
                row.left_at = None
                row.joined_at = now
                row.save(update_fields=["left_at", "joined_at"])
        if removed:
            ConversationMember.objects.filter(conversation=group, member__in=removed).update(left_at=now)
        if added:
            lines.append(f"You added {names_list([first_name(m.name) for m in added])}.")
        if removed:
            lines.append(
                f"You removed {names_list([first_name(m.name) for m in removed])}. "
                "They keep what was said before they left."
            )
    if "announce" in data and bool(data["announce"]) != group.announce:
        group.announce = bool(data["announce"])
        fields.append("announce")
        lines.append("Only you can post here now." if group.announce else "Everyone can post here again.")
    if "pinned" in data and bool(data["pinned"]) != group.owner_pinned:
        group.owner_pinned = bool(data["pinned"])
        fields.append("owner_pinned")
    if "muted" in data and bool(data["muted"]) != group.owner_muted:
        group.owner_muted = bool(data["muted"])
        fields.append("owner_muted")
    if fields:
        group.save(update_fields=fields)
    for i, text in enumerate(lines):
        system_line(group, text, now + timedelta(microseconds=i))
    return group_for_owner(owner, group.pk)  # type: ignore[return-value]


def mark_all_read_by_owner(owner: User) -> int:
    return Conversation.objects.filter(owner=owner, kind=GROUP).update(owner_last_read_at=timezone.now())


# --- Membership (device side) --------------------------------------------------------


def _row(conversation: Conversation, member: Member) -> ConversationMember | None:
    return ConversationMember.objects.filter(conversation=conversation, member=member).first()


def conversations_for_member(member: Member, now: datetime | None = None) -> list[tuple[Conversation, ConversationMember]]:
    """Groups and direct chats this member sees (read-only ones too, for 30 days after they ended)."""
    now = now or timezone.now()
    keep = now - KEEP_READ_ONLY
    rows = list(
        ConversationMember.objects.filter(member=member, conversation__owner_id=member.owner_id)
        .filter(Q(left_at__isnull=True) | Q(left_at__gt=keep))
        .select_related("conversation")
        .prefetch_related("conversation__people__member")
    )
    out = []
    for row in rows:
        conv = row.conversation
        if conv.kind == DIRECT:
            other = _other(conv, member)
            if other is not None and other.left_at is not None and other.left_at <= keep:
                continue
        out.append((conv, row))
    out.sort(key=lambda cr: (not cr[1].pinned, -(cr[0].last_message_at or cr[0].created_at).timestamp(), -cr[0].pk))
    return out


def conversation_for_member(member: Member, conversation_id: int) -> tuple[Conversation, ConversationMember] | None:
    for conv, row in conversations_for_member(member):
        if conv.pk == conversation_id:
            return conv, row
    return None


def _other(conv: Conversation, member: Member) -> ConversationMember | None:
    return next((p for p in conv.people.all() if p.member_id != member.pk), None)


def shares_group(a: Member, b_id: int) -> bool:
    mine = ConversationMember.objects.filter(
        member=a, left_at__isnull=True, conversation__kind=GROUP, conversation__owner_id=a.owner_id
    ).values("conversation_id")
    return ConversationMember.objects.filter(member_id=b_id, left_at__isnull=True, conversation_id__in=mine).exists()


def can_post(conv: Conversation, row: ConversationMember | None, member: Member | None) -> bool:
    """The owner posts in any group; a member in a group they're still in (not an announce group), or a live direct."""
    if member is None:
        return conv.kind == GROUP
    if row is None or row.left_at is not None or not member.is_active:
        return False
    if conv.kind == GROUP:
        return not conv.announce
    other = _other(conv, member)
    return bool(
        other is not None and other.left_at is None and other.member.is_active
        and settings_for(member.owner).allow_member_dm and shares_group(member, other.member_id)
    )


def people_for(member: Member) -> list[dict[str, Any]]:
    """GET people/: the active teammates this member shares a group with (none while the owner turned direct chats off)."""
    if not settings_for(member.owner).allow_member_dm:
        return []
    mine = ConversationMember.objects.filter(
        member=member, left_at__isnull=True, conversation__kind=GROUP, conversation__owner_id=member.owner_id
    ).values("conversation_id")
    ids = set(
        ConversationMember.objects.filter(conversation_id__in=mine, left_at__isnull=True)
        .exclude(member=member).values_list("member_id", flat=True)
    )
    directs = {
        r.member_id: r.conversation_id
        for r in ConversationMember.objects.filter(
            conversation__kind=DIRECT, conversation__owner_id=member.owner_id,
            conversation_id__in=ConversationMember.objects.filter(member=member).values("conversation_id"),
        ).exclude(member=member)
    }
    out = []
    for m in Member.objects.filter(pk__in=ids, is_active=True, owner_id=member.owner_id).order_by("name"):
        out.append({"member_id": m.pk, "name": m.name, "role": m.role, "direct_id": directs.get(m.pk), "_m": m})
    return out


@transaction.atomic
def start_direct(member: Member, other_id: int) -> tuple[Conversation, ConversationMember]:
    if other_id == member.pk:
        raise ChatRefused("Pick a teammate.", field="member_id")
    if not settings_for(member.owner).allow_member_dm:
        raise ChatRefused("Your admin has turned off chats between team members.", forbidden=True)
    other = Member.objects.filter(pk=other_id, owner_id=member.owner_id, is_active=True).first()
    if other is None or not shares_group(member, other.pk):
        raise ChatRefused("You can message people you share a group with.", field="member_id")
    key = f"{min(member.pk, other.pk)}:{max(member.pk, other.pk)}"
    conv = Conversation.objects.filter(owner_id=member.owner_id, kind=DIRECT, direct_key=key).first()
    if conv is None:
        now = timezone.now()
        try:
            with transaction.atomic():
                conv = Conversation.objects.create(owner_id=member.owner_id, kind=DIRECT, direct_key=key, created_at=now)
        except IntegrityError:
            conv = Conversation.objects.get(owner_id=member.owner_id, kind=DIRECT, direct_key=key)
        for m in (member, other):
            ConversationMember.objects.get_or_create(conversation=conv, member=m, defaults={"joined_at": now})
    found = conversation_for_member(member, conv.pk)
    if found is None:
        raise ChatRefused("That chat has ended.", field="member_id")
    return found


def member_left(member: Member) -> int:
    """A member was switched off: they leave every group and direct chat (the others keep a read-only copy)."""
    return ConversationMember.objects.filter(member=member, left_at__isnull=True).update(left_at=timezone.now())


def set_member_flags(row: ConversationMember, data: dict[str, Any]) -> ConversationMember:
    """PATCH <id>/ (device): pinned (at most 3), muted, unread (mark as unread)."""
    fields = []
    if "pinned" in data and bool(data["pinned"]) != row.pinned:
        if data["pinned"] and ConversationMember.objects.filter(member_id=row.member_id, pinned=True).count() >= MAX_PINS:
            raise ChatRefused("You can pin up to 3 chats.", field="pinned")
        row.pinned = bool(data["pinned"])
        fields.append("pinned")
    if "muted" in data:
        row.muted = bool(data["muted"])
        fields.append("muted")
    if data.get("unread"):
        last = (
            ChatMessage.objects.filter(conversation_id=row.conversation_id, system=False, deleted_at__isnull=True)
            .exclude(sender_member_id=row.member_id).order_by("-created_at", "-id").first()
        )
        if last is not None:
            row.last_read_at = last.created_at - timedelta(microseconds=1)
            fields.append("last_read_at")
    if fields:
        row.save(update_fields=list(dict.fromkeys(fields)))
    return row


# --- Lines ---------------------------------------------------------------------------


def visible_lines(conv: Conversation, row: ConversationMember | None) -> QuerySet:
    """What the viewer may read: the plan's chat history, and for a member only while they were in it."""
    qs = ChatMessage.objects.filter(conversation=conv)
    floor = chat_floor(conv.owner)
    if floor is not None:
        qs = qs.filter(created_at__gte=floor)
    if row is not None:
        qs = qs.filter(created_at__gte=row.joined_at - timedelta(seconds=1))
        if row.left_at is not None:
            qs = qs.filter(created_at__lte=row.left_at)
    return qs


def page(conv: Conversation, row: ConversationMember | None, before: int | None = None, after: int | None = None,
         limit: int = PAGE) -> list[ChatMessage]:
    limit = max(1, min(limit or PAGE, MAX_PAGE))
    qs = visible_lines(conv, row).select_related("sender_member", "reply_to__sender_member", "chat_file")
    if after:
        return list(qs.filter(pk__gt=after).order_by("created_at", "id")[:limit])
    if before:
        qs = qs.filter(pk__lt=before)
    return list(qs.order_by("-created_at", "-id")[:limit])[::-1]


def unread_count(conv: Conversation, row: ConversationMember | None) -> int:
    """Lines by someone else after the viewer's last read (row None = the owner of a group)."""
    qs = visible_lines(conv, row).filter(deleted_at__isnull=True, system=False)
    if row is None:
        qs = qs.exclude(sender_member__isnull=True)
        read = conv.owner_last_read_at
    else:
        qs = qs.exclude(sender_member_id=row.member_id)
        read = row.last_read_at
    if read is not None:
        qs = qs.filter(created_at__gt=read)
    return qs.count()


def last_line(conv: Conversation, row: ConversationMember | None) -> ChatMessage | None:
    return visible_lines(conv, row).filter(deleted_at__isnull=True).select_related("sender_member").order_by(
        "-created_at", "-id").first()


def _clean_body(body: str) -> str:
    body = (body or "").strip()[:BODY_MAX]
    if not body:
        raise ChatRefused("Write a message.", field="body")
    return body


@transaction.atomic
def post(conv: Conversation, body: str, member: Member | None, row: ConversationMember | None = None,
         reply_to_id: int | None = None, task: Any = None, file_id: Any = None) -> ChatMessage:
    """A line by a member (row = theirs) or by the owner (member None, groups only). v1.5: with `file_id` (the
    sender's own upload) the line carries that file; `body` is then its caption and may be blank, and the stored body
    is the fallback text old clients show."""
    if member is None and conv.kind != GROUP:
        raise ChatRefused("Not found.", forbidden=True)
    if not can_post(conv, row, member):
        if member is not None and conv.kind == GROUP and conv.announce and row is not None and row.left_at is None:
            raise ChatRefused("Only the admin can post in this group.", forbidden=True)
        raise ChatRefused("You can't post in this chat.", forbidden=True)
    chat_file = caption = None
    if file_id:
        try:
            chat_file = files.claim(conv.owner_id, member.pk if member else None, file_id)
        except files.FileRefused as exc:
            raise ChatRefused(exc.message, field=exc.field) from None
        caption = files.clean_caption(body)
        body = files.fallback_body(chat_file, caption)[:BODY_MAX]
    else:
        body = _clean_body(body)
    reply_to = None
    if reply_to_id:
        reply_to = visible_lines(conv, row).filter(pk=reply_to_id, system=False).first()
        if reply_to is None:
            raise ChatRefused("That message isn't in this chat.", field="reply_to")
    now = timezone.now()
    line = ChatMessage.objects.create(
        conversation=conv, sender_member=member, body=body, reply_to=reply_to, task=task, created_at=now,
    )
    if chat_file is not None:
        try:
            files.attach(chat_file, caption or "", line=line)
        except files.FileRefused as exc:
            raise ChatRefused(exc.message, field=exc.field) from None
        line.chat_file = chat_file
    Conversation.objects.filter(pk=conv.pk).update(last_message_at=now)
    if member is None:
        Conversation.objects.filter(pk=conv.pk).update(owner_last_read_at=now)
    elif row is not None:
        ConversationMember.objects.filter(pk=row.pk).update(last_read_at=now)
    return line


def post_task_card(group: Conversation, task: Any) -> ChatMessage:
    """The owner's task card line in a group (z_devices.work_tasks): its body is the task's title."""
    now = timezone.now()
    line = ChatMessage.objects.create(
        conversation=group, sender_member=None, body=(task.title or "")[:BODY_MAX], task=task, created_at=now
    )
    Conversation.objects.filter(pk=group.pk).update(last_message_at=now, owner_last_read_at=now)
    return line


def _own_recent(line: ChatMessage, member: Member | None) -> None:
    mine = line.sender_member_id is None if member is None else line.sender_member_id == member.pk
    if not mine or line.system:
        raise ChatRefused("You can only change your own messages.", forbidden=True)
    if line.deleted_at is not None:
        raise ChatRefused("This message was deleted.")
    if timezone.now() - line.created_at > EDIT_WINDOW:
        raise ChatRefused("You can change a message for 15 minutes after sending it.")


def edit(line: ChatMessage, body: str, member: Member | None) -> ChatMessage:
    _own_recent(line, member)
    if files.file_of(line) is not None:
        raise ChatRefused(files.NO_EDIT, field="body")
    line.body = _clean_body(body)
    line.edited_at = timezone.now()
    line.save(update_fields=["body", "edited_at"])
    return line


@transaction.atomic
def delete(line: ChatMessage, member: Member | None) -> ChatMessage:
    """Delete for everyone: the text is gone for good (the line stays as "deleted"), and its file too (v1.5)."""
    _own_recent(line, member)
    line.body = ""
    line.deleted_at = timezone.now()
    line.save(update_fields=["body", "deleted_at"])
    files.drop(line)
    return line


def mark_read(conv: Conversation, row: ConversationMember | None) -> None:
    now = timezone.now()
    if row is None:
        Conversation.objects.filter(pk=conv.pk, kind=GROUP).update(owner_last_read_at=now)
    else:
        ConversationMember.objects.filter(pk=row.pk).update(last_read_at=now)


# --- Reports ---------------------------------------------------------------------------


@transaction.atomic
def report(conv: Conversation, row: ConversationMember, member: Member, message_id: int | None, note: str) -> ChatReport:
    """A member reports a line (or the chat) to the owner. Only that line goes to the owner, as it was when reported."""
    line = None
    if message_id:
        line = visible_lines(conv, row).filter(pk=message_id, system=False).select_related("sender_member").first()
        if line is None:
            raise ChatRefused("That message isn't in this chat.", field="message_id")
        if line.sender_member_id == member.pk:
            raise ChatRefused("You can't report your own message.", field="message_id")
    note = (note or "").strip()[:1000]
    if line is None and not note:
        raise ChatRefused("Say what is wrong.", field="note")
    return ChatReport.objects.create(
        conversation=conv, conversation_kind=conv.kind, conversation_name=conv.name if conv.kind == GROUP else "",
        message=line, reporter=member, note=note,
        message_body=line.body if line else "",
        message_by=(line.sender_member.name if line.sender_member_id else owner_name(conv.owner)) if line else "",
        message_at=line.created_at if line else None,
    )


def reports_for_owner(owner: User) -> QuerySet:
    return ChatReport.objects.filter(reporter__owner=owner).select_related("conversation", "reporter").order_by(
        "handled_at", "-created_at", "-id"
    )


def handle_report(owner: User, report_id: int) -> ChatReport | None:
    row = ChatReport.objects.filter(reporter__owner=owner, pk=report_id).first()
    if row is not None and row.handled_at is None:
        row.handled_at = timezone.now()
        row.save(update_fields=["handled_at"])
    return row


# --- JSON ------------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def member_text(text: str, owner: User) -> str:
    """A system line as a member reads it: "You added Karan." -> "Ananya added Karan."."""
    first = first_name(owner_name(owner))
    if text.startswith("You "):
        text = f"{first} {text[4:]}"
    return text.replace("Only you can post", f"Only {first} can post")


def _by(line: ChatMessage, owner: User) -> dict[str, Any]:
    if line.system:
        return {"kind": "system", "member_id": None, "name": ""}
    if line.sender_member_id is None:
        return {"kind": "owner", "member_id": None, "name": owner_name(owner)}
    return {"kind": "member", "member_id": line.sender_member_id,
            "name": line.sender_member.name if line.sender_member else ""}


def readers(conv: Conversation) -> list[tuple[int | None, str, datetime | None]]:
    """(member id or None for the owner, name, last read) of everyone in the chat now."""
    out: list[tuple[int | None, str, datetime | None]] = []
    if conv.kind == GROUP:
        out.append((None, owner_name(conv.owner), conv.owner_last_read_at))
    for p in conv.people.all():
        if p.left_at is None:
            out.append((p.member_id, p.member.name, p.last_read_at))
    return out


def line_json(line: ChatMessage, conv: Conversation, viewer_member: Member | None,
              who_read: list[tuple[int | None, str, datetime | None]] | None = None,
              request: Any = None) -> dict[str, Any]:
    """A line. v1.5 `file`: FileJSON or null (null on a deleted line); its url is the device route for a member."""
    owner = conv.owner
    deleted = line.deleted_at is not None
    body = "" if deleted else line.body
    if line.system and viewer_member is not None:
        body = member_text(body, owner)
    reply = None
    if line.reply_to_id and line.reply_to is not None:
        r = line.reply_to
        reply = {"id": r.pk, "by": _by(r, owner), "body": "" if r.deleted_at else r.body[:300]}
    seen_by = []
    if not line.system and who_read is not None:
        sender = line.sender_member_id
        for mid, name, read in who_read:
            if read is not None and read >= line.created_at and not (mid == sender):
                if sender is None and mid is None:
                    continue
                seen_by.append({"member_id": mid, "name": name})
    return {
        "id": line.pk, "by": _by(line, owner), "body": body, "reply_to": reply, "task_id": line.task_id,
        "system": line.system, "edited": line.edited_at is not None, "deleted": deleted,
        "at": _iso(line.created_at), "seen_by": seen_by,
        "file": None if deleted else files.file_json(files.file_of(line), request, device=viewer_member is not None),
    }


def lines_json(lines: list[ChatMessage], conv: Conversation, viewer_member: Member | None,
               request: Any = None) -> list[dict[str, Any]]:
    who = readers(conv)
    return [line_json(line, conv, viewer_member, who, request) for line in lines]


def _last_json(line: ChatMessage | None, conv: Conversation, viewer_member: Member | None) -> dict[str, Any] | None:
    if line is None:
        return None
    if line.system:
        text, by = (member_text(line.body, conv.owner) if viewer_member else line.body), ""
    else:
        text = line.body
        by = first_name(owner_name(conv.owner)) if line.sender_member_id is None else first_name(
            line.sender_member.name if line.sender_member else "")
    return {"text": text[:140], "by": by, "at": _iso(line.created_at), "task_id": line.task_id}


def _people_json(conv: Conversation, request: Any = None) -> list[dict[str, Any]]:
    from z_devices.pictures import picture_payload

    return [
        {"member_id": p.member_id, "name": p.member.name, "role": p.member.role,
         "picture": picture_payload(p.member, request)}
        for p in conv.people.all() if p.left_at is None
    ]


def group_json(conv: Conversation, request: Any = None) -> dict[str, Any]:
    """A group as the owner sees it."""
    people = _people_json(conv, request)
    return {
        "id": conv.pk, "kind": GROUP, "name": conv.name, "icon": conv.icon, "announce": conv.announce,
        "members": people, "member_ids": [p["member_id"] for p in people],
        "unread": unread_count(conv, None), "last": _last_json(last_line(conv, None), conv, None),
        "last_message_at": _iso(conv.last_message_at), "created_at": _iso(conv.created_at),
        "pinned": conv.owner_pinned, "muted": conv.owner_muted,
    }


def conversation_json(conv: Conversation, row: ConversationMember, member: Member, request: Any = None) -> dict[str, Any]:
    """A group or direct chat as the member sees it in their Zinwe window."""
    from z_devices.pictures import picture_payload

    postable = can_post(conv, row, member)
    data: dict[str, Any] = {
        "id": conv.pk, "kind": conv.kind, "announce": conv.announce if conv.kind == GROUP else False,
        "unread": unread_count(conv, row), "last": _last_json(last_line(conv, row), conv, member),
        "last_message_at": _iso(conv.last_message_at), "pinned": row.pinned, "muted": row.muted,
        # Ended (they left, or a direct chat that can't go on): history only. An announce group is not read-only.
        "read_only": row.left_at is not None or (conv.kind == DIRECT and not postable),
        "can_post": postable,
    }
    if conv.kind == GROUP:
        data.update(name=conv.name, icon=conv.icon, member_id=None, picture=None, members=_people_json(conv, request))
    else:
        other = _other(conv, member)
        data.update(
            name=other.member.name if other else "", icon="", member_id=other.member_id if other else None,
            picture=picture_payload(other.member, request) if other else None, members=[],
        )
    return data


def report_json(row: ChatReport) -> dict[str, Any]:
    """A report as the owner sees it: the reported line only (never the rest of a direct chat)."""
    conv = row.conversation
    if conv is None:  # the owner deleted the group; a direct chat is never deleted by the owner
        if row.conversation_kind == DIRECT:
            where = {"id": None, "kind": DIRECT, "name": "A chat between team members"}
        else:
            where = {"id": None, "kind": GROUP, "name": f"{row.conversation_name or 'A group'} (deleted)"}
    elif conv.kind == GROUP:
        where = {"id": conv.pk, "kind": GROUP, "name": conv.name}
    else:
        where = {"id": None, "kind": DIRECT, "name": "A chat between team members"}
    return {
        "id": row.pk, "conversation": where,
        "message": {"id": row.message_id, "body": row.message_body, "by": row.message_by, "at": _iso(row.message_at)}
        if row.message_body or row.message_id else None,
        "reporter": {"member_id": row.reporter_id, "name": row.reporter.name},
        "note": row.note, "at": _iso(row.created_at), "handled": row.handled_at is not None,
        "handled_at": _iso(row.handled_at),
    }
