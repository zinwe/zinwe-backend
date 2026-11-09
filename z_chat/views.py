"""
Team chat HTTP layer (brief section 1b). Owner routes (/api/chat/) only ever reach GROUPS; device routes
(/api/devices/chat/) reach the PC member's own groups and direct chats. Rules: services.py.
"""
from __future__ import annotations

from typing import Any

from django.http import Http404, HttpResponse
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from z_auth import entitlements as plan
from z_devices.authentication import DeviceScopedThrottle, DeviceTokenAuthentication, IsDevice, IsOwner

from . import files, services
from .models import ChatMessage


def _refused(exc: services.ChatRefused) -> Response:
    if exc.forbidden:
        return Response({"detail": exc.message}, status=status.HTTP_403_FORBIDDEN)
    return Response({exc.field: [exc.message]}, status=status.HTTP_400_BAD_REQUEST)


class GroupInSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=60, trim_whitespace=True, required=False, allow_blank=True)
    icon = serializers.CharField(max_length=400, required=False, allow_blank=True)
    member_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=500)
    announce = serializers.BooleanField(required=False)
    pinned = serializers.BooleanField(required=False)
    muted = serializers.BooleanField(required=False)


class FileOptionalMixin:
    """v1.5: with a `file_id` the text field (`optional_with_file`) may be blank or left out (it becomes the caption).
    Without one it stays required exactly as before, so old clients see the same errors."""

    optional_with_file: tuple[str, ...] = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[call-arg]
        data = kwargs.get("data")
        if hasattr(data, "get") and data.get("file_id"):
            for name in self.optional_with_file:
                f = self.fields[name]  # type: ignore[attr-defined]
                f.required = False
                f.allow_blank = True
                f.default = ""


def file_id_field() -> serializers.UUIDField:
    return serializers.UUIDField(required=False, allow_null=True, default=None,
                                 error_messages={"invalid": files.NOT_FOUND})


class LineInSerializer(FileOptionalMixin, serializers.Serializer):
    optional_with_file = ("body",)

    body = serializers.CharField(max_length=services.BODY_MAX, trim_whitespace=True)
    reply_to = serializers.IntegerField(min_value=1, required=False, allow_null=True, default=None)
    file_id = file_id_field()


class LineEditSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=services.BODY_MAX, trim_whitespace=True)


class PageQuerySerializer(serializers.Serializer):
    before = serializers.IntegerField(min_value=1, required=False, default=None)
    after = serializers.IntegerField(min_value=1, required=False, default=None)
    limit = serializers.IntegerField(min_value=1, max_value=services.MAX_PAGE, required=False, default=services.PAGE)


# --- Owner ---------------------------------------------------------------------------


class _OwnerView(APIView):
    permission_classes = [IsOwner]

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        plan.require(request.user, plan.TEAM_CHAT)

    def group(self, request: Request, group_id: int):  # type: ignore[no-untyped-def]
        group = services.group_for_owner(request.user, group_id)
        if group is None:  # a direct chat's id is "not found" too: the owner never reaches one
            raise Http404
        return group


class GroupListView(_OwnerView):
    """GET /api/chat/groups/ -> [Group]; POST {name, icon?, member_ids, announce?} -> Group (201)."""

    def get(self, request: Request) -> Response:
        return Response([services.group_json(g, request) for g in services.groups_for_owner(request.user)])

    def post(self, request: Request) -> Response:
        data = GroupInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        try:
            group = services.create_group(request.user, v.get("name", ""), v.get("icon", ""), v.get("member_ids", []),
                                          v.get("announce", False))
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.group_json(group, request), status=status.HTTP_201_CREATED)


class GroupDetailView(_OwnerView):
    """GET /api/chat/groups/<id>/; PATCH {name?, icon?, member_ids?, announce?, pinned?, muted?} -> Group; DELETE -> 204."""

    def get(self, request: Request, group_id: int) -> Response:
        return Response(services.group_json(self.group(request, group_id), request))

    def patch(self, request: Request, group_id: int) -> Response:
        group = self.group(request, group_id)
        data = GroupInSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        try:
            group = services.update_group(request.user, group, dict(data.validated_data))
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.group_json(group, request))

    def delete(self, request: Request, group_id: int) -> Response:
        services.delete_group(request.user, self.group(request, group_id))
        return Response(status=status.HTTP_204_NO_CONTENT)


class GroupMessagesView(_OwnerView):
    """GET /api/chat/groups/<id>/messages/?before=<id>&after=<id> -> [Line]; POST {body, reply_to?} -> Line (201)."""

    def get(self, request: Request, group_id: int) -> Response:
        group = self.group(request, group_id)
        q = PageQuerySerializer(data=request.query_params)
        q.is_valid(raise_exception=True)
        lines = services.page(group, None, q.validated_data["before"], q.validated_data["after"], q.validated_data["limit"])
        return Response(services.lines_json(lines, group, None, request))

    def post(self, request: Request, group_id: int) -> Response:
        group = self.group(request, group_id)
        data = LineInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            line = services.post(group, data.validated_data["body"], None, None, data.validated_data["reply_to"],
                                 file_id=data.validated_data["file_id"])
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, group, None, services.readers(group), request), status=status.HTTP_201_CREATED)


def _line(group: Any, line_id: int) -> ChatMessage:
    line = ChatMessage.objects.filter(conversation=group, pk=line_id).select_related(
        "sender_member", "reply_to__sender_member", "chat_file__by_member", "chat_file__owner").first()
    if line is None:
        raise Http404
    return line


class GroupMessageView(_OwnerView):
    """PATCH /api/chat/groups/<id>/messages/<mid>/ {body} | DELETE -> Line (own lines, within 15 minutes)."""

    def patch(self, request: Request, group_id: int, line_id: int) -> Response:
        group = self.group(request, group_id)
        data = LineEditSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            line = services.edit(_line(group, line_id), data.validated_data["body"], None)
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, group, None, services.readers(group), request))

    def delete(self, request: Request, group_id: int, line_id: int) -> Response:
        group = self.group(request, group_id)
        try:
            line = services.delete(_line(group, line_id), None)
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, group, None, services.readers(group), request))


class GroupReadView(_OwnerView):
    """POST /api/chat/groups/<id>/read/."""

    def post(self, request: Request, group_id: int) -> Response:
        services.mark_read(self.group(request, group_id), None)
        return Response({"ok": True})


class ReadAllView(_OwnerView):
    """POST /api/chat/read-all/ -- Mark all as read (every group)."""

    def post(self, request: Request) -> Response:
        return Response({"ok": True, "groups": services.mark_all_read_by_owner(request.user)})


class ReportListView(_OwnerView):
    """GET /api/chat/reports/ -> [Report] (only the reported line, never the rest of a direct chat)."""

    def get(self, request: Request) -> Response:
        return Response([services.report_json(r) for r in services.reports_for_owner(request.user)[:200]])


class ReportHandledView(_OwnerView):
    """POST /api/chat/reports/<id>/handled/ -> Report."""

    def post(self, request: Request, report_id: int) -> Response:
        row = services.handle_report(request.user, report_id)
        if row is None:
            raise Http404
        return Response(services.report_json(row))


class SettingsView(_OwnerView):
    """GET|PATCH /api/chat/settings/ -- {allow_member_dm, allow_files, file_max_bytes, file_keep_days}; the last two
    are read-only. A PATCH sends either switch or both."""

    def get(self, request: Request) -> Response:
        return Response(services.settings_json(services.settings_for(request.user)))

    def patch(self, request: Request) -> Response:
        data = request.data if hasattr(request.data, "get") else {}
        keys = [k for k in ("allow_member_dm", "allow_files") if k in data]
        if not keys:
            return Response({"allow_member_dm": ["true or false."]}, status=status.HTTP_400_BAD_REQUEST)
        for k in keys:
            if not isinstance(data.get(k), bool):
                return Response({k: ["true or false."]}, status=status.HTTP_400_BAD_REQUEST)
        row = services.update_settings(request.user, {k: data[k] for k in keys})
        return Response(services.settings_json(row))

    put = patch


# --- Files (v1.5 round 2: the file stays on the sender's device) -----------------------------------------------------


class FileRegisterSerializer(serializers.Serializer):
    """POST .../files/ (JSON): the record of a file the sender holds. No bytes ever come here."""

    name = serializers.CharField(max_length=1000, trim_whitespace=False)  # made safe by files.safe_name
    size = serializers.IntegerField(min_value=0, max_value=2 ** 53)
    mime = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    sha256 = serializers.CharField(max_length=128, required=False, allow_blank=True, allow_null=True, default="")
    local_ref = serializers.CharField(max_length=files.LOCAL_REF_MAX, trim_whitespace=False)
    thumb_b64 = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    width = serializers.IntegerField(min_value=1, max_value=100000, required=False, allow_null=True, default=None)
    height = serializers.IntegerField(min_value=1, max_value=100000, required=False, allow_null=True, default=None)
    duration_s = serializers.FloatField(min_value=0, max_value=10 ** 7, required=False, allow_null=True, default=None)
    batch_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    browser_id = serializers.CharField(max_length=64, required=False, allow_blank=True, allow_null=True, default=None)


class FilePatchSerializer(serializers.Serializer):
    sha256 = serializers.CharField(max_length=128, required=False, allow_blank=True)
    gone = serializers.BooleanField(required=False)


def _file_refused(exc: files.FileRefused) -> Response:
    if exc.forbidden:
        return Response({"detail": exc.message}, status=status.HTTP_403_FORBIDDEN)
    return Response({exc.field: [exc.message]}, status=status.HTTP_400_BAD_REQUEST)


def _json(request: Request, row: Any, member: Any, device: Any) -> dict[str, Any] | None:
    return files.file_json(row, request, device=device is not None,
                           viewer_member_id=member.pk if member is not None else None)


def _register(request: Request, owner: Any, member: Any, device: Any) -> Response:
    """POST -> 201 FileJSON, not attached yet. The holder is the device, or the owner's browser (browser_id)."""
    if not services.settings_for(owner).allow_files:
        return Response({"detail": files.SHARING_OFF}, status=status.HTTP_403_FORBIDDEN)
    data = FileRegisterSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    v = data.validated_data
    try:
        holder = files.device_holder(device) if device is not None else files.browser_holder(owner.pk, v["browser_id"])
        row = files.register(owner.pk, member.pk if member is not None else None, holder,
                             plan.entitlements_for(owner).file_max_mb, v)
    except files.FileRefused as exc:
        return _file_refused(exc)
    row.owner, row.by_member = owner, member
    return Response(_json(request, row, member, device), status=status.HTTP_201_CREATED)


def _patch(request: Request, row: Any, owner: Any, member: Any, device: Any) -> Response:
    """PATCH .../files/<id>/ {sha256?} | {gone: true} -- the holder only (anyone else: 404)."""
    if not files.is_holder(row, owner, device):
        raise Http404
    data = FilePatchSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    v = data.validated_data
    if "sha256" not in v and not v.get("gone"):
        return Response({"sha256": ["Send sha256, or gone: true."]}, status=status.HTTP_400_BAD_REQUEST)
    try:
        files.patch(row, v.get("sha256"), bool(v.get("gone")))
    except files.FileRefused as exc:
        return _file_refused(exc)
    return Response(_json(request, row, member, device))


def _ticket(request: Request, row: Any) -> Response:
    if not files.is_available(row):
        return Response({"detail": files.NOT_SHARED}, status=status.HTTP_410_GONE)
    return Response(files.ticket_json(row, request))


def _thumb(row: Any) -> Any:
    data = bytes(row.thumb) if row.thumb is not None else b""
    if not data or not files.is_available(row):
        raise Http404
    response = HttpResponse(data, content_type=files.sniff_thumb(data) or "application/octet-stream")
    response["Cache-Control"] = "private, max-age=86400"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class _OwnerFileView(_OwnerView):
    """An owner route on one file: groups, the owner's admin chats and own files. Never a direct chat's file (404)."""

    def file(self, request: Request, file_id: Any) -> Any:
        row = files.find(file_id)
        if row is None or not files.owner_may_get(request.user, row):
            raise Http404
        return row


class FilesView(_OwnerView):
    """GET /api/chat/files/ -> {allowed, max_bytes, keep_days}; POST (JSON) registers a file -> 201 FileJSON."""

    def get(self, request: Request) -> Response:
        ent = plan.entitlements_for(request.user)
        return Response({"allowed": services.files_allowed(request.user), "max_bytes": ent.file_max_bytes,
                         "keep_days": files.FILE_KEEP_DAYS})

    def post(self, request: Request) -> Response:
        return _register(request, request.user, None, None)


class FileView(_OwnerFileView):
    """GET /api/chat/files/<uuid>/ -> FileJSON; PATCH {sha256?} | {gone: true} (the owner's own files)."""

    def get(self, request: Request, file_id: Any) -> Response:
        return Response(_json(request, self.file(request, file_id), None, None))

    def patch(self, request: Request, file_id: Any) -> Response:
        return _patch(request, self.file(request, file_id), request.user, None, None)


class FileStopView(_OwnerFileView):
    """POST /api/chat/files/<uuid>/stop/ -- the owner stops sharing their own file -> FileJSON."""

    def post(self, request: Request, file_id: Any) -> Response:
        row = self.file(request, file_id)
        if not files.is_sender(row, request.user, None):
            raise Http404
        return Response(_json(request, files.stop(row), None, None))


class FileTicketView(_OwnerFileView):
    """POST /api/chat/files/<uuid>/ticket/ -> {url, inline_url, status_url, expires_at}; not shared -> 410."""

    def post(self, request: Request, file_id: Any) -> Response:
        return _ticket(request, self.file(request, file_id))


class FileThumbView(_OwnerFileView):
    """GET /api/chat/files/<uuid>/thumb/ -> the preview picture (private cache, 1 day)."""

    def get(self, request: Request, file_id: Any) -> Any:
        return _thumb(self.file(request, file_id))


class ServeTicketView(_OwnerView):
    """GET /api/chat/files/serve-ticket/?browser_id= -> {ticket, wait_url, push_base, expires_at} for this browser."""

    def get(self, request: Request) -> Response:
        try:
            holder = files.browser_holder(request.user.pk, request.query_params.get("browser_id"))
        except files.FileRefused as exc:
            return _file_refused(exc)
        return Response(files.serve_ticket_json(holder, request))


class SharedFilesView(_OwnerView):
    """GET /api/chat/files/shared/ -> the owner's files still shared ("Shared by me"), newest first."""

    def get(self, request: Request) -> Response:
        return Response(files.shared_by(request.user, None, request, device=False))


class PrefsView(APIView):
    """GET|PUT /api/chat/prefs/ -- the owner's Chat list settings {pins, muted, marked}, the same on every browser.
    Not behind the team-chat plan check: the owner's own chats with each person use it too."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        return Response(services.owner_prefs(request.user))

    def put(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        try:
            return Response(services.save_owner_prefs(request.user, data))
        except services.ChatRefused as exc:
            return _refused(exc)

    patch = put


# --- Device (the member's Zinwe window) ----------------------------------------------------


class _DeviceView(APIView):
    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]
    throttle_classes = [DeviceScopedThrottle]
    throttle_scope = "device_chat"

    def member(self, request: Request):  # type: ignore[no-untyped-def]
        member = request.auth.member
        if member is None:
            raise Http404
        return member

    def allowed(self, request: Request) -> bool:
        return plan.entitlements_for(request.auth.owner).has(plan.TEAM_CHAT)

    def conv(self, request: Request, conversation_id: int):  # type: ignore[no-untyped-def]
        if not self.allowed(request):
            raise plan.FeatureLocked(plan.TEAM_CHAT)
        member = self.member(request)
        found = services.conversation_for_member(member, conversation_id)
        if found is None:
            raise Http404
        return member, found[0], found[1]


class DeviceConversationsView(_DeviceView):
    """GET /api/devices/chat/ -> [Conversation]: the member's groups and direct chats."""

    def get(self, request: Request) -> Response:
        if not self.allowed(request) or request.auth.member_id is None:
            return Response([])
        member = request.auth.member
        return Response([services.conversation_json(c, r, member, request)
                         for c, r in services.conversations_for_member(member)])


class DeviceConversationView(_DeviceView):
    """PATCH /api/devices/chat/<id>/ -- {pinned?, muted?, unread?} -> Conversation."""

    def get(self, request: Request, conversation_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        return Response(services.conversation_json(conv, row, member, request))

    def patch(self, request: Request, conversation_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        try:
            services.set_member_flags(row, {k: request.data.get(k) for k in ("pinned", "muted", "unread") if k in request.data})
        except services.ChatRefused as exc:
            return _refused(exc)
        row.refresh_from_db()
        return Response(services.conversation_json(conv, row, member, request))


class DeviceMessagesView(_DeviceView):
    """GET /api/devices/chat/<id>/messages/?after=<id>&before=<id> -> [Line]; POST {body, reply_to?} -> Line (201)."""

    def get(self, request: Request, conversation_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        q = PageQuerySerializer(data=request.query_params)
        q.is_valid(raise_exception=True)
        limit = q.validated_data["limit"] if "limit" in request.query_params else (
            services.MAX_PAGE if q.validated_data["after"] else services.PAGE)
        lines = services.page(conv, row, q.validated_data["before"], q.validated_data["after"], limit)
        return Response(services.lines_json(lines, conv, member, request))

    def post(self, request: Request, conversation_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        data = LineInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            line = services.post(conv, data.validated_data["body"], member, row, data.validated_data["reply_to"],
                                 file_id=data.validated_data["file_id"])
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, conv, member, services.readers(conv), request), status=status.HTTP_201_CREATED)


class DeviceMessageView(_DeviceView):
    """PATCH /api/devices/chat/<id>/messages/<mid>/ {body} | DELETE -> Line (own, within 15 minutes)."""

    def _line(self, conv: Any, row: Any, line_id: int) -> ChatMessage:
        line = services.visible_lines(conv, row).filter(pk=line_id).select_related(
            "sender_member", "reply_to__sender_member", "chat_file__by_member", "chat_file__owner").first()
        if line is None:
            raise Http404
        return line

    def patch(self, request: Request, conversation_id: int, line_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        data = LineEditSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            line = services.edit(self._line(conv, row, line_id), data.validated_data["body"], member)
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, conv, member, services.readers(conv), request))

    def delete(self, request: Request, conversation_id: int, line_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        try:
            line = services.delete(self._line(conv, row, line_id), member)
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, conv, member, services.readers(conv), request))


class DeviceReadView(_DeviceView):
    """POST /api/devices/chat/<id>/read/."""

    def post(self, request: Request, conversation_id: int) -> Response:
        _, conv, row = self.conv(request, conversation_id)
        services.mark_read(conv, row)
        return Response({"ok": True})


class DeviceDirectView(_DeviceView):
    """POST /api/devices/chat/direct/ -- {member_id} -> Conversation (201 new, 200 existing)."""

    def post(self, request: Request) -> Response:
        if not self.allowed(request):
            raise plan.FeatureLocked(plan.TEAM_CHAT)
        member = self.member(request)
        other = request.data.get("member_id")
        if not isinstance(other, int) or isinstance(other, bool):
            return Response({"member_id": ["Pick a teammate."]}, status=status.HTTP_400_BAD_REQUEST)
        existed = services.Conversation.objects.filter(
            owner_id=member.owner_id, kind="direct",
            direct_key=f"{min(member.pk, other)}:{max(member.pk, other)}").exists()
        try:
            conv, row = services.start_direct(member, other)
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.conversation_json(conv, row, member, request),
                        status=status.HTTP_200_OK if existed else status.HTTP_201_CREATED)


class ReportInSerializer(serializers.Serializer):
    message_id = serializers.IntegerField(min_value=1, required=False, allow_null=True, default=None)
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class DeviceReportView(_DeviceView):
    """POST /api/devices/chat/<id>/report/ -- {message_id?, note?} -> {id, ok} (201)."""

    def post(self, request: Request, conversation_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        data = ReportInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            report = services.report(conv, row, member, data.validated_data["message_id"], data.validated_data["note"])
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response({"id": report.pk, "ok": True}, status=status.HTTP_201_CREATED)


class DeviceFilesView(_DeviceView):
    """GET /api/devices/chat/files/ -> {allowed, max_bytes, keep_days}; POST (JSON) registers a file this PC holds."""

    def get(self, request: Request) -> Response:
        allowed = bool(request.auth.member_id) and services.files_allowed(request.auth.owner)
        max_bytes = plan.entitlements_for(request.auth.owner).file_max_bytes
        return Response({"allowed": allowed, "max_bytes": max_bytes, "keep_days": files.FILE_KEEP_DAYS})

    def post(self, request: Request) -> Response:
        if not self.allowed(request):
            raise plan.FeatureLocked(plan.TEAM_CHAT)
        member = self.member(request)
        return _register(request, request.auth.owner, member, request.auth)


class _DeviceFileView(_DeviceView):
    """A device route on one file: chats the member can see, their admin chat, their own files."""

    def file(self, request: Request, file_id: Any):  # type: ignore[no-untyped-def]
        if not self.allowed(request):
            raise plan.FeatureLocked(plan.TEAM_CHAT)
        member = self.member(request)
        row = files.find(file_id)
        if row is None or not files.member_may_get(member, row):
            raise Http404
        return member, row


class DeviceFileView(_DeviceFileView):
    """GET /api/devices/chat/files/<uuid>/ -> FileJSON; PATCH {sha256?} | {gone: true} (the PC that holds it)."""

    def get(self, request: Request, file_id: Any) -> Response:
        member, row = self.file(request, file_id)
        return Response(_json(request, row, member, request.auth))

    def patch(self, request: Request, file_id: Any) -> Response:
        member, row = self.file(request, file_id)
        return _patch(request, row, request.auth.owner, member, request.auth)


class DeviceFileStopView(_DeviceFileView):
    """POST /api/devices/chat/files/<uuid>/stop/ -- the member who shared it stops sharing -> FileJSON."""

    def post(self, request: Request, file_id: Any) -> Response:
        member, row = self.file(request, file_id)
        if not files.is_sender(row, request.auth.owner, member):
            raise Http404
        return Response(_json(request, files.stop(row), member, request.auth))


class DeviceFileTicketView(_DeviceFileView):
    """POST /api/devices/chat/files/<uuid>/ticket/ -> {url, inline_url, status_url, expires_at}."""

    def post(self, request: Request, file_id: Any) -> Response:
        return _ticket(request, self.file(request, file_id)[1])


class DeviceFileThumbView(_DeviceFileView):
    """GET /api/devices/chat/files/<uuid>/thumb/ -> the preview picture."""

    def get(self, request: Request, file_id: Any) -> Any:
        return _thumb(self.file(request, file_id)[1])


class DeviceServeTicketView(_DeviceView):
    """GET /api/devices/chat/files/serve-ticket/ -> {ticket, wait_url, push_base, expires_at} for this PC."""

    def get(self, request: Request) -> Response:
        if not self.allowed(request):
            raise plan.FeatureLocked(plan.TEAM_CHAT)
        return Response(files.serve_ticket_json(files.device_holder(request.auth), request))


class DeviceSharedFilesView(_DeviceView):
    """GET /api/devices/chat/files/shared/ -> this member's files still shared ("My shared files"), newest first."""

    def get(self, request: Request) -> Response:
        if not self.allowed(request):
            raise plan.FeatureLocked(plan.TEAM_CHAT)
        return Response(files.shared_by(request.auth.owner, self.member(request), request, device=True))


class DevicePeopleView(_DeviceView):
    """GET /api/devices/chat/people/ -> teammates this member can message."""

    def get(self, request: Request) -> Response:
        from z_devices.pictures import picture_payload

        if not self.allowed(request) or request.auth.member_id is None:
            return Response([])
        out = []
        for p in services.people_for(request.auth.member):
            m = p.pop("_m")
            out.append({**p, "picture": picture_payload(m, request)})
        return Response(out)
