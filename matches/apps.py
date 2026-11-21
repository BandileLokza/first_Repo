import os
from django.apps import AppConfig


class MatchesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "matches"

    def ready(self):
        # TEMP: disable scheduler completely for testing
        return

        # --- keep this below for later ---
        if os.environ.get("IS_CELERY_WORKER") == "1":
            return

        from . import scheduler
        scheduler.start()