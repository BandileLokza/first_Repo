"""Django admin setup for pool competition configuration and test data."""

from django.contrib import admin, messages
from django.contrib.admin import actions as admin_actions

from .models import (
    PoolCompetition,
    PoolCompetitionSeries,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionMarginPointRule,
    PoolCompetitionMembership,
    PoolCompetitionTeamStaffAssignment,
    PoolCompetitionStage,
    PoolCompetitionTieBreaker,
    PoolStandingBaseline,
    PoolLeagueAuditEvent,
    PoolGroup,
    PoolGroupEntry,
    PoolGroupQualificationRule,
    PoolBracketDrawPreview,
    PoolBracketAuditEvent,
    PoolKnockoutBracket,
    PoolKnockoutRoundDefinition,
    PoolKnockoutRoundRule,
    PoolKnockoutTie,
    PoolKnockoutTieSlot,
    PoolMatch,
    PoolMatchResultRevision,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamMatchAuditEvent,
    PoolTeamMatchLineup,
    PoolTeamMatchLineupMember,
    PoolTeamMatchLineupPosition,
    PoolTeamMatchScheduledRack,
    PoolTeamMatchSubstitution,
    PoolTeamRoster,
    PoolMatchScorerAssignment,
)


class PoolSuperuserAdmin(admin.ModelAdmin):
    """Pool setup is reserved for superusers until a scoped manager UI exists."""

    @staticmethod
    def _is_allowed(request):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_active
            and request.user.is_superuser
        )

    def has_module_permission(self, request):
        return self._is_allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._is_allowed(request)

    def has_add_permission(self, request):
        return self._is_allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._is_allowed(request)

    def has_delete_permission(self, request, obj=None):
        return self._is_allowed(request)


class PoolSuperuserInline(admin.TabularInline):
    """Keep inline setup fields behind the same temporary superuser boundary."""

    @staticmethod
    def _is_allowed(request):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_active
            and request.user.is_superuser
        )

    def has_view_permission(self, request, obj=None):
        return self._is_allowed(request)

    def has_add_permission(self, request, obj=None):
        return self._is_allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._is_allowed(request)

    def has_delete_permission(self, request, obj=None):
        return self._is_allowed(request)


class PoolOperationReadOnlyAdmin(PoolSuperuserAdmin):
    """Expose derived team-match operation history without write bypasses.

    Lineup, schedule, rack-link, substitution, and audit rows are maintained
    by the transactional pool team-operations service.  Keeping their Django
    admin representations read-only prevents a manual edit from desynchronising
    the parent fixture score, snapshots, or eligibility state.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PoolCompetitionMembershipInline(PoolSuperuserInline):
    model = PoolCompetitionMembership
    extra = 0
    autocomplete_fields = ("user",)


class PoolCompetitionStageInline(PoolSuperuserInline):
    model = PoolCompetitionStage
    extra = 0
    ordering = ("order",)


class PoolCompetitionEntryInline(PoolSuperuserInline):
    model = PoolCompetitionEntry
    extra = 0
    autocomplete_fields = ("player", "team")


class PoolCompetitionTieBreakerInline(PoolSuperuserInline):
    model = PoolCompetitionTieBreaker
    extra = 0
    ordering = ("order",)
    readonly_fields = ("criterion", "order", "created_at", "updated_at")

    # Ordered rows are replaced atomically by the scoped manager service.
    # Editing two unique order values through an inline would reintroduce the
    # temporary collision the pool implementation is designed to avoid.
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PoolCompetitionMarginPointRuleInline(PoolSuperuserInline):
    """Display configured bands without bypassing the scoped policy service."""

    model = PoolCompetitionMarginPointRule
    extra = 0
    ordering = ("minimum_margin",)
    readonly_fields = (
        "minimum_margin",
        "maximum_margin",
        "winner_points",
        "loser_points",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolPlayer)
class PoolPlayerAdmin(PoolSuperuserAdmin):
    list_display = (
        "full_name",
        "nickname",
        "region",
        "country",
        "is_active",
        "user",
    )
    list_filter = ("is_active", "gender", "country")
    search_fields = ("full_name", "nickname", "public_id", "user__username", "user__email")
    autocomplete_fields = ("user",)
    readonly_fields = ("public_id", "created_at", "updated_at")

    def has_delete_permission(self, request, obj=None):
        """Only real superusers may attempt a safe global player deletion."""
        return super().has_delete_permission(request, obj)

    def get_deleted_objects(self, objs, request):
        """Keep protected history visible instead of misreporting permission denial."""
        deleted_objects, model_count, perms_needed, protected = super().get_deleted_objects(
            objs,
            request,
        )
        if self._is_allowed(request):
            if protected:
                messages.warning(
                    request,
                    "This player has protected historical Pool data and cannot be hard-deleted. Set is_active to False instead.",
                )
            return deleted_objects, model_count, set(), protected
        return deleted_objects, model_count, perms_needed, protected


@admin.register(PoolTeam)
class PoolTeamAdmin(PoolSuperuserAdmin):
    list_display = ("name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PoolCompetition)
class PoolCompetitionAdmin(PoolSuperuserAdmin):
    list_display = (
        "name",
        "series",
        "season",
        "participant_mode",
        "format_type",
        "visibility",
        "operational_state",
        "is_archived",
        "owner",
        "start_date",
        "end_date",
    )
    list_filter = (
        "visibility",
        "operational_state",
        "participant_mode",
        "format_type",
        "is_archived",
    )
    search_fields = ("name", "slug", "venue", "owner__username", "owner__email")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("owner",)
    readonly_fields = (
        "series",
        "is_archived",
        "archived_at",
        "archived_by",
        "created_at",
        "updated_at",
    )
    inlines = (
        PoolCompetitionMembershipInline,
        PoolCompetitionStageInline,
        PoolCompetitionEntryInline,
        PoolCompetitionTieBreakerInline,
        PoolCompetitionMarginPointRuleInline,
    )

    def get_deleted_objects(self, objs, request):
        """Preview only selected competitions, without collecting descendants.

        Django's normal collector recursively renders every fixture, rack,
        lineup, and audit row before confirmation.  That is needlessly
        expensive for an already-authorised competition cascade and can time
        out long before the actual database deletion is attempted.
        """
        if self._is_allowed(request):
            selected = [str(competition) for competition in objs]
            messages.warning(
                request,
                "Competition-specific fixtures, results, stages, rosters, racks, and related records will also be deleted. This action cannot be undone.",
            )
            return (
                selected,
                {self.opts.verbose_name_plural: len(selected)},
                set(),
                [],
            )
        return super().get_deleted_objects(objs, request)

    def get_inline_instances(self, request, obj=None):
        """Let the creation signal provision stages before they can be edited.

        On the add form a stage inline could race the post-save default-stage
        service and collide with its unique competition/order constraints.
        Administrators can adjust stages from the competition change page once
        the explicit defaults exist.
        """
        instances = super().get_inline_instances(request, obj)
        if obj is None:
            return [
                inline
                for inline in instances
                if not isinstance(inline, PoolCompetitionStageInline)
            ]
        return instances

    def get_readonly_fields(self, request, obj=None):
        """The scoped manager service owns all standings-points changes.

        Direct admin updates would bypass complete margin-band validation and
        the generated-qualification-bracket lock, so superusers can inspect
        but not mutate this policy here.
        """
        return (*super().get_readonly_fields(request, obj), "points_mode", "points_for_win", "points_for_loss")


@admin.register(PoolCompetitionSeries)
class PoolCompetitionSeriesAdmin(PoolSuperuserAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")
    readonly_fields = ("created_at",)


@admin.register(PoolCompetitionMembership)
class PoolCompetitionMembershipAdmin(PoolSuperuserAdmin):
    list_display = ("user", "competition", "role", "is_active", "created_at")
    list_filter = ("role", "is_active", "competition")
    search_fields = ("user__username", "user__email", "competition__name")
    autocomplete_fields = ("user", "competition")


@admin.register(PoolCompetitionEntry)
class PoolCompetitionEntryAdmin(PoolSuperuserAdmin):
    list_display = ("display_name", "competition", "participant_kind", "seed")
    list_filter = ("competition",)
    search_fields = (
        "competition__name",
        "player__full_name",
        "player__nickname",
        "team__name",
    )
    autocomplete_fields = ("competition", "player", "team")

    @admin.display(description="Participant type")
    def participant_kind(self, obj):
        return "Team" if obj.team_id else "Player"


@admin.register(PoolCompetitionEntryLife)
class PoolCompetitionEntryLifeAdmin(PoolOperationReadOnlyAdmin):
    list_display = ("competition_entry", "life_number", "status", "eliminated_at")
    list_filter = ("status", "competition_entry__competition")
    search_fields = (
        "competition_entry__player__full_name",
        "competition_entry__player__nickname",
        "competition_entry__competition__name",
    )
    autocomplete_fields = ("competition_entry",)
    readonly_fields = (
        "competition_entry",
        "life_number",
        "status",
        "eliminated_at",
        "created_at",
        "updated_at",
    )


@admin.register(PoolTeamRoster)
class PoolTeamRosterAdmin(PoolSuperuserAdmin):
    list_display = ("player", "team", "competition", "is_active", "sort_order")
    list_filter = ("is_active", "competition_entry__competition")
    search_fields = (
        "player__full_name",
        "player__nickname",
        "competition_entry__team__name",
        "competition_entry__competition__name",
    )
    autocomplete_fields = ("competition_entry", "player")

    @admin.display(description="Team")
    def team(self, obj):
        return obj.team

    @admin.display(description="Competition")
    def competition(self, obj):
        return obj.competition


@admin.register(PoolCompetitionTeamStaffAssignment)
class PoolCompetitionTeamStaffAssignmentAdmin(PoolOperationReadOnlyAdmin):
    list_display = (
        "user",
        "competition_entry",
        "role",
        "is_active",
        "created_at",
    )
    list_filter = ("role", "is_active", "competition_entry__competition")
    search_fields = (
        "user__username",
        "user__email",
        "competition_entry__team__name",
        "competition_entry__competition__name",
    )
    autocomplete_fields = ("user", "competition_entry")
    readonly_fields = (
        "competition_entry",
        "user",
        "role",
        "is_active",
        "created_at",
        "updated_at",
    )

@admin.register(PoolMatchScorerAssignment)
class PoolMatchScorerAssignmentAdmin(PoolOperationReadOnlyAdmin):
    list_display = (
        "user",
        "match",
        "is_active",
        "assigned_by",
        "created_at",
    )
    list_filter = (
        "is_active",
        "match__competition",
    )
    search_fields = (
        "user__username",
        "user__email",
        "match__competition__name",
        "match__home_entry__player__full_name",
        "match__away_entry__player__full_name",
        "match__home_entry__team__name",
        "match__away_entry__team__name",
    )
    readonly_fields = (
        "match",
        "user",
        "assigned_by",
        "is_active",
        "created_at",
        "updated_at",
    )

@admin.register(PoolTeamMatchLineup)
class PoolTeamMatchLineupAdmin(PoolOperationReadOnlyAdmin):
    list_display = ("match", "team_entry", "submitted_by", "submitted_at", "updated_at")
    list_filter = ("match__competition",)
    search_fields = (
        "match__competition__name",
        "team_entry__team__name",
        "submitted_by__username",
    )
    autocomplete_fields = ("match", "team_entry", "submitted_by")
    readonly_fields = (
        "match",
        "team_entry",
        "submitted_by",
        "submitted_at",
        "updated_at",
    )


@admin.register(PoolTeamMatchLineupMember)
class PoolTeamMatchLineupMemberAdmin(PoolOperationReadOnlyAdmin):
    list_display = ("player", "lineup", "created_at", "updated_at")
    list_filter = ("lineup__match__competition",)
    search_fields = (
        "player__full_name",
        "player__nickname",
        "lineup__team_entry__team__name",
        "lineup__match__competition__name",
    )
    autocomplete_fields = ("lineup", "player")
    readonly_fields = ("lineup", "player", "created_at", "updated_at")


@admin.register(PoolTeamMatchLineupPosition)
class PoolTeamMatchLineupPositionAdmin(PoolOperationReadOnlyAdmin):
    list_display = ("lineup", "position", "player", "updated_at")
    list_filter = ("lineup__match__competition",)
    search_fields = (
        "player__full_name",
        "player__nickname",
        "lineup__team_entry__team__name",
        "lineup__match__competition__name",
    )
    autocomplete_fields = ("lineup", "player")
    readonly_fields = ("lineup", "position", "player", "created_at", "updated_at")


@admin.register(PoolTeamMatchScheduledRack)
class PoolTeamMatchScheduledRackAdmin(PoolOperationReadOnlyAdmin):
    list_display = (
        "match",
        "sequence_number",
        "session_number",
        "home_position",
        "away_position",
        "status",
        "table_number",
        "home_player",
        "away_player",
        "result",
    )
    list_filter = ("status", "match__competition")
    search_fields = (
        "match__competition__name",
        "match__home_entry__team__name",
        "match__away_entry__team__name",
        "home_player__full_name",
        "away_player__full_name",
    )
    autocomplete_fields = ("match", "home_player", "away_player", "result")
    readonly_fields = (
        "match",
        "sequence_number",
        "session_number",
        "home_position",
        "away_position",
        "status",
        "table_number",
        "home_player",
        "away_player",
        "result",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    )


@admin.register(PoolTeamMatchSubstitution)
class PoolTeamMatchSubstitutionAdmin(PoolOperationReadOnlyAdmin):
    list_display = (
        "match",
        "team_entry",
        "position",
        "outgoing_player",
        "incoming_player",
        "actor",
        "performed_at",
    )
    list_filter = ("match__competition", "team_entry")
    search_fields = (
        "match__competition__name",
        "team_entry__team__name",
        "outgoing_player__full_name",
        "incoming_player__full_name",
        "actor__username",
    )
    autocomplete_fields = (
        "match",
        "team_entry",
        "outgoing_player",
        "incoming_player",
        "actor",
    )
    readonly_fields = (
        "match",
        "team_entry",
        "position",
        "outgoing_player",
        "incoming_player",
        "actor",
        "reason",
        "performed_at",
    )


@admin.register(PoolTeamMatchAuditEvent)
class PoolTeamMatchAuditEventAdmin(PoolOperationReadOnlyAdmin):
    list_display = ("match", "team_entry", "action", "actor", "created_at")
    list_filter = ("action", "match__competition")
    search_fields = (
        "match__competition__name",
        "team_entry__team__name",
        "actor__username",
        "message",
    )
    autocomplete_fields = ("match", "team_entry", "actor")
    readonly_fields = (
        "match",
        "team_entry",
        "actor",
        "action",
        "message",
        "payload",
        "created_at",
    )


@admin.register(PoolCompetitionStage)
class PoolCompetitionStageAdmin(PoolSuperuserAdmin):
    list_display = ("name", "competition", "stage_type", "order")
    list_filter = ("stage_type", "competition")
    search_fields = ("name", "competition__name")
    autocomplete_fields = ("competition",)
    ordering = ("competition__name", "order")


@admin.register(PoolKnockoutRoundDefinition)
class PoolKnockoutRoundDefinitionAdmin(PoolOperationReadOnlyAdmin):
    """Round metadata is generated atomically by the manager workflow."""

    list_display = ("name", "stage", "round_size", "order")
    list_filter = ("stage__competition",)
    search_fields = ("name", "stage__name", "stage__competition__name")
    autocomplete_fields = ("stage",)
    ordering = ("stage__competition__name", "stage__order", "order")
    readonly_fields = ("stage", "order", "round_size", "name", "created_at", "updated_at")


@admin.register(PoolCompetitionTieBreaker)
class PoolCompetitionTieBreakerAdmin(PoolSuperuserAdmin):
    list_display = ("competition", "order", "criterion")
    list_filter = ("criterion", "competition")
    search_fields = ("competition__name",)
    autocomplete_fields = ("competition",)
    ordering = ("competition__name", "order")
    readonly_fields = ("competition", "order", "criterion", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolCompetitionMarginPointRule)
class PoolCompetitionMarginPointRuleAdmin(PoolSuperuserAdmin):
    """Read-only audit view; the scoped standings form is the write boundary."""

    list_display = (
        "competition",
        "minimum_margin",
        "maximum_margin",
        "winner_points",
        "loser_points",
    )
    list_filter = ("competition",)
    search_fields = ("competition__name",)
    autocomplete_fields = ("competition",)
    ordering = ("competition__name", "minimum_margin", "id")
    readonly_fields = (
        "competition",
        "minimum_margin",
        "maximum_margin",
        "winner_points",
        "loser_points",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolGroup)
class PoolGroupAdmin(PoolSuperuserAdmin):
    list_display = ("name", "stage", "competition_name", "order", "qualifiers")
    list_filter = ("stage__competition",)
    search_fields = ("name", "stage__name", "stage__competition__name")
    autocomplete_fields = ("stage",)
    ordering = ("stage__competition__name", "stage__order", "order")

    @admin.display(description="Competition")
    def competition_name(self, obj):
        return obj.stage.competition


@admin.register(PoolGroupEntry)
class PoolGroupEntryAdmin(PoolSuperuserAdmin):
    list_display = ("entry", "group", "stage", "sort_order")
    list_filter = ("stage__competition", "group")
    search_fields = (
        "entry__player__full_name",
        "entry__team__name",
        "group__name",
        "stage__competition__name",
    )
    autocomplete_fields = ("stage", "group", "entry")


@admin.register(PoolStandingBaseline)
class PoolStandingBaselineAdmin(PoolSuperuserAdmin):
    """Maintenance/debug access; normal editing happens in the manager UI."""

    list_display = (
        "competition",
        "stage",
        "group",
        "entry",
        "played",
        "won",
        "lost",
        "racks_for",
        "racks_against",
        "points",
    )
    list_filter = ("competition", "stage", "group")
    search_fields = (
        "competition__name",
        "stage__name",
        "group__name",
        "entry__player__full_name",
        "entry__team__name",
    )
    autocomplete_fields = ("competition", "stage", "group", "entry")
    ordering = ("competition__name", "stage__order", "group__order", "entry__id")


@admin.register(PoolGroupQualificationRule)
class PoolGroupQualificationRuleAdmin(PoolSuperuserAdmin):
    list_display = (
        "group",
        "position",
        "knockout_stage",
        "destination_slot",
        "keep_groups_apart",
    )
    list_filter = ("source_stage__competition", "knockout_stage")
    search_fields = (
        "group__name",
        "source_stage__competition__name",
        "knockout_stage__name",
    )
    autocomplete_fields = ("source_stage", "group", "knockout_stage")


@admin.register(PoolMatch)
class PoolMatchAdmin(PoolSuperuserAdmin):
    list_display = (
        "__str__",
        "competition",
        "stage",
        "scheduled_at",
        "status",
        "home_score",
        "away_score",
        "winner",
    )
    list_filter = ("status", "result_method", "competition", "stage")
    search_fields = (
        "competition__name",
        "home_entry__player__full_name",
        "away_entry__player__full_name",
        "home_entry__team__name",
        "away_entry__team__name",
        "round_label",
        "venue",
    )
    autocomplete_fields = (
        "competition",
        "stage",
        "group",
        "knockout_round",
        "home_entry",
        "away_entry",
    )
    readonly_fields = (
        "status",
        "home_score",
        "away_score",
        "winner",
        "winner_entry_life",
        "result_method",
        "completion_reason",
        "completed_at",
        "applied_time_limit_minutes",
        "timer_snapshot_at",
        "timer_started_at",
        "timer_deadline",
        "time_expired_tied",
        "is_in_correction_mode",
        "created_at",
        "updated_at",
    )
    date_hierarchy = "scheduled_at"
    list_per_page = 50

    def get_queryset(self, request):
        """Load every relation used while rendering one changelist row.

        ``action_checkbox`` stringifies each match, as do the first and winner
        columns.  A match name follows its two competition entries to either a
        player or a team, while a winner entry's ``__str__`` additionally uses
        its competition.  The stage label follows its competition as well.
        Keep all of those joins in the main query so the changelist does not
        issue relation queries for every rendered fixture.
        """

        return super().get_queryset(request).select_related(
            "competition",
            "stage",
            "stage__competition",
            "home_entry",
            "home_entry__player",
            "home_entry__team",
            "away_entry",
            "away_entry__player",
            "away_entry__team",
            "winner",
            "winner__competition",
            "winner__player",
            "winner__team",
        )

    def get_actions(self, request):
        """Wrap, rather than remove, Django's standard bulk-delete action."""

        actions = super().get_actions(request)
        if "delete_selected" in actions:
            _action, name, description = actions["delete_selected"]
            actions["delete_selected"] = (
                type(self).delete_selected,
                name,
                description,
            )
        return actions

    def delete_selected(self, request, queryset):
        """Keep generated bracket fixtures attached to their knockout ties.

        Django's built-in action checks ``has_delete_permission(request)`` but
        does not call the object-level form for every selected fixture.  That
        would allow ``PoolKnockoutTie.match`` (which intentionally uses
        ``SET_NULL``) to be cleared.  Check the complete selection before the
        confirmation or deletion path so a mixed selection cannot be partially
        deleted.  Repeating this check on the confirmation POST also closes
        the gap between the two action requests.
        """

        if PoolKnockoutTie.objects.filter(match__in=queryset).exists():
            self.message_user(
                request,
                "Generated bracket fixtures cannot be deleted from the match "
                "admin. Reset the bracket through its lifecycle workflow instead.",
                messages.ERROR,
            )
            return None
        return admin_actions.delete_selected(self, request, queryset)

    def get_deleted_objects(self, objs, request):
        """Preview only selected fixtures, without collecting match history.

        Django's normal deletion collector recursively renders every
        match-owned rack, lineup, result revision, and team-operation row for
        confirmation.  That can traverse relation-heavy ``__str__`` methods
        and time out before an authorised deletion is even confirmed.  The
        actual deletion still uses Django's normal cascade behaviour.
        """

        if self._is_allowed(request):
            selected = [f"Pool match #{pool_match.pk}" for pool_match in objs]
            messages.warning(
                request,
                "Match-owned racks, lineups, scorer assignments, result history, "
                "and other related match data will also be deleted. This action "
                "cannot be undone.",
            )
            return (
                selected,
                {self.opts.verbose_name_plural: len(selected)},
                set(),
                [],
            )
        return super().get_deleted_objects(objs, request)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and PoolKnockoutTie.objects.filter(match=obj).exists():
            # The bracket service owns every result-relevant fixture field for
            # generated ties.  Superusers can still inspect/schedule it, but
            # cannot silently redraw the graph through Django admin.
            fields.extend(
                [
                    "competition",
                    "stage",
                    "group",
                    "knockout_round",
                    "home_entry",
                    "away_entry",
                    "race_target",
                    "round_label",
                ]
            )
        if obj and obj.team_scheduled_racks.exists():
            # Once a persistent team sheet exists, its target and table
            # capacity are owned by the team-operations service.  Direct
            # admin edits could leave previously started rows ineligible.
            fields.extend(["race_target", "table_count"])
        return tuple(fields)

    def has_delete_permission(self, request, obj=None):
        if obj and PoolKnockoutTie.objects.filter(match=obj).exists():
            return False
        return super().has_delete_permission(request, obj)


@admin.register(PoolRack)
class PoolRackAdmin(PoolSuperuserAdmin):
    """Rack history is written through the transactional manager service."""

    list_display = (
        "match",
        "rack_number",
        "winner",
        "winning_player",
        "outcome_type",
        "breaker",
        "recorded_at",
    )
    list_filter = ("outcome_type", "match__competition")
    search_fields = (
        "match__competition__name",
        "winner__player__full_name",
        "winner__team__name",
        "winning_player__full_name",
    )
    readonly_fields = (
        "match",
        "rack_number",
        "winner",
        "winning_player",
        "outcome_type",
        "breaker",
        "recorded_by",
        "recorded_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolMatchResultRevision)
class PoolMatchResultRevisionAdmin(PoolSuperuserAdmin):
    list_display = ("match", "action", "revised_by", "created_at", "reason")
    list_filter = ("action", "match__competition", "created_at")
    search_fields = ("match__competition__name", "reason", "revised_by__username")
    autocomplete_fields = ("match", "revised_by")
    readonly_fields = (
        "match",
        "revised_by",
        "action",
        "reason",
        "previous_result",
        "revised_result",
        "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolBracketDrawPreview)
class PoolBracketDrawPreviewAdmin(PoolSuperuserAdmin):
    list_display = ("competition", "stage", "draw_method", "bracket_size", "created_by", "created_at")
    list_filter = ("draw_method", "stage__competition")
    search_fields = ("competition__name", "stage__name", "token")
    autocomplete_fields = ("competition", "stage", "handoff_from_round", "created_by")
    readonly_fields = (
        "token",
        "draw_order",
        "life_draw_order",
        "round_race_targets",
        "created_at",
    )


@admin.register(PoolKnockoutBracket)
class PoolKnockoutBracketAdmin(PoolSuperuserAdmin):
    list_display = ("competition", "stage", "size", "draw_method", "third_place_enabled", "generated_at")
    list_filter = ("draw_method", "third_place_enabled", "competition")
    search_fields = ("competition__name", "stage__name")
    autocomplete_fields = ("competition", "stage", "handoff_from_round", "generated_by")
    readonly_fields = ("draw_order", "life_draw_order", "generated_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolKnockoutRoundRule)
class PoolKnockoutRoundRuleAdmin(PoolSuperuserAdmin):
    list_display = ("bracket", "round_number", "name", "race_target", "is_third_place")
    list_filter = ("is_third_place", "bracket__competition")
    search_fields = ("bracket__competition__name", "name")
    autocomplete_fields = ("bracket",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolKnockoutTie)
class PoolKnockoutTieAdmin(PoolSuperuserAdmin):
    list_display = ("bracket", "round_name", "position", "is_third_place", "match", "resolved_winner", "resolution_method")
    list_filter = ("is_third_place", "resolution_method", "bracket__competition")
    search_fields = ("bracket__competition__name", "round_name")
    autocomplete_fields = ("bracket", "match", "resolved_winner", "resolved_winner_life")
    readonly_fields = (
        "resolved_winner",
        "resolved_winner_life",
        "resolution_method",
        "resolved_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolKnockoutTieSlot)
class PoolKnockoutTieSlotAdmin(PoolSuperuserAdmin):
    list_display = ("tie", "slot", "entry", "source_type", "source_label")
    list_filter = ("slot", "source_type", "tie__bracket__competition")
    search_fields = ("tie__bracket__competition__name", "entry__player__full_name", "entry__team__name")
    autocomplete_fields = (
        "tie",
        "entry",
        "entry_life",
        "source_entry",
        "source_entry_life",
        "source_group",
        "source_tie",
    )
    readonly_fields = ("source_label", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolBracketAuditEvent)
class PoolBracketAuditEventAdmin(PoolSuperuserAdmin):
    list_display = ("competition", "stage", "bracket", "action", "actor", "created_at")
    list_filter = ("action", "competition", "stage")
    search_fields = ("competition__name", "stage__name", "message", "actor__username")
    autocomplete_fields = ("bracket", "competition", "stage", "actor")
    readonly_fields = (
        "bracket",
        "competition",
        "stage",
        "actor",
        "action",
        "message",
        "payload",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PoolLeagueAuditEvent)
class PoolLeagueAuditEventAdmin(PoolSuperuserAdmin):
    list_display = ("competition", "stage", "group", "action", "actor", "created_at")
    list_filter = ("action", "competition", "stage", "group")
    search_fields = ("competition__name", "stage__name", "group__name", "message", "actor__username")
    autocomplete_fields = ("competition", "stage", "group", "actor")
    readonly_fields = (
        "competition",
        "stage",
        "group",
        "actor",
        "action",
        "message",
        "payload",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
