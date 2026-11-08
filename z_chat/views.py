"""
Team chat HTTP layer (brief section 1b). Owner routes (/api/chat/) only ever reach GROUPS; device routes
(/api/devices/chat/) reach the PC member's own groups and direct chats. Rules: services.py.
"""
from __future__ import annotations

from typing import Any

from django.http import Http404
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from z_auth import entitlements as plan
from z_devices.authentication import DeviceScopedThrottle, DeviceTokenAuthentication, IsDevice, IsOwner

from . import services
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


class LineInSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=services.BODY_MAX, trim_whitespace=True)
    reply_to = serializers.IntegerField(min_value=1, required=False, allow_null=True, default=None)


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
    """GET /api/chat/groups/<id>/; PATCH {name?, icon?, member_ids?, announce?, pinned?, muted?} -> Group."""

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


class GroupMessagesView(_OwnerView):
    """GET /api/chat/groups/<id>/messages/?before=<id>&after=<id> -> [Line]; POST {body, reply_to?} -> Line (201)."""

    def get(self, request: Request, group_id: int) -> Response:
        group = self.group(request, group_id)
        q = PageQuerySerializer(data=request.query_params)
        q.is_valid(raise_exception=True)
        lines = services.page(group, None, q.validated_data["before"], q.validated_data["after"], q.validated_data["limit"])
        return Response(services.lines_json(lines, group, None))

    def post(self, request: Request, group_id: int) -> Response:
        group = self.group(request, group_id)
        data = LineInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            line = services.post(group, data.validated_data["body"], None, None, data.validated_data["reply_to"])
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, group, None, services.readers(group)), status=status.HTTP_201_CREATED)


def _line(group: Any, line_id: int) -> ChatMessage:
    line = ChatMessage.objects.filter(conversation=group, pk=line_id).select_related(
        "sender_member", "reply_to__sender_member").first()
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
        return Response(services.line_json(line, group, None, services.readers(group)))

    def delete(self, request: Request, group_id: int, line_id: int) -> Response:
        group = self.group(request, group_id)
        try:
            line = services.delete(_line(group, line_id), None)
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, group, None, services.readers(group)))


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
    """GET|PATCH /api/chat/settings/ -- {allow_member_dm}."""

    def get(self, request: Request) -> Response:
        return Response({"allow_member_dm": services.settings_for(request.user).allow_member_dm})

    def patch(self, request: Request) -> Response:
        value = request.data.get("allow_member_dm")
        if not isinstance(value, bool):
            return Response({"allow_member_dm": ["true or false."]}, status=status.HTTP_400_BAD_REQUEST)
        row = services.update_settings(request.user, {"allow_member_dm": value})
        return Response({"allow_member_dm": row.allow_member_dm})

    put = patch


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
        return Response(services.lines_json(lines, conv, member))

    def post(self, request: Request, conversation_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        data = LineInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            line = services.post(conv, data.validated_data["body"], member, row, data.validated_data["reply_to"])
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, conv, member, services.readers(conv)), status=status.HTTP_201_CREATED)


class DeviceMessageView(_DeviceView):
    """PATCH /api/devices/chat/<id>/messages/<mid>/ {body} | DELETE -> Line (own, within 15 minutes)."""

    def _line(self, conv: Any, row: Any, line_id: int) -> ChatMessage:
        line = services.visible_lines(conv, row).filter(pk=line_id).select_related(
            "sender_member", "reply_to__sender_member").first()
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
        return Response(services.line_json(line, conv, member, services.readers(conv)))

    def delete(self, request: Request, conversation_id: int, line_id: int) -> Response:
        member, conv, row = self.conv(request, conversation_id)
        try:
            line = services.delete(self._line(conv, row, line_id), member)
        except services.ChatRefused as exc:
            return _refused(exc)
        return Response(services.line_json(line, conv, member, services.readers(conv)))


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
