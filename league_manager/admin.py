from django.contrib import admin

from .models import (
    ActivityLog,
    LeagueMembership,
    OrganisationAdministrator,
    OrganisationReportPreference,
    RegionalAdministrator,
)


@admin.register(RegionalAdministrator)
class RegionalAdministratorAdmin(admin.ModelAdmin):
    list_display = ("user", "region", "is_active", "created_at")
    list_filter = ("region", "is_active")
    search_fields = ("user__username", "user__email", "region__name")
    autocomplete_fields = ("user", "region")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(OrganisationAdministrator)
class OrganisationAdministratorAdmin(admin.ModelAdmin):
    list_display = ("user", "organisation", "is_active", "created_at")
    list_filter = ("organisation", "is_active")
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ("user",)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(OrganisationReportPreference)
class OrganisationReportPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "organisation", "report_kind", "selection_mode", "output_format")
    list_filter = ("organisation", "report_kind", "selection_mode", "output_format")
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ("user", "selected_series")
    readonly_fields = ("created_at", "updated_at")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(LeagueMembership)
class LeagueMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "competition",
        "role",
        "is_active",
        "created_at",
    )

    list_filter = (
        "competition",
        "role",
        "is_active",
    )

    search_fields = (
        "user__username",
        "user__email",
        "competition__name",
    )

    autocomplete_fields = (
        "user",
        "competition",
    )

    ordering = (
        "competition__name",
        "role",
        "user__username",
    )


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "actor",
        "competition",
        "match",
        "action",
    )

    list_filter = (
        "competition",
        "action",
        "created_at",
    )

    search_fields = (
        "actor__username",
        "actor__email",
        "competition__name",
        "description",
    )

    readonly_fields = (
        "actor",
        "competition",
        "match",
        "action",
        "description",
        "old_values",
        "new_values",
        "created_at",
    )

    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
