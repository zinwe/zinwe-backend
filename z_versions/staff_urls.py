from django.urls import path

from . import views

app_name = "z_versions_staff"

# Staff side, mounted at /api/admin/versions/ (the admin app's New version tab).
urlpatterns = [
    path("", views.PlansView.as_view(), name="plans"),
    path("<int:pk>/", views.PlanView.as_view(), name="plan"),
    path("<int:pk>/pdf/", views.PlanPdfView.as_view(), name="plan-pdf"),
]
