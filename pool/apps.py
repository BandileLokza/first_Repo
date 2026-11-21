from django.apps import AppConfig


class PoolConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pool"
    verbose_name = "8-Ball Pool"

    def ready(self):
        # Register post-save provisioning without importing signals at module
        # import time (which would make app loading order significant).
        from . import signals  # noqa: F401
