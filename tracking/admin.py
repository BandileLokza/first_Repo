from django.contrib import admin
from .models import PageVisit


@admin.register(PageVisit)
class PageVisitAdmin(admin.ModelAdmin):
    list_display = ("path", "user", "session_key", "visited_at")
    list_filter = ("visited_at",)
    search_fields = ("path", "session_key", "user__username", "user__email")