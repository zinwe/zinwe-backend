from django.apps import AppConfig


class ZChatConfig(AppConfig):
    """v1.4 team chat: groups (the owner is in every one) and direct chats between members (the owner never reads)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "z_chat"
    verbose_name = "Team chat"
