from django.contrib import admin

from .models import VersionPlan


@admin.register(VersionPlan)
class VersionPlanAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "version", "status", "email_status")
    list_filter = ("kind", "status")
