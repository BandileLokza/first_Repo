from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.template.response import TemplateResponse
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html

from league_manager.models import ActivityLog
from league_manager.services import record_activity
from league_manager.permissions import can_manage_abandonment_decisions, can_resolve_abandoned_match
from .models import (
    Match,
    Player,
    MatchLineup,
    MatchLineupEntry,
    Goal,
    MatchAdminAssignment,
    FootballKnockoutRoundRule,
    FootballKnockoutTie,
    AbandonedMatchDecision,
)
from .forms import (
    ABANDONED_MATCH_ADMIN_METADATA_FIELDS,
    GoalAdminForm,
    MatchAdminForm,
    goal_admin_match_is_protected,
    validate_abandoned_match_admin_edit,
    validate_goal_admin_edit,
)
from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django.db.models import Count, Q
from .result_corrections import is_lock_conflict, result_snapshot
from .result_outbox import RESULT_BUSY, RESULT_SAVED, lock_result_competition, lock_result_match
from .services import (
    apply_official_match_result,
    end_regulation,
    finish_ready_match,
    rebuild_league_standings,
)
from .control_state import get_match_control_state
from .bracket import BracketCorrectionBlocked, record_blocked_bracket_correction
from league_manager.fixture_deletion import (
    FixtureDeletionBlocked,
    delete_fixtures,
    validate_fixture_deletion,
)

# --- Admin Actions ---
@admin.action(description="Start First Half")
def start_first_half_action(modeladmin, request, queryset):
    completed = 0
    for match in queryset:
        if get_match_control_state(match).can_start_first_half:
            match.start_first_half()
            completed += 1
    modeladmin.message_user(
        request,
        f"Started {completed} match(es); skipped invalid states.",
        level=messages.INFO,
    )


@admin.action(description="End First Half")
def end_first_half_action(modeladmin, request, queryset):
    completed = 0
    for match in queryset:
        if get_match_control_state(match).can_end_first_half:
            match.end_first_half()
            completed += 1
    modeladmin.message_user(
        request,
        f"Ended the first half for {completed} match(es); skipped invalid states.",
        level=messages.INFO,
    )


@admin.action(description="Start Second Half")
def start_second_half_action(modeladmin, request, queryset):
    completed = 0
    for match in queryset:
        if get_match_control_state(match).can_start_second_half:
            match.start_second_half()
            completed += 1
    modeladmin.message_user(
        request,
        f"Started the second half for {completed} match(es); skipped invalid states.",
        level=messages.INFO,
    )


@admin.action(description="End Match")
def end_match_action(modeladmin, request, queryset):
    finished_count = 0
    pending_count = 0
    skipped_count = 0
    busy_count = 0
    for fixture in queryset:
        try:
            with transaction.atomic():
                match = lock_result_match(fixture.pk, nowait=True)
                state = get_match_control_state(match)
                before_result = result_snapshot(match)
                if state.can_end_regulation:
                    match = end_regulation(match, actor=request.user)
                elif state.can_finish_match:
                    match = finish_ready_match(match, actor=request.user)
                else:
                    skipped_count += 1
                    continue
                finished = match.status == "FINISHED"
                record_activity(
                    actor=request.user, competition=match.competition, match=match,
                    action=ActivityLog.Action.MATCH_FINISHED if finished else ActivityLog.Action.REGULATION_ENDED,
                    description="Result saved from Django admin." if finished else "Regulation ended from Django admin.",
                    old_values=before_result, new_values=result_snapshot(match),
                )
        except Match.DoesNotExist:
            skipped_count += 1
            continue
        except OperationalError as error:
            if not is_lock_conflict(error):
                raise
            busy_count += 1
            continue
        except ValidationError as error:
            record_blocked_bracket_correction(error, actor=request.user)
            skipped_count += 1
            continue

        if finished:
            finished_count += 1
        else:
            pending_count += 1
    modeladmin.message_user(
        request,
        (
            f"Finished {finished_count} match(es), ended regulation for "
            f"{pending_count}, and skipped {skipped_count} invalid state(s)."
            + (" Standings and prediction points are updating." if finished_count else "")
        ),
        level=messages.SUCCESS,
    )
    if busy_count:
        modeladmin.message_user(
            request, f"{busy_count} match(es) were busy. Please retry those matches.",
            level=messages.WARNING,
        )


@admin.action(description="Unlock selected match lineups")
def unlock_lineups_action(modeladmin, request, queryset):
    if not request.user.is_superuser:
        modeladmin.message_user(
            request,
            "Only a superuser can unlock lineups from the Django admin.",
            level=messages.ERROR,
        )
        return

    unlocked_count = 0
    skipped_non_upcoming_count = 0
    for match in queryset:
        if match.status != "UPCOMING":
            skipped_non_upcoming_count += 1
            continue
        if match.lineups_locked:
            match.unlock_lineups()
            unlocked_count += 1

    modeladmin.message_user(
        request,
        (
            f"Unlocked lineups for {unlocked_count} match(es); skipped "
            f"{skipped_non_upcoming_count} active or closed match(es)."
        ),
        level=messages.SUCCESS,
    )


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    form = MatchAdminForm
    readonly_fields = ("table_updated", "generated_by_system", "resolution_workflow")

    list_display = ("competition", "home_team", "away_team", "match_date", "status", "phase", "minute", "lineups_locked")
    list_filter = ("competition", "status", "phase")
    search_fields = ("home_team__name", "away_team__name")

    fields = (
        "competition",
        "stage",
        "group",
        "home_team",
        "away_team",
        "home_score",
        "away_score",
        "regulation_home_score",
        "regulation_away_score",
        "penalty_home_score",
        "penalty_away_score",
        "resolution_method",
        "knockout_tie",
        "match_date",
        "location",
        "status",
        "phase",
        "phase_start_time",
        "minute",
        "abandon_reason",
        "resolution_workflow",
        "gameweek",
        "leg",
        "generated_by_system",
        "table_updated",
    )

    actions = [
        "delete_selected_matches",
        start_first_half_action,
        end_first_half_action,
        start_second_half_action,
        end_match_action,
        unlock_lineups_action,
    ]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.status in {"ABANDONED", "FINISHED"}:
            readonly.append("status")
        if obj and goal_admin_match_is_protected(obj):
            readonly.extend(
                field.name for field in Match._meta.concrete_fields
                if not field.primary_key
                and field.name not in ABANDONED_MATCH_ADMIN_METADATA_FIELDS
            )
        if obj and (obj.generated_by_system or obj.competition.schedule_locked_at):
            readonly.extend([
                "competition",
                "stage",
                "group",
                "home_team",
                "away_team",
                "gameweek",
                "leg",
                "knockout_tie",
            ])
        return tuple(dict.fromkeys(readonly))

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj and can_resolve_abandoned_match(request.user, obj):
            return fields
        return tuple(field for field in fields if field != "resolution_workflow")

    @admin.display(description="Abandoned-match workflow")
    def resolution_workflow(self, obj):
        if not obj or obj.status != "ABANDONED":
            return "—"
        url = reverse(
            "league_manager:resolve_abandoned_match",
            args=[obj.competition.slug, obj.pk],
        )
        return format_html('<a class="button" href="{}">Resolve abandoned match</a>', url)

    def get_urls(self):
        custom_urls = [
            path(
                "<path:object_id>/resolve-abandoned/",
                self.admin_site.admin_view(self.resolve_abandoned_view),
                name="matches_match_resolve_abandoned",
            ),
        ]
        return custom_urls + super().get_urls()

    def resolve_abandoned_view(self, request, object_id):
        match = Match.objects.select_related("competition").filter(pk=object_id).first()
        if not match:
            self.message_user(request, "The match no longer exists.", messages.ERROR)
            return redirect("admin:matches_match_changelist")
        if not can_manage_abandonment_decisions(request.user, match.competition):
            raise PermissionDenied("You do not have permission to resolve this match.")
        if match.status != "ABANDONED":
            self.message_user(request, "Only abandoned matches need this workflow.", messages.ERROR)
            return redirect("admin:matches_match_change", object_id=match.pk)
        return redirect(
            "league_manager:resolve_abandoned_match",
            slug=match.competition.slug,
            match_id=match.pk,
        )

    def has_delete_permission(self, request, obj=None):
        # Fixture removal is deliberately funnelled through the audited League
        # Manager confirmation flow. This prevents Django admin/bulk deletion
        # from bypassing scope checks, standings rebuilds and ActivityLog.
        return False

    def _can_bulk_delete_matches(self, request):
        """Apply the admin's delete permission without reopening direct delete."""

        return bool(
            request.user.is_authenticated
            and request.user.is_active
            and request.user.is_superuser
            and super().has_delete_permission(request)
        )

    def get_actions(self, request):
        """Expose the safe action only to the superuser deletion boundary."""

        actions = super().get_actions(request)
        if not self._can_bulk_delete_matches(request):
            actions.pop("delete_selected_matches", None)
        # ``has_delete_permission()`` is intentionally false, but remove the
        # stock action defensively in case Django's permission handling changes.
        actions.pop("delete_selected", None)
        return actions

    @admin.action(description="Delete selected matches")
    def delete_selected_matches(self, request, queryset):
        """Confirm then delete the full selection through the shared workflow."""

        if not self._can_bulk_delete_matches(request):
            raise PermissionDenied("Only a superuser may delete matches from Django admin.")

        fixture_ids = list(queryset.values_list("pk", flat=True))
        if request.POST.get("post"):
            try:
                deleted_count = delete_fixtures(
                    actor=request.user,
                    fixture_ids=fixture_ids,
                )
            except FixtureDeletionBlocked as error:
                self.message_user(request, " ".join(error.messages), messages.ERROR)
                return None
            self.message_user(
                request,
                (
                    f"Successfully deleted {deleted_count} selected match(es). "
                    "Standings were rebuilt where a deleted result affected them."
                ),
                messages.SUCCESS,
            )
            return None

        try:
            fixtures = validate_fixture_deletion(
                actor=request.user,
                fixture_ids=fixture_ids,
            )
        except FixtureDeletionBlocked as error:
            self.message_user(request, " ".join(error.messages), messages.ERROR)
            return None

        context = {
            **self.admin_site.each_context(request),
            "title": "Delete selected matches",
            "opts": self.opts,
            "media": self.media,
            "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            "queryset": fixtures,
            "selected_count": len(fixtures),
            "affected_competitions": sorted(
                {fixture.competition.name for fixture in fixtures}
            ),
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            "admin/matches/match/delete_selected_matches_confirmation.html",
            context,
        )

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Return a clear, audited response when an admin correction is unsafe.

        ``super()`` owns the admin transaction.  Catching after it unwinds
        keeps the rejected result and any metadata changes rolled back before
        writing the separate blocked-correction audit record.
        """
        try:
            return super().changeform_view(
                request,
                object_id,
                form_url,
                extra_context,
            )
        except BracketCorrectionBlocked as error:
            record_blocked_bracket_correction(error, actor=request.user)
            self.message_user(
                request,
                " ".join(error.messages),
                level=messages.ERROR,
            )
            if object_id:
                return redirect(request.path)
            raise
        except OperationalError as error:
            if not is_lock_conflict(error):
                raise
            self.message_user(request, RESULT_BUSY, level=messages.WARNING)
            return redirect(request.path)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        if obj.status == "FINISHED":
            competition_ids = {obj.competition_id}
            if change:
                competition_ids.add(
                    Match.objects.values_list("competition_id", flat=True).get(pk=obj.pk)
                )
            for competition_id in sorted(competition_ids):
                lock_result_competition(competition_id, nowait=True)
        validate_abandoned_match_admin_edit(obj, lock=True)
        old_match = None
        if change:
            old_match = Match.objects.get(pk=obj.pk)

        outcome_fields_changed = any(
            field in form.changed_data
            for field in [
                "home_score",
                "away_score",
                "regulation_home_score",
                "regulation_away_score",
                "penalty_home_score",
                "penalty_away_score",
                "resolution_method",
                "status",
                "phase",
                "competition",
                "stage",
                "group",
                "home_team",
                "away_team",
                "leg",
                "knockout_tie",
            ]
        )

        if obj.status == "FINISHED" and (outcome_fields_changed or not change):
            # The central finalizer intentionally writes result fields only.
            # Preserve innocuous metadata edits made alongside a score correction
            # before it runs; structural fixture edits are rejected by the form.
            if change:
                metadata_fields = [
                    field
                    for field in ("match_date", "location")
                    if field in form.changed_data
                ]
                if (
                    old_match
                    and not old_match.knockout_tie_id
                    and "knockout_tie" in form.changed_data
                ):
                    # This is the one permitted structural edit on a finished
                    # fixture: explicitly attaching a previously unlinked
                    # legacy match to a validated tie.
                    metadata_fields.append("knockout_tie")
                if metadata_fields:
                    obj.save(update_fields=metadata_fields)

            before_result = result_snapshot(old_match) if old_match else {}
            apply_official_match_result(
                obj,
                home_score=obj.home_score,
                away_score=obj.away_score,
                regulation_home_score=obj.regulation_home_score,
                regulation_away_score=obj.regulation_away_score,
                penalty_home_score=obj.penalty_home_score,
                penalty_away_score=obj.penalty_away_score,
                actor=request.user,
            )
            after_result = result_snapshot(obj)
            if before_result != after_result:
                record_activity(
                    actor=request.user, competition=obj.competition, match=obj,
                    action=(
                        ActivityLog.Action.RESULT_CORRECTED
                        if before_result.get("status") == "FINISHED"
                        else ActivityLog.Action.MATCH_FINISHED
                    ),
                    description="Result saved from Django admin.",
                    old_values=before_result, new_values=after_result,
                )

            obj.refresh_from_db()
            self.message_user(request, RESULT_SAVED, level=messages.SUCCESS)
            return

        if obj.status != "FINISHED" and any(
            field in form.changed_data
            for field in [
                "regulation_home_score",
                "regulation_away_score",
                "penalty_home_score",
                "penalty_away_score",
                "resolution_method",
            ]
        ):
            raise ValidationError(
                "Official knockout result details can only be saved through a finished result."
            )

        if (
            obj.stage.stage_type == "KNOCKOUT"
            and obj.phase == "PENALTIES"
            and any(field in form.changed_data for field in ["home_score", "away_score"])
        ):
            raise ValidationError(
                "Penalty shootout kicks must be recorded separately from football goals."
            )

        super().save_model(request, obj, form, change)

        # Non-final metadata/status edits never need a full table rebuild.
        # Finished results above are already sent through the authoritative
        # result path and its idempotent standings task.
        return

@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "team",
        "jersey_number",
        "starting_goals",
        "display_recorded_goals",
        "display_total_goals",
    )

    list_filter = ("team",)
    search_fields = ("name", "team__name")
    fields = ("name", "team", "photo", "jersey_number", "starting_goals")
    list_select_related = ("team",)
    list_per_page = 50

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        return qs.annotate(
            recorded_goals_count=Count(
                "goal",
                filter=(
                    ~Q(goal__match__status="ABANDONED")
                    & Q(
                        goal__canceled=False,
                        goal__match__abandonment_resolution__in=["", "RESUMED", "CONFIRMED"],
                        goal__goal_type__in=[
                            Goal.GoalType.NORMAL,
                            Goal.GoalType.PENALTY,
                        ],
                    )
                ),
            )
        )

    @admin.display(description="Recorded Goals", ordering="recorded_goals_count")
    def display_recorded_goals(self, obj):
        return obj.recorded_goals_count

    @admin.display(description="Total Goals", ordering="recorded_goals_count")
    def display_total_goals(self, obj):
        return obj.starting_goals + obj.recorded_goals_count

@admin.register(MatchLineup)
class MatchLineupAdmin(admin.ModelAdmin):
    list_display = ("match", "team", "submitted_by", "submitted_at", "updated_by", "updated_at")
    filter_horizontal = ("players",)


@admin.register(MatchLineupEntry)
class MatchLineupEntryAdmin(admin.ModelAdmin):
    list_display = ("lineup", "player", "role", "sort_order", "position")
    list_filter = ("role",)
    search_fields = ("player__name", "lineup__team__name")


@admin.register(Goal)
class GoalAdmin(admin.ModelAdmin):
    form = GoalAdminForm

    def has_change_permission(self, request, obj=None):
        if obj and goal_admin_match_is_protected(obj.match):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and goal_admin_match_is_protected(obj.match):
            return False
        return super().has_delete_permission(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "match":
            kwargs["queryset"] = Match.objects.exclude(
                Q(status="ABANDONED")
                | Q(abandonment_resolution__in=["AWARDED", "CONFIRMED"])
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        validate_goal_admin_edit(obj, lock=True)
        super().save_model(request, obj, form, change)

    @transaction.atomic
    def delete_model(self, request, obj):
        try:
            validate_goal_admin_edit(obj, lock=True)
        except ValidationError as error:
            raise PermissionDenied(" ".join(error.messages)) from error
        super().delete_model(request, obj)

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        selected = list(queryset.values_list("pk", "match_id"))
        match_ids = {match_id for _, match_id in selected}
        # Match controls lock the fixture before its event rows. Use the same
        # order for the entire selection before touching any goal row.
        list(Match.objects.select_for_update().filter(pk__in=match_ids).order_by("pk"))
        selected_ids = [goal_id for goal_id, _ in selected]
        goals = list(Goal.objects.select_for_update().filter(pk__in=selected_ids).order_by("pk"))
        for goal in goals:
            if goal.match_id not in match_ids:
                raise PermissionDenied("A selected goal changed concurrently. Reload the selection.")
            try:
                validate_goal_admin_edit(goal, lock=True)
            except ValidationError as error:
                raise PermissionDenied(" ".join(error.messages)) from error
        super().delete_queryset(request, Goal.objects.filter(pk__in=selected_ids))
    list_display = (
        "match",
        "team",
        "player_name",
        "goal_type",
        "minute",
        "period",
        "canceled",
        "created_at",
    )
    list_filter = ("team", "goal_type", "period", "canceled")
    search_fields = ("player_name",)


@admin.register(MatchAdminAssignment)
class MatchAdminAssignmentAdmin(admin.ModelAdmin):
    list_display = ("match", "admin")
    search_fields = ("admin__username", "match__competition__name")


@admin.register(AbandonedMatchDecision)
class AbandonedMatchDecisionAdmin(admin.ModelAdmin):
    """Expose the audit history without creating a second decision path."""

    list_display = (
        "match", "decision_type", "decided_by", "decided_at", "is_current",
    )
    list_filter = ("decision_type", "is_current", "match__competition")
    search_fields = ("match__home_team__name", "match__away_team__name", "decision_reference")
    readonly_fields = tuple(field.name for field in AbandonedMatchDecision._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {"GET", "HEAD"} and super().has_view_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FootballKnockoutRoundRule)
class FootballKnockoutRoundRuleAdmin(admin.ModelAdmin):
    list_display = ("stage", "number_of_legs", "decider_policy")
    list_select_related = ("stage",)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.stage.competition.schedule_locked_at:
            return ("stage", "number_of_legs", "decider_policy")
        if obj and Match.objects.filter(stage=obj.stage).exclude(
            status="UPCOMING"
        ).exists():
            return ("stage", "number_of_legs", "decider_policy")
        if obj and Match.objects.filter(stage=obj.stage).exists():
            return ("stage",)
        return ()


@admin.register(FootballKnockoutTie)
class FootballKnockoutTieAdmin(admin.ModelAdmin):
    list_display = ("stage", "team_one", "team_two", "created_at")
    list_filter = ("stage__competition", "stage")
    search_fields = ("team_one__name", "team_two__name")
    # Connected bracket state is deliberately owned by matches.bracket and the
    # authorized league management flow.  Keeping the legacy pairing fields in
    # this admin avoids a second, unsafe way to relink or force advancement.
    fields = ("stage", "team_one", "team_two", "created_at")
    readonly_fields = ("created_at",)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.stage.competition.schedule_locked_at:
            return ("stage", "team_one", "team_two", "created_at")
        if obj and (obj.position is not None or obj.matches.exists()):
            return ("stage", "team_one", "team_two", "created_at")
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj and obj.stage.competition.schedule_locked_at:
            return False
        if obj and obj.position is not None:
            return False
        return super().has_delete_permission(request, obj)
