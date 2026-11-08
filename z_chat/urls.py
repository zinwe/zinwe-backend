"""Owner routes, mounted at /api/chat/ (groups only: the owner never reaches a direct chat)."""
from django.urls import path

from . import views

app_name = "z_chat"

urlpatterns = [
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
    # v1.5 file sharing: upload, then download (never a direct chat's file for the owner).
    path("files/", views.FileUploadView.as_view(), name="files"),
    path("files/<uuid:file_id>/", views.FileView.as_view(), name="file"),
]
