"""Competition-scoped management views for the 8-ball pool domain.

Pool management deliberately does not share the football manager's models or
URLs.  Every nested object lookup is constrained by an already-authorised
competition, preventing identifier changes from crossing competition
boundaries.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.db.models.deletion import ProtectedError, RestrictedError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from uuid import UUID, uuid4

from .forms import (
    PoolBracketDrawForm,
    PoolBracketHandoffForm,
    PoolBracketPreviewConfirmForm,
    PoolBracketResetForm,
    PoolBracketRoundTargetsForm,
    PoolCompetitionAdminForm,
    PoolCompetitionEntryForm,
    PoolCompetitionEntrySeedForm,
    PoolCompetitionInfoForm,
    PoolMoneyMatchCreateForm,
    PoolMoneyMatchEditForm,
    PoolNextSeasonForm,
    PoolExistingPlayerBulkEntryForm,
    PoolPlayerRegistrationForm,
    PoolMarginPointThresholdFormSet,
    PoolCompetitionStageForm,
    PoolCompetitionStandingsForm,
    PoolCompetitionTeamStaffForm,
    PoolCompetitionTieBreakerForm,
    PoolFixtureForm,
    PoolLeagueFixturePreviewConfirmForm,
    PoolLeagueFixtureScopeForm,
    PoolKnockoutRoundConfigurationForm,
    PoolGroupEntryForm,
    PoolGroupBulkEntryForm,
    PoolGroupEntryOrderForm,
    PoolGroupForm,
    PoolGroupQualificationRuleForm,
    PoolMatchTransitionForm,
    PoolManualFinalResultForm,
    PoolNonPlayedResultForm,
    PoolRackCorrectionForm,
    PoolRackResultForm,
    PoolResultCorrectionForm,
    PoolTeamForm,
    PoolTeamMatchLineupForm,
    PoolTeamMatchSubstitutionForm,
    PoolTeamMatchVacancyFillForm,
    PoolTeamScheduledRackCorrectionForm,
    PoolTeamScheduledRackResultForm,
    PoolTeamScheduledRackStartForm,
    PoolTeamRosterBulkForm,
    PoolTeamRosterForm,
    PoolTeamRosterPlayerRegistrationForm,
    PoolTeamRosterUpdateForm,
    PoolMatchScorerForm,
    PoolStandingBaselineFormSet,
    competition_allows_two_lives,
    margin_point_threshold_initial,
)
from .baselines import (
    BASELINE_FIELDS,
    baseline_entries_for_scope,
    baseline_values_by_entry,
    save_pool_standing_baselines,
    scope_has_baselines,
)
from .league_fixtures import (
    LeagueFixtureValidationError,
    confirm_fixture_preview,
    get_league_fixture_scope,
    league_fixture_status,
    plan_first_round_preview,
    plan_second_round_preview,
    preview_entries_by_id,
    serialize_fixture_preview,
)
from .models import (
    PoolBracketAuditEvent,
    PoolBracketDrawPreview,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolCompetitionTeamStaffAssignment,
    PoolGroup,
    PoolGroupEntry,
    PoolGroupQualificationRule,
    PoolKnockoutBracket,
    PoolKnockoutRoundDefinition,
    PoolKnockoutTie,
    PoolKnockoutTieSlot,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    SUPPORTED_KNOCKOUT_ROUND_SIZES,
    PoolTeamMatchLineup,
    PoolTeamMatchScheduledRack,
    PoolTeamRoster,
    PoolMatchScorerAssignment,
)
from .permissions import (
    managed_pool_competitions,
    user_can_manage_competition,
    user_can_manage_team_lineup,
    user_has_pool_management_access,
    user_is_pool_competition_owner,
    user_can_score_pool_match,
)
from .queries import match_rack_summary
from .standings import (
    configured_pool_margin_point_rules,
    configured_pool_tie_breakers,
    derive_pool_margin_point_rules_from_thresholds,
    maximum_meaningful_pool_margin,
    replace_pool_standings_points_configuration,
    replace_pool_tie_breakers,
    standings_for_stage,
)
from .services import (
    PoolResultValidationError,
    correct_rack,
    expire_match_if_due,
    open_result_correction,
    record_manual_final_result,
    record_non_played_result,
    record_rack,
    create_money_match,
    configure_money_match,
    ensure_money_match_fixture,
    start_match,
    transition_match_status,
    undo_rack,
)
from .seasons import (
    PoolSeasonValidationError,
    archive_eligibility_errors,
    archive_pool_competition,
    create_next_pool_season,
)
from .team_operations import (
    complete_team_match_rack,
    correct_completed_team_match_rack,
    fill_team_match_vacancy,
    is_team_fixture,
    start_next_eligible_team_rack,
    submit_team_match_lineup,
    substitute_team_match_player,
    team_match_schedule_state,
)
from .knockout_rounds import configure_knockout_rounds


def _validation_message(error):
    """Return a user-facing message from a domain or form validation error."""
    if hasattr(error, "messages"):
        return " ".join(str(message) for message in error.messages)
    return str(error)


def _margin_allocation_preview(rules, *, maximum_margin=None):
    """Return small, display-ready summaries of centrally-derived ranges."""

    summaries = []
    for rule in rules:
        minimum = rule.minimum_margin
        maximum = rule.maximum_margin
        if maximum is None:
            # Older records may be open ended.  Their manager preview still
            # reflects the finite current table maximum, never a fictional
            # beyond-target range.
            maximum = maximum_margin
        if maximum is None:
            margin_label = f"{minimum} racks"
        elif minimum == maximum:
            margin_label = f"{minimum} rack"
        else:
            margin_label = f"{minimum}\N{EN DASH}{maximum} racks"
        winner_label = f"{rule.winner_points} point{'s' if rule.winner_points != 1 else ''}"
        loser_label = f"{rule.loser_points} point{'s' if rule.loser_points != 1 else ''}"
        summaries.append(f"Win by {margin_label}: {winner_label} / {loser_label}")
    return summaries


def _managed_competition_or_404(request, slug):
    """Fetch a competition only from the current user's management queryset."""
    competition = get_object_or_404(managed_pool_competitions(request.user), slug=slug)
    if (
        competition.is_archived
        and not request.user.is_superuser
        and request.method not in {"GET", "HEAD", "OPTIONS"}
    ):
        raise PermissionDenied("Archived season — historical data is read-only.")
    return competition


_MANAGER_TAB_URL_NAMES = {
    "manage_competition_dashboard": "overview",
    "manage_competition_information": "information",
    "manage_competition_entries": "entries",
    "manage_competition_teams": "teams",
    "manage_competition_stages": "stages",
    "manage_competition_standings": "standings",
    "manage_competition_base_standings": "base_standings",
    "manage_competition_fixture_generator": "fixture_generator",
    "manage_competition_groups": "groups",
    "manage_competition_qualifications": "qualification",
    "manage_competition_knockout_rounds": "rounds",
    "manage_competition_bracket": "bracket",
    "manage_competition_fixtures": "fixtures",
    "manage_competition_admins": "administrators",
}


def _manager_tab_definitions(request, competition):
    """Return the tabs that are structurally available to this manager.

    These URLs deliberately remain first-class Django pages.  The tab shell
    enhances them with one-time partial loading, while direct bookmarks,
    create/edit pages, and no-JavaScript navigation all retain their current
    server-rendered behaviour.
    """

    tabs = [
        {
            "key": "overview",
            "label": "Overview",
            "url": reverse("pool:manage_competition_dashboard", args=[competition.slug]),
        },
        {
            "key": "information",
            "label": "Information",
            "url": reverse("pool:manage_competition_information", args=[competition.slug]),
        },
        {
            "key": "entries",
            "label": "Entries",
            "url": reverse("pool:manage_competition_entries", args=[competition.slug]),
        },
    ]
    if competition.is_money_match:
        tabs.pop()
        tabs.append(
            {
                "key": "fixtures",
                "label": "Money Match",
                "url": reverse("pool:manage_competition_fixtures", args=[competition.slug]),
            }
        )
        if user_is_pool_competition_owner(request.user, competition):
            tabs.append(
                {
                    "key": "administrators",
                    "label": "Administrators",
                    "url": reverse("pool:manage_competition_admins", args=[competition.slug]),
                }
            )
        return tabs
    if competition.participant_mode == PoolCompetition.ParticipantMode.TEAMS:
        tabs.append(
            {
                "key": "teams",
                "label": "Teams & rosters",
                "url": reverse("pool:manage_competition_teams", args=[competition.slug]),
            }
        )
    tabs.append(
        {
            "key": "stages",
            "label": "Stages",
            "url": reverse("pool:manage_competition_stages", args=[competition.slug]),
        }
    )
    if competition.has_league_or_group_stage:
        tabs.append(
            {
                "key": "standings",
                "label": "Standings",
                "url": reverse("pool:manage_competition_standings", args=[competition.slug]),
            }
        )
        if request.user.is_superuser:
            tabs.append(
                {
                    "key": "base_standings",
                    "label": "Base standings",
                    "url": reverse(
                        "pool:manage_competition_base_standings",
                        args=[competition.slug],
                    ),
                }
            )
        tabs.append(
            {
                "key": "fixture_generator",
                "label": "Fixture generator",
                "url": reverse(
                    "pool:manage_competition_fixture_generator",
                    args=[competition.slug],
                ),
            }
        )
    if competition.format_type == PoolCompetition.FormatType.LEAGUE_KNOCKOUT:
        tabs.extend(
            (
                {
                    "key": "groups",
                    "label": "Groups",
                    "url": reverse("pool:manage_competition_groups", args=[competition.slug]),
                },
                {
                    "key": "qualification",
                    "label": "Qualification",
                    "url": reverse(
                        "pool:manage_competition_qualifications",
                        args=[competition.slug],
                    ),
                },
            )
        )
    if competition.has_knockout_stage:
        tabs.append(
            {
                "key": "rounds",
                "label": "Rounds",
                "url": reverse(
                    "pool:manage_competition_knockout_rounds",
                    args=[competition.slug],
                ),
            }
        )
        tabs.append(
            {
                "key": "bracket",
                "label": "Bracket",
                "url": reverse("pool:manage_competition_bracket", args=[competition.slug]),
            }
        )
    tabs.append(
        {
            "key": "fixtures",
            "label": "Fixtures & results",
            "url": reverse("pool:manage_competition_fixtures", args=[competition.slug]),
        }
    )
    if user_is_pool_competition_owner(request.user, competition):
        tabs.append(
            {
                "key": "administrators",
                "label": "Administrators",
                "url": reverse("pool:manage_competition_admins", args=[competition.slug]),
            }
        )
    return tabs


def _manager_active_tab(request):
    """Return the top-level source tab for this request, if it has one."""

    partial_source = getattr(request, "_pool_manager_source_tab", None)
    if partial_source:
        return partial_source
    resolver_match = getattr(request, "resolver_match", None)
    return _MANAGER_TAB_URL_NAMES.get(
        getattr(resolver_match, "url_name", None),
    )


def _manager_context(request, competition, **extra):
    """Shared template context for competition-scoped manager pages."""
    navigation_fallback = extra.get("page_navigation_back_url")
    if not navigation_fallback:
        pool_match = extra.get("match")
        if pool_match is not None:
            if (
                extra.get("scorer_mode")
                and pool_match.competition.visibility
                != PoolCompetition.Visibility.PUBLISHED
            ):
                # An exact scorer does not inherit manager access to a draft
                # competition's public detail page.  Do not make the safe
                # fallback point back at the current scorer screen.
                navigation_fallback = reverse("pool:home")
            else:
                navigation_fallback = reverse(
                    "pool:match_detail",
                    args=[pool_match.pk],
                )
        elif request.resolver_match and (
            request.resolver_match.url_name == "manage_competition_dashboard"
        ):
            navigation_fallback = reverse(
                "pool:competition_detail",
                args=[competition.slug],
            )
        else:
            navigation_fallback = reverse(
                "pool:manage_competition_dashboard",
                args=[competition.slug],
            )
    context = {
        "competition": competition,
        "is_owner": user_is_pool_competition_owner(request.user, competition),
        "is_archived_read_only": bool(
            competition.is_archived and not request.user.is_superuser
        ),
        "public_preview_url": (
            f"{reverse('pool:competition_detail', args=[competition.slug])}?preview=1"
        ),
        "page_navigation_back_url": navigation_fallback,
        "page_navigation_show_pool_home": True,
        "pool_manager_tab_partial": bool(
            getattr(request, "_pool_manager_tab_partial", False)
        ),
        "pool_manager_base_template": (
            "pool/manage/_tab_fragment_base.html"
            if getattr(request, "_pool_manager_tab_partial", False)
            else "base.html"
        ),
    }
    active_tab = _manager_active_tab(request)
    if active_tab:
        manager_tabs = _manager_tab_definitions(request, competition)
        if any(tab["key"] == active_tab for tab in manager_tabs):
            context.update(
                {
                    "manager_tab_shell": True,
                    "manager_tabs": manager_tabs,
                    "manager_active_tab": active_tab,
                    "manager_tab_partial_url": reverse(
                        "pool:manage_competition_tab_content",
                        args=[competition.slug, "__tab__"],
                    ),
                }
            )
    context.update(extra)
    return context


def _competition_match_or_404(competition, match_id):
    return get_object_or_404(
        PoolMatch.objects.select_related(
            "competition",
            "stage",
            "group",
            "knockout_round",
            "home_entry__player",
            "home_entry__team",
            "away_entry__player",
            "away_entry__team",
            "home_entry_life",
            "away_entry_life",
            "winner__player",
            "winner__team",
            "winner_entry_life",
        ),
        pk=match_id,
        competition=competition,
    )

def _scoreable_match_or_404(request, slug, match_id):
    """Return one exact fixture only when the user may operate its scorer.

    This deliberately starts from the match-level scorer permission rather
    than the competition-manager queryset.  Managers still pass the same
    helper through ``user_can_score_pool_match``; a delegated scorer gains no
    route to any other fixture or competition management object.
    """
    competition = get_object_or_404(PoolCompetition, slug=slug)
    pool_match = _competition_match_or_404(competition, match_id)
    if not user_can_score_pool_match(request.user, pool_match):
        raise Http404("Match not found.")
    return competition, pool_match


def _scoreable_team_match_or_404(request, slug, match_id):
    """Return one exact team fixture only when the user may score it."""

    competition, pool_match = _scoreable_match_or_404(request, slug, match_id)
    if not is_team_fixture(pool_match):
        raise Http404("Team scoring is not available for this fixture.")
    return competition, pool_match


def _live_scorable_match_or_404(request, slug, match_id):
    """Return an authorised fixture only while live scoring remains available."""
    competition, pool_match = _scoreable_match_or_404(request, slug, match_id)
    if not pool_match.is_live_scoring_available:
        raise Http404("Live scoring is no longer available for this fixture.")
    return competition, pool_match


def _live_scorable_team_match_or_404(request, slug, match_id):
    """Return an authorised team fixture only while live scoring remains available."""
    competition, pool_match = _live_scorable_match_or_404(request, slug, match_id)
    if not is_team_fixture(pool_match):
        raise Http404("Team scoring is not available for this fixture.")
    return competition, pool_match


def _rack_scoring_match_or_404(request, slug, match_id):
    """Allow live rack scoring, plus only an idempotent retry after completion."""
    competition, pool_match = _scoreable_match_or_404(request, slug, match_id)
    if pool_match.is_live_scoring_available:
        return competition, pool_match

    try:
        request_token = UUID(str(request.POST.get("request_token", "")))
    except (TypeError, ValueError, AttributeError):
        raise Http404("Live scoring is no longer available for this fixture.")

    if not PoolRack.objects.filter(
        match=pool_match,
        request_token=request_token,
    ).exists():
        raise Http404("Live scoring is no longer available for this fixture.")
    return competition, pool_match

def _team_match_entry_or_404(pool_match, entry_id):
    """Resolve one exact team side of a fixture without trusting its id alone."""

    return get_object_or_404(
        PoolCompetitionEntry.objects.select_related("competition", "team"),
        pk=entry_id,
        competition=pool_match.competition,
        team__isnull=False,
        pk__in=(pool_match.home_entry_id, pool_match.away_entry_id),
    )


def _team_lineup_editable(*, user, pool_match, is_manager):
    """Status-only team-sheet lock, deliberately independent of schedule time."""

    has_started_scheduled_rack = PoolTeamMatchScheduledRack.objects.filter(
        match=pool_match,
        status__in=(
            PoolTeamMatchScheduledRack.Status.LIVE,
            PoolTeamMatchScheduledRack.Status.COMPLETED,
        ),
    ).exists()
    if pool_match.status in {
        PoolMatch.Status.UPCOMING,
        PoolMatch.Status.POSTPONED,
    }:
        # A postponed parent fixture does not erase actual team-match play.
        # Its sheet stays locked once a scheduled rack has been started.
        return not has_started_scheduled_rack
    return bool(
        is_manager
        and pool_match.status == PoolMatch.Status.LIVE
        and not has_started_scheduled_rack
    )


def _team_match_operations_context(request, competition, pool_match, **extra):
    """Build presentation data from the schedule read model, never mutations."""

    schedule_state = team_match_schedule_state(pool_match)
    lineups = {
        lineup.team_entry_id: lineup
        for lineup in PoolTeamMatchLineup.objects.filter(match=pool_match)
        .select_related("team_entry__team")
        .prefetch_related("positions__player", "members__player")
    }
    entries = (pool_match.home_entry, pool_match.away_entry)
    lineup_cards = []
    for entry in entries:
        lineup = lineups.get(entry.pk)
        positions = list(lineup.positions.all()) if lineup else []
        members = list(lineup.members.all()) if lineup else []
        complete = bool(
            lineup
            and len(positions) == 5
            and {position.position for position in positions} == {1, 2, 3, 4, 5}
        )
        filled_positions = [position for position in positions if position.player_id]
        lineup_cards.append(
            {
                "entry": entry,
                "lineup": lineup,
                "positions": positions,
                "is_complete": complete,
                "filled_position_count": len(filled_positions),
                "substitute_count": max(len(members) - len(filled_positions), 0),
            }
        )

    schedules = schedule_state["schedules"]
    live_schedules = schedule_state["live"]
    live_player_ids = {
        player_id
        for scheduled_rack in live_schedules
        for player_id in (
            scheduled_rack.home_player_id,
            scheduled_rack.away_player_id,
        )
        if player_id is not None
    }
    schedule_rows = []
    for scheduled_rack in schedules:
        row = {
            "scheduled_rack": scheduled_rack,
            "home_player": getattr(
                scheduled_rack,
                "current_home_player",
                scheduled_rack.home_player,
            ),
            "away_player": getattr(
                scheduled_rack,
                "current_away_player",
                scheduled_rack.away_player,
            ),
            "rack": scheduled_rack.result,
        }
        if (
            scheduled_rack.result_id
            and scheduled_rack.result.outcome_type
            != PoolRack.OutcomeType.LINEUP_VACANCY
        ):
            row["correction_form"] = PoolTeamScheduledRackCorrectionForm(
                scheduled_rack=scheduled_rack,
                initial={
                    "winner_player": scheduled_rack.result.winning_player_id,
                    "outcome_type": scheduled_rack.result.outcome_type,
                },
            )
        schedule_rows.append(row)
    live_rack_rows = [
        {
            "scheduled_rack": scheduled_rack,
            "home_player": scheduled_rack.home_player,
            "away_player": scheduled_rack.away_player,
            "result_form": PoolTeamScheduledRackResultForm(
                scheduled_rack=scheduled_rack
            ),
        }
        for scheduled_rack in live_schedules
    ]

    # The service repeats these eligibility checks under row locks, so this is
    # presentation only.  A live team fixture may use every configured free
    # physical table even when one side is near the race target; only an
    # actual completed clinching rack closes the remaining tables.
    pending_has_free_pairing = False
    for scheduled_rack in schedule_state["pending"]:
        home_player = getattr(scheduled_rack, "current_home_player", None)
        away_player = getattr(scheduled_rack, "current_away_player", None)
        if home_player is None or away_player is None:
            # The service resolves only the next due vacant pairing, never a
            # future cascade.  Exposing the normal Start control lets scorers
            # reach that authoritative operation without a fake table/rack.
            pending_has_free_pairing = True
            break
        if home_player.pk not in live_player_ids and away_player.pk not in live_player_ids:
            pending_has_free_pairing = True
            break
    no_new_racks = (
        bool(getattr(pool_match, "time_expired_tied", False))
        or not schedule_state["pending"]
    )
    occupied_tables = {
        scheduled_rack.table_number
        for scheduled_rack in live_schedules
        if scheduled_rack.table_number
    }
    available_tables = []
    if pool_match.status == PoolMatch.Status.LIVE and pending_has_free_pairing and not no_new_racks:
        available_tables = [
            table_number
            for table_number in range(1, pool_match.table_count + 1)
            if table_number not in occupied_tables
        ]

    substitution_rows = []
    vacancy_fill_rows = []
    if pool_match.status == PoolMatch.Status.LIVE:
        for card in lineup_cards:
            if card["lineup"] and card["is_complete"]:
                substitution_form = PoolTeamMatchSubstitutionForm(
                    match=pool_match,
                    team_entry=card["entry"],
                    lineup=card["lineup"],
                    busy_player_ids=live_player_ids,
                )
                if substitution_form.fields["position"].choices:
                    substitution_rows.append(
                        {
                            "entry": card["entry"],
                            "form": substitution_form,
                        }
                    )
                vacancy_form = PoolTeamMatchVacancyFillForm(
                    match=pool_match,
                    team_entry=card["entry"],
                    lineup=card["lineup"],
                    busy_player_ids=live_player_ids,
                )
                if vacancy_form.fields["position"].choices:
                    vacancy_fill_rows.append(
                        {
                            "entry": card["entry"],
                            "form": vacancy_form,
                        }
                    )
    can_manage_scorers = user_can_manage_competition(
        request.user,
        competition,
    )
    can_open_live_scorer = (
        user_can_score_pool_match(request.user, pool_match)
        and pool_match.is_live_scoring_available
    )

    scorer_assignments = (
        pool_match.scorer_assignments
        .select_related("user", "assigned_by")
        .order_by("-is_active", "user__username", "id")
    )

    scorer_form = (
        PoolMatchScorerForm(match=pool_match)
        if can_manage_scorers
        else None
    )

    up_next_rows = []
    for scheduled_rack in schedule_state.get("up_next", ()):
        up_next_rows.append(
            {
                "scheduled_rack": scheduled_rack,
                "home_player": getattr(
                    scheduled_rack,
                    "current_home_player",
                    None,
                ),
                "away_player": getattr(
                    scheduled_rack,
                    "current_away_player",
                    None,
                ),
            }
        )
    context = _manager_context(
        request,
        competition,
        match=pool_match,
        lineup_cards=lineup_cards,
        lineups_ready=all(card["is_complete"] for card in lineup_cards),
        schedule_rows=schedule_rows,
        live_rack_rows=live_rack_rows,
        substitution_rows=substitution_rows,
        vacancy_fill_rows=vacancy_fill_rows,
        available_tables=available_tables,
        no_new_racks=no_new_racks,
        team_timer_no_live_tie=(
            bool(getattr(pool_match, "time_expired_tied", False))
            and not live_schedules
            and bool(schedules)
        ),
        team_timer_no_live_tie_can_abandon=not PoolKnockoutTie.objects.filter(
            match=pool_match
        ).exists(),
        result_revisions=pool_match.result_revisions.select_related("revised_by").all(),
        team_audit_events=pool_match.team_match_audit_events.select_related("actor").all(),
        rack_summary=match_rack_summary(pool_match),
        non_played_form=PoolNonPlayedResultForm(match=pool_match),
        transition_form=PoolMatchTransitionForm(),
        correction_form=PoolResultCorrectionForm(),
        correction_is_open=bool(getattr(pool_match, "is_in_correction_mode", False)),
        result_is_locked=bool(getattr(pool_match, "result_is_locked", False)),
        up_next_rows=up_next_rows,
        can_manage_scorers=can_manage_scorers,
        can_open_live_scorer=can_open_live_scorer,
        scorer_assignments=scorer_assignments,
        scorer_form=scorer_form,
        can_expire_match=True,
    )
    context.update(extra)
    return context


def _competition_stage_or_404(competition, stage_id, *, stage_type=None):
    """Fetch a stage only through the already-authorised competition."""
    filters = {"pk": stage_id, "competition": competition}
    if stage_type is not None:
        filters["stage_type"] = stage_type
    return get_object_or_404(PoolCompetitionStage.objects.select_related("competition"), **filters)


def _group_stage_or_404(competition, stage_id):
    return _competition_stage_or_404(
        competition,
        stage_id,
        stage_type=PoolCompetitionStage.StageType.GROUP,
    )


def _group_or_404(competition, stage, group_id):
    return get_object_or_404(
        PoolGroup.objects.select_related("stage", "stage__competition"),
        pk=group_id,
        stage=stage,
        stage__competition=competition,
    )


def _group_is_referenced_by_generated_bracket(competition, group):
    return PoolKnockoutTieSlot.objects.filter(
        tie__bracket__competition=competition,
        source_group=group,
    ).exists()


def _knockout_stage_or_404(competition, stage_id):
    return _competition_stage_or_404(
        competition,
        stage_id,
        stage_type=PoolCompetitionStage.StageType.KNOCKOUT,
    )


def _competition_bracket_or_404(competition, stage_id):
    return get_object_or_404(
        PoolKnockoutBracket.objects.select_related("competition", "stage"),
        competition=competition,
        stage_id=stage_id,
    )


def _bracket_display_context(bracket):
    """Return an efficient, template-ready fallback graph for manager pages.

    The bracket service may supply a richer presentation object once available.
    Keeping this database fallback here lets the manager remain readable for
    historic brackets and avoids coupling templates to a service return type.
    """
    ties = list(
        bracket.ties.select_related(
            "match",
            "match__home_entry__player",
            "match__home_entry__team",
            "match__away_entry__player",
            "match__away_entry__team",
            "resolved_winner__player",
            "resolved_winner__team",
            "resolved_winner_life__competition_entry__player",
        )
        .prefetch_related(
            "slots__entry__player",
            "slots__entry__team",
            "slots__entry_life__competition_entry__player",
            "slots__source_entry__player",
            "slots__source_entry__team",
            "slots__source_entry_life__competition_entry__player",
            "slots__source_group",
            "slots__source_tie",
        )
        .order_by("round_number", "is_third_place", "position", "id")
    )
    def slot_label(slot):
        if slot is None:
            return "TBD"
        life = getattr(slot, "entry_life", None)
        if life is not None:
            return life.display_name
        if slot.entry_id:
            return slot.entry.display_name
        return slot.source_label or "TBD"

    rounds = []
    current_key = None
    current_round = None
    for tie in ties:
        key = (tie.round_number, tie.is_third_place, tie.round_name)
        if key != current_key:
            current_round = {
                "number": tie.round_number,
                "name": tie.round_name,
                "is_third_place": tie.is_third_place,
                "ties": [],
            }
            rounds.append(current_round)
            current_key = key
        slots = {slot.slot: slot for slot in tie.slots.all()}
        home_slot = slots.get(PoolKnockoutTieSlot.Slot.HOME)
        away_slot = slots.get(PoolKnockoutTieSlot.Slot.AWAY)
        large_draw = bracket.size >= 64
        first_round_half_tie_count = bracket.size // 4
        draw_half = ""
        half_heading = ""
        if large_draw and not tie.is_third_place:
            tie_count = bracket.size // (2**tie.round_number)
            if tie_count > 1:
                ties_per_half = tie_count // 2
                draw_half = (
                    "top" if tie.position <= ties_per_half else "bottom"
                )
                if tie.round_number == 1 and tie.position == 1:
                    half_heading = "Top half"
                elif (
                    tie.round_number == 1
                    and tie.position == first_round_half_tie_count + 1
                ):
                    half_heading = "Bottom half"
            else:
                draw_half = "final"
        current_round["ties"].append(
            {
                "tie": tie,
                "home_slot": home_slot,
                "away_slot": away_slot,
                "home_label": slot_label(home_slot),
                "away_label": slot_label(away_slot),
                "same_entry_final": tie.resolution_method == "SAME_ENTRY_FINAL",
                "champion_label": (
                    tie.resolved_winner.display_name
                    if tie.resolution_method == "SAME_ENTRY_FINAL"
                    and tie.resolved_winner_id
                    else ""
                ),
                "draw_half": draw_half,
                "half_heading": half_heading,
            }
        )
    third_place = next((tie for tie in ties if tie.is_third_place), None)
    same_entry_third_place = bool(
        third_place
        and getattr(third_place, "resolution_method", "")
        == "SAME_ENTRY_THIRD"
    )
    return {
        "rounds": rounds,
        "ties": ties,
        "large_draw": bracket.size >= 64,
        "first_round_half_tie_count": bracket.size // 4,
        "same_entry_third_place": same_entry_third_place,
        "third_place_label": (
            third_place.resolved_winner.display_name
            if same_entry_third_place and third_place.resolved_winner_id
            else ""
        ),
    }


def _team_entry_or_404(competition, entry_id):
    return get_object_or_404(
        PoolCompetitionEntry.objects.select_related("team", "competition"),
        pk=entry_id,
        competition=competition,
        team__isnull=False,
    )


def _require_team_competition(competition):
    if competition.participant_mode != PoolCompetition.ParticipantMode.TEAMS:
        raise PermissionDenied("Teams and rosters are only available in team competitions.")


def _require_singles_competition(competition):
    if competition.participant_mode != PoolCompetition.ParticipantMode.SINGLES:
        raise PermissionDenied("Player entries are only available in singles competitions.")


def _set_competition_entry_lives(entry, requested_lives):
    """Persist the selected bracket-life count for an eligible entry.

    This is called inside the entry transaction.  ``PoolCompetitionEntry``
    ensures Life 1 exists on a newly saved eligible entry; repeating that
    ``get_or_create`` here makes the entry workflow explicit and handles
    legacy rows consistently.  The life model itself protects preview/bracket
    dependencies when a later edit tries to add or remove Life 2.
    """

    try:
        requested_lives = int(requested_lives or 1)
    except (TypeError, ValueError) as error:
        raise ValidationError("Choose either one or two lives.") from error

    # Match ``create_draw_preview()``'s lock ordering: competition root first,
    # then the rows that describe its tournament lives.  Without this root
    # lock, an entry edit and a draw preview can both observe an old life set.
    competition = PoolCompetition.objects.select_for_update().get(
        pk=entry.competition_id
    )
    two_lives_allowed = (
        competition.allow_two_lives
        and competition.participant_mode == PoolCompetition.ParticipantMode.SINGLES
        and competition.format_type == PoolCompetition.FormatType.KNOCKOUT
        and entry.player_id
    )
    if not two_lives_allowed:
        if requested_lives != 1:
            raise ValidationError("Two lives are not available for this competition.")
        return
    if requested_lives not in {1, 2}:
        raise ValidationError("Choose either one or two lives.")

    # Recheck mutable topology only after acquiring the competition root lock.
    # The bracket service takes that same lock before reading life rows.
    if (
        PoolBracketDrawPreview.objects.filter(competition=competition).exists()
        or PoolKnockoutBracket.objects.filter(competition=competition).exists()
    ):
        raise ValidationError(
            "Reset the draw preview or bracket before changing tournament lives."
        )

    entry = PoolCompetitionEntry.objects.select_for_update().get(
        pk=entry.pk,
        competition=competition,
    )
    locked_lives = {
        life.life_number: life
        for life in PoolCompetitionEntryLife.objects.select_for_update().filter(
            competition_entry=entry,
        )
    }
    if 1 not in locked_lives:
        PoolCompetitionEntryLife.objects.create(
            competition_entry=entry,
            life_number=1,
        )
    second_life = locked_lives.get(2)
    if requested_lives == 2:
        if second_life is None:
            PoolCompetitionEntryLife.objects.create(
                competition_entry=entry,
                life_number=2,
            )
    elif second_life is not None:
        second_life.delete()


def _add_singles_player_entry(
    competition,
    player,
    *,
    requested_lives=1,
    seed=None,
    actor=None,
):
    """Use the normal entry workflow, or the sole safe post-draw exception.

    A confirmed direct knockout may accept a late player only by consuming a
    service-validated open first-round BYE.  Keep that exceptional decision
    here rather than weakening the normal entry model guard for manager forms.
    """

    if PoolKnockoutBracket.objects.filter(competition=competition).exists():
        if seed is not None:
            raise ValidationError(
                "Late BYE entries are unseeded so the confirmed bracket is not reordered."
            )
        return _bracket_services()["place_late_entry_in_open_bye"](
            competition,
            player=player,
            requested_lives=requested_lives,
            actor=actor,
        )
    entry = PoolCompetitionEntry.objects.create(
        competition=competition,
        player=player,
        seed=seed,
    )
    _set_competition_entry_lives(entry, requested_lives)
    return entry


def _require_owner(request, competition):
    if not user_is_pool_competition_owner(request.user, competition):
        raise PermissionDenied("Only the competition owner can manage administrators.")


def _require_team_metadata_scope(request, team):
    """Prevent an entry manager changing a globally shared team's metadata.

    Teams are intentionally reusable domain records, unlike a competition
    roster.  A manager who controls only one of a team's entries must not be
    able to alter its name, logo, or active state for another competition.
    """
    has_unmanaged_entry = PoolCompetitionEntry.objects.filter(team=team).exclude(
        competition__in=managed_pool_competitions(request.user)
    ).exists()
    if has_unmanaged_entry:
        raise PermissionDenied(
            "This shared team is also entered in a competition you do not manage."
        )


@login_required
def dashboard(request):
    """List competitions this user can manage, never merely public ones."""
    if not user_has_pool_management_access(request.user):
        raise PermissionDenied("You do not manage any pool competitions.")

    competitions = (
        managed_pool_competitions(request.user)
        .annotate(
            entry_count=Count("entries", distinct=True),
            match_count=Count("matches", distinct=True),
            live_match_count=Count(
                "matches",
                filter=Q(matches__status=PoolMatch.Status.LIVE),
                distinct=True,
            ),
        )
        .order_by("name", "id")
    )
    return render(
        request,
        "pool/manage/dashboard.html",
        {
            "managed_competitions": competitions,
            "competitions": competitions,
            "page_navigation_back_url": reverse("pool:home"),
            "page_navigation_show_pool_home": True,
            "pool_manager_base_template": "base.html",
        },
    )


@login_required
def money_match_create(request):
    """Create one manager-owned Money Match and its only fixture."""

    if not user_has_pool_management_access(request.user):
        raise PermissionDenied("You do not have Pool management access.")
    form = PoolMoneyMatchCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            competition, pool_match = create_money_match(
                opponent_one=form.cleaned_data["opponent_one"],
                opponent_two=form.cleaned_data["opponent_two"],
                race_target=form.cleaned_data["race_target"],
                scheduled_at=form.cleaned_data["scheduled_at"],
                venue=form.cleaned_data["venue"],
                title=form.cleaned_data["title"],
                owner=request.user,
                scorer=form.cleaned_data["scorer"],
                visibility=form.cleaned_data["visibility"],
                money_match_prize_pot=form.cleaned_data["money_match_prize_pot"],
            )
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, "Money Match created with its single fixture.")
            return redirect(
                "pool:manage_competition_dashboard",
                slug=competition.slug,
            )
    return render(
        request,
        "pool/manage/money_match_form.html",
        {
            "form": form,
            "page_navigation_back_url": reverse("pool:manage_dashboard"),
            "page_navigation_show_pool_home": True,
            "pool_manager_base_template": "base.html",
            "page_title": "Create Money Match",
        },
    )


@login_required
def money_match_edit(request, slug):
    """Configure or edit a Money Match without exposing the normal entries flow."""

    competition = _managed_competition_or_404(request, slug)
    if not competition.is_money_match:
        raise Http404("This competition is not a Money Match.")
    return _money_match_edit_response(request, competition)


def _money_match_edit_response(request, competition):
    """Render the reusable Money Match information form in its current shell."""

    pool_match = None
    configuration_error = None
    if competition.participant_mode != PoolCompetition.ParticipantMode.SINGLES:
        configuration_error = "Money Matches are available for singles players only."
    elif competition.entries.count() == 2:
        try:
            pool_match = ensure_money_match_fixture(competition)
        except (IntegrityError, ValidationError) as error:
            configuration_error = _validation_message(error)
    elif competition.entries.count() > 2:
        configuration_error = (
            "This Money Match has more than two entries. Remove the invalid extra "
            "entries before configuring opponents."
        )

    if pool_match is not None and pool_match.status != PoolMatch.Status.UPCOMING:
        messages.error(request, "Only an upcoming Money Match can be edited.")
        return redirect("pool:manage_competition_dashboard", slug=competition.slug)
    form = PoolMoneyMatchEditForm(
        request.POST or None,
        competition=competition,
        pool_match=pool_match,
    )
    if request.method == "POST" and not configuration_error and form.is_valid():
        try:
            competition, pool_match = configure_money_match(
                competition,
                opponent_one=form.cleaned_data["opponent_one"],
                opponent_two=form.cleaned_data["opponent_two"],
                race_target=form.cleaned_data["race_target"],
                scheduled_at=form.cleaned_data["scheduled_at"],
                venue=form.cleaned_data["venue"],
                title=form.cleaned_data["title"],
                scorer=form.cleaned_data["scorer"],
                visibility=form.cleaned_data["visibility"],
                assigned_by=request.user,
                money_match_prize_pot=form.cleaned_data["money_match_prize_pot"],
            )
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, "Money Match opponents and information were updated.")
            return redirect("pool:manage_competition_dashboard", slug=competition.slug)
    return render(
        request,
        "pool/manage/money_match_form.html",
        _manager_context(
            request,
            competition,
            form=form,
            page_title=("Edit Money Match" if pool_match else "Configure Money Match"),
            pool_match=pool_match,
            configuration_error=configuration_error,
        ),
    )


@login_required
def competition_dashboard(request, slug):
    competition = _managed_competition_or_404(request, slug)
    matches = competition.matches
    context = _manager_context(
        request,
        competition,
        entry_count=competition.entries.count(),
        match_count=matches.count(),
        upcoming_count=matches.filter(status=PoolMatch.Status.UPCOMING).count(),
        live_count=matches.filter(status=PoolMatch.Status.LIVE).count(),
        finished_count=matches.filter(status=PoolMatch.Status.FINISHED).count(),
        recent_matches=(
            matches.select_related(
                "stage",
                "knockout_round",
                "home_entry__player",
                "home_entry__team",
                "away_entry__player",
                "away_entry__team",
                "home_entry_life",
                "away_entry_life",
            )
            .order_by("-updated_at", "-id")[:8]
        ),
        can_archive_season=(
            request.user.is_superuser
            and not competition.is_archived
            and not archive_eligibility_errors(competition)
        ),
        archive_season_errors=(
            archive_eligibility_errors(competition)
            if request.user.is_superuser and not competition.is_archived
            else ()
        ),
    )
    return render(request, "pool/manage/competition_dashboard.html", context)


@login_required
@require_POST
def season_archive(request, slug):
    competition = _managed_competition_or_404(request, slug)
    try:
        archive_pool_competition(competition, actor=request.user)
    except PoolSeasonValidationError as error:
        messages.error(request, _validation_message(error))
    else:
        messages.success(request, "The pool league season was archived unchanged.")
    return redirect("pool:manage_competition_dashboard", slug=competition.slug)


@login_required
def season_next(request, slug):
    source = _managed_competition_or_404(request, slug)
    if not request.user.is_superuser:
        raise PermissionDenied("Only a superuser can create the next season.")

    form = PoolNextSeasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            competition = create_next_pool_season(
                source,
                season=form.cleaned_data["season"],
                slug=form.cleaned_data["slug"],
                actor=request.user,
            )
        except PoolSeasonValidationError as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, "A fresh pool league season was created.")
            return redirect(
                "pool:manage_competition_dashboard",
                slug=competition.slug,
            )
    return render(
        request,
        "pool/manage/season_form.html",
        _manager_context(request, source, form=form),
    )


@login_required
@require_GET
def competition_tab_content(request, slug, tab):
    """Serve one authorised manager source page as a cached tab fragment.

    The handler intentionally delegates to the established source views
    below.  That keeps each direct URL, permission check, form workflow, and
    heavy query in one place instead of creating a second manager API.
    """

    competition = _managed_competition_or_404(request, slug)
    available_tabs = {
        definition["key"] for definition in _manager_tab_definitions(request, competition)
    }
    tab_handlers = {
        "overview": competition_dashboard,
        "information": competition_information,
        "entries": competition_entries,
        "teams": competition_teams,
        "stages": competition_stages,
        "standings": competition_standings,
        "base_standings": competition_base_standings,
        "fixture_generator": competition_fixture_generator,
        "groups": competition_groups,
        "qualification": competition_qualifications,
        "rounds": competition_knockout_rounds,
        "bracket": competition_bracket,
        "fixtures": competition_fixtures,
        "administrators": competition_admins,
    }
    handler = tab_handlers.get(tab)
    if handler is None or tab not in available_tabs:
        raise Http404("Manager tab not found.")

    request._pool_manager_tab_partial = True
    request._pool_manager_source_tab = tab
    return handler(request, slug)


@login_required
def competition_information(request, slug):
    competition = _managed_competition_or_404(request, slug)
    if competition.is_money_match:
        # The lazy tab endpoint delegates here with the fragment base template.
        # Returning the same inner form instead of redirecting to the direct
        # full-page edit URL prevents the tab loader from nesting a manager
        # shell inside its existing manager shell.
        return _money_match_edit_response(request, competition)
    form = PoolCompetitionInfoForm(
        request.POST or None,
        request.FILES or None,
        instance=competition,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Competition information was updated.")
        return redirect("pool:manage_competition_information", slug=competition.slug)
    return render(
        request,
        "pool/manage/competition_form.html",
        _manager_context(request, competition, form=form),
    )


@login_required
def competition_entries(request, slug):
    competition = _managed_competition_or_404(request, slug)
    entries = (
        competition.entries.select_related("player", "team")
        .annotate(
            roster_count=Count("roster_members", distinct=True),
            life_count=Count("lives", distinct=True),
        )
        .order_by("seed", "id")
    )
    return render(
        request,
        "pool/manage/entry_list.html",
        _manager_context(request, competition, entries=entries),
    )


@login_required
def competition_entry_create(request, slug):
    competition = _managed_competition_or_404(request, slug)
    is_singles = (
        competition.participant_mode == PoolCompetition.ParticipantMode.SINGLES
    )
    form = (
        PoolExistingPlayerBulkEntryForm(request.POST or None, competition=competition)
        if is_singles
        else PoolCompetitionEntryForm(request.POST or None, competition=competition)
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                if is_singles:
                    player_ids = form.cleaned_data["player_ids"]
                    players = list(form.cleaned_data["players"])
                    lives_by_player_id = form.cleaned_data["lives_by_player_id"]
                    if PoolKnockoutBracket.objects.filter(
                        competition=competition
                    ).exists():
                        # The late-BYE service owns bracket -> competition
                        # locking.  Do not take the competition lock first in
                        # this wrapper or a simultaneous scoring transaction
                        # could observe reversed lock order.
                        for player in players:
                            _add_singles_player_entry(
                                competition,
                                player,
                                requested_lives=lives_by_player_id[player.pk],
                                actor=request.user,
                            )
                    else:
                        # Lock the same root that the draw lifecycle locks
                        # before rechecking every mutable condition. A
                        # submitted basket is never trusted merely because it
                        # passed form clean().
                        locked_competition = PoolCompetition.objects.select_for_update().get(
                            pk=competition.pk
                        )
                        if (
                            locked_competition.participant_mode
                            != PoolCompetition.ParticipantMode.SINGLES
                        ):
                            raise ValidationError(
                                "Existing players can only be added to a singles competition."
                            )

                        locked_players_by_id = {
                            player.pk: player
                            for player in PoolPlayer.objects.select_for_update().filter(
                                pk__in=player_ids
                            )
                        }
                        if len(locked_players_by_id) != len(player_ids):
                            raise ValidationError(
                                "One or more selected players no longer exist."
                            )
                        players = [locked_players_by_id[player_id] for player_id in player_ids]
                        if any(not player.is_active for player in players):
                            raise ValidationError(
                                "Only active players can be added to a competition."
                            )
                        if PoolCompetitionEntry.objects.filter(
                            competition=locked_competition,
                            player_id__in=player_ids,
                        ).exists():
                            raise ValidationError(
                                "One or more selected players are already entered in this competition."
                            )

                        two_lives_allowed = competition_allows_two_lives(
                            locked_competition
                        )
                        if any(
                            requested_lives != 1
                            for requested_lives in lives_by_player_id.values()
                        ) and not two_lives_allowed:
                            raise ValidationError(
                                "Two lives are not available for this competition."
                            )

                        # This manager preserves Life 1 for every singles
                        # entry, while the explicit Life 2 rows below retain
                        # the existing one-real-player/two-lives architecture.
                        PoolCompetitionEntry.objects.bulk_create(
                            [
                                PoolCompetitionEntry(
                                    competition=locked_competition,
                                    player=player,
                                )
                                for player in players
                            ]
                        )
                        if two_lives_allowed and any(
                            requested_lives == 2
                            for requested_lives in lives_by_player_id.values()
                        ):
                            entries_by_player_id = {
                                entry.player_id: entry
                                for entry in PoolCompetitionEntry.objects.select_related(
                                    "competition"
                                ).filter(
                                    competition=locked_competition,
                                    player_id__in=player_ids,
                                )
                            }
                            for player_id, requested_lives in lives_by_player_id.items():
                                if requested_lives == 2:
                                    PoolCompetitionEntryLife.objects.create(
                                        competition_entry=entries_by_player_id[player_id],
                                        life_number=2,
                                    )
                    added_count = len(players)
                else:
                    entry = form.save()
                    _set_competition_entry_lives(
                        entry,
                        form.cleaned_data.get("lives", 1),
                    )
                    added_count = 1
        except IntegrityError:
            form.add_error(
                None,
                "One or more selected participants are already entered in the competition.",
            )
        except ValidationError as error:
            form.add_error(None, _validation_message(error))
        else:
            if request.POST.get("save_and_add_another"):
                if is_singles:
                    messages.success(
                        request,
                        f"{added_count} {'player was' if added_count == 1 else 'players were'} added. You can add another.",
                    )
                else:
                    messages.success(request, "Entry added. You can add another.")
                return redirect(
                    "pool:manage_competition_entry_create",
                    slug=competition.slug,
                )
            if is_singles:
                messages.success(
                    request,
                    f"{added_count} {'player was' if added_count == 1 else 'players were'} added to the competition.",
                )
            else:
                messages.success(request, "Competition entry was added.")
            return redirect("pool:manage_competition_entries", slug=competition.slug)
    return render(
        request,
        "pool/manage/entry_form.html",
        _manager_context(
            request,
            competition,
            form=form,
            page_title="Add entry",
            player_picker=is_singles,
            player_search_url=(
                reverse(
                    "pool:manage_competition_entry_player_search",
                    args=[competition.slug],
                )
                if is_singles
                else None
            ),
            selected_players=[
                {
                    "id": player.pk,
                    "full_name": player.full_name,
                    "nickname": player.nickname,
                    "region": player.region,
                    "lives": form.lives_by_player_id.get(player.pk, 1),
                }
                for player in getattr(form, "selected_players", ())
            ],
            allows_two_lives=(
                competition_allows_two_lives(competition) if is_singles else False
            ),
            has_generated_knockout_bracket=(
                is_singles
                and PoolKnockoutBracket.objects.filter(competition=competition).exists()
            ),
            can_save_and_add_another=True,
            page_navigation_back_url=reverse(
                "pool:manage_competition_entries",
                args=[competition.slug],
            ),
        ),
    )


@login_required
@require_GET
def competition_entry_player_search(request, slug):
    """Return safe, bounded player-picker data for one managed competition."""

    competition = _managed_competition_or_404(request, slug)
    _require_singles_competition(competition)
    query = request.GET.get("q", "").strip()
    region = request.GET.get("region", "").strip()

    # A deliberately selected existing player remains valid through the
    # hidden ModelChoiceField when the query has been cleared.  Every search
    # response is bounded, even for a one-character surname fragment.
    if not query:
        return JsonResponse({"results": []})

    players = (
        PoolPlayer.objects.filter(is_active=True)
        .exclude(
            pk__in=competition.entries.filter(player__isnull=False).values("player_id")
        )
        .filter(Q(full_name__icontains=query) | Q(nickname__icontains=query))
    )
    if region:
        players = players.filter(region__icontains=region)

    results = [
        {
            "id": player.pk,
            "full_name": player.full_name,
            "nickname": player.nickname,
            "region": player.region,
        }
        for player in players.order_by("full_name", "id")[:25]
    ]
    return JsonResponse({"results": results})


@login_required
def competition_entry_register_player(request, slug):
    """Atomically add a new global player and their singles entry.

    Before creating a profile, a small set of likely duplicates is shown.
    The organiser can select one of those existing profiles or explicitly
    confirm that a genuinely separate person should be registered.
    """

    competition = _managed_competition_or_404(request, slug)
    _require_singles_competition(competition)
    form = PoolPlayerRegistrationForm(
        request.POST or None,
        request.FILES or None,
        competition=competition,
    )
    duplicate_suggestions = ()
    save_mode = request.POST.get("save_mode", "save")
    if save_mode not in {"save", "add_another"}:
        save_mode = "save"
    save_and_add_another = save_mode == "add_another"

    if request.method == "POST" and form.is_valid():
        duplicate_suggestions = tuple(form.potential_duplicate_players())
        requested_existing_id = request.POST.get("existing_player", "").strip()
        requested_action = request.POST.get("action", "")
        explicitly_confirmed_new_player = (
            requested_action == "save_as_new"
            or request.POST.get("confirm_create") in {"1", "true", "True", "on"}
        )

        if requested_existing_id and requested_action in {"", "use_existing"}:
            try:
                requested_existing_id = int(requested_existing_id)
            except (TypeError, ValueError):
                requested_existing_id = None
            selected_existing = next(
                (
                    player
                    for player in duplicate_suggestions
                    if player.pk == requested_existing_id
                ),
                None,
            )
            if selected_existing is None:
                form.add_error(
                    None,
                    "Choose one of the suggested existing players, or create the new profile explicitly.",
                )
            else:
                try:
                    with transaction.atomic():
                        entry = _add_singles_player_entry(
                            competition,
                            selected_existing,
                            requested_lives=form.cleaned_data.get("lives", 1),
                            seed=form.cleaned_data["seed"],
                            actor=request.user,
                        )
                except (IntegrityError, ValidationError) as error:
                    form.add_error(None, _validation_message(error))
                else:
                    messages.success(
                        request,
                        f"{selected_existing.full_name} was added to this competition.",
                    )
                    if save_and_add_another:
                        messages.success(request, "You can register another player.")
                        return redirect(
                            "pool:manage_competition_entry_register_player",
                            slug=competition.slug,
                        )
                    return redirect(
                        "pool:manage_competition_entries",
                        slug=competition.slug,
                    )
        elif duplicate_suggestions and not explicitly_confirmed_new_player:
            form.add_error(
                None,
                "Possible existing players were found. Select one, or confirm that you want to create a new profile.",
            )
        else:
            try:
                with transaction.atomic():
                    player = form.save()
                    entry = _add_singles_player_entry(
                        competition,
                        player,
                        requested_lives=form.cleaned_data.get("lives", 1),
                        seed=form.cleaned_data["seed"],
                        actor=request.user,
                    )
            except (IntegrityError, ValidationError) as error:
                form.add_error(None, _validation_message(error))
            else:
                messages.success(
                    request,
                    f"{player.full_name} was registered and added to this competition.",
                )
                if save_and_add_another:
                    messages.success(request, "You can register another player.")
                    return redirect(
                        "pool:manage_competition_entry_register_player",
                        slug=competition.slug,
                    )
                return redirect(
                    "pool:manage_competition_entries",
                    slug=competition.slug,
                )

    return render(
        request,
        "pool/manage/player_registration_form.html",
        _manager_context(
            request,
            competition,
            form=form,
            duplicate_suggestions=duplicate_suggestions,
            save_mode=save_mode,
            has_generated_knockout_bracket=PoolKnockoutBracket.objects.filter(
                competition=competition
            ).exists(),
            page_navigation_back_url=reverse(
                "pool:manage_competition_entries",
                args=[competition.slug],
            ),
        ),
    )


@login_required
def competition_entry_update(request, slug, entry_id):
    competition = _managed_competition_or_404(request, slug)
    entry = get_object_or_404(
        PoolCompetitionEntry.objects.select_related("player", "team"),
        pk=entry_id,
        competition=competition,
    )
    form = PoolCompetitionEntrySeedForm(
        request.POST or None,
        instance=entry,
        competition=competition,
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                entry = form.save()
                _set_competition_entry_lives(
                    entry,
                    form.cleaned_data.get("lives", 1),
                )
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, "Entry settings were updated.")
            return redirect("pool:manage_competition_entries", slug=competition.slug)
    return render(
        request,
        "pool/manage/entry_form.html",
        _manager_context(
            request,
            competition,
            entry=entry,
            form=form,
            page_title=f"Edit {entry.display_name}",
            page_navigation_back_url=reverse(
                "pool:manage_competition_entries",
                args=[competition.slug],
            ),
        ),
    )


@login_required
@require_POST
def competition_entry_delete(request, slug, entry_id):
    competition = _managed_competition_or_404(request, slug)
    entry = get_object_or_404(PoolCompetitionEntry, pk=entry_id, competition=competition)
    try:
        entry.delete()
    except (ProtectedError, RestrictedError):
        messages.error(
            request,
            "This entry cannot be removed because it is used by one or more fixtures.",
        )
    except ValidationError as error:
        messages.error(request, _validation_message(error))
    else:
        messages.success(request, "Competition entry was removed.")
    return redirect("pool:manage_competition_entries", slug=competition.slug)


@login_required
def competition_teams(request, slug):
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    team_entries = (
        competition.entries.filter(team__isnull=False)
        .select_related("team")
        .annotate(roster_count=Count("roster_members", distinct=True))
        .order_by("team__name", "id")
    )
    return render(
        request,
        "pool/manage/team_list.html",
        _manager_context(request, competition, team_entries=team_entries),
    )


@login_required
def competition_team_create(request, slug):
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    form = PoolTeamForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                team = form.save()
                PoolCompetitionEntry.objects.create(competition=competition, team=team)
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            if request.POST.get("save_and_add_another"):
                messages.success(request, "Team and competition entry were created. You can add another.")
                return redirect(
                    "pool:manage_competition_team_create",
                    slug=competition.slug,
                )
            messages.success(request, "Team and competition entry were created.")
            return redirect("pool:manage_competition_teams", slug=competition.slug)
    return render(
        request,
        "pool/manage/team_form.html",
        _manager_context(
            request,
            competition,
            form=form,
            page_title="Add team",
            can_save_and_add_another=True,
        ),
    )


@login_required
def competition_team_update(request, slug, entry_id):
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    _require_team_metadata_scope(request, entry.team)
    form = PoolTeamForm(request.POST or None, request.FILES or None, instance=entry.team)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Team information was updated.")
        return redirect("pool:manage_competition_teams", slug=competition.slug)
    return render(
        request,
        "pool/manage/team_form.html",
        _manager_context(
            request,
            competition,
            entry=entry,
            team=entry.team,
            form=form,
            page_title=f"Edit {entry.team.name}",
            can_save_and_add_another=False,
        ),
    )


@login_required
def competition_team_roster(request, slug, entry_id):
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    roster_memberships = entry.roster_members.select_related("player").order_by(
        "sort_order", "player__full_name", "id"
    )
    return render(
        request,
        "pool/manage/roster_list.html",
        _manager_context(
            request,
            competition,
            entry=entry,
            team=entry.team,
            roster_memberships=roster_memberships,
        ),
    )


def _add_players_to_team_roster(competition, entry, player_ids):
    """Atomically add active global players to one competition team roster."""
    requested_ids = list(player_ids)
    if not requested_ids or len(set(requested_ids)) != len(requested_ids):
        raise ValidationError("Select each player only once.")

    locked_competition = PoolCompetition.objects.select_for_update().get(
        pk=competition.pk
    )
    locked_entry = PoolCompetitionEntry.objects.select_for_update().get(
        pk=entry.pk,
        competition=locked_competition,
        team__isnull=False,
    )
    players_by_id = {
        player.pk: player
        for player in PoolPlayer.objects.select_for_update()
        .filter(pk__in=requested_ids)
        .order_by("pk")
    }
    if len(players_by_id) != len(requested_ids):
        raise ValidationError("One or more selected players no longer exist.")
    selected_players = [players_by_id[player_id] for player_id in requested_ids]
    if any(not player.is_active for player in selected_players):
        raise ValidationError("Only active players can join a competition roster.")

    # PostgreSQL cannot lock the nullable side of an outer join.  Lock only
    # roster rows here; related names for validation are loaded afterwards.
    existing_memberships = list(
        PoolTeamRoster._base_manager.select_for_update()
        .filter(
            competition_entry__competition=locked_competition,
            player_id__in=requested_ids,
        )
        .order_by("pk")
    )
    if existing_memberships:
        existing = existing_memberships[0]
        player = players_by_id[existing.player_id]
        if existing.competition_entry_id == locked_entry.pk:
            raise ValidationError(
                f"{player.full_name} is already on this competition roster."
            )
        conflicting_entry = PoolCompetitionEntry.objects.select_related("team").get(
            pk=existing.competition_entry_id
        )
        raise ValidationError(
            f"{player.full_name} is already registered for "
            f"{conflicting_entry.team.name} in this competition."
        )

    current_maximum = PoolTeamRoster._base_manager.filter(
        competition_entry=locked_entry
    ).aggregate(maximum=Max("sort_order"))["maximum"]
    next_sort_order = 0 if current_maximum is None else current_maximum + 1
    for offset, player in enumerate(selected_players):
        PoolTeamRoster(
            competition_entry=locked_entry,
            player=player,
            is_active=True,
            sort_order=next_sort_order + offset,
        ).save()
    return selected_players


@login_required
def competition_team_roster_add(request, slug, entry_id):
    """Add a validated basket of active players to one competition roster."""

    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    form = PoolTeamRosterBulkForm(request.POST or None, competition_entry=entry)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                _add_players_to_team_roster(
                    competition,
                    entry,
                    form.cleaned_data["player_ids"],
                )
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            if request.POST.get("save_and_add_another"):
                messages.success(
                    request,
                    (
                        f"{len(form.cleaned_data['player_ids'])} "
                        f"{'player was' if len(form.cleaned_data['player_ids']) == 1 else 'players were'} added. You can add another."
                    ),
                )
                return redirect(
                    "pool:manage_competition_team_roster_add",
                    slug=competition.slug,
                    entry_id=entry.pk,
                )
            added_count = len(form.cleaned_data["player_ids"])
            messages.success(
                request,
                f"{added_count} {'player was' if added_count == 1 else 'players were'} added to the competition roster.",
            )
            return redirect(
                "pool:manage_competition_team_roster",
                slug=competition.slug,
                entry_id=entry.pk,
            )
    return render(
        request,
        "pool/manage/roster_bulk_form.html",
        _manager_context(
            request,
            competition,
            entry=entry,
            team=entry.team,
            form=form,
            page_title="Add roster player",
            can_save_and_add_another=True,
            page_navigation_back_url=reverse(
                "pool:manage_competition_team_roster",
                args=[competition.slug, entry.pk],
            ),
            player_search_url=reverse(
                "pool:manage_competition_team_roster_player_search",
                args=[competition.slug, entry.pk],
            ),
            selected_players=[
                {
                    "id": player.pk,
                    "full_name": player.full_name,
                    "nickname": player.nickname,
                    "region": player.region,
                }
                for player in getattr(form, "selected_players", ())
            ],
        ),
    )


@login_required
def competition_team_roster_register_player(request, slug, entry_id):
    """Register or select a global player, then add them to this team roster."""
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    form = PoolTeamRosterPlayerRegistrationForm(
        request.POST or None,
        request.FILES or None,
        competition=competition,
    )
    duplicate_suggestions = ()
    save_mode = request.POST.get("save_mode", "save")
    if save_mode not in {"save", "add_another"}:
        save_mode = "save"

    if request.method == "POST" and form.is_valid():
        duplicate_suggestions = tuple(form.potential_duplicate_players())
        requested_existing_id = request.POST.get("existing_player", "").strip()
        requested_action = request.POST.get("action", "")
        explicitly_confirmed_new_player = (
            requested_action == "save_as_new"
            or request.POST.get("confirm_create") in {"1", "true", "True", "on"}
        )
        selected_existing = None
        if requested_existing_id and requested_action in {"", "use_existing"}:
            try:
                requested_existing_id = int(requested_existing_id)
            except (TypeError, ValueError):
                requested_existing_id = None
            selected_existing = next(
                (
                    player
                    for player in duplicate_suggestions
                    if player.pk == requested_existing_id
                ),
                None,
            )
            if selected_existing is None:
                form.add_error(
                    None,
                    "Choose one of the suggested existing players, or create the new profile explicitly.",
                )
        elif duplicate_suggestions and not explicitly_confirmed_new_player:
            form.add_error(
                None,
                "Possible existing players were found. Select one, or confirm that you want to create a new profile.",
            )

        if not form.errors:
            try:
                with transaction.atomic():
                    player = selected_existing or form.save()
                    _add_players_to_team_roster(competition, entry, [player.pk])
            except (IntegrityError, ValidationError) as error:
                form.add_error(None, _validation_message(error))
            else:
                if selected_existing:
                    messages.success(
                        request,
                        f"{player.full_name} was added to {entry.team.name}.",
                    )
                else:
                    messages.success(
                        request,
                        f"{player.full_name} was registered and added to {entry.team.name}.",
                    )
                if save_mode == "add_another":
                    messages.success(request, "You can register another player.")
                    return redirect(
                        "pool:manage_competition_team_roster_register_player",
                        slug=competition.slug,
                        entry_id=entry.pk,
                    )
                return redirect(
                    "pool:manage_competition_team_roster",
                    slug=competition.slug,
                    entry_id=entry.pk,
                )

    return render(
        request,
        "pool/manage/team_roster_player_registration_form.html",
        _manager_context(
            request,
            competition,
            entry=entry,
            team=entry.team,
            form=form,
            duplicate_suggestions=duplicate_suggestions,
            save_mode=save_mode,
            page_navigation_back_url=reverse(
                "pool:manage_competition_team_roster",
                args=[competition.slug, entry.pk],
            ),
        ),
    )


@login_required
@require_GET
def competition_team_roster_player_search(request, slug, entry_id):
    """Return bounded, non-contact player picker data for one team roster."""

    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    query = request.GET.get("q", "").strip()
    region = request.GET.get("region", "").strip()
    if not query:
        return JsonResponse({"results": []})
    players = PoolPlayer.objects.filter(is_active=True).filter(
        Q(full_name__icontains=query) | Q(nickname__icontains=query)
    )
    if region:
        players = players.filter(region__icontains=region)
    players = list(players.order_by("full_name", "id")[:25])
    memberships = {
        membership.player_id: membership
        for membership in PoolTeamRoster._base_manager.filter(
            competition_entry__competition=competition,
            player_id__in=[player.pk for player in players],
        ).select_related("competition_entry__team")
    }
    results = []
    for player in players:
        membership = memberships.get(player.pk)
        unavailable = membership is not None
        if membership is None:
            unavailable_reason = ""
        elif membership.competition_entry_id == entry.pk:
            unavailable_reason = "Already on this team roster"
        else:
            unavailable_reason = (
                f"Already registered for {membership.competition_entry.team.name}"
            )
        results.append(
            {
                "id": player.pk,
                "full_name": player.full_name,
                "nickname": player.nickname,
                "region": player.region,
                "unavailable": unavailable,
                "unavailable_reason": unavailable_reason,
            }
        )
    return JsonResponse({"results": results})


@login_required
def competition_team_roster_update(request, slug, entry_id, roster_id):
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    roster = get_object_or_404(
        PoolTeamRoster.objects.select_related("player"),
        pk=roster_id,
        competition_entry=entry,
    )
    form = PoolTeamRosterUpdateForm(request.POST or None, instance=roster)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Roster membership was updated.")
        return redirect(
            "pool:manage_competition_team_roster",
            slug=competition.slug,
            entry_id=entry.pk,
        )
    return render(
        request,
        "pool/manage/roster_form.html",
        _manager_context(
            request,
            competition,
            entry=entry,
            team=entry.team,
            roster=roster,
            form=form,
            page_title=f"Edit {roster.player.full_name}",
        ),
    )


@login_required
@require_POST
def competition_team_roster_delete(request, slug, entry_id, roster_id):
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    roster = get_object_or_404(PoolTeamRoster, pk=roster_id, competition_entry=entry)
    roster.delete()
    messages.success(request, "Player was removed from the competition roster.")
    return redirect(
        "pool:manage_competition_team_roster",
        slug=competition.slug,
        entry_id=entry.pk,
    )


@login_required
def competition_team_staff(request, slug, entry_id):
    """Manage coaches/captains scoped to one competition team entry only."""

    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    assignments = (
        entry.team_staff_assignments.select_related("user")
        .order_by("-is_active", "user__username", "id")
    )
    return render(
        request,
        "pool/manage/team_staff_list.html",
        _manager_context(
            request,
            competition,
            entry=entry,
            assignments=assignments,
            form=PoolCompetitionTeamStaffForm(competition_entry=entry),
        ),
    )


@login_required
@require_POST
def competition_team_staff_add(request, slug, entry_id):
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    form = PoolCompetitionTeamStaffForm(
        request.POST,
        competition_entry=entry,
    )
    if form.is_valid():
        try:
            assignment = form.save()
        except IntegrityError:
            form.add_error(
                None,
                "This staff assignment could not be saved. Please try again.",
            )
        else:
            messages.success(
                request,
                f"{assignment.user.get_username()} is now a {assignment.get_role_display().lower()} for {entry.display_name}.",
            )
            return redirect(
                "pool:manage_competition_team_staff",
                slug=competition.slug,
                entry_id=entry.pk,
            )

    assignments = (
        entry.team_staff_assignments.select_related("user")
        .order_by("-is_active", "user__username", "id")
    )
    return render(
        request,
        "pool/manage/team_staff_list.html",
        _manager_context(
            request,
            competition,
            entry=entry,
            assignments=assignments,
            form=form,
        ),
    )


@login_required
@require_POST
def competition_team_staff_toggle(request, slug, entry_id, assignment_id):
    competition = _managed_competition_or_404(request, slug)
    _require_team_competition(competition)
    entry = _team_entry_or_404(competition, entry_id)
    assignment = get_object_or_404(
        PoolCompetitionTeamStaffAssignment.objects.select_related("user"),
        pk=assignment_id,
        competition_entry=entry,
    )
    assignment.is_active = not assignment.is_active
    assignment.save()
    messages.success(
        request,
        f"{assignment.user.get_username()} is now {'active' if assignment.is_active else 'inactive'} for this team entry.",
    )
    return redirect(
        "pool:manage_competition_team_staff",
        slug=competition.slug,
        entry_id=entry.pk,
    )


@login_required
def competition_stages(request, slug):
    competition = _managed_competition_or_404(request, slug)
    stages = competition.stages.annotate(match_count=Count("matches", distinct=True)).order_by(
        "order", "id"
    )
    return render(
        request,
        "pool/manage/stage_list.html",
        _manager_context(request, competition, stages=stages),
    )


@login_required
def competition_stage_create(request, slug):
    competition = _managed_competition_or_404(request, slug)
    form = PoolCompetitionStageForm(request.POST or None, competition=competition)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except IntegrityError:
            form.add_error(None, "Stage names and orders must be unique in this competition.")
        else:
            messages.success(request, "Stage was added.")
            return redirect("pool:manage_competition_stages", slug=competition.slug)
    return render(
        request,
        "pool/manage/stage_form.html",
        _manager_context(request, competition, form=form, page_title="Add stage"),
    )


@login_required
def competition_stage_update(request, slug, stage_id):
    competition = _managed_competition_or_404(request, slug)
    stage = get_object_or_404(PoolCompetitionStage, pk=stage_id, competition=competition)
    form = PoolCompetitionStageForm(
        request.POST or None,
        competition=competition,
        instance=stage,
    )
    if request.method == "POST" and form.is_valid():
        if stage.matches.exists() and {"stage_type", "order"}.intersection(form.changed_data):
            form.add_error(
                None,
                "A stage with fixtures cannot have its type or order changed.",
            )
        else:
            try:
                form.save()
            except IntegrityError:
                form.add_error(None, "Stage names and orders must be unique in this competition.")
            else:
                messages.success(request, "Stage was updated.")
                return redirect("pool:manage_competition_stages", slug=competition.slug)
    return render(
        request,
        "pool/manage/stage_form.html",
        _manager_context(
            request,
            competition,
            stage=stage,
            form=form,
            page_title=f"Edit {stage.name}",
        ),
    )


@login_required
@require_POST
def competition_stage_delete(request, slug, stage_id):
    competition = _managed_competition_or_404(request, slug)
    stage = get_object_or_404(PoolCompetitionStage, pk=stage_id, competition=competition)
    try:
        stage.delete()
    except (ProtectedError, RestrictedError):
        messages.error(request, "This stage cannot be removed because it has fixtures.")
    except ValidationError as error:
        messages.error(request, _validation_message(error))
    else:
        messages.success(request, "Stage was removed.")
    return redirect("pool:manage_competition_stages", slug=competition.slug)


@login_required
def competition_knockout_rounds(request, slug):
    """Configure independent, canonical manual-round metadata for a stage."""

    competition = _managed_competition_or_404(request, slug)
    knockout_stages = list(
        competition.stages.filter(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        ).order_by("order", "id")
    )
    if not knockout_stages:
        raise Http404("This competition does not have a knockout stage.")

    requested_stage_id = (
        request.POST.get("stage") if request.method == "POST" else request.GET.get("stage")
    )
    if requested_stage_id:
        selected_stage = _knockout_stage_or_404(competition, requested_stage_id)
    else:
        selected_stage = knockout_stages[0]

    form = PoolKnockoutRoundConfigurationForm(
        request.POST or None,
        competition=competition,
    )
    if request.method == "POST" and form.is_valid():
        try:
            definitions = configure_knockout_rounds(
                selected_stage,
                form.cleaned_data["competitor_count"],
                actor=request.user,
            )
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(
                request,
                "Knockout rounds configured: "
                + ", ".join(definition.name for definition in definitions),
            )
            return redirect(
                f"{reverse('pool:manage_competition_knockout_rounds', args=[competition.slug])}?stage={selected_stage.pk}"
            )

    round_definitions = PoolKnockoutRoundDefinition.objects.filter(
        stage=selected_stage
    ).order_by("order", "id")
    return render(
        request,
        "pool/manage/knockout_rounds.html",
        _manager_context(
            request,
            competition,
            knockout_stages=knockout_stages,
            selected_stage=selected_stage,
            round_definitions=round_definitions,
            form=form,
            page_navigation_back_url=reverse(
                "pool:manage_competition_dashboard",
                args=[competition.slug],
            ),
        ),
    )


@login_required
def competition_standings(request, slug):
    """Configure table points/tie-breakers and inspect scoped derived tables."""
    competition = _managed_competition_or_404(request, slug)
    if not competition.has_league_or_group_stage:
        raise Http404("This knockout-only competition does not have standings settings.")

    requested_points_mode = (
        request.POST.get("points_mode")
        if request.method == "POST" and request.POST.get("action") == "points"
        else request.GET.get("points_mode")
    )
    if requested_points_mode not in PoolCompetition.PointsMode.values:
        requested_points_mode = competition.points_mode
    display_points_mode = requested_points_mode

    maximum_margin = maximum_meaningful_pool_margin(competition)
    base_margin_initial = margin_point_threshold_initial(competition)
    requested_rule_count = request.GET.get("margin_rule_count")
    if requested_rule_count is None:
        requested_rule_count = len(base_margin_initial)
        # A normal link works without JavaScript.  It adds one deterministic
        # threshold card within the finite range ending at the applicable
        # table race target; the saved policy is never mutated by this GET.
        if request.GET.get("add_margin_rule"):
            requested_rule_count += 1
    margin_initial = margin_point_threshold_initial(
        competition,
        requested_rule_count=requested_rule_count,
    )
    standings_form = PoolCompetitionStandingsForm(
        instance=competition,
        display_mode=display_points_mode,
    )
    margin_rule_formset = PoolMarginPointThresholdFormSet(
        competition=competition,
        initial=margin_initial,
        prefix="margin_rules",
    )
    tie_breaker_form = PoolCompetitionTieBreakerForm(competition=competition)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "points":
            # Keep an invalid submitted margin editor visible with its field
            # errors instead of falling back to the saved fixed-mode view.
            posted_points_mode = request.POST.get("points_mode")
            if posted_points_mode in PoolCompetition.PointsMode.values:
                display_points_mode = posted_points_mode
            standings_form = PoolCompetitionStandingsForm(
                request.POST,
                instance=competition,
                display_mode=display_points_mode,
            )
            formset_is_bound = "margin_rules-TOTAL_FORMS" in request.POST
            if formset_is_bound:
                margin_rule_formset = PoolMarginPointThresholdFormSet(
                    request.POST,
                    competition=competition,
                    prefix="margin_rules",
                )
            form_is_valid = standings_form.is_valid()
            mode_requires_cards = (
                form_is_valid
                and standings_form.cleaned_data["points_mode"]
                == PoolCompetition.PointsMode.MARGIN_BASED
            )
            formset_is_valid = formset_is_bound and margin_rule_formset.is_valid()
            if mode_requires_cards and not formset_is_bound:
                standings_form.add_error(
                    None,
                    "Complete the played-match points rules before saving margin-based points.",
                )
                formset_is_valid = False
            if form_is_valid and (not mode_requires_cards or formset_is_valid):
                try:
                    if mode_requires_cards:
                        margin_rules = margin_rule_formset.derived_rules
                    else:
                        # Switching temporarily to fixed points must not erase
                        # a valid saved margin policy.  An imported malformed
                        # policy is cleared by this explicit repair save.
                        try:
                            margin_rules = configured_pool_margin_point_rules(
                                competition,
                                require_complete=True,
                            )
                        except ValidationError:
                            margin_rules = ()
                    replace_pool_standings_points_configuration(
                        competition,
                        points_mode=standings_form.cleaned_data["points_mode"],
                        points_for_win=standings_form.cleaned_data["points_for_win"],
                        points_for_loss=standings_form.cleaned_data["points_for_loss"],
                        margin_rules=margin_rules,
                        # Only the fixed-mode preservation path can carry a
                        # pre-bounded-release open-ended row.  Threshold-card
                        # submissions are always finite and must never use
                        # this compatibility escape hatch.
                        allow_legacy_open_ended=not mode_requires_cards,
                    )
                except ValidationError as error:
                    standings_form.add_error(None, _validation_message(error))
                else:
                    messages.success(request, "Standings points settings were updated.")
                    return redirect("pool:manage_competition_standings", slug=competition.slug)
            elif mode_requires_cards:
                messages.error(request, "Please correct the played-match points rules.")
        elif action == "tie_breakers":
            tie_breaker_form = PoolCompetitionTieBreakerForm(
                request.POST,
                competition=competition,
            )
            if tie_breaker_form.is_valid():
                try:
                    replace_pool_tie_breakers(
                        competition,
                        tie_breaker_form.cleaned_data["criteria"],
                    )
                except ValidationError as error:
                    tie_breaker_form.add_error(None, _validation_message(error))
                else:
                    messages.success(request, "Standings tie-breakers were updated.")
                    return redirect("pool:manage_competition_standings", slug=competition.slug)
        else:
            messages.error(request, "That standings action is not available.")

    league_stages = list(
        competition.stages.filter(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        ).order_by("order", "id")
    )
    group_stages = list(
        competition.stages.filter(
            stage_type=PoolCompetitionStage.StageType.GROUP
        )
        .prefetch_related("groups")
        .order_by("order", "id")
    )
    requested_stage_id = request.GET.get("stage")
    selected_stage = None
    standings_rows = []
    standings_error = None
    standings_stage_choices = []
    for stage in league_stages:
        standings_stage_choices.append({"stage": stage, "group": None, "label": stage.name})
    for stage in group_stages:
        for group in stage.groups.all():
            standings_stage_choices.append(
                {"stage": stage, "group": group, "label": f"{stage.name}: {group.name}"}
            )

    if requested_stage_id:
        selected_stage = get_object_or_404(
            competition.stages.filter(
                stage_type__in=(
                    PoolCompetitionStage.StageType.LEAGUE,
                    PoolCompetitionStage.StageType.GROUP,
                )
            ),
            pk=requested_stage_id,
        )
    elif league_stages:
        selected_stage = league_stages[0]
    elif group_stages:
        selected_stage = group_stages[0]

    selected_group = None
    if selected_stage:
        if selected_stage.stage_type == PoolCompetitionStage.StageType.GROUP:
            requested_group_id = request.GET.get("group")
            groups = selected_stage.groups.order_by("order", "id")
            if requested_group_id:
                selected_group = get_object_or_404(groups, pk=requested_group_id)
            else:
                selected_group = groups.first()
            if selected_group:
                try:
                    standings_rows = standings_for_stage(
                        selected_stage,
                        group=selected_group,
                    )
                except ValidationError:
                    standings_error = (
                        "The active points policy is incomplete or invalid. "
                        "Repair and save the full points configuration above "
                        "before calculated standings can be shown."
                    )
        else:
            try:
                standings_rows = standings_for_stage(selected_stage)
            except ValidationError:
                standings_error = (
                    "The active points policy is incomplete or invalid. Repair "
                    "and save the full points configuration above before "
                    "calculated standings can be shown."
                )

    for choice in standings_stage_choices:
        choice["is_selected"] = (
            selected_stage is not None
            and choice["stage"].pk == selected_stage.pk
            and (
                (choice["group"] is None and selected_group is None)
                or (
                    choice["group"] is not None
                    and selected_group is not None
                    and choice["group"].pk == selected_group.pk
                )
            )
        )

    margin_allocation_preview = []
    if display_points_mode == PoolCompetition.PointsMode.MARGIN_BASED:
        try:
            if request.method == "POST" and request.POST.get("action") == "points" and margin_rule_formset.is_bound:
                # ``derived_rules`` is assigned only after the formset has
                # passed all server-side threshold validation.  A failed post
                # keeps field errors visible without pretending its values are
                # an effective points policy.
                preview_rules = margin_rule_formset.derived_rules
            else:
                preview_rules = derive_pool_margin_point_rules_from_thresholds(
                    competition,
                    margin_initial,
                )
            margin_allocation_preview = _margin_allocation_preview(
                preview_rules,
                maximum_margin=maximum_margin,
            )
        except ValidationError:
            # A corrupted historic policy remains repairable through the card
            # editor; calculated tables already expose the same safe warning.
            margin_allocation_preview = []

    baseline_head_to_head_warning = bool(
        selected_stage
        and scope_has_baselines(selected_stage, group=selected_group)
        and "HEAD_TO_HEAD" in configured_pool_tie_breakers(competition)
    )

    return render(
        request,
        "pool/manage/standings.html",
        _manager_context(
            request,
            competition,
            standings_form=standings_form,
            margin_rule_formset=margin_rule_formset,
            margin_rule_count=margin_rule_formset.total_form_count(),
            maximum_margin=maximum_margin,
            margin_allocation_preview=margin_allocation_preview,
            display_points_mode=display_points_mode,
            tie_breaker_form=tie_breaker_form,
            standings_rows=standings_rows,
            standings_error=standings_error,
            standings_stage_choices=standings_stage_choices,
            selected_stage=selected_stage,
            selected_group=selected_group,
            group_stages=group_stages,
            baseline_head_to_head_warning=baseline_head_to_head_warning,
        ),
    )


def _default_table_scope(competition):
    """Return the first configured league/group table without guessing a group."""

    stages = list(
        competition.stages.filter(
            stage_type__in=(
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            )
        ).order_by("order", "id")
    )
    if not stages:
        return None, None
    stage = stages[0]
    if stage.stage_type == PoolCompetitionStage.StageType.GROUP:
        return stage, stage.groups.order_by("order", "id").first()
    return stage, None


def _table_scope_query(stage, group=None):
    """Build a small safe redirect query for one manager table scope."""

    if not stage:
        return ""
    value = f"?stage={stage.pk}"
    if group is not None:
        value += f"&group={group.pk}"
    return value


@login_required
def competition_base_standings(request, slug):
    """Bulk-edit imported aggregate standings; superusers only."""

    competition = _managed_competition_or_404(request, slug)
    if not request.user.is_superuser:
        raise PermissionDenied("Only a superuser can edit imported base standings.")
    if not competition.has_league_or_group_stage:
        raise Http404("This competition does not have a league or group table.")

    scope_data = request.POST if request.method == "POST" else request.GET
    scope_form = PoolLeagueFixtureScopeForm(
        scope_data or None,
        competition=competition,
    )
    selected_stage = selected_group = None
    if scope_data:
        if scope_form.is_valid():
            selected_stage = scope_form.cleaned_data["stage"]
            selected_group = scope_form.cleaned_data["group"]
    else:
        selected_stage, selected_group = _default_table_scope(competition)
        scope_form = PoolLeagueFixtureScopeForm(
            initial={
                "stage": getattr(selected_stage, "pk", None),
                "group": getattr(selected_group, "pk", None),
            },
            competition=competition,
        )

    entries = []
    baseline_initial = []
    if selected_stage is not None:
        entries = baseline_entries_for_scope(selected_stage, group=selected_group)
        baselines = baseline_values_by_entry(
            selected_stage,
            group=selected_group,
            entry_ids=[entry.pk for entry in entries],
        )
        baseline_initial = [
            {
                "entry": entry.pk,
                **{
                    field: getattr(baselines.get(entry.pk), field, 0)
                    for field in BASELINE_FIELDS
                },
            }
            for entry in entries
        ]

    baseline_formset = PoolStandingBaselineFormSet(
        request.POST if request.method == "POST" else None,
        entries=entries,
        initial=baseline_initial,
        prefix="baseline",
    )
    if request.method == "POST" and scope_form.is_valid() and baseline_formset.is_valid():
        try:
            result = save_pool_standing_baselines(
                selected_stage,
                [form.cleaned_data for form in baseline_formset.forms],
                group=selected_group,
                actor=request.user,
            )
        except ValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(
                request,
                (
                    "Base standings were saved "
                    f"({result['created']} added, {result['updated']} updated, "
                    f"{result['deleted']} cleared)."
                ),
            )
            return redirect(
                reverse("pool:manage_competition_base_standings", args=[competition.slug])
                + _table_scope_query(selected_stage, selected_group)
            )

    baseline_rows = [
        {"entry": entry, "form": form}
        for entry, form in zip(entries, baseline_formset.forms)
    ]
    return render(
        request,
        "pool/manage/base_standings.html",
        _manager_context(
            request,
            competition,
            scope_form=scope_form,
            baseline_formset=baseline_formset,
            baseline_rows=baseline_rows,
            selected_stage=selected_stage,
            selected_group=selected_group,
            baseline_scope_has_values=(
                scope_has_baselines(selected_stage, group=selected_group)
                if selected_stage is not None
                else False
            ),
        ),
    )


@login_required
def competition_fixture_generator(request, slug):
    """Preview and safely confirm one separately generated round-robin leg."""

    competition = _managed_competition_or_404(request, slug)
    if not competition.has_league_or_group_stage:
        raise Http404("This competition does not have a league or group table.")

    scope_data = request.POST if request.method == "POST" else request.GET
    scope_form = PoolLeagueFixtureScopeForm(
        scope_data or None,
        competition=competition,
    )
    selected_stage = selected_group = scope = None
    if scope_data:
        if scope_form.is_valid():
            selected_stage = scope_form.cleaned_data["stage"]
            selected_group = scope_form.cleaned_data["group"]
    else:
        selected_stage, selected_group = _default_table_scope(competition)
        scope_form = PoolLeagueFixtureScopeForm(
            initial={
                "stage": getattr(selected_stage, "pk", None),
                "group": getattr(selected_group, "pk", None),
            },
            competition=competition,
        )

    if selected_stage is not None:
        try:
            scope = get_league_fixture_scope(
                competition,
                selected_stage,
                selected_group,
            )
        except LeagueFixtureValidationError as error:
            scope_form.add_error(None, _validation_message(error))

    preview = None
    preview_entries = {}
    preview_gameweeks = ()
    confirm_form = PoolLeagueFixturePreviewConfirmForm()
    if request.method == "POST" and scope is not None:
        action = request.POST.get("action")
        if action == "preview_first_round" or action == "preview_second_round":
            try:
                preview = (
                    plan_first_round_preview(scope)
                    if action == "preview_first_round"
                    else plan_second_round_preview(scope)
                )
                confirm_form = PoolLeagueFixturePreviewConfirmForm(
                    initial={"token": serialize_fixture_preview(preview)}
                )
                preview_entries = preview_entries_by_id(scope)
                preview_gameweeks = tuple(
                    {
                        "number": gameweek.number,
                        "fixtures": tuple(
                            {
                                "fixture": fixture,
                                "home_entry": preview_entries[fixture.home_entry_id],
                                "away_entry": preview_entries[fixture.away_entry_id],
                            }
                            for fixture in gameweek.fixtures
                        ),
                    }
                    for gameweek in preview.gameweeks
                )
            except LeagueFixtureValidationError as error:
                messages.error(request, _validation_message(error))
        elif action == "confirm":
            confirm_form = PoolLeagueFixturePreviewConfirmForm(request.POST)
            if confirm_form.is_valid():
                try:
                    created = confirm_fixture_preview(
                        confirm_form.cleaned_data["token"],
                        actor=request.user,
                        scope=scope,
                    )
                except LeagueFixtureValidationError as error:
                    messages.error(request, _validation_message(error))
                else:
                    leg = created[0].league_leg if created else None
                    messages.success(
                        request,
                        f"Generated {len(created)} {'first' if leg == 1 else 'second'}-round fixtures.",
                    )
                    return redirect(
                        reverse(
                            "pool:manage_competition_fixture_generator",
                            args=[competition.slug],
                        )
                        + _table_scope_query(selected_stage, selected_group)
                    )
            else:
                messages.error(request, "Confirm that you reviewed the fixture preview.")
        elif action:
            messages.error(request, "That fixture-generator action is not available.")

    status = league_fixture_status(scope) if scope is not None else None
    return render(
        request,
        "pool/manage/fixture_generator.html",
        _manager_context(
            request,
            competition,
            scope_form=scope_form,
            selected_stage=selected_stage,
            selected_group=selected_group,
            fixture_scope=scope,
            fixture_status=status,
            fixture_preview=preview,
            preview_entries=preview_entries,
            preview_gameweeks=preview_gameweeks,
            fixture_round_term=(
                PoolMatch.fixture_round_term_for(competition, selected_stage)
                if selected_stage is not None
                else "Gameweek"
            ),
            confirm_form=confirm_form,
        ),
    )


@login_required
def competition_groups(request, slug):
    competition = _managed_competition_or_404(request, slug)
    group_stages = (
        competition.stages.filter(stage_type=PoolCompetitionStage.StageType.GROUP)
        .annotate(
            group_count=Count("groups", distinct=True),
            membership_count=Count("group_memberships", distinct=True),
        )
        .order_by("order", "id")
    )
    return render(
        request,
        "pool/manage/group_list.html",
        _manager_context(request, competition, group_stages=group_stages),
    )


@login_required
def competition_group_stage(request, slug, stage_id):
    competition = _managed_competition_or_404(request, slug)
    stage = _group_stage_or_404(competition, stage_id)
    groups = (
        stage.groups.prefetch_related("memberships__entry__player", "memberships__entry__team")
        .order_by("order", "id")
    )
    return render(
        request,
        "pool/manage/group_stage.html",
        _manager_context(request, competition, stage=stage, groups=groups),
    )


@login_required
def competition_group_create(request, slug, stage_id):
    competition = _managed_competition_or_404(request, slug)
    stage = _group_stage_or_404(competition, stage_id)
    form = PoolGroupForm(request.POST or None, stage=stage)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except (IntegrityError, ValidationError):
            form.add_error(None, "Group names and orders must be unique within this stage.")
        else:
            messages.success(request, "Group was added.")
            return redirect(
                "pool:manage_competition_group_stage",
                slug=competition.slug,
                stage_id=stage.pk,
            )
    return render(
        request,
        "pool/manage/group_form.html",
        _manager_context(request, competition, stage=stage, form=form, page_title="Add group"),
    )


@login_required
def competition_group_update(request, slug, stage_id, group_id):
    competition = _managed_competition_or_404(request, slug)
    stage = _group_stage_or_404(competition, stage_id)
    group = _group_or_404(competition, stage, group_id)
    form = PoolGroupForm(request.POST or None, stage=stage, instance=group)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except (IntegrityError, ValidationError):
            form.add_error(None, "Group names and orders must be unique within this stage.")
        else:
            messages.success(request, "Group was updated.")
            return redirect(
                "pool:manage_competition_group_stage",
                slug=competition.slug,
                stage_id=stage.pk,
            )
    return render(
        request,
        "pool/manage/group_form.html",
        _manager_context(
            request,
            competition,
            stage=stage,
            group=group,
            form=form,
            page_title=f"Edit {group.name}",
        ),
    )


@login_required
@require_POST
def competition_group_delete(request, slug, stage_id, group_id):
    competition = _managed_competition_or_404(request, slug)
    stage = _group_stage_or_404(competition, stage_id)
    group = _group_or_404(competition, stage, group_id)
    try:
        group.delete()
    except (ProtectedError, RestrictedError):
        messages.error(request, "This group cannot be removed because it has fixtures.")
    else:
        messages.success(request, "Group was removed.")
    return redirect(
        "pool:manage_competition_group_stage",
        slug=competition.slug,
        stage_id=stage.pk,
    )


@login_required
@require_GET
def competition_group_entry_search(request, slug, stage_id, group_id):
    """Return eligible group-stage entries for the lightweight basket picker."""

    competition = _managed_competition_or_404(request, slug)
    stage = _group_stage_or_404(competition, stage_id)
    _group_or_404(competition, stage, group_id)
    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"results": []})

    assigned_entry_ids = PoolGroupEntry.objects.filter(stage=stage).values("entry_id")
    entries = (
        competition.entries.exclude(pk__in=assigned_entry_ids)
        .select_related("player", "team")
        .filter(
            Q(player__full_name__icontains=query)
            | Q(player__nickname__icontains=query)
            | Q(team__name__icontains=query)
        )
        .order_by("seed", "id")[:25]
    )
    return JsonResponse(
        {
            "results": [
                {
                    "id": entry.pk,
                    "display_name": entry.display_name,
                    "seed": entry.seed,
                }
                for entry in entries
            ]
        }
    )


def _locked_group_entries(entry_ids):
    """Lock only entry rows while a group-membership basket is committed.

    ``player`` and ``team`` are nullable alternatives on an entry.  Joining
    either relation here would produce outer joins which PostgreSQL cannot
    lock with ``FOR UPDATE``.  The write path only needs entry identity and
    competition scope, so keep the lock query deliberately base-table only.
    """

    return PoolCompetitionEntry.objects.select_for_update().filter(pk__in=entry_ids)


@login_required
def competition_group_entry_create(request, slug, stage_id, group_id):
    competition = _managed_competition_or_404(request, slug)
    stage = _group_stage_or_404(competition, stage_id)
    group = _group_or_404(competition, stage, group_id)
    form = PoolGroupBulkEntryForm(request.POST or None, group=group)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                # The root lock serialises this with bracket generation, while
                # the stage/group locks make eligibility and appended ordering
                # stable for the entire submitted basket.
                locked_competition = PoolCompetition.objects.select_for_update().get(
                    pk=competition.pk
                )
                locked_stage = PoolCompetitionStage.objects.select_for_update().get(
                    pk=stage.pk,
                    competition=locked_competition,
                    stage_type=PoolCompetitionStage.StageType.GROUP,
                )
                locked_group = PoolGroup.objects.select_for_update().get(
                    pk=group.pk,
                    stage=locked_stage,
                )
                if _group_is_referenced_by_generated_bracket(
                    locked_competition,
                    locked_group,
                ):
                    raise ValidationError(
                        "Reset the generated bracket before changing a qualifying group membership."
                    )

                entry_ids = form.cleaned_data["entry_ids"]
                locked_entries_by_id = {
                    entry.pk: entry
                    for entry in _locked_group_entries(entry_ids)
                }
                if len(locked_entries_by_id) != len(entry_ids):
                    raise ValidationError(
                        "One or more selected participants no longer exist."
                    )
                entries = [locked_entries_by_id[entry_id] for entry_id in entry_ids]
                if any(
                    entry.competition_id != locked_competition.pk for entry in entries
                ):
                    raise ValidationError(
                        "Every selected participant must belong to this competition."
                    )
                if PoolGroupEntry.objects.select_for_update().filter(
                    stage=locked_stage,
                    entry_id__in=entry_ids,
                ).exists():
                    raise ValidationError(
                        "One or more selected participants are already assigned to this group stage."
                    )

                existing_memberships = list(
                    PoolGroupEntry.objects.select_for_update()
                    .filter(group=locked_group)
                    .only("sort_order")
                )
                next_sort_order = max(
                    (membership.sort_order for membership in existing_memberships),
                    default=-1,
                ) + 1
                for offset, entry in enumerate(entries):
                    PoolGroupEntry.objects.create(
                        stage=locked_stage,
                        group=locked_group,
                        entry=entry,
                        sort_order=next_sort_order + offset,
                    )
                assigned_count = len(entries)
        except IntegrityError:
            form.add_error(
                "entry_ids",
                "One or more selected participants are already assigned to this group stage.",
            )
        except ValidationError as error:
            form.add_error(None, _validation_message(error))
        else:
            if request.POST.get("save_and_add_another"):
                messages.success(
                    request,
                    f"{assigned_count} {'participant was' if assigned_count == 1 else 'participants were'} assigned. You can add another.",
                )
                return redirect(
                    "pool:manage_competition_group_entry_create",
                    slug=competition.slug,
                    stage_id=stage.pk,
                    group_id=group.pk,
                )
            messages.success(
                request,
                f"{assigned_count} {'participant was' if assigned_count == 1 else 'participants were'} assigned to the group.",
            )
            return redirect(
                "pool:manage_competition_group_stage",
                slug=competition.slug,
                stage_id=stage.pk,
            )
    return render(
        request,
        "pool/manage/group_entry_form.html",
        _manager_context(
            request,
            competition,
            stage=stage,
            group=group,
            form=form,
            page_title="Add group participant",
            entry_search_url=reverse(
                "pool:manage_competition_group_entry_search",
                args=[competition.slug, stage.pk, group.pk],
            ),
            selected_entries=[
                {
                    "id": entry.pk,
                    "display_name": entry.display_name,
                    "seed": entry.seed,
                }
                for entry in getattr(form, "selected_entries", ())
            ],
            can_save_and_add_another=True,
            page_navigation_back_url=reverse(
                "pool:manage_competition_group_stage",
                args=[competition.slug, stage.pk],
            ),
        ),
    )


@login_required
def competition_group_entry_update(request, slug, stage_id, group_id, membership_id):
    competition = _managed_competition_or_404(request, slug)
    stage = _group_stage_or_404(competition, stage_id)
    group = _group_or_404(competition, stage, group_id)
    membership = get_object_or_404(
        PoolGroupEntry.objects.select_related("entry__player", "entry__team"),
        pk=membership_id,
        stage=stage,
        group=group,
        entry__competition=competition,
    )
    form = PoolGroupEntryOrderForm(request.POST or None, instance=membership)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Group assignment order was updated.")
        return redirect(
            "pool:manage_competition_group_stage",
            slug=competition.slug,
            stage_id=stage.pk,
        )
    return render(
        request,
        "pool/manage/group_entry_form.html",
        _manager_context(
            request,
            competition,
            stage=stage,
            group=group,
            membership=membership,
            form=form,
            page_title=f"Edit {membership.entry.display_name}",
        ),
    )


@login_required
@require_POST
def competition_group_entry_delete(request, slug, stage_id, group_id, membership_id):
    competition = _managed_competition_or_404(request, slug)
    stage = _group_stage_or_404(competition, stage_id)
    group = _group_or_404(competition, stage, group_id)
    membership = get_object_or_404(
        PoolGroupEntry,
        pk=membership_id,
        stage=stage,
        group=group,
        entry__competition=competition,
    )
    entry_is_used_by_group_fixture = PoolMatch.objects.filter(group=group).filter(
        Q(home_entry=membership.entry) | Q(away_entry=membership.entry)
    ).exists()
    group_is_used_by_generated_bracket = _group_is_referenced_by_generated_bracket(
        competition,
        group,
    )
    if entry_is_used_by_group_fixture or group_is_used_by_generated_bracket:
        messages.error(
            request,
            "This participant cannot be removed because this group is used by a fixture or generated bracket.",
        )
    else:
        membership.delete()
        messages.success(request, "Participant was removed from the group.")
    return redirect(
        "pool:manage_competition_group_stage",
        slug=competition.slug,
        stage_id=stage.pk,
    )


@login_required
def competition_qualifications(request, slug):
    competition = _managed_competition_or_404(request, slug)
    group_stages = competition.stages.filter(
        stage_type=PoolCompetitionStage.StageType.GROUP
    ).order_by("order", "id")
    requested_stage_id = request.GET.get("stage")
    if requested_stage_id:
        selected_stage = get_object_or_404(group_stages, pk=requested_stage_id)
    else:
        selected_stage = group_stages.first()
    qualification_rules = PoolGroupQualificationRule.objects.none()
    if selected_stage:
        qualification_rules = (
            PoolGroupQualificationRule.objects.filter(
                source_stage=selected_stage,
                source_stage__competition=competition,
            )
            .select_related("group", "knockout_stage")
            .order_by("group__order", "position", "id")
        )
    qualification_handoffs = _qualification_handoffs(
        competition,
        selected_stage,
        qualification_rules,
    )
    qualification_preview = None
    qualification_preview_qualifiers = ()
    qualification_preview_first_round = ()
    preview_token = request.GET.get("preview")
    open_destination_ids = [
        handoff["stage"].pk
        for handoff in qualification_handoffs
        if handoff["bracket"] is None
    ]
    if preview_token and open_destination_ids:
        qualification_preview = get_object_or_404(
            PoolBracketDrawPreview.objects.select_related("stage").filter(
                token=preview_token,
                competition=competition,
                stage_id__in=open_destination_ids,
                draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
                created_by=request.user,
                confirmed_pool_bracket__isnull=True,
            )
        )
        (
            qualification_preview_qualifiers,
            qualification_preview_first_round,
        ) = _qualification_preview_display(competition, qualification_preview)
    return render(
        request,
        "pool/manage/qualification_list.html",
        _manager_context(
            request,
            competition,
            group_stages=group_stages,
            selected_stage=selected_stage,
            qualification_rules=qualification_rules,
            qualification_handoffs=qualification_handoffs,
            qualification_preview=qualification_preview,
            qualification_preview_qualifiers=qualification_preview_qualifiers,
            qualification_preview_first_round=qualification_preview_first_round,
            qualification_confirm_form=PoolBracketPreviewConfirmForm(
                initial={"token": qualification_preview.token}
                if qualification_preview
                else None
            ),
        ),
    )


def _qualification_handoffs(competition, source_stage, qualification_rules):
    """Build display-only apply targets from the configured rule mappings."""

    if source_stage is None:
        return ()
    source_rules = list(qualification_rules)
    destination_ids = sorted(
        {rule.knockout_stage_id for rule in source_rules if rule.knockout_stage_id}
    )
    if not destination_ids:
        return ()
    destination_rules = list(
        PoolGroupQualificationRule.objects.filter(
            knockout_stage_id__in=destination_ids,
            source_stage__competition=competition,
        )
        .select_related("knockout_stage")
        .order_by("knockout_stage__order", "destination_slot", "id")
    )
    brackets_by_stage = {
        bracket.stage_id: bracket
        for bracket in PoolKnockoutBracket.objects.filter(
            competition=competition,
            stage_id__in=destination_ids,
        ).select_related("stage")
    }
    handoffs = []
    for destination_id in destination_ids:
        target_rules = [
            rule for rule in destination_rules if rule.knockout_stage_id == destination_id
        ]
        target_stage = target_rules[0].knockout_stage
        maximum_slot = max(rule.destination_slot for rule in target_rules)
        bracket_size = next(
            (
                size
                for size in SUPPORTED_KNOCKOUT_ROUND_SIZES
                if size >= maximum_slot
            ),
            None,
        )
        handoffs.append(
            {
                "stage": target_stage,
                "source_rule_count": sum(
                    rule.knockout_stage_id == destination_id for rule in source_rules
                ),
                "rule_count": len(target_rules),
                "bracket_size": bracket_size,
                "bracket": brackets_by_stage.get(destination_id),
            }
        )
    return tuple(handoffs)


def _qualification_preview_display(competition, preview):
    """Present persisted qualification-preview slots without recalculating tables."""

    rules = list(
        PoolGroupQualificationRule.objects.filter(
            knockout_stage=preview.stage,
            source_stage__competition=competition,
        )
        .select_related("group")
        .order_by("group__order", "position", "id")
    )
    entry_ids = [entry_id for entry_id in preview.draw_order if entry_id]
    entries = {
        entry.pk: entry
        for entry in competition.entries.filter(pk__in=entry_ids).select_related(
            "player", "team"
        )
    }
    qualifiers = tuple(
        {
            "group": rule.group,
            "position": rule.position,
            "entry": entries.get(preview.draw_order[rule.destination_slot - 1]),
            "destination_slot": rule.destination_slot,
        }
        for rule in rules
        if rule.destination_slot <= len(preview.draw_order)
    )
    first_round = tuple(
        {
            "number": position // 2 + 1,
            "home": entries.get(preview.draw_order[position]),
            "away": entries.get(preview.draw_order[position + 1]),
        }
        for position in range(0, len(preview.draw_order), 2)
    )
    return qualifiers, first_round


@login_required
@require_POST
def competition_qualification_apply(request, slug):
    """Create a reviewable Group Qualification draw from current standings."""

    competition = _managed_competition_or_404(request, slug)
    source_stage = _group_stage_or_404(competition, request.POST.get("source_stage"))
    target_stage = _knockout_stage_or_404(competition, request.POST.get("knockout_stage"))
    redirect_url = (
        f"{reverse('pool:manage_competition_qualifications', args=[competition.slug])}"
        f"?stage={source_stage.pk}"
    )
    if target_stage.order <= source_stage.order:
        messages.error(
            request,
            "The qualification destination must be a later knockout stage.",
        )
        return redirect(redirect_url)
    source_rules = list(
        PoolGroupQualificationRule.objects.filter(
            source_stage=source_stage,
            knockout_stage=target_stage,
        )
    )
    if not source_rules:
        messages.error(
            request,
            "No qualification mappings are configured for that knockout stage.",
        )
        return redirect(redirect_url)
    if PoolKnockoutBracket.objects.filter(
        competition=competition,
        stage=target_stage,
    ).exists():
        messages.error(request, "This knockout stage already has a generated bracket.")
        return redirect(redirect_url)
    all_target_rules = PoolGroupQualificationRule.objects.filter(
        knockout_stage=target_stage,
        source_stage__competition=competition,
    )
    maximum_slot = all_target_rules.aggregate(maximum=Max("destination_slot"))["maximum"]
    bracket_size = next(
        (
            size
            for size in SUPPORTED_KNOCKOUT_ROUND_SIZES
            if size >= (maximum_slot or 0)
        ),
        None,
    )
    if bracket_size is None:
        messages.error(request, "Qualification destinations exceed the supported bracket size.")
        return redirect(redirect_url)
    try:
        preview = _bracket_services()["create_draw_preview"](
            competition,
            stage=target_stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION,
            bracket_size=bracket_size,
            actor=request.user,
        )
    except (ImportError, ValidationError, IntegrityError) as error:
        messages.error(request, _validation_message(error))
        return redirect(redirect_url)
    messages.success(
        request,
        "Qualification rules were applied to the current group standings. Review the knockout preview before generating it.",
    )
    return redirect(f"{redirect_url}&preview={preview.token}")


@login_required
def competition_qualification_create(request, slug, stage_id):
    competition = _managed_competition_or_404(request, slug)
    source_stage = _group_stage_or_404(competition, stage_id)
    initial = {}
    if request.method == "GET":
        requested_knockout_stage_id = request.GET.get("knockout_stage")
        if requested_knockout_stage_id:
            initial_knockout_stage = competition.stages.filter(
                pk=requested_knockout_stage_id,
                stage_type=PoolCompetitionStage.StageType.KNOCKOUT,
                order__gt=source_stage.order,
            ).first()
            if initial_knockout_stage:
                # This is the only value carried forward after Save & Add
                # Another.  Group, position, destination, and separation are
                # deliberately blank so a subsequent mapping cannot become a
                # silent duplicate.
                initial["knockout_stage"] = initial_knockout_stage.pk
    form = PoolGroupQualificationRuleForm(
        request.POST or None,
        competition=competition,
        source_stage=source_stage,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        rule = form.save(commit=False)
        rule.source_stage = source_stage
        try:
            with transaction.atomic():
                rule.save()
                _configure_group_qualification_if_available(rule, request.user)
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, "Qualification rule was added.")
            if request.POST.get("save_and_add_another"):
                create_url = reverse(
                    "pool:manage_competition_qualification_create",
                    args=[competition.slug, source_stage.pk],
                )
                return redirect(
                    f"{create_url}?knockout_stage={rule.knockout_stage_id}"
                )
            return redirect(
                f"{reverse('pool:manage_competition_qualifications', args=[competition.slug])}?stage={source_stage.pk}"
            )
    return render(
        request,
        "pool/manage/qualification_form.html",
        _manager_context(
            request,
            competition,
            source_stage=source_stage,
            form=form,
            page_title="Add qualification rule",
            can_save_and_add_another=True,
        ),
    )


@login_required
def competition_qualification_update(request, slug, rule_id):
    competition = _managed_competition_or_404(request, slug)
    rule = get_object_or_404(
        PoolGroupQualificationRule.objects.select_related("source_stage", "group"),
        pk=rule_id,
        source_stage__competition=competition,
    )
    source_stage = rule.source_stage
    if request.method == "POST" and PoolKnockoutBracket.objects.filter(
        competition=competition,
        stage=rule.knockout_stage,
    ).exists():
        messages.error(
            request,
            "Reset the generated bracket before changing one of its qualification rules.",
        )
        return redirect(
            f"{reverse('pool:manage_competition_qualifications', args=[competition.slug])}?stage={source_stage.pk}"
        )
    form = PoolGroupQualificationRuleForm(
        request.POST or None,
        competition=competition,
        source_stage=source_stage,
        instance=rule,
    )
    if request.method == "POST" and form.is_valid():
        updated_rule = form.save(commit=False)
        updated_rule.source_stage = source_stage
        try:
            with transaction.atomic():
                updated_rule.save()
                _configure_group_qualification_if_available(updated_rule, request.user)
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, "Qualification rule was updated.")
            return redirect(
                f"{reverse('pool:manage_competition_qualifications', args=[competition.slug])}?stage={source_stage.pk}"
            )
    return render(
        request,
        "pool/manage/qualification_form.html",
        _manager_context(
            request,
            competition,
            source_stage=source_stage,
            qualification_rule=rule,
            form=form,
            page_title="Edit qualification rule",
        ),
    )


@login_required
@require_POST
def competition_qualification_delete(request, slug, rule_id):
    competition = _managed_competition_or_404(request, slug)
    rule = get_object_or_404(
        PoolGroupQualificationRule,
        pk=rule_id,
        source_stage__competition=competition,
    )
    source_stage_id = rule.source_stage_id
    if PoolKnockoutBracket.objects.filter(
        competition=competition,
        stage=rule.knockout_stage,
    ).exists():
        messages.error(
            request,
            "Reset the generated bracket before removing one of its qualification rules.",
        )
    else:
        rule.delete()
        messages.success(request, "Qualification rule was removed.")
    return redirect(
        f"{reverse('pool:manage_competition_qualifications', args=[competition.slug])}?stage={source_stage_id}"
    )


def _configure_group_qualification_if_available(rule, actor):
    """Use the optional service hook without breaking normal CRUD pre-engine."""
    try:
        from .brackets import configure_group_qualification
    except ImportError:
        try:
            from .services import configure_group_qualification
        except ImportError:
            return rule
    return configure_group_qualification(rule, actor=actor)


def _bracket_services():
    """Delay the bracket-engine import until a bracket manager action is used.

    The pool manager remains importable for ordinary group/fixture operations
    before the bracket service module is installed in a deployment.
    """
    try:
        from .brackets import (
            bracket_display,
            create_handoff_draw_preview,
            create_draw_preview,
            generate_bracket_from_preview,
            generate_random_handoff_bracket,
            generate_random_bracket,
            handoff_field_for_round,
            place_late_entry_in_open_bye,
            reset_bracket,
            update_bracket_round_targets,
        )
    except ImportError:
        from .services import (
            bracket_display,
            create_handoff_draw_preview,
            create_draw_preview,
            generate_bracket_from_preview,
            generate_random_handoff_bracket,
            generate_random_bracket,
            handoff_field_for_round,
            place_late_entry_in_open_bye,
            reset_bracket,
            update_bracket_round_targets,
        )

    return {
        "bracket_display": bracket_display,
        "create_draw_preview": create_draw_preview,
        "generate_bracket_from_preview": generate_bracket_from_preview,
        "generate_random_bracket": generate_random_bracket,
        "create_handoff_draw_preview": create_handoff_draw_preview,
        "generate_random_handoff_bracket": generate_random_handoff_bracket,
        "handoff_field_for_round": handoff_field_for_round,
        "place_late_entry_in_open_bye": place_late_entry_in_open_bye,
        "reset_bracket": reset_bracket,
        "update_bracket_round_targets": update_bracket_round_targets,
    }


def _prepare_large_bracket_display(display, bracket):
    """Add presentation-only half metadata without changing bracket services.

    The bracket engine intentionally returns a portable topology.  The manager
    enriches that payload at the boundary so 64+ brackets can offer a genuine
    top/bottom selector through every non-final round, including when an older
    service implementation labelled only its opening round.
    """

    if not isinstance(display, dict):
        return display

    large_draw = bracket.size >= 64
    display["large_draw"] = large_draw
    display["first_round_half_tie_count"] = bracket.size // 4

    third_place_tie = None
    for round_data in display.get("rounds", ()):
        for item in round_data.get("ties", ()):
            tie = item.get("tie")
            if tie is None:
                continue
            if getattr(tie, "is_third_place", False):
                third_place_tie = third_place_tie or tie
                item["draw_half"] = "shared"
                continue

            if not large_draw:
                continue

            round_number = getattr(tie, "round_number", round_data.get("number"))
            position = getattr(tie, "position", None)
            if not round_number or not position:
                continue
            tie_count = bracket.size // (2**round_number)
            if tie_count <= 1:
                item["draw_half"] = "final"
                item["half_heading"] = ""
                continue

            ties_per_half = tie_count // 2
            item["draw_half"] = (
                "top" if position <= ties_per_half else "bottom"
            )
            if round_number == 1 and position == 1:
                item["half_heading"] = "Top half"
            elif round_number == 1 and position == ties_per_half + 1:
                item["half_heading"] = "Bottom half"
            else:
                item["half_heading"] = ""

    # Preserve a service-supplied convergence flag and derive it for the ORM
    # fallback when the new resolution method is available on the tie.
    if "same_entry_third_place" not in display:
        display["same_entry_third_place"] = bool(
            third_place_tie
            and getattr(third_place_tie, "resolution_method", "")
            == "SAME_ENTRY_THIRD"
        )
    if (
        display.get("same_entry_third_place")
        and not display.get("third_place_label")
        and third_place_tie is not None
        and getattr(third_place_tie, "resolved_winner_id", None)
    ):
        display["third_place_label"] = (
            third_place_tie.resolved_winner.display_name
        )
    if display.get("same_entry_third_place"):
        # A same-entry convergence is an automatic placement, not a playable
        # fixture.  Keep the explanatory note but omit a stale third-place
        # card from this presentation-only manager payload.
        display["rounds"] = [
            round_data
            for round_data in display.get("rounds", ())
            if not round_data.get("is_third_place")
        ]
    return display


def _manager_bracket_display(bracket):
    """Prefer the bracket service presentation, with a safe ORM fallback."""
    try:
        display = _bracket_services()["bracket_display"](bracket)
    except (ImportError, ValidationError):
        return _prepare_large_bracket_display(_bracket_display_context(bracket), bracket)
    if isinstance(display, dict) and "rounds" in display:
        return _prepare_large_bracket_display(display, bracket)
    return _prepare_large_bracket_display(_bracket_display_context(bracket), bracket)


@login_required
def competition_bracket(request, slug):
    competition = _managed_competition_or_404(request, slug)
    knockout_stages = list(
        competition.stages.filter(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        ).order_by("order", "id")
    )
    requested_stage_id = request.GET.get("stage")
    selected_stage = None
    if requested_stage_id:
        selected_stage = _knockout_stage_or_404(competition, requested_stage_id)
    elif knockout_stages:
        selected_stage = knockout_stages[0]

    bracket = None
    if selected_stage:
        bracket = PoolKnockoutBracket.objects.filter(
            competition=competition,
            stage=selected_stage,
        ).first()

    preview = None
    other_unconfirmed_previews = ()
    preview_token = request.GET.get("preview")
    if preview_token and selected_stage and not bracket:
        preview = get_object_or_404(
            PoolBracketDrawPreview.objects.select_related("stage"),
            token=preview_token,
            competition=competition,
            stage=selected_stage,
            created_by=request.user,
        )
    elif selected_stage and not bracket:
        # Do not expose one manager's preview to another administrator.
        preview = PoolBracketDrawPreview.objects.filter(
            competition=competition,
            stage=selected_stage,
            created_by=request.user,
        ).first()

    # Draw contents remain creator-private until confirmation, but the owner
    # must be able to release a departed organiser's unconfirmed preview.
    # Otherwise one stale JSON draw would permanently block the entry/life and
    # competition configuration safeguards before a bracket even exists.
    if (
        selected_stage
        and not bracket
        and user_is_pool_competition_owner(request.user, competition)
    ):
        other_unconfirmed_previews = (
            PoolBracketDrawPreview.objects.filter(
                competition=competition,
                stage=selected_stage,
                confirmed_pool_bracket__isnull=True,
            )
            .exclude(created_by=request.user)
            .select_related("created_by")
            .order_by("-created_at", "-id")
        )

    draw_form_initial = {"stage": selected_stage.pk} if selected_stage else {}
    # This is configuration for an unbound setup form, not a draw request.
    # In particular, do not pass request.GET as form data: the refresh URL
    # intentionally omits required draw fields and must never validate/save.
    if "bracket_size" in request.GET:
        draw_form_initial["bracket_size"] = request.GET["bracket_size"]
    if "draw_method" in request.GET:
        # Keep the public refresh URL friendly while retaining the model's
        # uppercase choice values in the actual form and eventual POST.
        draw_form_initial["draw_method"] = request.GET["draw_method"].upper()
    draw_form = PoolBracketDrawForm(
        competition=competition,
        initial=draw_form_initial,
    )
    handoff_form = PoolBracketHandoffForm(
        competition=competition,
        initial={"stage": selected_stage.pk} if selected_stage else None,
    )
    handoff_rounds_available = handoff_form.fields["source_round"].queryset.exists()
    confirm_form = PoolBracketPreviewConfirmForm(
        initial={"token": preview.token} if preview else None
    )
    round_targets_form = None
    reset_form = PoolBracketResetForm()
    bracket_display_data = {"rounds": [], "ties": []}
    audit_events = ()
    if selected_stage:
        # Stable scope fields keep reset history visible even after the
        # bracket itself has been intentionally removed.
        audit_events = (
            PoolBracketAuditEvent.objects.filter(
                competition=competition,
                stage=selected_stage,
            )
            .select_related("actor")
            .order_by("-created_at", "-id")[:20]
        )
    preview_draw_order = []
    if preview:
        life_draw_order = getattr(preview, "life_draw_order", None) or []
        if life_draw_order:
            life_ids = [life_id for life_id in life_draw_order if life_id]
            lives = {
                life.pk: life.display_name
                for life in PoolCompetitionEntryLife.objects.filter(
                    pk__in=life_ids,
                    competition_entry__competition=competition,
                ).select_related("competition_entry__player")
            }
            preview_draw_order = [
                lives.get(life_id) if life_id else None
                for life_id in life_draw_order
            ]
        else:
            entry_ids = [entry_id for entry_id in preview.draw_order if entry_id]
            entries = {
                entry.pk: entry.display_name
                for entry in competition.entries.filter(pk__in=entry_ids).select_related(
                    "player", "team"
                )
            }
            preview_draw_order = [
                entries.get(entry_id) if entry_id else None
                for entry_id in preview.draw_order
            ]
    if bracket:
        round_targets_form = PoolBracketRoundTargetsForm(bracket=bracket)
        bracket_display_data = _manager_bracket_display(bracket)

    return render(
        request,
        "pool/manage/bracket.html",
        _manager_context(
            request,
            competition,
            knockout_stages=knockout_stages,
            selected_stage=selected_stage,
            bracket=bracket,
            preview=preview,
            draw_form=draw_form,
            handoff_form=handoff_form,
            handoff_rounds_available=handoff_rounds_available,
            confirm_form=confirm_form,
            reset_form=reset_form,
            round_targets_form=round_targets_form,
            bracket_display=bracket_display_data,
            audit_events=audit_events,
            preview_draw_order=preview_draw_order,
            other_unconfirmed_previews=other_unconfirmed_previews,
        ),
    )


@login_required
@require_POST
def competition_bracket_preview(request, slug):
    competition = _managed_competition_or_404(request, slug)
    form = PoolBracketDrawForm(request.POST, competition=competition)
    if not form.is_valid():
        messages.error(request, "Please correct the bracket draw details and try again.")
        return render(
            request,
            "pool/manage/bracket.html",
            _manager_context(
                request,
                competition,
                knockout_stages=competition.stages.filter(
                    stage_type=PoolCompetitionStage.StageType.KNOCKOUT
                ).order_by("order", "id"),
                selected_stage=form.cleaned_data.get("stage"),
                bracket=None,
                preview=None,
                draw_form=form,
                handoff_form=PoolBracketHandoffForm(
                    competition=competition,
                    initial={"stage": getattr(form.cleaned_data.get("stage"), "pk", None)},
                ),
                handoff_rounds_available=PoolKnockoutRoundDefinition.objects.filter(
                    stage__competition=competition
                ).exists(),
                confirm_form=PoolBracketPreviewConfirmForm(),
                reset_form=PoolBracketResetForm(),
                round_targets_form=None,
                bracket_display={"rounds": [], "ties": []},
                audit_events=(),
                preview_draw_order=(),
            ),
        )

    stage = _knockout_stage_or_404(competition, form.cleaned_data["stage"].pk)
    if PoolKnockoutBracket.objects.filter(competition=competition, stage=stage).exists():
        if form.cleaned_data.get("draw_method") == PoolBracketDrawPreview.DrawMethod.RANDOM:
            messages.error(
                request,
                "This random draw has already been confirmed and cannot be drawn again.",
            )
        else:
            messages.error(request, "This stage already has a generated bracket.")
        return redirect(
            f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}"
        )

    try:
        draw_method = form.cleaned_data["draw_method"]
        if draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM:
            bracket = _bracket_services()["generate_random_bracket"](
                competition,
                stage=stage,
                bracket_size=form.cleaned_data["bracket_size"],
                third_place_enabled=form.cleaned_data["third_place_enabled"],
                round_race_targets=form.cleaned_data["round_race_targets"],
                actor=request.user,
                confirmed=form.cleaned_data["confirm_random_draw"],
            )
            messages.success(
                request,
                "The one-shot random draw was confirmed and the official bracket is now generated.",
            )
            return redirect(
                f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={bracket.stage_id}"
            )

        manual_entry_ids = None
        manual_life_ids = None
        if draw_method == PoolBracketDrawPreview.DrawMethod.MANUAL:
            # Preserve empty slot positions: an empty manual slot represents a
            # deliberate bye/unused place, not a compacted entrant list.
            if form.life_aware:
                manual_life_ids = form.cleaned_data["manual_life_ids"]
            else:
                manual_entry_ids = form.cleaned_data["draw_order"]
        preview = _bracket_services()["create_draw_preview"](
            competition,
            stage=stage,
            draw_method=draw_method,
            bracket_size=form.cleaned_data["bracket_size"],
            manual_entry_ids=manual_entry_ids,
            manual_life_ids=manual_life_ids,
            third_place_enabled=form.cleaned_data["third_place_enabled"],
            round_race_targets=form.cleaned_data["round_race_targets"],
            actor=request.user,
        )
    except (ImportError, ValidationError, IntegrityError) as error:
        messages.error(request, _validation_message(error))
        return redirect(
            f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}"
        )
    messages.success(request, "Bracket draw preview was created. Review it before generating fixtures.")
    return redirect(
        f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}&preview={preview.token}"
    )


@login_required
@require_POST
def competition_bracket_handoff_preview(request, slug):
    """Create a safe seeded preview, or one-shot random draw, from a manual round."""

    competition = _managed_competition_or_404(request, slug)
    form = PoolBracketHandoffForm(request.POST, competition=competition)
    if not form.is_valid():
        messages.error(request, "Choose a valid configured manual round for the bracket handoff.")
        return redirect("pool:manage_competition_bracket", slug=competition.slug)

    stage = _knockout_stage_or_404(competition, form.cleaned_data["stage"].pk)
    source_round = form.cleaned_data["source_round"]
    try:
        if form.cleaned_data["draw_method"] == PoolBracketDrawPreview.DrawMethod.RANDOM:
            bracket = _bracket_services()["generate_random_handoff_bracket"](
                competition,
                stage=stage,
                source_round=source_round,
                actor=request.user,
                confirmed=form.cleaned_data["confirm_random_draw"],
            )
            messages.success(
                request,
                "The one-shot random handoff draw was confirmed and the bracket is generated.",
            )
            return redirect(
                f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={bracket.stage_id}"
            )
        preview = _bracket_services()["create_handoff_draw_preview"](
            competition,
            stage=stage,
            source_round=source_round,
            actor=request.user,
        )
    except (ImportError, ValidationError, IntegrityError) as error:
        messages.error(request, _validation_message(error))
        return redirect(
            f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}"
        )
    messages.success(
        request,
        "A seeded bracket handoff preview was created from the completed manual-round winners.",
    )
    return redirect(
        f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={stage.pk}&preview={preview.token}"
    )


@login_required
@require_POST
def competition_bracket_preview_discard(request, slug, stage_id, token):
    """Discard one unconfirmed preview for one exact knockout stage.

    Draw previews are intentionally user-scoped for review and confirmation.
    Their creator may discard their own draft; the competition owner may also
    discard an unavailable organiser's stale draft so it cannot indefinitely
    lock entry-life and competition configuration edits.
    """

    competition = _managed_competition_or_404(request, slug)
    # Validate the URL's stage scope before taking locks.  The locked preview
    # query below repeats this scope so neither a token nor a stage ID can be
    # substituted across competitions.
    _knockout_stage_or_404(competition, stage_id)
    redirect_url = (
        f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}"
        f"?stage={stage_id}"
    )

    with transaction.atomic():
        # Confirmation and bracket reset lock the preview before the
        # competition root.  Follow that order here too: locking the root
        # first would deadlock a concurrent confirmation/reset that already
        # owns this preview row.
        preview_queryset = PoolBracketDrawPreview.objects.select_for_update().filter(
            token=token,
            competition_id=competition.pk,
            stage_id=stage_id,
            confirmed_pool_bracket__isnull=True,
        )
        if not user_is_pool_competition_owner(request.user, competition):
            preview_queryset = preview_queryset.filter(created_by=request.user)
        preview = get_object_or_404(preview_queryset)
        locked_competition = PoolCompetition.objects.select_for_update().get(
            pk=preview.competition_id
        )
        # The initial stage check and the exact competition/stage filters
        # above make this defensive assertion normally unreachable.  It keeps
        # the locked root explicit if those scopes are changed later.
        if locked_competition.pk != competition.pk:
            raise PermissionDenied("Bracket preview is outside this competition.")
        # A confirmed preview is filtered out above and is removed only by the
        # owner-authorised bracket reset flow, preserving the audit trail.
        # The competition owner may also remove a different manager's stale,
        # unconfirmed preview to unblock safely scoped configuration changes.
        preview.delete()

    messages.success(request, "Bracket draw preview was discarded.")
    return redirect(redirect_url)


@login_required
@require_POST
def competition_bracket_generate(request, slug):
    competition = _managed_competition_or_404(request, slug)
    form = PoolBracketPreviewConfirmForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Confirm the reviewed draw before generating the bracket.")
        return redirect("pool:manage_competition_bracket", slug=competition.slug)
    preview = get_object_or_404(
        PoolBracketDrawPreview.objects.select_related("stage", "competition"),
        token=form.cleaned_data["token"],
        competition=competition,
        created_by=request.user,
        stage__competition=competition,
        stage__stage_type=PoolCompetitionStage.StageType.KNOCKOUT,
    )
    try:
        bracket = _bracket_services()["generate_bracket_from_preview"](
            preview,
            actor=request.user,
        )
    except (ImportError, ValidationError, IntegrityError) as error:
        messages.error(request, _validation_message(error))
        return redirect(
            f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={preview.stage_id}&preview={preview.token}"
        )
    if bracket.competition_id != competition.id or bracket.stage_id != preview.stage_id:
        # The service should guarantee this. Do not redirect to any bracket
        # outside the authorised competition if a future implementation regresses.
        raise PermissionDenied("Generated bracket is outside this competition.")
    messages.success(request, "Knockout bracket and its fixtures were generated.")
    return redirect(
        f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={bracket.stage_id}"
    )


@login_required
@require_POST
def competition_bracket_round_targets(request, slug, stage_id):
    competition = _managed_competition_or_404(request, slug)
    bracket = _competition_bracket_or_404(competition, stage_id)
    form = PoolBracketRoundTargetsForm(request.POST, bracket=bracket)
    if form.is_valid():
        try:
            _bracket_services()["update_bracket_round_targets"](
                bracket,
                form.round_targets(),
                actor=request.user,
            )
        except (ImportError, ValidationError, IntegrityError) as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(request, "Bracket round race targets were updated.")
    else:
        messages.error(request, "Please correct the round race targets and try again.")
    return redirect(
        f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={bracket.stage_id}"
    )


@login_required
@require_POST
def competition_bracket_reset(request, slug, stage_id):
    competition = _managed_competition_or_404(request, slug)
    bracket = _competition_bracket_or_404(competition, stage_id)
    _require_owner(request, competition)
    form = PoolBracketResetForm(request.POST)
    if form.is_valid():
        try:
            _bracket_services()["reset_bracket"](
                bracket,
                actor=request.user,
                reason="Reset confirmed from the pool competition manager.",
            )
        except (ImportError, ValidationError, IntegrityError) as error:
            messages.error(request, _validation_message(error))
            return redirect(
                f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={bracket.stage_id}"
            )
        messages.success(request, "Generated bracket was reset.")
    else:
        messages.error(request, "Confirm the reset acknowledgement before continuing.")
    return redirect(
        f"{reverse('pool:manage_competition_bracket', args=[competition.slug])}?stage={bracket.stage_id}"
    )


@login_required
def competition_fixtures(request, slug):
    competition = _managed_competition_or_404(request, slug)
    selected_status = request.GET.get("status", "upcoming")
    status_filters = {
        "upcoming": (PoolMatch.Status.UPCOMING, PoolMatch.Status.POSTPONED),
        "live": (PoolMatch.Status.LIVE,),
        "completed": (
            PoolMatch.Status.FINISHED,
            PoolMatch.Status.CANCELLED,
            PoolMatch.Status.ABANDONED,
        ),
    }
    if selected_status not in {*status_filters, "all"}:
        selected_status = "upcoming"

    # Load the scoped fixture rows once.  The template keeps the selected
    # direct-link view visible without JavaScript, while the small client-side
    # controller can switch the already rendered rows instantly.
    ordering = (
        ("-scheduled_at", "-id")
        if selected_status == "all"
        else ("scheduled_at", "id")
    )
    matches = list(
        PoolMatch.objects.filter(competition=competition)
        .select_related(
            "stage",
            "group",
            "knockout_round",
            "home_entry__player",
            "home_entry__team",
            "away_entry__player",
            "away_entry__team",
            "home_entry_life",
            "away_entry_life",
            "winner__player",
            "winner__team",
            "winner_entry_life",
        )
        .order_by(*ordering)
    )
    counts = {
        filter_name: sum(
            match.fixture_filter_status == filter_name for match in matches
        )
        for filter_name in status_filters
    }
    counts["all"] = len(matches)
    for match in matches:
        match.fixture_filter_initially_visible = (
            selected_status == "all"
            or match.fixture_filter_status == selected_status
        )
    return render(
        request,
        "pool/manage/fixture_list.html",
        _manager_context(
            request,
            competition,
            matches=matches,
            current_filter=selected_status,
            counts=counts,
            visible_count=counts[selected_status],
        ),
    )


@login_required
def competition_fixture_create(request, slug):
    competition = _managed_competition_or_404(request, slug)
    if competition.is_money_match:
        raise Http404("A Money Match already has its single configured fixture.")
    form = PoolFixtureForm(request.POST or None, competition=competition)
    if request.method == "POST" and form.is_valid():
        fixture = form.save(commit=False)
        fixture.competition = competition
        fixture.status = PoolMatch.Status.UPCOMING
        fixture.home_score = 0
        fixture.away_score = 0
        fixture.winner = None
        fixture.result_method = PoolMatch.ResultMethod.PLAYED
        try:
            fixture.save()
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            if request.POST.get("save_and_add_another"):
                messages.success(request, "Fixture created. You can add another.")
                return redirect(
                    "pool:manage_competition_fixture_create",
                    slug=competition.slug,
                )
            messages.success(request, "Fixture was created.")
            return redirect("pool:manage_competition_fixtures", slug=competition.slug)
    return render(
        request,
        "pool/manage/fixture_form.html",
        _manager_context(
            request,
            competition,
            form=form,
            page_title="Create fixture",
            can_save_and_add_another=True,
            page_navigation_back_url=reverse(
                "pool:manage_competition_fixtures",
                args=[competition.slug],
            ),
        ),
    )


@login_required
def competition_fixture_update(request, slug, match_id):
    competition = _managed_competition_or_404(request, slug)
    if competition.is_money_match:
        return redirect("pool:manage_money_match_edit", slug=competition.slug)
    fixture = _competition_match_or_404(competition, match_id)
    bracket_managed = PoolKnockoutTie.objects.filter(match=fixture).exists()
    if fixture.status != PoolMatch.Status.UPCOMING:
        messages.error(
            request,
            "Only upcoming fixtures can be edited. Use the match manager for live or completed fixtures.",
        )
        return redirect("pool:manage_competition_fixtures", slug=competition.slug)

    form = PoolFixtureForm(
        request.POST or None,
        competition=competition,
        instance=fixture,
    )
    if bracket_managed:
        # The persistent graph owns the participants, stage, round label and
        # race target.  Managers can still schedule an unstarted bracket race
        # and add its venue/stream without providing a side door to redraw it.
        for field_name in (
            "stage",
            "group",
            "knockout_round",
            "home_entry",
            "away_entry",
            "home_entry_life",
            "away_entry_life",
            "race_target",
            "round_label",
        ):
            form.fields.pop(field_name, None)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except (IntegrityError, ValidationError) as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, "Fixture was updated.")
            return redirect("pool:manage_competition_fixtures", slug=competition.slug)
    return render(
        request,
        "pool/manage/fixture_form.html",
        _manager_context(
            request,
            competition,
            match=fixture,
            form=form,
            page_title="Edit fixture",
            page_navigation_back_url=reverse(
                "pool:manage_competition_fixtures",
                args=[competition.slug],
            ),
        ),
    )


def _standard_match_operations_context(
    request,
    competition,
    pool_match,
    *,
    scorer_mode=False,
):
    """Build the shared singles/legacy-rack scorer page context.

    Managers use the same template as exact-match scorers, but only the
    manager receives finished-result correction, lifecycle, and non-played
    controls.  The services remain the authority for all score validation.
    """

    rack_form = PoolRackResultForm(match=pool_match)
    racks = pool_match.racks.select_related(
        "winner__player",
        "winner__team",
        "winning_player",
        "breaker__player",
        "breaker__team",
    ).order_by("rack_number", "id")
    racks = list(racks)
    rack_rows = [
        {
            "rack": rack,
            "correction_form": PoolRackCorrectionForm(
                match=pool_match,
                preserved_winning_player=rack.winning_player,
                preserved_winner_entry=rack.winner,
                require_complete_team_rosters=False,
                initial={
                    "winner": rack.winner_id,
                    "winning_player": rack.winning_player_id,
                    "outcome_type": rack.outcome_type,
                    "breaker": rack.breaker_id,
                },
            ),
        }
        for rack in racks
    ]
    can_manage_scorers = user_can_manage_competition(request.user, competition)
    can_open_live_scorer = (
        user_can_score_pool_match(request.user, pool_match)
        and pool_match.is_live_scoring_available
    )
    return _manager_context(
        request,
        competition,
        match=pool_match,
        racks=racks,
        rack_rows=rack_rows,
        latest_rack_id=racks[-1].pk if racks else None,
        rack_summary=match_rack_summary(pool_match),
        result_revisions=pool_match.result_revisions.select_related("revised_by").all(),
        rack_form=rack_form,
        non_played_form=PoolNonPlayedResultForm(match=pool_match),
        transition_form=PoolMatchTransitionForm(),
        undo_form=PoolMatchTransitionForm(),
        correction_form=PoolResultCorrectionForm(),
        correction_is_open=bool(getattr(pool_match, "is_in_correction_mode", False)),
        result_is_locked=bool(getattr(pool_match, "result_is_locked", False)),
        team_rack_scoring=rack_form.is_team_match,
        missing_roster_entries=rack_form.missing_roster_entries,
        ambiguous_roster_players=rack_form.ambiguous_roster_players,
        can_record_racks=(
            pool_match.status == PoolMatch.Status.LIVE
            and not pool_match.is_in_correction_mode
            and rack_form.team_roster_ready
        ),
        can_manage_scorers=can_manage_scorers,
        can_open_live_scorer=can_open_live_scorer,
        scorer_assignments=(
            pool_match.scorer_assignments.select_related("user", "assigned_by")
            .order_by("-is_active", "user__username", "id")
        ),
        scorer_form=(PoolMatchScorerForm(match=pool_match) if can_manage_scorers else None),
        scorer_mode=scorer_mode,
        can_expire_match=True,
    )


@login_required
def match_manage(request, slug, match_id):
    competition = _managed_competition_or_404(request, slug)
    pool_match = _competition_match_or_404(competition, match_id)
    has_team_schedule = is_team_fixture(pool_match) and PoolTeamMatchScheduledRack.objects.filter(
        match=pool_match
    ).exists()
    # Existing Phase Two team fixtures can already be live or historical
    # without a Phase Four score sheet.  Keep their established generic rack
    # manager available rather than stranding a real fixture halfway through.
    # New/upcoming team fixtures use the dedicated lineup-first operations
    # screen; once they start, their persistent schedule is the only scorer
    # path.
    legacy_team_scoring = (
        is_team_fixture(pool_match)
        and not has_team_schedule
        and (
            pool_match.status == PoolMatch.Status.LIVE
            or pool_match.racks.exists()
        )
    )
    if is_team_fixture(pool_match) and not legacy_team_scoring:
        return render(
            request,
            "pool/manage/team_match_operations.html",
            _team_match_operations_context(request, competition, pool_match),
        )
    return render(
        request,
        "pool/manage/match_manage.html",
        _standard_match_operations_context(request, competition, pool_match),
    )


@login_required
def team_match_lineup(request, match_id, entry_id):
    """Narrow, standalone lineup page for one fixture side.

    Coaches and captains never enter the manager dashboard through this route:
    they can only submit a sheet for their exact assigned competition entry.
    Owners, assigned competition admins, and superusers retain an explicit
    override for either side.
    """

    pool_match = get_object_or_404(
        PoolMatch.objects.select_related(
            "competition",
            "home_entry__team",
            "away_entry__team",
        ),
        pk=match_id,
    )
    if not is_team_fixture(pool_match):
        raise Http404("Team lineups are not available for this fixture.")
    entry = _team_match_entry_or_404(pool_match, entry_id)
    if not user_can_manage_team_lineup(request.user, pool_match, entry):
        raise PermissionDenied("You can only manage lineups assigned to your account.")

    is_manager = user_can_manage_competition(request.user, pool_match.competition)
    can_edit_lineup = _team_lineup_editable(
        user=request.user,
        pool_match=pool_match,
        is_manager=is_manager,
    )
    lineup = (
        PoolTeamMatchLineup.objects.filter(match=pool_match, team_entry=entry)
        .prefetch_related("positions__player", "members__player")
        .first()
    )
    form = PoolTeamMatchLineupForm(
        request.POST or None,
        match=pool_match,
        team_entry=entry,
        lineup=lineup,
    )
    if request.method == "POST":
        if not can_edit_lineup:
            raise PermissionDenied("This lineup is no longer available for editing.")
        if form.is_valid():
            try:
                lineup = submit_team_match_lineup(
                    pool_match,
                    entry,
                    starters={
                        position: form.cleaned_data[f"starter_{position}"]
                        for position in range(1, 6)
                    },
                    substitutes=form.cleaned_data["substitutes"],
                    actor=request.user,
                    allow_live_correction=(
                        is_manager and pool_match.status == PoolMatch.Status.LIVE
                    ),
                )
            except PoolResultValidationError as error:
                form.add_error(None, _validation_message(error))
            else:
                messages.success(request, "The team lineup was saved.")
                return redirect(
                    "pool:team_match_lineup",
                    match_id=pool_match.pk,
                    entry_id=entry.pk,
                )

    lineup_positions = list(lineup.positions.select_related("player").all()) if lineup else []
    lineup_members = list(lineup.members.select_related("player").all()) if lineup else []
    lineup_back_url = reverse("pool:home")
    if (
        is_manager
        or pool_match.competition.visibility
        == PoolCompetition.Visibility.PUBLISHED
    ):
        lineup_back_url = reverse("pool:match_detail", args=[pool_match.pk])
    return render(
        request,
        "pool/team_match_lineup.html",
        {
            "match": pool_match,
            "entry": entry,
            "lineup": lineup,
            "lineup_positions": lineup_positions,
            "lineup_members": lineup_members,
            "form": form,
            "starter_fields": form.starter_fields,
            "can_edit_lineup": can_edit_lineup,
            "is_manager": is_manager,
            "lineup_back_url": lineup_back_url,
        },
    )

@login_required
def match_score(request, match_id):
    """Legacy scorer URL: authorise it, then use the inline fixture page."""

    pool_match = get_object_or_404(
        PoolMatch.objects.select_related(
            "competition",
            "stage",
            "knockout_round",
            "home_entry__player",
            "home_entry__team",
            "away_entry__player",
            "away_entry__team",
            "winner__player",
            "winner__team",
        ),
        pk=match_id,
    )
    if not user_can_score_pool_match(request.user, pool_match):
        raise PermissionDenied(
            "You are not assigned to score this fixture."
        )
    return redirect("pool:match_detail", pk=pool_match.pk)


@login_required
@require_POST
def match_expire(request, match_id):
    """Poll the authoritative timer for one scorer-authorised fixture."""

    pool_match = get_object_or_404(
        PoolMatch.objects.select_related("competition"),
        pk=match_id,
    )
    if not user_can_score_pool_match(request.user, pool_match):
        raise PermissionDenied("You are not assigned to score this fixture.")
    try:
        updated_match = expire_match_if_due(
            pool_match,
            recorded_by=request.user,
            reason="Authoritative scorer timer check.",
        )
    except PoolResultValidationError as error:
        return JsonResponse(
            {"ok": False, "error": _validation_message(error)},
            status=400,
        )
    return JsonResponse(
        {
            "ok": True,
            "status": updated_match.status,
            "home_score": updated_match.home_score,
            "away_score": updated_match.away_score,
            "time_expired_tied": bool(updated_match.time_expired_tied),
            "completion_reason": updated_match.completion_reason,
            "timer_deadline": (
                updated_match.timer_deadline.isoformat()
                if updated_match.timer_deadline
                else None
            ),
        }
    )


@login_required
def team_match_score(request, match_id):
    """Legacy team scorer URL; retain it as a checked generic-score redirect."""

    pool_match = get_object_or_404(
        PoolMatch.objects.select_related("competition"),
        pk=match_id,
    )
    if not is_team_fixture(pool_match):
        raise Http404("Team scoring is not available for this fixture.")
    if not user_can_score_pool_match(request.user, pool_match):
        raise PermissionDenied("You are not assigned to score this team fixture.")
    return redirect("pool:match_detail", pk=pool_match.pk)


def _match_manager_redirect(competition, pool_match):
    return redirect(
        "pool:manage_match",
        slug=competition.slug,
        match_id=pool_match.pk,
    )


def _uses_match_detail_scoring_surface(request):
    """Accept only the explicit inline scoring surface marker.

    This is intentionally a fixed value rather than a caller-supplied URL.
    It lets a competition manager score from the public fixture surface
    without changing the manager-page redirect behavior.
    """

    return request.POST.get("scoring_surface") == "match_detail"


def _match_operation_redirect(request, competition, pool_match):
    """Return an operator to its only permitted match page."""

    if _uses_match_detail_scoring_surface(request):
        return redirect("pool:match_detail", pk=pool_match.pk)
    if user_can_manage_competition(request.user, competition):
        return _match_manager_redirect(competition, pool_match)
    return redirect("pool:match_detail", pk=pool_match.pk)


def _team_operation_redirect(request, competition, pool_match):
    """Compatibility wrapper for existing team-operation call sites."""

    return _match_operation_redirect(request, competition, pool_match)


def _completed_match_operation_redirect(
    request,
    competition,
    pool_match,
    *,
    was_finished,
):
    """Take an operator to the public result only after this rack finished it.

    Result services deliberately update a locked, freshly read match object.
    Refresh the caller's object before deciding where to send the scorer so a
    last rack cannot be mistaken for an ordinary live-rack operation.
    """

    pool_match.refresh_from_db(fields=["status"])
    if (
        not was_finished
        and pool_match.status == PoolMatch.Status.FINISHED
        and competition.visibility == PoolCompetition.Visibility.PUBLISHED
    ):
        return redirect("pool:match_detail", pk=pool_match.pk)
    return _match_operation_redirect(request, competition, pool_match)


def _wants_scoring_json(request):
    """Recognise the progressive scorer enhancement without changing POST fallback."""

    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
        or request.headers.get("X-Pool-Scoring") == "1"
    )


@login_required
def match_manual_final_result(request, slug, match_id):
    """Manager-only offline result form for an otherwise unstarted fixture."""

    competition = _managed_competition_or_404(request, slug)
    pool_match = _competition_match_or_404(competition, match_id)
    unavailable_reason = ""
    if pool_match.status == PoolMatch.Status.LIVE or PoolRack.objects.filter(
        match=pool_match
    ).exists():
        unavailable_reason = (
            "This fixture has live or detailed rack history. Use the existing rack correction workflow instead of replacing it with an aggregate result."
        )
    elif (
        pool_match.status == PoolMatch.Status.FINISHED
        and pool_match.result_entry_method
        != PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT
    ):
        unavailable_reason = (
            "This completed result was recorded through the detailed result workflow. Use result correction rather than manual aggregate entry."
        )
    form = PoolManualFinalResultForm(request.POST or None, match=pool_match)
    if not unavailable_reason and request.method == "POST" and form.is_valid():
        try:
            updated_match = record_manual_final_result(
                pool_match,
                home_score=form.cleaned_data["home_score"],
                away_score=form.cleaned_data["away_score"],
                home_outcomes={
                    key: form.cleaned_data[f"home_{key}"]
                    for key in ("green_apples", "red_apples", "one_visits")
                },
                away_outcomes={
                    key: form.cleaned_data[f"away_{key}"]
                    for key in ("green_apples", "red_apples", "one_visits")
                },
                recorded_by=request.user,
                reason=form.cleaned_data["reason"],
            )
        except PoolResultValidationError as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, "Manual final result saved.")
            return redirect(
                "pool:manage_competition_fixtures", slug=competition.slug
            )
    return render(
        request,
        "pool/manage/manual_final_result_form.html",
        _manager_context(
            request,
            competition,
            match=pool_match,
            form=form,
            manual_result_unavailable_reason=unavailable_reason,
            page_title=(
                "Update manual final result"
                if (
                    pool_match.status == PoolMatch.Status.FINISHED
                    and pool_match.result_entry_method
                    == PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT
                )
                else "Enter manual final result"
            ),
            page_navigation_back_url=reverse(
                "pool:manage_competition_fixtures", args=[competition.slug]
            ),
        ),
    )


def _scoring_json_payload(pool_match, *, rack=None, scheduled_rack=None):
    """Return the small authoritative state a scorer needs after one rack."""

    refreshed_match = PoolMatch.objects.select_related(
        "winner__player",
        "winner__team",
    ).get(pk=pool_match.pk)
    latest_rack = None
    if rack is not None:
        latest_rack = PoolRack.objects.select_related(
            "winner__player",
            "winner__team",
            "winning_player",
        ).get(pk=rack.pk)
    payload = {
        "ok": True,
        "home_score": refreshed_match.home_score,
        "away_score": refreshed_match.away_score,
        "status": refreshed_match.status,
        "status_label": refreshed_match.get_status_display(),
        "race_target": refreshed_match.race_target,
        "finished": refreshed_match.status == PoolMatch.Status.FINISHED,
        "winner": refreshed_match.winner_participant_display_name or None,
        "time_expired_tied": bool(refreshed_match.time_expired_tied),
        "completion_reason": refreshed_match.completion_reason,
        "timer_deadline": (
            refreshed_match.timer_deadline.isoformat()
            if refreshed_match.timer_deadline
            else None
        ),
        "can_record_racks": (
            refreshed_match.status == PoolMatch.Status.LIVE
            and not refreshed_match.time_expired_tied
        ),
        "latest_rack": (
            {
                "id": latest_rack.pk,
                "number": latest_rack.rack_number,
                "winner": latest_rack.winner.display_name,
                "winning_player": (
                    latest_rack.winning_player.full_name
                    if latest_rack.winning_player_id
                    else None
                ),
                "outcome": latest_rack.get_outcome_type_display(),
            }
            if latest_rack is not None
            else None
        ),
    }
    if scheduled_rack is not None:
        scheduled = PoolTeamMatchScheduledRack.objects.select_related(
            "result__winner__player",
            "result__winner__team",
            "result__winning_player",
        ).get(pk=scheduled_rack.pk)
        next_pending = (
            PoolTeamMatchScheduledRack.objects.filter(
                match=refreshed_match,
                status=PoolTeamMatchScheduledRack.Status.PENDING,
            )
            .order_by("sequence_number", "pk")
            .values("pk", "sequence_number", "home_position", "away_position")
            .first()
        )
        payload["scheduled_rack"] = {
            "id": scheduled.pk,
            "sequence_number": scheduled.sequence_number,
            "status": scheduled.status,
            "status_label": scheduled.get_status_display(),
            "result_rack_number": (
                scheduled.result.rack_number if scheduled.result_id else None
            ),
        }
        payload["next_scheduled_rack"] = next_pending
    return payload


@login_required
@require_POST
def match_start(request, slug, match_id):
    competition, pool_match = _live_scorable_match_or_404(
        request,
        slug,
        match_id,
    )

    try:
        start_match(
            pool_match,
            recorded_by=request.user,
        )
    except PoolResultValidationError as error:
        messages.error(
            request,
            _validation_message(error),
        )
    else:
        messages.success(request, "Match is now live.")

    return _match_operation_redirect(
        request,
        competition,
        pool_match,
    )

@login_required
@require_POST
def match_scorer_add(request, slug, match_id):
    competition = _managed_competition_or_404(request, slug)
    pool_match = _competition_match_or_404(competition, match_id)

    form = PoolMatchScorerForm(
        request.POST,
        match=pool_match,
    )

    if form.is_valid():
        try:
            assignment = form.save(assigned_by=request.user)
        except (IntegrityError, ValidationError) as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(
                request,
                (
                    f"{assignment.user.get_username()} is now assigned "
                    "to score this fixture."
                ),
            )
    else:
        messages.error(
            request,
            "Enter the exact username or email of an active account.",
        )

    return _match_operation_redirect(request, competition, pool_match)


@login_required
@require_POST
def match_scorer_toggle(
    request,
    slug,
    match_id,
    assignment_id,
):
    competition = _managed_competition_or_404(request, slug)
    pool_match = _competition_match_or_404(competition, match_id)

    assignment = get_object_or_404(
        PoolMatchScorerAssignment.objects.select_related("user"),
        pk=assignment_id,
        match=pool_match,
    )

    assignment.is_active = not assignment.is_active
    assignment.save(
        update_fields=[
            "is_active",
            "updated_at",
        ]
    )

    messages.success(
        request,
        (
            f"{assignment.user.get_username()} is now "
            f"{'active' if assignment.is_active else 'inactive'} "
            "for this fixture."
        ),
    )

    return _match_operation_redirect(request, competition, pool_match)


# Compatibility imports/routes use these former team-only symbols.  They now
# operate identically for singles and team fixtures.
team_match_scorer_add = match_scorer_add
team_match_scorer_toggle = match_scorer_toggle

@login_required
@require_POST
def match_record_rack(request, slug, match_id):
    competition, pool_match = _rack_scoring_match_or_404(
        request,
        slug,
        match_id,
    )
    wants_json = _wants_scoring_json(request)
    if is_team_fixture(pool_match) and PoolTeamMatchScheduledRack.objects.filter(
        match=pool_match
    ).exists():
        if wants_json:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Use team match operations to complete a scheduled player-versus-player rack.",
                },
                status=400,
            )
        messages.error(
            request,
            "Use team match operations to complete a scheduled player-versus-player rack.",
        )
        return _match_operation_redirect(request, competition, pool_match)
    form = PoolRackResultForm(request.POST, match=pool_match)
    was_finished = pool_match.status == PoolMatch.Status.FINISHED
    rack_recorded = False
    if form.is_valid():
        try:
            rack_values = {
                "outcome_type": form.cleaned_data["outcome_type"],
                "breaker": form.cleaned_data["breaker"],
                "recorded_by": request.user,
                "reason": form.cleaned_data["reason"],
                "request_token": form.cleaned_data.get("request_token"),
            }
            if form.is_team_match:
                rack_values["winning_player"] = form.cleaned_data["winning_player"]
            else:
                rack_values["winner"] = form.cleaned_data["winner"]
            rack = record_rack(pool_match, **rack_values)
        except PoolResultValidationError as error:
            if wants_json:
                return JsonResponse(
                    {"ok": False, "error": _validation_message(error)},
                    status=400,
                )
            messages.error(request, _validation_message(error))
        else:
            rack_recorded = True
            if wants_json:
                payload = _scoring_json_payload(pool_match, rack=rack)
                payload["next_request_token"] = str(uuid4())
                return JsonResponse(payload)
            messages.success(request, f"Rack {rack.rack_number} was recorded.")
    else:
        if wants_json:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Please correct the rack result and try again.",
                    "errors": form.errors.get_json_data(),
                },
                status=400,
            )
        messages.error(request, "Please correct the rack result and try again.")
    if rack_recorded:
        return _completed_match_operation_redirect(
            request,
            competition,
            pool_match,
            was_finished=was_finished,
        )
    return _match_operation_redirect(request, competition, pool_match)


@login_required
@require_POST
def match_team_rack_start(request, slug, match_id):
    competition, pool_match = _live_scorable_team_match_or_404(
        request,
        slug,
        match_id,
    )
    if not is_team_fixture(pool_match):
        raise Http404("Team rack operations are not available for this fixture.")
    form = PoolTeamScheduledRackStartForm(request.POST, match=pool_match)
    if form.is_valid():
        try:
            scheduled_rack = start_next_eligible_team_rack(
                pool_match,
                table_number=form.cleaned_data["table_number"],
                actor=request.user,
            )
        except PoolResultValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            if scheduled_rack.status == PoolTeamMatchScheduledRack.Status.VOID:
                messages.success(
                    request,
                    f"Scheduled rack {scheduled_rack.sequence_number} was marked void because both positions are vacant.",
                )
            elif scheduled_rack.status == PoolTeamMatchScheduledRack.Status.COMPLETED:
                messages.success(
                    request,
                    f"Scheduled rack {scheduled_rack.sequence_number} was awarded for a lineup vacancy.",
                )
            else:
                messages.success(
                    request,
                    f"Scheduled rack {scheduled_rack.sequence_number} is live on table {scheduled_rack.table_number}.",
                )
    else:
        messages.error(request, "Choose a valid available physical table.")
    return _team_operation_redirect(
        request,
        competition,
        pool_match,
    )

@login_required
@require_POST
def match_team_rack_complete(request, slug, match_id, scheduled_rack_id):
    competition, pool_match = _live_scorable_team_match_or_404(
        request,
        slug,
        match_id,
    )
    if not is_team_fixture(pool_match):
        raise Http404("Team rack operations are not available for this fixture.")
    wants_json = _wants_scoring_json(request)
    scheduled_rack = get_object_or_404(
        PoolTeamMatchScheduledRack.objects.select_related("home_player", "away_player"),
        pk=scheduled_rack_id,
        match=pool_match,
    )
    form = PoolTeamScheduledRackResultForm(request.POST, scheduled_rack=scheduled_rack)
    was_finished = pool_match.status == PoolMatch.Status.FINISHED
    rack_completed = False
    if form.is_valid():
        try:
            completed = complete_team_match_rack(
                pool_match,
                scheduled_rack,
                winning_player=form.cleaned_data["winner_player"],
                outcome_type=form.cleaned_data["outcome_type"],
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except PoolResultValidationError as error:
            if wants_json:
                return JsonResponse(
                    {"ok": False, "error": _validation_message(error)},
                    status=400,
                )
            messages.error(request, _validation_message(error))
        else:
            rack_completed = True
            if wants_json:
                rack = completed.result if completed.result_id else None
                return JsonResponse(
                    _scoring_json_payload(
                        pool_match,
                        rack=rack,
                        scheduled_rack=completed,
                    )
                )
            messages.success(
                request,
                f"Scheduled rack {completed.sequence_number} was completed.",
            )
    else:
        if wants_json:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Choose the winner and a valid rack outcome.",
                    "errors": form.errors.get_json_data(),
                },
                status=400,
            )
        messages.error(request, "Choose the winner and a valid rack outcome.")
    if rack_completed:
        return _completed_match_operation_redirect(
            request,
            competition,
            pool_match,
            was_finished=was_finished,
        )
    return _team_operation_redirect(
        request,
        competition,
        pool_match,
    )

@login_required
@require_POST
def match_team_rack_correct(request, slug, match_id, scheduled_rack_id):
    competition = _managed_competition_or_404(request, slug)
    pool_match = _competition_match_or_404(competition, match_id)
    if not is_team_fixture(pool_match):
        raise Http404("Team rack operations are not available for this fixture.")
    scheduled_rack = get_object_or_404(
        PoolTeamMatchScheduledRack.objects.select_related(
            "home_player",
            "away_player",
            "result",
        ),
        pk=scheduled_rack_id,
        match=pool_match,
        result__isnull=False,
    )
    form = PoolTeamScheduledRackCorrectionForm(
        request.POST,
        scheduled_rack=scheduled_rack,
    )
    if form.is_valid():
        try:
            corrected = correct_completed_team_match_rack(
                pool_match,
                scheduled_rack,
                winning_player=form.cleaned_data["winner_player"],
                outcome_type=form.cleaned_data["outcome_type"],
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except PoolResultValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(
                request,
                f"Scheduled rack {scheduled_rack.sequence_number} was corrected.",
            )
    else:
        messages.error(request, "Choose a valid snapshot player, outcome, and correction reason.")
    return _match_operation_redirect(request, competition, pool_match)


@login_required
@require_POST
def match_team_substitute(request, slug, match_id, entry_id):
    # Existing team scorers may operate a live substitution as part of the
    # score-sheet workflow.  This is intentionally still exact-fixture scope
    # and never grants roster or lineup-management access elsewhere.
    competition, pool_match = _live_scorable_team_match_or_404(
        request,
        slug,
        match_id,
    )
    if not is_team_fixture(pool_match):
        raise Http404("Team substitutions are not available for this fixture.")
    entry = _team_match_entry_or_404(pool_match, entry_id)
    lineup = PoolTeamMatchLineup.objects.filter(
        match=pool_match,
        team_entry=entry,
    ).first()
    busy_player_ids = {
        player_id
        for scheduled_rack in team_match_schedule_state(pool_match)["live"]
        for player_id in (
            scheduled_rack.home_player_id,
            scheduled_rack.away_player_id,
        )
        if player_id is not None
    }
    form = PoolTeamMatchSubstitutionForm(
        request.POST,
        match=pool_match,
        team_entry=entry,
        lineup=lineup,
        busy_player_ids=busy_player_ids,
    )
    if form.is_valid():
        try:
            substitution = substitute_team_match_player(
                pool_match,
                entry,
                position=form.cleaned_data["position"],
                incoming_player=form.cleaned_data["incoming_player"],
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except PoolResultValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(
                request,
                f"Position {substitution.position} now has {substitution.incoming_player.full_name}.",
            )
    else:
        messages.error(request, "Choose a valid position and an eligible substitute.")
    return _team_operation_redirect(
        request,
        competition,
        pool_match,
    )


@login_required
@require_POST
def match_team_fill_vacancy(request, slug, match_id, entry_id):
    """Let an exact-fixture scorer fill an empty live lineup position only."""

    competition, pool_match = _live_scorable_team_match_or_404(
        request,
        slug,
        match_id,
    )
    if not is_team_fixture(pool_match):
        raise Http404("Team vacancy operations are not available for this fixture.")
    entry = _team_match_entry_or_404(pool_match, entry_id)
    lineup = PoolTeamMatchLineup.objects.filter(
        match=pool_match,
        team_entry=entry,
    ).first()
    busy_player_ids = {
        player_id
        for scheduled_rack in team_match_schedule_state(pool_match)["live"]
        for player_id in (
            scheduled_rack.home_player_id,
            scheduled_rack.away_player_id,
        )
        if player_id is not None
    }
    form = PoolTeamMatchVacancyFillForm(
        request.POST,
        match=pool_match,
        team_entry=entry,
        lineup=lineup,
        busy_player_ids=busy_player_ids,
    )
    if form.is_valid():
        try:
            filled_position = fill_team_match_vacancy(
                pool_match,
                entry,
                position=form.cleaned_data["position"],
                incoming_player=form.cleaned_data["incoming_player"],
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except PoolResultValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(
                request,
                (
                    f"Position {filled_position.position} is now filled by "
                    f"{filled_position.player.full_name}."
                ),
            )
    else:
        messages.error(
            request,
            "Choose a currently vacant position and an eligible roster player.",
        )
    return _team_operation_redirect(
        request,
        competition,
        pool_match,
    )

@login_required
@require_POST
def match_open_correction(request, slug, match_id):
    competition = _managed_competition_or_404(request, slug)
    pool_match = _competition_match_or_404(competition, match_id)
    form = PoolResultCorrectionForm(request.POST)
    if form.is_valid():
        try:
            open_result_correction(
                pool_match,
                recorded_by=request.user,
                reason=form.cleaned_data["reason"],
            )
        except PoolResultValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(request, "Correction mode is now open for this result.")
    else:
        messages.error(request, "Provide a reason before opening correction mode.")
    return _match_operation_redirect(request, competition, pool_match)


@login_required
@require_POST
def match_correct_rack(request, slug, match_id, rack_id):
    competition, pool_match = _scoreable_match_or_404(request, slug, match_id)
    if (
        not user_can_manage_competition(request.user, competition)
        and pool_match.status != PoolMatch.Status.LIVE
    ):
        # An assigned scorer may repair a live accidental result, but cannot
        # enter the manager-only finished-result correction workflow.
        raise Http404("Match not found.")
    rack = get_object_or_404(PoolRack, pk=rack_id, match=pool_match)
    if is_team_fixture(pool_match) and hasattr(rack, "team_scheduled_rack"):
        messages.error(
            request,
            "Correct this scheduled team rack through team match operations so its player snapshots remain authoritative.",
        )
        return _match_operation_redirect(request, competition, pool_match)
    form = PoolRackCorrectionForm(
        request.POST,
        match=pool_match,
        preserved_winning_player=rack.winning_player,
        preserved_winner_entry=rack.winner,
        require_complete_team_rosters=False,
    )
    if form.is_valid():
        try:
            correction_values = {
                "outcome_type": form.cleaned_data["outcome_type"],
                "breaker": form.cleaned_data["breaker"],
                "recorded_by": request.user,
                "reason": form.cleaned_data["reason"],
            }
            if form.is_team_match:
                correction_values["winning_player"] = form.cleaned_data[
                    "winning_player"
                ]
            else:
                correction_values["winner"] = form.cleaned_data["winner"]
            corrected = correct_rack(pool_match, rack, **correction_values)
        except PoolResultValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(request, f"Rack {corrected.rack_number} was corrected.")
    else:
        messages.error(request, "Please correct the rack information and try again.")
    return _match_operation_redirect(request, competition, pool_match)


@login_required
@require_POST
def match_undo_rack(request, slug, match_id, rack_id):
    competition, pool_match = _scoreable_match_or_404(request, slug, match_id)
    if (
        not user_can_manage_competition(request.user, competition)
        and pool_match.status != PoolMatch.Status.LIVE
    ):
        raise Http404("Match not found.")
    rack = get_object_or_404(PoolRack, pk=rack_id, match=pool_match)
    try:
        undo_rack(
            pool_match,
            rack,
            recorded_by=request.user,
            reason=request.POST.get("reason", "").strip(),
        )
    except PoolResultValidationError as error:
        messages.error(request, _validation_message(error))
    else:
        messages.success(request, "Rack was removed and the score was synchronised.")
    return _match_operation_redirect(request, competition, pool_match)


@login_required
@require_POST
def match_non_played_result(request, slug, match_id):
    competition = _managed_competition_or_404(request, slug)
    pool_match = _competition_match_or_404(competition, match_id)
    form = PoolNonPlayedResultForm(request.POST, match=pool_match)
    if form.is_valid():
        try:
            record_non_played_result(
                pool_match,
                result_method=form.cleaned_data["result_method"],
                winner=form.cleaned_data["winner"],
                recorded_by=request.user,
                reason=form.cleaned_data["reason"],
            )
        except PoolResultValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(request, "Non-played result was recorded.")
    else:
        messages.error(request, "Choose a valid result method and winner.")
    return _match_operation_redirect(request, competition, pool_match)


@login_required
@require_POST
def match_transition(request, slug, match_id, status):
    competition = _managed_competition_or_404(request, slug)
    pool_match = _competition_match_or_404(competition, match_id)
    allowed_statuses = {
        PoolMatch.Status.POSTPONED,
        PoolMatch.Status.CANCELLED,
        PoolMatch.Status.ABANDONED,
    }
    if status not in allowed_statuses:
        raise PermissionDenied("This match transition is not available.")
    form = PoolMatchTransitionForm(request.POST)
    if form.is_valid():
        try:
            updated_match = transition_match_status(
                pool_match,
                status=status,
                recorded_by=request.user,
                reason=form.cleaned_data["reason"],
            )
        except PoolResultValidationError as error:
            messages.error(request, _validation_message(error))
        else:
            messages.success(
                request,
                f"Match marked as {updated_match.get_status_display().lower()}.",
            )
    else:
        messages.error(request, "Please correct the match transition details.")
    return _match_operation_redirect(request, competition, pool_match)


@login_required
def competition_admins(request, slug):
    competition = _managed_competition_or_404(request, slug)
    _require_owner(request, competition)
    memberships = (
        PoolCompetitionMembership.objects.filter(
            competition=competition,
            role__in=(
                PoolCompetitionMembership.Role.OWNER,
                PoolCompetitionMembership.Role.ADMIN,
            ),
        )
        .select_related("user")
        .order_by("role", "-is_active", "user__username", "id")
    )
    return render(
        request,
        "pool/manage/admin_list.html",
        _manager_context(request, competition, memberships=memberships),
    )


@login_required
def competition_admin_add(request, slug):
    competition = _managed_competition_or_404(request, slug)
    _require_owner(request, competition)
    form = PoolCompetitionAdminForm(request.POST or None, competition=competition)
    if request.method == "POST" and form.is_valid():
        administrator = form.cleaned_data["user"]
        PoolCompetitionMembership.objects.update_or_create(
            competition=competition,
            user=administrator,
            defaults={
                "role": PoolCompetitionMembership.Role.ADMIN,
                "is_active": True,
            },
        )
        messages.success(request, "Competition administrator was added.")
        return redirect("pool:manage_competition_admins", slug=competition.slug)
    return render(
        request,
        "pool/manage/admin_form.html",
        _manager_context(request, competition, form=form),
    )


@login_required
@require_POST
def competition_admin_toggle(request, slug, membership_id):
    competition = _managed_competition_or_404(request, slug)
    _require_owner(request, competition)
    membership = get_object_or_404(
        PoolCompetitionMembership.objects.select_related("user"),
        pk=membership_id,
        competition=competition,
        role=PoolCompetitionMembership.Role.ADMIN,
    )
    membership.is_active = not membership.is_active
    membership.save(update_fields=("is_active", "updated_at"))
    state = "reactivated" if membership.is_active else "deactivated"
    messages.success(request, f"{membership.user.get_username()} was {state}.")
    return redirect("pool:manage_competition_admins", slug=competition.slug)


@login_required
def competition_preview(request, slug):
    competition = _managed_competition_or_404(request, slug)
    return redirect(
        f"{reverse('pool:competition_detail', args=[competition.slug])}?preview=1"
    )
