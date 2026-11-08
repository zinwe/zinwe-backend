"""Device routes (the member's Zinwe window), mounted at /api/devices/chat/."""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.DeviceConversationsView.as_view(), name="chat"),
    path("direct/", views.DeviceDirectView.as_view(), name="chat-direct"),
    path("people/", views.DevicePeopleView.as_view(), name="chat-people"),
    # v1.5 file sharing: GET = is it on + limits, POST = upload; files/<uuid>/ = download.
    path("files/", views.DeviceFilesView.as_view(), name="chat-files"),
    path("files/<uuid:file_id>/", views.DeviceFileView.as_view(), name="chat-file"),
    path("<int:conversation_id>/", views.DeviceConversationView.as_view(), name="chat-conversation"),
    path("<int:conversation_id>/messages/", views.DeviceMessagesView.as_view(), name="chat-messages"),
    path("<int:conversation_id>/messages/<int:line_id>/", views.DeviceMessageView.as_view(), name="chat-message"),
    path("<int:conversation_id>/read/", views.DeviceReadView.as_view(), name="chat-read"),
    path("<int:conversation_id>/report/", views.DeviceReportView.as_view(), name="chat-report"),
]
