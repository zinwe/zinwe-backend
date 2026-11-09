"""Owner routes, mounted at /api/chat/ (groups only: the owner never reaches a direct chat)."""
from django.urls import path

from . import views

app_name = "z_chat"

urlpatterns = [
    # v1.5 file sharing (round 2: the file stays on the sender's device; bytes go through the relay, z_relay).
    path("files/", views.FilesView.as_view(), name="files"),
    path("files/serve-ticket/", views.ServeTicketView.as_view(), name="files-serve-ticket"),
    path("files/shared/", views.SharedFilesView.as_view(), name="files-shared"),
    path("files/<uuid:file_id>/", views.FileView.as_view(), name="file"),
    path("files/<uuid:file_id>/stop/", views.FileStopView.as_view(), name="file-stop"),
    path("files/<uuid:file_id>/ticket/", views.FileTicketView.as_view(), name="file-ticket"),
    path("files/<uuid:file_id>/thumb/", views.FileThumbView.as_view(), name="file-thumb"),
    path("groups/", views.GroupListView.as_view(), name="groups"),
    path("groups/<int:group_id>/", views.GroupDetailView.as_view(), name="group"),
    path("groups/<int:group_id>/messages/", views.GroupMessagesView.as_view(), name="group-messages"),
    path("groups/<int:group_id>/messages/<int:line_id>/", views.GroupMessageView.as_view(), name="group-message"),
    path("groups/<int:group_id>/read/", views.GroupReadView.as_view(), name="group-read"),
    path("read-all/", views.ReadAllView.as_view(), name="read-all"),
    path("reports/", views.ReportListView.as_view(), name="reports"),
    path("reports/<int:report_id>/handled/", views.ReportHandledView.as_view(), name="report-handled"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("prefs/", views.PrefsView.as_view(), name="prefs"),
]
