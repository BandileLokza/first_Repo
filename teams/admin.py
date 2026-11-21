from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from .models import Team, TeamCoachAssignment
from matches.models import Player
from leagues.team_moves import move_team_to_competition, validate_team_competition_move


class PlayerInline(admin.TabularInline):
    model = Player
    extra = 0


class TeamAdminForm(forms.ModelForm):
    """Validate competition edits before the admin save path can bypass repair."""

    class Meta:
        model = Team
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_competition_id = self.instance.competition_id

    def clean(self):
        cleaned_data = super().clean()
        destination = cleaned_data.get("competition")
        if self.instance.pk and destination and destination.id != self.original_competition_id:
            original = Team.objects.select_related("competition").get(pk=self.instance.pk)
            try:
                validate_team_competition_move(original, destination)
            except ValidationError as error:
                self.add_error("competition", error)
        return cleaned_data


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    form = TeamAdminForm
    list_display = ("name", "home_ground")
    search_fields = ("name",)
    ordering = ("name",)
    inlines = [PlayerInline]

    def save_model(self, request, obj, form, change):
        if change and form.original_competition_id != obj.competition_id:
            original = Team.objects.get(pk=obj.pk)
            moved = move_team_to_competition(
                original,
                obj.competition,
                actor=request.user,
            )
            changed_fields = [
                field for field in ("name", "home_ground", "logo")
                if field in form.changed_data
            ]
            for field in changed_fields:
                setattr(moved, field, getattr(obj, field))
            if changed_fields:
                moved.save(update_fields=changed_fields)
            obj.competition_id = moved.competition_id
            return
        super().save_model(request, obj, form, change)


@admin.register(TeamCoachAssignment)
class TeamCoachAssignmentAdmin(admin.ModelAdmin):
    list_display = ("team", "user", "created_at")
    list_filter = ("team__competition",)
    search_fields = ("team__name", "user__username", "user__email")
