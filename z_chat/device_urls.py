"""Device routes (the member's Zinwe window), mounted at /api/devices/chat/."""
from django.urls import path

from . import views

urlpatterns = [
    # v1.5 file sharing (round 2: the file stays on the sender's device; bytes go through the relay, z_relay).
    path("files/", views.DeviceFilesView.as_view(), name="chat-files"),
    path("files/serve-ticket/", views.DeviceServeTicketView.as_view(), name="chat-files-serve-ticket"),
    path("files/shared/", views.DeviceSharedFilesView.as_view(), name="chat-files-shared"),
    path("files/<uuid:file_id>/", views.DeviceFileView.as_view(), name="chat-file"),
    path("files/<uuid:file_id>/stop/", views.DeviceFileStopView.as_view(), name="chat-file-stop"),
    path("files/<uuid:file_id>/ticket/", views.DeviceFileTicketView.as_view(), name="chat-file-ticket"),
    path("files/<uuid:file_id>/thumb/", views.DeviceFileThumbView.as_view(), name="chat-file-thumb"),
    path("", views.DeviceConversationsView.as_view(), name="chat"),
    path("direct/", views.DeviceDirectView.as_view(), name="chat-direct"),
    path("people/", views.DevicePeopleView.as_view(), name="chat-people"),
    path("<int:conversation_id>/", views.DeviceConversationView.as_view(), name="chat-conversation"),
    path("<int:conversation_id>/messages/", views.DeviceMessagesView.as_view(), name="chat-messages"),
    path("<int:conversation_id>/messages/<int:line_id>/", views.DeviceMessageView.as_view(), name="chat-message"),
    path("<int:conversation_id>/read/", views.DeviceReadView.as_view(), name="chat-read"),
    path("<int:conversation_id>/report/", views.DeviceReportView.as_view(), name="chat-report"),
]
