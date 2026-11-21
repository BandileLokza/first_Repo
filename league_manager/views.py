import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import HttpResponse
from django.core.paginator import Paginator
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from types import SimpleNamespace
from requests.exceptions import RequestException

from matches.forms import MatchResultForm
from matches.finished_scorers import finished_scorer_eligible
from matches.abandonment import (
    AbandonedMatchResolutionError,
    award_official_result,
    confirm_score_at_abandonment,
    reopen_abandoned_resolution,
    resume_abandoned_match,
    schedule_full_replay,
    void_abandoned_match,
)
from matches.bracket import (
    bracket_ties_for_competition,
    competition_has_bracket,
    create_bracket_structure,
    generate_bracket_matches,
    random_seed_bracket,
    record_blocked_bracket_correction,
    save_bracket_links,
    save_manual_seeding,
    save_bracket_schedule,
)
from matches.result_corrections import (
    RESULT_CONFLICT,
    RESULT_SAVE_FAILED,
    SCORE_FIELDS,
    is_lock_conflict,
    result_snapshot,
)
from matches.services import (
    OFFICIAL_ABANDONMENT_RESULT_RESOLUTIONS,
    finalize_match_result_fast,
)
from matches.result_outbox import RESULT_SAVED, lock_result_competition
from leagues.models import Competition, CompetitionSeries, FootballOrganisation, Group, Region, ReportDocumentSettings, Stage
from matches.models import (
    FootballKnockoutRoundRule,
    FootballKnockoutTieSlot,
    Match,
    MatchAdminAssignment,
    Player,
    Goal,
    YellowCard,
    AbandonedMatchDecision,
)
from teams.models import Team, TeamCoachAssignment

from .forms import (
    AgentAssignmentForm,
    AbandonedMatchResolutionForm,
    AgentRegistrationForm,
    BracketLinkForm,
    BracketRandomDrawForm,
    BracketScheduleForm,
    BracketSeedingForm,
    FixtureForm,
    FixtureLetterheadForm,
    ReportCommitteeFormSet,
    ReportDocumentSettingsForm,
    LeagueAdminRegistrationForm,
    NextCompetitionSeasonForm,
    TeamCoachRegistrationForm,
    KnockoutRoundsForm,
    KnockoutRoundRuleForm,
    PlayerForm,
    TeamForm,
    GroupForm,
    LeagueRegionForm,
    LeagueBrandingForm,
    RegionalLeagueOwnerForm,
    ScheduleUnlockForm,
)
from .models import (
    ActivityLog,
    LeagueMembership,
    OrganisationAdministrator,
    OrganisationReportPreference,
    RegionalReportPreference,
)
from .permissions import (
    MANAGER_ROLES,
    competition_is_read_only_for,
    get_active_membership,
    user_is_league_owner,
    user_manages_series,
    user_manages_competition,
    can_delete_fixture,
    can_manage_abandonment_decisions,
    can_manage_organisation,
    can_manage_region,
    managed_competitions_queryset,
)
from .services import record_activity
from .fixture_deletion import (
    FixtureDeletionBlocked,
    delete_fixtures,
    fixture_deletion_block_reason,
)
from leagues.services import (
    configure_knockout_rounds,
    ensure_competition_structure,
)
from leagues.seasons import (
    archive_competition_season,
    create_next_competition_season,
)
from leagues.fixtures import (
    build_generation_preview,
    confirm_generation_preview,
    round_generation_state,
)
from .regional_reports import (
    ReportExportError,
    FIXTURE_EXPORT_SCOPES,
    FIXTURE_SCOPE_FIRST_ROUND,
    FIXTURE_SCOPE_FULL_SEASON,
    FIXTURE_SCOPE_LABELS,
    FIXTURE_SCOPE_NEXT_UPCOMING,
    FIXTURE_SCOPE_SECOND_ROUND,
    WEEKLY_SELECTION_AUTOMATIC,
    WEEKLY_SELECTION_MANUAL,
    WEEKLY_SELECTION_MODES,
    build_regional_report_data,
    build_scope_report_data,
    report_letterheads_for,
    report_stationery_owner,
    build_organisation_report_data,
    manual_fixture_gameweek_options,
    render_docx,
    render_pdf,
    report_filename,
    selected_competitions,
    selected_organisation_competitions,
)


logger = logging.getLogger(__name__)


def get_managed_league(request, slug):
    """
    Return a competition only when the current user is permitted
    to manage it.

    Match agents are not considered league managers.
    """
    league = get_object_or_404(
        Competition,
        slug=slug,
    )

    if user_manages_competition(request.user, league):
        return league

    # Let existing owners/admins read their historical manager pages, where
    # the dashboard makes the archived state explicit.  Every non-safe method
    # remains centrally denied here, while scorer and coach endpoints are
    # independently guarded by the permission helpers.
    membership = get_active_membership(request.user, league)
    has_historical_manager_access = bool(
        membership and membership.role in MANAGER_ROLES
    )
    if (
        competition_is_read_only_for(request.user, league)
        and has_historical_manager_access
        and request.method in {"GET", "HEAD", "OPTIONS"}
    ):
        return league

    if (
        competition_is_read_only_for(request.user, league)
        and has_historical_manager_access
    ):
        raise PermissionDenied("Archived season — historical data is read-only.")

    raise PermissionDenied("You do not have permission to manage this league.")


def user_has_direct_league_owner_role(user, league):
    """Direct ownership only, for operations that can move a league's scope."""
    return LeagueMembership.objects.filter(
        user=user,
        competition=league,
        role=LeagueMembership.Role.OWNER,
        is_active=True,
    ).exists()

def get_group_stage_for_league(league):
    if league.format_type != "GROUP_KNOCKOUT":
        raise PermissionDenied(
            "Groups are only available for Group + Knockout competitions."
        )

    return get_object_or_404(
        Stage,
        competition=league,
        stage_type="GROUP",
    )

def fixture_snapshot(match):
    return {
        "match_id": match.id,
        "stage_id": match.stage_id,
        "stage": match.stage.name if match.stage else None,
        "group_id": match.group_id,
        "group": match.group.name if match.group else None,
        "home_team_id": match.home_team_id,
        "home_team": match.home_team.name,
        "away_team_id": match.away_team_id,
        "away_team": match.away_team.name,
        "match_date": match.match_date.isoformat() if match.match_date else None,
        "location": match.location or "",
        "gameweek": match.gameweek,
        "leg": match.leg,
        "status": match.status,
        "generated_by_system": match.generated_by_system,
    }


def _schedule_lock_snapshot(league):
    """Keep lock audits meaningful if league metadata is later renamed."""

    series = league.series
    return {
        "competition_id": league.id,
        "competition": league.name,
        "season": league.season,
        "series_id": series.id,
        "series": series.name,
        "league_type": series.league_type,
        "region": series.region.name if series.region_id else None,
        "fixture_count": Match.objects.filter(competition=league).count(),
        "schedule_locked": bool(league.schedule_locked_at),
        "schedule_locked_at": (
            league.schedule_locked_at.isoformat() if league.schedule_locked_at else None
        ),
        "schedule_locked_by_id": league.schedule_locked_by_id,
    }


def _schedule_lock_competition_or_403(request, slug):
    if not request.user.is_superuser:
        raise PermissionDenied("Only superusers may lock or unlock a competition schedule.")
    return get_object_or_404(
        Competition.objects.select_related("series", "series__region", "schedule_locked_by"),
        slug=slug,
    )


ORGANISATION_SCOPE_DETAILS = {
    CompetitionSeries.LeagueType.EWFA: {
        "name": "EWFA",
        "dashboard_title": "Manage EWFA Leagues",
        "description": "League owners and downloadable fixture, results, and standings reports for EWFA.",
        "report_description": "EWFA fixture, results, and standings report",
        "empty_message": "There are no EWFA leagues yet.",
    },
    CompetitionSeries.LeagueType.PLE: {
        "name": "PLE",
        "dashboard_title": "Manage PLE Leagues",
        "description": "League owners and downloadable fixture, results, and standings reports for the Premier League of Eswatini.",
        "report_description": "Premier League of Eswatini (PLE) fixture, results, and standings report",
        "empty_message": "There are no PLE leagues yet.",
    },
    CompetitionSeries.LeagueType.ZONAL: {
        "name": "Zonal Leagues",
        "dashboard_title": "Manage Zonal Leagues",
        "description": "League owners and downloadable fixture, results, and standings reports for Zonal Leagues.",
        "report_description": "Zonal Leagues fixture, results, and standings report",
        "empty_message": "There are no Zonal Leagues yet.",
    },
}


def _regional_scope(region):
    return SimpleNamespace(
        name=region.name,
        key=region.slug,
        dashboard_title=f"Manage {region.name} Region",
        administration_label=f"{region.name} Regional Administration",
        description="League owners and downloadable fixture, results, and standings reports for this region.",
        report_description=f"{region.name} regional newspaper submission",
        empty_message="There are no regional leagues in this region yet.",
        owner_assign_route="league_manager:regional_league_owner_assign",
        owner_remove_route="league_manager:regional_league_owner_remove",
        report_route="league_manager:regional_report_export",
    )


def _organisation_scope(organisation):
    details = ORGANISATION_SCOPE_DETAILS[organisation]
    return SimpleNamespace(
        key=organisation.lower(),
        organisation=organisation,
        owner_assign_route="league_manager:organisation_league_owner_assign",
        owner_remove_route="league_manager:organisation_league_owner_remove",
        report_route="league_manager:organisation_report_export",
        administration_label=details["dashboard_title"],
        **details,
    )


def _organisation_values_for(user):
    if user.is_superuser:
        return list(ORGANISATION_SCOPE_DETAILS)
    return list(
        OrganisationAdministrator.objects.filter(
            user=user,
            is_active=True,
            organisation__in=list(ORGANISATION_SCOPE_DETAILS),
        ).order_by("organisation").values_list("organisation", flat=True)
    )


@login_required
def dashboard(request):
    if request.user.is_superuser:
        regional_regions = Region.objects.filter(is_active=True).order_by(
            "display_order", "name", "id"
        )
    else:
        regional_regions = (
            Region.objects.filter(
                is_active=True,
                regional_administrators__user=request.user,
                regional_administrators__is_active=True,
            )
            .distinct()
            .order_by("display_order", "name", "id")
        )

    organisation_values = _organisation_values_for(request.user)
    organisation_scopes = [
        _organisation_scope(organisation)
        for organisation in organisation_values
    ]

    authorized_leagues = managed_competitions_queryset(request.user)
    has_league_scope = authorized_leagues.exists()
    league_search_query = (request.GET.get("q") or "").strip()
    leagues = authorized_leagues
    if league_search_query:
        leagues = leagues.filter(name__icontains=league_search_query)

    leagues = (
        leagues
        .annotate(
            team_count=Count(
                "teams",
                distinct=True,
            ),
            match_count=Count(
                "matches",
                distinct=True,
            ),
            live_match_count=Count(
                "matches",
                filter=Q(matches__status="LIVE"),
                distinct=True,
            ),
            agent_count=Count(
                "memberships",
                filter=Q(
                    memberships__role=LeagueMembership.Role.AGENT,
                    memberships__is_active=True,
                ),
                distinct=True,
            ),
        )
        .order_by("name")
    )

    if (
        not request.user.is_superuser
        and not has_league_scope
        and not regional_regions.exists()
        and not organisation_scopes
    ):
        raise PermissionDenied(
            "You do not manage any league."
        )

    return render(
        request,
        "league_manager/dashboard.html",
        {
            "managed_leagues": leagues,
            "league_search_query": league_search_query,
            "regional_regions": regional_regions,
            "organisation_scopes": organisation_scopes,
        },
    )


def get_managed_region(request, slug):
    region = get_object_or_404(Region, slug=slug, is_active=True)
    if not can_manage_region(request.user, region):
        raise PermissionDenied("You do not have permission to administer this region.")
    return region


def _regional_series(region):
    return CompetitionSeries.objects.filter(
        region=region,
        league_type=CompetitionSeries.LeagueType.REGIONAL,
    ).order_by("name", "id")


def get_managed_organisation(request, organisation):
    organisation = organisation.upper()
    if organisation not in ORGANISATION_SCOPE_DETAILS:
        raise PermissionDenied("Unknown football organisation.")
    if not can_manage_organisation(request.user, organisation):
        raise PermissionDenied("You do not have permission to administer this organisation.")
    return organisation


def _organisation_series(organisation):
    return CompetitionSeries.objects.filter(
        league_type=organisation,
    ).order_by("name", "id")


def _regional_fixture_gameweek_options(region, manual_gameweeks=None):
    competitions = Competition.objects.filter(
        is_archived=False,
        series__region=region,
        series__league_type=CompetitionSeries.LeagueType.REGIONAL,
    ).select_related("series").order_by("series__name", "-id")
    return _manual_gameweek_options_context(competitions, manual_gameweeks)


def _organisation_fixture_gameweek_options(organisation, manual_gameweeks=None):
    competitions = Competition.objects.filter(
        is_archived=False,
        series__league_type=organisation,
    ).select_related("series").order_by("series__name", "-id")
    return _manual_gameweek_options_context(competitions, manual_gameweeks)


def _owner_series_data(series_queryset):
    series_data = []
    for series in series_queryset.prefetch_related("competitions"):
        active_competitions = list(series.competitions.filter(is_archived=False).order_by("-id"))
        owner_memberships = LeagueMembership.objects.filter(
            competition__in=active_competitions,
            role=LeagueMembership.Role.OWNER,
            is_active=True,
        ).select_related("user").order_by("user__username")
        owners = []
        seen = set()
        for membership in owner_memberships:
            if membership.user_id not in seen:
                owners.append(membership.user)
                seen.add(membership.user_id)
        series_data.append({"series": series, "competitions": active_competitions, "owners": owners})
    return series_data


def _assign_owner_to_active_seasons(request, series, *, scope_values):
    form = RegionalLeagueOwnerForm(request.POST)
    if not form.is_valid():
        return None, " ".join(error for errors in form.errors.values() for error in errors)
    owner = form.cleaned_data["user"]
    competitions = list(series.competitions.filter(is_archived=False))
    if not competitions:
        return None, "This league has no active season to receive an owner assignment."
    for competition in competitions:
        LeagueMembership.objects.update_or_create(
            user=owner,
            competition=competition,
            defaults={"role": LeagueMembership.Role.OWNER, "is_active": True},
        )
        record_activity(
            actor=request.user,
            competition=competition,
            action=ActivityLog.Action.STAFF_ADDED,
            description=f"League owner assigned by {scope_values['scope_label']}: {owner}",
            new_values={"user_id": owner.id, "role": LeagueMembership.Role.OWNER, **scope_values},
        )
    return owner, None


def _remove_owner_from_active_seasons(request, series, user_id, *, scope_values):
    memberships = LeagueMembership.objects.select_for_update().filter(
        competition__series=series,
        competition__is_archived=False,
        user_id=user_id,
        role=LeagueMembership.Role.OWNER,
        is_active=True,
    )
    changed = 0
    for membership in memberships:
        membership.is_active = False
        membership.save(update_fields=["is_active", "updated_at"])
        changed += 1
        record_activity(
            actor=request.user,
            competition=membership.competition,
            action=ActivityLog.Action.STAFF_REMOVED,
            description=f"League owner removed by {scope_values['scope_label']}: {membership.user}",
            old_values={"user_id": membership.user_id, "role": LeagueMembership.Role.OWNER, **scope_values},
        )
    return changed


@login_required
def regional_league_owners(request, slug):
    region = get_managed_region(request, slug)
    series_data = _owner_series_data(_regional_series(region))
    return render(request, "league_manager/regional_league_owners.html", {
        "region": region,
        "scope": _regional_scope(region),
        "series_data": series_data,
        "owner_form": RegionalLeagueOwnerForm(),
    })


@login_required
@require_POST
@transaction.atomic
def regional_league_owner_assign(request, slug, series_id):
    region = get_managed_region(request, slug)
    series = get_object_or_404(_regional_series(region), pk=series_id)
    owner, error = _assign_owner_to_active_seasons(
        request,
        series,
        scope_values={"scope_label": "regional administrator", "region_id": region.id},
    )
    if error:
        messages.error(request, error)
        return redirect("league_manager:regional_league_owners", slug=region.slug)
    messages.success(request, f"{owner.get_full_name() or owner.username} is now an owner of {series.name}.")
    return redirect("league_manager:regional_league_owners", slug=region.slug)


@login_required
@require_POST
@transaction.atomic
def regional_league_owner_remove(request, slug, series_id, user_id):
    region = get_managed_region(request, slug)
    series = get_object_or_404(_regional_series(region), pk=series_id)
    changed = _remove_owner_from_active_seasons(
        request,
        series,
        user_id,
        scope_values={"scope_label": "regional administrator", "region_id": region.id},
    )
    if changed:
        messages.success(request, "League owner access removed.")
    else:
        messages.info(request, "That user does not have an active owner assignment for this league.")
    return redirect("league_manager:regional_league_owners", slug=region.slug)


def _report_preference(request, region, report_kind):
    preference, _ = RegionalReportPreference.objects.get_or_create(
        user=request.user,
        region=region,
        report_kind=report_kind,
        defaults={"selection_mode": RegionalReportPreference.SelectionMode.ALL},
    )
    return preference


def _report_selection_from_post(request, preference, series_queryset):
    selection_mode = request.POST.get("selection_mode", preference.SelectionMode.ALL)
    output_format = request.POST.get("output_format", preference.OutputFormat.DOCX)
    if selection_mode not in preference.SelectionMode.values:
        raise ValidationError("Select a valid league selection mode.")
    if output_format not in preference.OutputFormat.values:
        raise ValidationError("Select a valid download format.")
    submitted_ids = {value for value in request.POST.getlist("series") if value}
    allowed = series_queryset.filter(pk__in=submitted_ids)
    if {str(value) for value in allowed.values_list("pk", flat=True)} != submitted_ids:
        raise ValidationError("Only leagues in this administrative scope can be selected.")
    if selection_mode == preference.SelectionMode.CUSTOM and not submitted_ids:
        raise ValidationError("Select at least one league for a custom report.")
    preference.selection_mode = selection_mode
    preference.output_format = output_format
    selected_series = allowed if selection_mode == preference.SelectionMode.CUSTOM else []
    if request.POST.get("action") == "preview" and preference.report_kind != "STANDINGS":
        return list(allowed.values_list("pk", flat=True)) if selection_mode == preference.SelectionMode.CUSTOM else []
    preference.save(update_fields=["selection_mode", "output_format", "updated_at"])
    preference.selected_series.set(selected_series)
    return None


def _fixture_export_scope_from_post(request, report_kind):
    """Validate the additive fixture-only selector without persisting it.

    The saved preference remains limited to the established league and output
    choices. This makes every fresh export page default to the original next
    upcoming gameweek, including old links and requests with no new field.
    """
    if report_kind != "FIXTURES":
        return FIXTURE_SCOPE_NEXT_UPCOMING
    fixture_scope = request.POST.get("fixture_scope", FIXTURE_SCOPE_NEXT_UPCOMING)
    if fixture_scope not in FIXTURE_EXPORT_SCOPES:
        raise ValidationError("Select a valid fixture export scope.")
    return fixture_scope


def _fixture_gameweek_selection_from_post(request, report_kind, fixture_scope, competitions):
    """Validate the automatic/manual weekly choice for each selected league."""
    if report_kind != "FIXTURES" or fixture_scope != FIXTURE_SCOPE_NEXT_UPCOMING:
        return WEEKLY_SELECTION_AUTOMATIC, {}

    mode = request.POST.get("fixture_gameweek_mode", WEEKLY_SELECTION_AUTOMATIC)
    # Keep a previously introduced one-value override usable for posted forms,
    # while new pages submit the explicit per-league manual controls below.
    legacy_override = request.POST.get("gameweek_override", "").strip()
    if "fixture_gameweek_mode" not in request.POST and legacy_override:
        mode = WEEKLY_SELECTION_MANUAL
    if mode not in WEEKLY_SELECTION_MODES:
        raise ValidationError("Select a valid weekly fixture selection mode.")
    if mode == WEEKLY_SELECTION_AUTOMATIC:
        return mode, {}

    manual_gameweeks = {}
    for competition in competitions:
        value = request.POST.get(
            f"manual_gameweek_{competition.id}",
            legacy_override,
        ).strip()
        if not value:
            raise ValidationError(f"Select a gameweek for {competition.name}.")
        try:
            gameweek = int(value)
        except (TypeError, ValueError) as error:
            raise ValidationError(
                f"Select a valid gameweek for {competition.name}."
            ) from error
        if gameweek <= 0 or not competition.matches.filter(gameweek=gameweek).exists():
            raise ValidationError(
                f"Gameweek {gameweek} is not recorded for {competition.name}."
            )
        manual_gameweeks[competition.id] = gameweek
    return mode, manual_gameweeks


def _manual_gameweek_options_context(competitions, manual_gameweeks=None):
    manual_gameweeks = manual_gameweeks or {}
    return [
        {
            **option,
            "selected_gameweek": manual_gameweeks.get(option["competition_id"]),
        }
        for option in manual_fixture_gameweek_options(competitions)
    ]


def _fixture_scope_options():
    return (
        (
            FIXTURE_SCOPE_NEXT_UPCOMING,
            FIXTURE_SCOPE_LABELS[FIXTURE_SCOPE_NEXT_UPCOMING],
            "The existing next-upcoming-gameweek export.",
        ),
        (
            FIXTURE_SCOPE_FIRST_ROUND,
            "Entire First Round",
            "Every recorded first-round league fixture.",
        ),
        (
            FIXTURE_SCOPE_SECOND_ROUND,
            "Entire Second Round",
            "Every recorded return-round league fixture.",
        ),
        (
            FIXTURE_SCOPE_FULL_SEASON,
            "Entire Season",
            "Every recorded first- and second-round league fixture.",
        ),
    )


def _fixture_letterhead_page(request, owner, *, back_url, description, manage_official=True):
    page = {"back_url": back_url, "description": description, "manage_official": manage_official}
    if request.method == "POST":
        with transaction.atomic():
            owners = type(owner).objects.select_for_update()
            if isinstance(owner, CompetitionSeries):
                owners = owners.filter(league_type=owner.league_type, region_id=owner.region_id)
            owner = get_object_or_404(owners, pk=owner.pk)
            return _edit_report_stationery(request, owner, **page)
    return _edit_report_stationery(request, owner, **page)


def _edit_report_stationery(request, owner, *, back_url, description, manage_official):
    post = request.POST if request.method == "POST" else None
    files = request.FILES if post is not None else None
    form = FixtureLetterheadForm(
        post, files, initial={"fixture_letterhead": owner.fixture_letterhead, "report_template": owner.report_template},
    )
    official_form = committee = None
    has_signature = has_stamp = False
    edit_official = manage_official and post is not None and post.get("official_settings") == "1"
    if manage_official:
        owner_filter = ReportDocumentSettings.owner_filter(owner)
        settings = ReportDocumentSettings.objects.filter(**owner_filter).first() or ReportDocumentSettings(**owner_filter)
        has_signature = bool(settings.signature_image)
        has_stamp = bool(settings.stamp_image)
        official_form = ReportDocumentSettingsForm(post if edit_official else None, files if edit_official else None, instance=settings)
        committee = ReportCommitteeFormSet(post if edit_official else None, instance=settings, prefix="committee")
    if post is not None:
        letterhead_valid = form.is_valid()
        official_valid = official_form.is_valid() if edit_official else True
        committee_valid = committee.is_valid() if edit_official else True
        if letterhead_valid and official_valid and committee_valid:
            for field in ("fixture_letterhead", "report_template"):
                value = form.cleaned_data[field]
                if value is False:
                    setattr(owner, field, None)
                elif value:
                    setattr(owner, field, value)
            try:
                with transaction.atomic():
                    if form.has_changed():
                        owner.save(update_fields=form.changed_data)
                    if edit_official:
                        official_form.save()
                        committee.save()
            except (RequestException, OSError, ValueError):
                owner.refresh_from_db(fields=["fixture_letterhead", "report_template"])
                form.add_error(None, "The report stationery could not be saved. Please try again or contact your administrator.")
            else:
                messages.success(request, "Report stationery updated.")
                return redirect(request.path)
    return render(request, "league_manager/fixture_letterhead.html", {
        "letterhead_owner": owner,
        "letterhead_form": form,
        "official_form": official_form,
        "committee_formset": committee,
        "has_signature": has_signature,
        "has_stamp": has_stamp,
        "back_url": back_url,
        "letterhead_description": description,
    })


def _can_approve_league_artwork(user, league):
    if league.series.league_type == CompetitionSeries.LeagueType.ZONAL:
        return user_is_league_owner(user, league)
    return user_manages_series(user, league.series)


def _document_export_response(request, data, output_format, *, can_approve_artwork):
    preview = request.POST.get("action") == "preview" and data["kind"] != "STANDINGS"
    approved = can_approve_artwork and not preview and data["kind"] != "STANDINGS"
    artwork = {
        "include_signature": approved and request.POST.get("include_signature") == "1",
        "include_stamp": approved and request.POST.get("include_stamp") == "1",
    }
    if preview or output_format == "PDF":
        content = render_pdf(data, **artwork)
        content_type = "application/pdf"
        output_format = "PDF"
    else:
        content = render_docx(data, **artwork)
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(not preview, report_filename(data, output_format))
    if data["kind"] != "STANDINGS":
        response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def regional_fixture_letterhead(request, slug):
    region = get_managed_region(request, slug)
    return _fixture_letterhead_page(
        request, region,
        back_url=reverse("league_manager:regional_league_owners", args=[region.slug]),
        description=(f"Default stationery for regional leagues in {region.name} across seasons. "
                     "The official sign-off and committee apply to the whole region."),
    )


@login_required
@require_http_methods(["GET", "POST"])
def regional_league_fixture_letterhead(request, slug, series_id):
    region = get_managed_region(request, slug)
    series = get_object_or_404(_regional_series(region), pk=series_id)
    return _fixture_letterhead_page(
        request, series,
        back_url=reverse("league_manager:regional_league_owners", args=[region.slug]),
        description=(
            f"Optional league-specific stationery for {region.name}. It carries over to new seasons. "
            "A league Word template supplies its Word header and footer. A league image supplies the PDF letterhead "
            "and replaces the shared Word header when there is no league Word template. "
            "The shared Word footer and page settings still apply in that case. PDF uses the regional image when no league image is set. "
            "Clear both files to use the shared regional stationery. The sign-off and committee are managed at regional level."
        ),
        manage_official=False,
    )


@login_required
@require_http_methods(["GET", "POST"])
def organisation_fixture_letterhead(request, organisation):
    organisation = get_managed_organisation(request, organisation)
    if organisation not in FootballOrganisation.Code.values:
        raise PermissionDenied("Zonal leagues each have their own report stationery.")
    owner = get_object_or_404(FootballOrganisation, code=organisation)
    return _fixture_letterhead_page(
        request, owner,
        back_url=reverse("league_manager:organisation_league_owners", args=[organisation.lower()]),
        description=f"Shared by all {organisation} leagues across seasons.",
    )


@login_required
@require_http_methods(["GET", "POST"])
def zonal_fixture_letterhead(request, series_id):
    series = get_object_or_404(
        CompetitionSeries, pk=series_id, league_type=CompetitionSeries.LeagueType.ZONAL,
    )
    # Organisation administrators retain their established Zonal scope. A
    # direct league owner can alter only their own permanent league identity.
    if not can_manage_organisation(request.user, CompetitionSeries.LeagueType.ZONAL):
        if not LeagueMembership.objects.filter(
            user=request.user, competition__series=series,
            competition__is_archived=False, is_active=True,
            role=LeagueMembership.Role.OWNER,
        ).exists():
            raise PermissionDenied("Only this zonal league's owner or Zonal administrator can change its report stationery.")
    return _fixture_letterhead_page(
        request, series,
        back_url=reverse("league_manager:dashboard"),
        description="Shared by every season of this zonal league only.",
    )


@login_required
def regional_report_export(request, slug, report_kind):
    region = get_managed_region(request, slug)
    report_kind = report_kind.upper()
    if report_kind not in RegionalReportPreference.ReportKind.values:
        raise PermissionDenied("Unknown regional report type.")
    preference = _report_preference(request, region, report_kind)
    if request.method == "POST":
        fixture_scope = FIXTURE_SCOPE_NEXT_UPCOMING
        fixture_gameweek_mode = WEEKLY_SELECTION_AUTOMATIC
        manual_gameweeks = {}
        try:
            fixture_scope = _fixture_export_scope_from_post(request, report_kind)
            preview_series = _report_selection_from_post(
                request,
                preference,
                _regional_series(region),
            )
            fixture_gameweek_mode, manual_gameweeks = _fixture_gameweek_selection_from_post(
                request,
                report_kind,
                fixture_scope,
                list(selected_competitions(region, preference, selected_series_ids=preview_series)),
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            data = build_regional_report_data(
                region=region,
                preference=preference,
                selected_series_ids=preview_series,
                fixture_scope=fixture_scope,
                fixture_gameweek_mode=fixture_gameweek_mode,
                manual_gameweeks=manual_gameweeks,
            )
            if request.POST.get("action") == "save":
                messages.success(request, "Your default regional report preferences were saved.")
                return redirect("league_manager:regional_report_export", slug=region.slug, report_kind=report_kind.lower())
            if (
                report_kind == RegionalReportPreference.ReportKind.FIXTURES
                and fixture_scope != FIXTURE_SCOPE_NEXT_UPCOMING
                and data["warnings"]
            ):
                return render(request, "league_manager/regional_report_export.html", {
                    "region": region, "preference": preference, "report_kind": report_kind,
                    "can_approve_artwork": True, "preview_series": preview_series,
                    "series": _regional_series(region), "warnings": data["warnings"],
                    "warnings_title": "Fixture export warning",
                    "fixture_scope": fixture_scope,
                    "fixture_scope_options": _fixture_scope_options(),
                    "fixture_gameweek_mode": fixture_gameweek_mode,
                    "manual_gameweek_options": _regional_fixture_gameweek_options(
                        region, manual_gameweeks
                    ),
                })
            if data["warnings"] and report_kind == RegionalReportPreference.ReportKind.RESULTS and request.POST.get("confirmed") != "1":
                return render(request, "league_manager/regional_report_export.html", {
                    "region": region, "preference": preference, "report_kind": report_kind,
                    "can_approve_artwork": True, "preview_series": preview_series,
                    "series": _regional_series(region), "warnings": data["warnings"], "confirm_download": True,
                    "warnings_title": "Incomplete results warning",
                    "fixture_scope": fixture_scope,
                    "fixture_scope_options": _fixture_scope_options(),
                    "fixture_gameweek_mode": fixture_gameweek_mode,
                    "manual_gameweek_options": _regional_fixture_gameweek_options(
                        region, manual_gameweeks
                    ),
                })
            try:
                return _document_export_response(request, data, preference.output_format, can_approve_artwork=True)
            except ReportExportError as error:
                messages.error(request, str(error))
                return redirect(request.path)
    return render(request, "league_manager/regional_report_export.html", {
        "region": region,
        "preference": preference,
        "report_kind": report_kind,
        "can_approve_artwork": True,
        "series": _regional_series(region),
        "fixture_scope": FIXTURE_SCOPE_NEXT_UPCOMING,
        "fixture_scope_options": _fixture_scope_options(),
        "fixture_gameweek_mode": WEEKLY_SELECTION_AUTOMATIC,
        "manual_gameweek_options": _regional_fixture_gameweek_options(region),
    })


@login_required
def organisation_league_owners(request, organisation):
    organisation = get_managed_organisation(request, organisation)
    scope = _organisation_scope(organisation)
    return render(request, "league_manager/organisation_league_owners.html", {
        "scope": scope,
        "series_data": _owner_series_data(_organisation_series(organisation)),
        "owner_form": RegionalLeagueOwnerForm(),
    })


@login_required
@require_POST
@transaction.atomic
def organisation_league_owner_assign(request, organisation, series_id):
    organisation = get_managed_organisation(request, organisation)
    series = get_object_or_404(_organisation_series(organisation), pk=series_id)
    owner, error = _assign_owner_to_active_seasons(
        request,
        series,
        scope_values={"scope_label": f"{organisation} administrator", "organisation": organisation},
    )
    if error:
        messages.error(request, error)
    else:
        messages.success(request, f"{owner.get_full_name() or owner.username} is now an owner of {series.name}.")
    return redirect("league_manager:organisation_league_owners", organisation=organisation.lower())


@login_required
@require_POST
@transaction.atomic
def organisation_league_owner_remove(request, organisation, series_id, user_id):
    organisation = get_managed_organisation(request, organisation)
    series = get_object_or_404(_organisation_series(organisation), pk=series_id)
    changed = _remove_owner_from_active_seasons(
        request,
        series,
        user_id,
        scope_values={"scope_label": f"{organisation} administrator", "organisation": organisation},
    )
    if changed:
        messages.success(request, "League owner access removed.")
    else:
        messages.info(request, "That user does not have an active owner assignment for this league.")
    return redirect("league_manager:organisation_league_owners", organisation=organisation.lower())


def _organisation_report_preference(request, organisation, report_kind):
    preference, _ = OrganisationReportPreference.objects.get_or_create(
        user=request.user,
        organisation=organisation,
        report_kind=report_kind,
        defaults={"selection_mode": OrganisationReportPreference.SelectionMode.ALL},
    )
    return preference


@login_required
def organisation_report_export(request, organisation, report_kind):
    organisation = get_managed_organisation(request, organisation)
    report_kind = report_kind.upper()
    if report_kind not in OrganisationReportPreference.ReportKind.values:
        raise PermissionDenied("Unknown organisation report type.")
    scope = _organisation_scope(organisation)
    preference = _organisation_report_preference(request, organisation, report_kind)
    if request.method == "POST":
        fixture_scope = FIXTURE_SCOPE_NEXT_UPCOMING
        fixture_gameweek_mode = WEEKLY_SELECTION_AUTOMATIC
        manual_gameweeks = {}
        try:
            fixture_scope = _fixture_export_scope_from_post(request, report_kind)
            preview_series = _report_selection_from_post(
                request,
                preference,
                _organisation_series(organisation),
            )
            fixture_gameweek_mode, manual_gameweeks = _fixture_gameweek_selection_from_post(
                request,
                report_kind,
                fixture_scope,
                list(selected_organisation_competitions(organisation, preference, selected_series_ids=preview_series)),
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            data = build_organisation_report_data(
                organisation=organisation,
                preference=preference,
                selected_series_ids=preview_series,
                fixture_scope=fixture_scope,
                fixture_gameweek_mode=fixture_gameweek_mode,
                manual_gameweeks=manual_gameweeks,
            )
            if request.POST.get("action") == "save":
                messages.success(request, "Your default organisation report preferences were saved.")
                return redirect(
                    "league_manager:organisation_report_export",
                    organisation=organisation.lower(),
                    report_kind=report_kind.lower(),
                )
            if (
                report_kind == OrganisationReportPreference.ReportKind.FIXTURES
                and fixture_scope != FIXTURE_SCOPE_NEXT_UPCOMING
                and data["warnings"]
            ):
                return render(request, "league_manager/organisation_report_export.html", {
                    "scope": scope, "preference": preference, "report_kind": report_kind,
                    "can_approve_artwork": True, "preview_series": preview_series,
                    "series": _organisation_series(organisation), "warnings": data["warnings"],
                    "warnings_title": "Fixture export warning",
                    "fixture_scope": fixture_scope,
                    "fixture_scope_options": _fixture_scope_options(),
                    "fixture_gameweek_mode": fixture_gameweek_mode,
                    "manual_gameweek_options": _organisation_fixture_gameweek_options(
                        organisation, manual_gameweeks
                    ),
                })
            if data["warnings"] and report_kind == OrganisationReportPreference.ReportKind.RESULTS and request.POST.get("confirmed") != "1":
                return render(request, "league_manager/organisation_report_export.html", {
                    "scope": scope, "preference": preference, "report_kind": report_kind,
                    "can_approve_artwork": True, "preview_series": preview_series,
                    "series": _organisation_series(organisation), "warnings": data["warnings"], "confirm_download": True,
                    "warnings_title": "Incomplete results warning",
                    "fixture_scope": fixture_scope,
                    "fixture_scope_options": _fixture_scope_options(),
                    "fixture_gameweek_mode": fixture_gameweek_mode,
                    "manual_gameweek_options": _organisation_fixture_gameweek_options(
                        organisation, manual_gameweeks
                    ),
                })
            try:
                return _document_export_response(request, data, preference.output_format, can_approve_artwork=True)
            except ReportExportError as error:
                messages.error(request, str(error))
                return redirect(request.path)
    return render(request, "league_manager/organisation_report_export.html", {
        "scope": scope,
        "preference": preference,
        "report_kind": report_kind,
        "can_approve_artwork": True,
        "series": _organisation_series(organisation),
        "fixture_scope": FIXTURE_SCOPE_NEXT_UPCOMING,
        "fixture_scope_options": _fixture_scope_options(),
        "fixture_gameweek_mode": WEEKLY_SELECTION_AUTOMATIC,
        "manual_gameweek_options": _organisation_fixture_gameweek_options(organisation),
    })

@login_required
@require_http_methods(["GET", "POST"])
def league_report_export(request, slug, report_kind):
    """Export exactly one managed season without granting aggregate access."""
    league = get_managed_league(request, slug)
    report_kind = report_kind.upper()
    if report_kind not in {"FIXTURES", "RESULTS"}:
        raise PermissionDenied("Unknown league report type.")
    preference = RegionalReportPreference(
        user=request.user, report_kind=report_kind,
        output_format=RegionalReportPreference.OutputFormat.DOCX,
        selection_mode=RegionalReportPreference.SelectionMode.ALL,
    )
    fixture_scope = FIXTURE_SCOPE_NEXT_UPCOMING
    fixture_gameweek_mode = WEEKLY_SELECTION_AUTOMATIC
    manual_gameweeks = {}
    warnings = []
    confirm_download = False
    can_approve_artwork = _can_approve_league_artwork(request.user, league)
    if request.method == "POST":
        try:
            output_format = request.POST.get("output_format", "DOCX")
            if output_format not in RegionalReportPreference.OutputFormat.values:
                raise ValidationError("Select a valid output format.")
            preference.output_format = output_format
            fixture_scope = _fixture_export_scope_from_post(request, report_kind)
            fixture_gameweek_mode, manual_gameweeks = _fixture_gameweek_selection_from_post(
                request, report_kind, fixture_scope, [league],
            )
            owner = report_stationery_owner(league)
            template, image, use_image_in_word = report_letterheads_for(league, owner)
            data = build_scope_report_data(
                preference=preference, scope_name=league.series.name,
                scope_template=template,
                scope_letterhead=image,
                scope_use_image_in_word=use_image_in_word,
                scope_stationery_owner=owner,
                competitions=[league], fixture_scope=fixture_scope,
                fixture_gameweek_mode=fixture_gameweek_mode,
                manual_gameweeks=manual_gameweeks,
            )
            warnings = data["warnings"]
            blocked_fixture_warning = bool(
                report_kind == "FIXTURES" and fixture_scope != FIXTURE_SCOPE_NEXT_UPCOMING and warnings
            )
            confirm_download = bool(report_kind == "RESULTS" and warnings and request.POST.get("confirmed") != "1")
            if not blocked_fixture_warning and not confirm_download:
                return _document_export_response(request, data, output_format, can_approve_artwork=can_approve_artwork)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        except ReportExportError as error:
            messages.error(request, str(error))
            return redirect(request.path)
    stationery_url = ""
    if can_approve_artwork and league.series.league_type == CompetitionSeries.LeagueType.REGIONAL and league.series.region_id:
        stationery_url = reverse("league_manager:regional_league_fixture_letterhead", args=[league.series.region.slug, league.series_id])
    elif can_approve_artwork and league.series.league_type == CompetitionSeries.LeagueType.ZONAL:
        stationery_url = reverse("league_manager:zonal_fixture_letterhead", args=[league.series_id])
    return render(request, "league_manager/league_report_export.html", {
        "league": league, "report_kind": report_kind, "preference": preference,
        "fixture_scope": fixture_scope, "fixture_scope_options": _fixture_scope_options(),
        "fixture_gameweek_mode": fixture_gameweek_mode,
        "manual_gameweek_options": _manual_gameweek_options_context([league], manual_gameweeks),
        "warnings": warnings, "confirm_download": confirm_download, "can_approve_artwork": can_approve_artwork,
        "stationery_url": stationery_url,
    })


@login_required
def league_dashboard(request, slug):
    league = get_managed_league(
        request,
        slug,
    )
    is_owner = user_is_league_owner(
        request.user,
        league,
    )

    team_count = Team.objects.filter(
        competition=league
    ).count()

    player_count = Player.objects.filter(
        team__competition=league
    ).count()

    match_count = league.matches.count()

    upcoming_count = league.matches.filter(
        status="UPCOMING"
    ).count()

    live_count = league.matches.filter(
        status="LIVE"
    ).count()

    finished_count = league.matches.filter(
        status="FINISHED"
    ).count()

    recent_activity = ActivityLog.objects.filter(
        competition=league
    ).select_related(
        "actor",
        "match",
    )[:8]

    return render(
        request,
        "league_manager/league_dashboard.html",
        {
            "league": league,
            "region_form": LeagueRegionForm(initial={
                "league_type": league.series.league_type,
                "region": league.series.region_id,
            }),
            "branding_form": LeagueBrandingForm(initial={
                "league_logo": league.series.logo,
                "sponsor_name": league.sponsor_name,
                "sponsor_logo": league.sponsor_logo,
            }),
            "team_count": team_count,
            "player_count": player_count,
            "match_count": match_count,
            "upcoming_count": upcoming_count,
            "live_count": live_count,
            "finished_count": finished_count,
            "recent_activity": recent_activity,
            "is_owner": is_owner,
            "can_export_reports": user_manages_competition(request.user, league),
            "is_direct_owner": user_has_direct_league_owner_role(request.user, league),
            "is_superuser": request.user.is_superuser,
            "can_archive_season": (
                request.user.is_superuser
                and league.format_type == "TABLE"
                and not league.is_archived
                and not league.matches.filter(
                    status__in=["UPCOMING", "LIVE"]
                ).exists()
            ),
            "can_create_next_season": (
                request.user.is_superuser
                and league.format_type == "TABLE"
                and league.is_archived
            ),
        },
    )


@login_required
@require_POST
def update_league_region(request, slug):
    league = get_managed_league(request, slug)
    if not request.user.is_superuser:
        raise PermissionDenied(
            "Only a platform superuser can change a league's football section or region."
        )
    form = LeagueRegionForm(request.POST)
    if form.is_valid():
        scope_changed = (
            league.series.league_type != form.cleaned_data["league_type"]
            or league.series.region_id != (
                form.cleaned_data["region"].id
                if form.cleaned_data["region"]
                else None
            )
        )
        if scope_changed and request.POST.get("confirm_scope_change") != "1":
            messages.error(
                request,
                "Confirm the administrator reassignment before moving this league's scope.",
            )
            return redirect("league_manager:league_dashboard", slug=league.slug)
        league.series.league_type = form.cleaned_data["league_type"]
        league.series.region = form.cleaned_data["region"]
        league.series.full_clean()
        league.series.save(update_fields=["league_type", "region"])
        messages.success(request, "League classification updated.")
    else:
        messages.error(request, "Select a valid region.")
    return redirect("league_manager:league_dashboard", slug=league.slug)


@login_required
@require_POST
def update_league_branding(request, slug):
    """Let owners manage series branding and this season's sponsorship."""
    league = get_managed_league(request, slug)
    if not (request.user.is_superuser or user_is_league_owner(request.user, league)):
        raise PermissionDenied("Only the league owner can change league branding.")

    form = LeagueBrandingForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Upload valid branding images and try again.")
        return redirect("league_manager:league_dashboard", slug=league.slug)

    series = league.series
    logo = form.cleaned_data["league_logo"]
    sponsor_logo = form.cleaned_data["sponsor_logo"]
    if request.POST.get("league_logo-clear"):
        series.logo = None
    elif logo:
        series.logo = logo
    series.save(update_fields=["logo"])

    league.sponsor_name = form.cleaned_data["sponsor_name"].strip()
    if request.POST.get("sponsor_logo-clear"):
        league.sponsor_logo = None
    elif sponsor_logo:
        league.sponsor_logo = sponsor_logo
    league.save(update_fields=["sponsor_name", "sponsor_logo"])
    messages.success(request, "League branding updated.")
    return redirect("league_manager:league_dashboard", slug=league.slug)


def get_superuser_league(request, slug):
    if not request.user.is_superuser:
        raise PermissionDenied(
            "Only a superuser can manage football league seasons."
        )

    return get_object_or_404(Competition, slug=slug)


@login_required
@require_POST
def archive_season(request, slug):
    league = get_superuser_league(request, slug)

    try:
        archive_competition_season(league, actor=request.user)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(
            request,
            "Season archived. Historical data remains available and read-only.",
        )

    return redirect("league_manager:league_dashboard", slug=league.slug)


@login_required
def create_next_season(request, slug):
    league = get_superuser_league(request, slug)

    if league.format_type != "TABLE" or not league.is_archived:
        messages.error(
            request,
            "Only an archived Table Only football league can create a next season.",
        )
        return redirect("league_manager:league_dashboard", slug=league.slug)

    form = NextCompetitionSeasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            next_league = create_next_competition_season(
                league,
                actor=request.user,
                season=form.cleaned_data["season"],
                slug=form.cleaned_data["slug"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(
                request,
                f"Created clean {next_league.season} season for {next_league.name}.",
            )
            return redirect(
                "league_manager:league_dashboard",
                slug=next_league.slug,
            )

    return render(
        request,
        "league_manager/next_season_form.html",
        {
            "league": league,
            "form": form,
        },
    )


# ==========================================================
# TEAM MANAGEMENT
# ==========================================================

@login_required
def team_list(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    teams = (
        Team.objects
        .filter(
            competition=league
        )
        .annotate(
            player_count=Count(
                "players",
                distinct=True,
            )
        )
        .order_by("name")
    )

    return render(
        request,
        "league_manager/team_list.html",
        {
            "league": league,
            "teams": teams,
            "is_owner": user_is_league_owner(request.user, league),
        },
    )


@login_required
def team_create(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    if league.format_type == "GROUP_KNOCKOUT":
        group_stage = Stage.objects.filter(
            competition=league,
            stage_type="GROUP",
        ).first()

        groups_exist = (
            group_stage
            and Group.objects.filter(
                stage=group_stage
            ).exists()
        )

        if not groups_exist:
            messages.warning(
                request,
                "Create at least one group before adding teams.",
            )

            return redirect(
                "league_manager:group_list",
                slug=league.slug,
            )

    form = TeamForm(
        request.POST or None,
        request.FILES or None,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        team = form.save(commit=False)
        team.competition = league
        team.save()

        form.save_group_assignment(team)

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.TEAM_CREATED,
            description=f"Team created: {team.name}",
            new_values={
                "team_id": team.id,
                "name": team.name,
            },
        )

        messages.success(
            request,
            f"{team.name} was added successfully.",
        )

        if request.POST.get("save_and_add_another") == "1":
            return redirect(
                "league_manager:team_create",
                slug=league.slug,
            )

        return redirect(
            "league_manager:team_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/team_form.html",
        {
            "league": league,
            "form": form,
            "page_title": "Add Team",
            "submit_text": "Add Team",
            "is_create": True,
        },
    )


@login_required
def team_update(request, slug, team_id):
    league = get_managed_league(
        request,
        slug,
    )

    team = get_object_or_404(
        Team,
        pk=team_id,
        competition=league,
    )

    old_values = {
        "team_id": team.id,
        "name": team.name,
    }

    form = TeamForm(
        request.POST or None,
        request.FILES or None,
        instance=team,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        updated_team = form.save()
        form.save_group_assignment(updated_team)

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.TEAM_UPDATED,
            description=f"Team updated: {updated_team.name}",
            old_values=old_values,
            new_values={
                "team_id": updated_team.id,
                "name": updated_team.name,
                "logo_changed": "logo" in form.changed_data,
            },
        )

        messages.success(
            request,
            f"{updated_team.name} was updated successfully.",
        )

        return redirect(
            "league_manager:team_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/team_form.html",
        {
            "league": league,
            "team": team,
            "form": form,
            "page_title": "Edit Team",
            "submit_text": "Save Changes",
            "is_create": False,
        },
    )


# ==========================================================
# PLAYER MANAGEMENT
# ==========================================================

@login_required
def player_list(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    selected_team_id = request.GET.get("team")

    players = (
        Player.objects
        .filter(
            team__competition=league
        )
        .select_related("team")
        .order_by(
            "team__name",
            "name",
        )
    )

    if selected_team_id:
        try:
            selected_team_id = int(selected_team_id)
        except (TypeError, ValueError):
            selected_team_id = None

    if selected_team_id:
        players = players.filter(
            team_id=selected_team_id,
        )

    teams = Team.objects.filter(
        competition=league
    ).order_by("name")

    return render(
        request,
        "league_manager/player_list.html",
        {
            "league": league,
            "players": players,
            "teams": teams,
            "selected_team_id": selected_team_id,
        },
    )


@login_required
def player_create(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    initial = {}

    requested_team_id = request.GET.get("team")

    if requested_team_id:
        requested_team = Team.objects.filter(
            pk=requested_team_id,
            competition=league,
        ).first()

        if requested_team:
            initial["team"] = requested_team

    form = PlayerForm(
        request.POST or None,
        request.FILES or None,
        league=league,
        initial=initial,
    )

    if request.method == "POST" and form.is_valid():
        player = form.save()

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.PLAYER_CREATED,
            description=(
                f"Player created: {player.name} "
                f"({player.team.name})"
            ),
            new_values={
                "player_id": player.id,
                "name": player.name,
                "team_id": player.team_id,
                "team": player.team.name,
                "jersey_number": player.jersey_number,
                "starting_goals": player.starting_goals,
                "photo_added": "photo" in form.changed_data,
            },
        )

        messages.success(
            request,
            f"{player.name} was added successfully.",
        )

        if request.POST.get("save_and_add_another") == "1":
            create_url = reverse(
                "league_manager:player_create",
                kwargs={"slug": league.slug},
            )
            return redirect(f"{create_url}?team={player.team_id}")

        return redirect(
            "league_manager:player_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/player_form.html",
        {
            "league": league,
            "form": form,
            "page_title": "Add Player",
            "submit_text": "Add Player",
            "is_create": True,
        },
    )


@login_required
def player_update(request, slug, player_id):
    league = get_managed_league(
        request,
        slug,
    )

    player = get_object_or_404(
        Player.objects.select_related("team"),
        pk=player_id,
        team__competition=league,
    )

    old_values = {
        "player_id": player.id,
        "name": player.name,
        "team_id": player.team_id,
        "team": player.team.name,
        "jersey_number": player.jersey_number,
        "starting_goals": player.starting_goals,
    }

    form = PlayerForm(
        request.POST or None,
        request.FILES or None,
        instance=player,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        updated_player = form.save()

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.PLAYER_UPDATED,
            description=(
                f"Player updated: {updated_player.name} "
                f"({updated_player.team.name})"
            ),
            old_values=old_values,
            new_values={
                "player_id": updated_player.id,
                "name": updated_player.name,
                "team_id": updated_player.team_id,
                "team": updated_player.team.name,
                "jersey_number": updated_player.jersey_number,
                "starting_goals": updated_player.starting_goals,
                "photo_changed": "photo" in form.changed_data,
            },
        )

        messages.success(
            request,
            f"{updated_player.name} was updated successfully.",
        )

        return redirect(
            "league_manager:player_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/player_form.html",
        {
            "league": league,
            "player": player,
            "form": form,
            "page_title": "Edit Player",
            "submit_text": "Save Changes",
            "is_create": False,
        },
    )

# ==========================================================
# FIXTURE MANAGEMENT
# ==========================================================

@login_required
def fixture_list(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    current_filter = request.GET.get(
        "status",
        "upcoming",
    )

    assignment_queryset = (
        MatchAdminAssignment.objects
        .select_related("admin")
        .order_by("id")
    )

    matches = (
        Match.objects
        .filter(competition=league)
        .select_related(
            "stage",
            "group",
            "home_team",
            "away_team",
            "knockout_tie",
        )
        .prefetch_related(
            Prefetch(
                "assigned_admins",
                queryset=assignment_queryset,
                to_attr="agent_assignment_rows",
            )
        )
    )

    table_ordering = ("gameweek", "match_date", "id")

    if current_filter == "live":
        matches = matches.filter(
            status="LIVE"
        ).order_by(*table_ordering if league.format_type == "TABLE" else ("match_date",))

    elif current_filter == "completed":
        matches = matches.filter(
            status__in=[
                "FINISHED",
                "ABANDONED",
            ]
        ).order_by(*table_ordering if league.format_type == "TABLE" else ("-match_date",))

    elif current_filter == "all":
        matches = matches.order_by(*(table_ordering if league.format_type == "TABLE" else ("-match_date",)))

    else:
        current_filter = "upcoming"

        matches = matches.filter(
            status="UPCOMING"
        ).order_by(*table_ordering if league.format_type == "TABLE" else ("match_date",))

    counts = {
        "upcoming": Match.objects.filter(
            competition=league,
            status="UPCOMING",
        ).count(),

        "live": Match.objects.filter(
            competition=league,
            status="LIVE",
        ).count(),

        "completed": Match.objects.filter(
            competition=league,
            status__in=[
                "FINISHED",
                "ABANDONED",
            ],
        ).count(),

        "all": Match.objects.filter(
            competition=league
        ).count(),
    }

    generation_state = round_generation_state(league)

    return render(
        request,
        "league_manager/fixture_list.html",
        {
            "league": league,
            "matches": matches,
            "current_filter": current_filter,
            "counts": counts,
            "can_delete_fixtures": can_delete_fixture(
                request.user,
                SimpleNamespace(competition=league),
            ),
            "can_manage_schedule_lock": request.user.is_superuser,
            "can_manage_abandonment_decisions": can_manage_abandonment_decisions(request.user, league),
            "schedule_locked": bool(league.schedule_locked_at),
            **generation_state,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def schedule_lock(request, slug):
    """Explicitly publish and protect one competition season's fixture structure."""

    league = _schedule_lock_competition_or_403(request, slug)
    if request.method == "POST":
        with transaction.atomic():
            locked_league = Competition.objects.select_for_update(of=("self",)).select_related(
                "series", "series__region", "schedule_locked_by"
            ).get(pk=league.pk)
            if locked_league.schedule_locked_at:
                messages.info(request, "This competition schedule is already locked.")
            else:
                old_values = _schedule_lock_snapshot(locked_league)
                locked_league.schedule_locked_at = timezone.now()
                locked_league.schedule_locked_by = request.user
                locked_league.save(update_fields=["schedule_locked_at", "schedule_locked_by"])
                new_values = _schedule_lock_snapshot(locked_league)
                record_activity(
                    actor=request.user,
                    competition=locked_league,
                    action=ActivityLog.Action.SCHEDULE_LOCKED,
                    description="Competition schedule locked by superuser.",
                    old_values=old_values,
                    new_values=new_values,
                )
                messages.success(request, "The competition schedule is now locked.")
        return redirect("league_manager:fixture_list", slug=league.slug)

    return render(
        request,
        "league_manager/schedule_lock_confirm.html",
        {
            "league": league,
            "operation": "lock",
            "fixture_count": Match.objects.filter(competition=league).count(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def schedule_unlock(request, slug):
    """Reopen structural fixture management only after an audited explanation."""

    league = _schedule_lock_competition_or_403(request, slug)
    form = ScheduleUnlockForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            locked_league = Competition.objects.select_for_update(of=("self",)).select_related(
                "series", "series__region", "schedule_locked_by"
            ).get(pk=league.pk)
            if not locked_league.schedule_locked_at:
                messages.info(request, "This competition schedule is already unlocked.")
            else:
                old_values = _schedule_lock_snapshot(locked_league)
                locked_league.schedule_locked_at = None
                locked_league.schedule_locked_by = None
                locked_league.save(update_fields=["schedule_locked_at", "schedule_locked_by"])
                new_values = _schedule_lock_snapshot(locked_league)
                new_values["unlock_reason"] = form.cleaned_data["reason"]
                record_activity(
                    actor=request.user,
                    competition=locked_league,
                    action=ActivityLog.Action.SCHEDULE_UNLOCKED,
                    description=(
                        "Competition schedule unlocked by superuser: "
                        f"{form.cleaned_data['reason']}"
                    ),
                    old_values=old_values,
                    new_values=new_values,
                )
                messages.success(request, "The competition schedule is unlocked.")
        return redirect("league_manager:fixture_list", slug=league.slug)

    return render(
        request,
        "league_manager/schedule_lock_confirm.html",
        {
            "league": league,
            "operation": "unlock",
            "fixture_count": Match.objects.filter(competition=league).count(),
            "form": form,
        },
    )


@login_required
@require_POST
def generate_first_round(request, slug):
    league = get_managed_league(request, slug)
    try:
        token = request.POST.get("preview_token")
        if not token:
            raise ValidationError("Review and confirm a fixture preview before generating fixtures.")
        count = confirm_generation_preview(league, token, action="first")
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
    else:
        messages.success(request, f"Generated {count} unscheduled first-round fixtures.")
    return redirect("league_manager:fixture_list", slug=league.slug)


@login_required
@require_POST
def generate_remaining_first_round(request, slug):
    league = get_managed_league(request, slug)
    try:
        token = request.POST.get("preview_token")
        if not token:
            raise ValidationError("Review and confirm a fixture preview before generating fixtures.")
        count = confirm_generation_preview(league, token, action="remaining_first")
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
    else:
        messages.success(request, f"Generated {count} unscheduled remaining first-round fixtures.")
    return redirect("league_manager:fixture_list", slug=league.slug)


@login_required
@require_POST
def generate_second_round(request, slug):
    league = get_managed_league(request, slug)
    try:
        token = request.POST.get("preview_token")
        if not token:
            raise ValidationError("Review and confirm a fixture preview before generating fixtures.")
        count = confirm_generation_preview(league, token, action="second")
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
    else:
        messages.success(request, f"Generated {count} unscheduled reverse second-round fixtures.")
    return redirect("league_manager:fixture_list", slug=league.slug)


@login_required
@require_POST
def fixture_generation_preview(request, slug, action):
    """Show a signed, non-mutating fixture plan before the manager confirms."""

    league = get_managed_league(request, slug)
    action_routes = {
        "first": ("league_manager:generate_first_round", "Generate First Round"),
        "remaining_first": (
            "league_manager:generate_remaining_first_round",
            "Generate Remaining First Round",
        ),
        "second": ("league_manager:generate_second_round", "Generate Second Round"),
    }
    if action not in action_routes:
        raise PermissionDenied("Unknown fixture-generation action.")
    try:
        preview = build_generation_preview(league, action)
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
        return redirect("league_manager:fixture_list", slug=league.slug)

    proposed_by_gameweek = {}
    for fixture in preview["proposed"]:
        proposed_by_gameweek.setdefault(fixture.gameweek, []).append(
            {
                "home": preview["team_names"][fixture.home_team_id],
                "away": preview["team_names"][fixture.away_team_id],
            }
        )
    existing_by_gameweek = {}
    for fixture in preview["existing"]:
        existing_by_gameweek.setdefault(fixture.gameweek, []).append(
            {"home": fixture.home_team.name, "away": fixture.away_team.name}
        )
    preview["gameweeks"] = [
        {
            "number": gameweek,
            "existing_count": preview["existing_by_gameweek"].get(gameweek, 0),
            "existing": existing_by_gameweek.get(gameweek, []),
            "proposed": proposed_by_gameweek.get(gameweek, []),
        }
        for gameweek in preview["expected_gameweeks"]
    ]
    preview["balance_rows"] = [
        {
            "team": preview["team_names"][team_id],
            **values,
        }
        for team_id, values in sorted(
            preview["balance"]["teams"].items(),
            key=lambda item: preview["team_names"][item[0]],
        )
    ]
    preview["confirm_route"], preview["confirm_label"] = action_routes[action]
    return render(request, "league_manager/fixture_generation_preview.html", preview)


def _validate_manual_table_fixture_slot(fixture):
    """Recheck table-round invariants after the shared competition lock.

    ``FixtureForm`` performs user-facing validation before this point, but a
    confirmed fixture preview may have inserted rows while that form was being
    completed. The parent-row lock serialises the two writes; this recheck
    prevents the form's earlier snapshot from creating a same-round team
    conflict or a reversed duplicate in the same leg.
    """

    if (
        fixture.competition.format_type != "TABLE"
        or fixture.stage.stage_type != "LEAGUE"
    ):
        return
    existing = Match.objects.filter(
        competition=fixture.competition,
        stage=fixture.stage,
    )
    if fixture.pk:
        existing = existing.exclude(pk=fixture.pk)
    if existing.filter(
        leg=fixture.leg,
    ).filter(
        Q(home_team_id=fixture.away_team_id, away_team_id=fixture.home_team_id)
    ).exists():
        raise ValidationError(
            "The reverse of this pairing already exists in the same table-round leg."
        )
    if fixture.gameweek is not None and existing.filter(
        gameweek=fixture.gameweek,
    ).filter(
        Q(home_team_id__in=(fixture.home_team_id, fixture.away_team_id))
        | Q(away_team_id__in=(fixture.home_team_id, fixture.away_team_id))
    ).exists():
        raise ValidationError("A selected team already has a fixture in this gameweek.")


@login_required
def fixture_create(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    if league.schedule_locked_at:
        messages.error(
            request,
            "This competition schedule is locked. New fixtures cannot be created until a superuser unlocks it.",
        )
        return redirect("league_manager:fixture_list", slug=league.slug)

    if (
        league.format_type == "KNOCKOUT"
        and not Stage.objects.filter(
            competition=league,
            stage_type="KNOCKOUT",
        ).exists()
    ):
        messages.warning(
            request,
            (
                "Configure the knockout rounds before "
                "creating fixtures."
            ),
        )

        return redirect(
            "league_manager:knockout_rounds",
            slug=league.slug,
        )

    form = FixtureForm(
        request.POST or None,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        fixture = form.save(commit=False)

        fixture.competition = league
        fixture.status = "UPCOMING"
        fixture.phase = "NONE"
        fixture.minute = 0

        if (
            fixture.stage_id
            and fixture.stage.stage_type == "KNOCKOUT"
            and competition_has_bracket(league)
        ):
            form.add_error(
                None,
                "Connected bracket fixtures are created only through bracket progression.",
            )
        else:
            try:
                # Fixture planning locks the same parent row.  Taking this
                # lock here prevents a manual save from racing a confirmed
                # preview between its stale-state check and bulk insert.
                with transaction.atomic():
                    fixture.competition = Competition.objects.select_for_update().get(
                        pk=league.pk
                    )
                    _validate_manual_table_fixture_slot(fixture)
                    fixture.save()
            except ValidationError as error:
                form.add_error(None, "; ".join(error.messages))
            except IntegrityError:
                form.add_error(
                    None,
                    "This fixture already exists for this stage and leg.",
                )
            else:
                record_activity(
                    actor=request.user,
                    competition=league,
                    action=ActivityLog.Action.FIXTURE_CREATED,
                    description=(
                        f"Fixture created: "
                        f"{fixture.home_team.name} vs "
                        f"{fixture.away_team.name}"
                    ),
                    new_values=fixture_snapshot(fixture),
                )

                messages.success(
                    request,
                    (
                        f"{fixture.home_team.name} vs "
                        f"{fixture.away_team.name} was created."
                    ),
                )

                if request.POST.get("save_and_add_another") == "1":
                    return redirect(
                        "league_manager:fixture_create",
                        slug=league.slug,
                    )

                return redirect(
                    "league_manager:fixture_list",
                    slug=league.slug,
                )

    return render(
        request,
        "league_manager/fixture_form.html",
        {
            "league": league,
            "form": form,
            "page_title": "Create Fixture",
            "submit_text": "Create Fixture",
            "is_create": True,
        },
    )


@login_required
def fixture_update(request, slug, match_id):
    league = get_managed_league(
        request,
        slug,
    )

    fixture = get_object_or_404(
        Match.objects.select_related(
            "stage",
            "group",
            "home_team",
            "away_team",
            "knockout_tie",
        ),
        pk=match_id,
        competition=league,
    )

    # Live and completed matches must not be changed through
    # the ordinary fixture editor.
    if fixture.status != "UPCOMING":
        messages.error(
            request,
            (
                "Only upcoming fixtures can be edited. "
                "Live or completed matches must use the appropriate "
                "match or result controls."
            ),
        )

        return redirect(
            "league_manager:fixture_list",
            slug=league.slug,
        )

    old_values = fixture_snapshot(fixture)

    form = FixtureForm(
        request.POST or None,
        instance=fixture,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        structural_changes = {
            "stage",
            "group",
            "home_team",
            "away_team",
            "leg",
        }.intersection(form.changed_data)
        if (
            fixture.knockout_tie_id
            and fixture.knockout_tie.position is not None
            and structural_changes
        ):
            form.add_error(
                None,
                "Teams, stage and leg for a connected bracket fixture are controlled by the bracket.",
            )
        else:
            try:
                with transaction.atomic():
                    locked_competition = Competition.objects.select_for_update().get(
                        pk=league.pk
                    )
                    updated_fixture = form.save(commit=False)
                    updated_fixture.competition = locked_competition
                    _validate_manual_table_fixture_slot(updated_fixture)
                    updated_fixture.save()
            except ValidationError as error:
                form.add_error(None, "; ".join(error.messages))
            except IntegrityError:
                form.add_error(
                    None,
                    "This fixture already exists for this stage and leg.",
                )
            else:
                record_activity(
                    actor=request.user,
                    competition=league,
                    action=ActivityLog.Action.FIXTURE_UPDATED,
                    description=(
                        f"Fixture updated: "
                        f"{updated_fixture.home_team.name} vs "
                        f"{updated_fixture.away_team.name}"
                    ),
                    old_values=old_values,
                    new_values=fixture_snapshot(
                        updated_fixture
                    ),
                )

                messages.success(
                    request,
                    "The fixture was updated successfully.",
                )

                return redirect(
                    "league_manager:fixture_list",
                    slug=league.slug,
                )

    return render(
        request,
        "league_manager/fixture_form.html",
        {
            "league": league,
            "fixture": fixture,
            "form": form,
            "page_title": "Edit Fixture",
            "submit_text": "Save Changes",
            "is_create": False,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def fixture_delete(request, slug, match_id):
    """Confirm then delete an authorized fixture with an audit-safe snapshot."""

    league = get_object_or_404(
        Competition.objects.select_related("series", "series__region"),
        slug=slug,
    )
    fixture = get_object_or_404(
        Match.objects.select_related(
            "competition",
            "competition__series",
            "competition__series__region",
            "stage",
            "group",
            "home_team",
            "away_team",
            "knockout_tie",
        ),
        pk=match_id,
        competition=league,
    )
    if not can_delete_fixture(request.user, fixture):
        raise PermissionDenied("You do not have permission to delete this fixture.")
    deletion_block_reason = fixture_deletion_block_reason(fixture)

    if request.method == "GET":
        return render(
            request,
            "league_manager/fixture_confirm_delete.html",
            {
                "league": league,
                "fixture": fixture,
                "deletion_block_reason": deletion_block_reason,
            },
        )

    try:
        delete_fixtures(actor=request.user, fixture_ids=[fixture.pk])
    except FixtureDeletionBlocked as error:
        messages.error(request, " ".join(error.messages))
        return redirect("league_manager:fixture_list", slug=league.slug)

    messages.success(
        request,
        "The fixture was deleted. Standings were rebuilt only where a deleted result affected them.",
    )
    return redirect("league_manager:fixture_list", slug=league.slug)


@login_required
@require_http_methods(["GET", "POST"])
def resolve_abandoned_match(request, slug, match_id):
    """The single management surface for official abandonment decisions."""

    league = get_managed_league(request, slug)
    match = get_object_or_404(
        Match.objects.select_related(
            "competition", "stage", "group", "home_team", "away_team"
        ),
        pk=match_id,
        competition=league,
    )
    if not can_manage_abandonment_decisions(request.user, league):
        raise PermissionDenied("You do not have permission to resolve this match.")
    current_decision = (
        AbandonedMatchDecision.objects.filter(match=match, is_current=True)
        .select_related("decided_by", "replay_match")
        .first()
    )
    can_resolve = not current_decision and match.status == "ABANDONED"
    can_review = bool(current_decision)
    can_reopen = bool(
        can_review
        and match.stage.stage_type != "KNOCKOUT"
        and current_decision.decision_type
        in {
            AbandonedMatchDecision.DecisionType.AWARD,
            AbandonedMatchDecision.DecisionType.CONFIRM,
            AbandonedMatchDecision.DecisionType.VOID,
        }
    )
    if not can_resolve and not can_review:
        raise PermissionDenied("You do not have permission to resolve this match.")

    decisions = (
        AbandonedMatchDecision.objects.filter(match=match)
        .select_related("decided_by", "replay_match")
        .order_by("-decided_at", "-id")
    )
    goals = match.goals.filter(canceled=False).select_related("player", "team").order_by("minute", "id")
    cards = match.yellow_cards.select_related("player", "team").order_by("minute", "id")
    form = AbandonedMatchResolutionForm(
        request.POST if request.method == "POST" else None, match=match
    )

    if request.method == "POST":
        if request.POST.get("action") == "reopen":
            try:
                reopen_abandoned_resolution(
                    match_id=match.id,
                    actor=request.user,
                    note=request.POST.get("additional_note", ""),
                )
            except (AbandonedMatchResolutionError, ValidationError) as error:
                form.add_error(None, " ".join(error.messages))
            else:
                messages.success(request, "The official decision was reopened for correction.")
                return redirect(
                    "league_manager:resolve_abandoned_match",
                    slug=league.slug,
                    match_id=match.id,
                )
        else:
            form = AbandonedMatchResolutionForm(request.POST, match=match)
            if form.is_valid():
                data = form.cleaned_data
                try:
                    if data["decision_type"] == AbandonedMatchDecision.DecisionType.REPLAY:
                        _match, replay, _decision = schedule_full_replay(
                            match_id=match.id,
                            actor=request.user,
                            scheduled_for=data["scheduled_for"],
                            venue=data["venue"],
                            note=data["additional_note"],
                        )
                        messages.success(
                            request,
                            f"Replay scheduled: {replay.home_team} vs {replay.away_team}.",
                        )
                    elif data["decision_type"] == AbandonedMatchDecision.DecisionType.RESUME:
                        resume_abandoned_match(
                            match_id=match.id,
                            actor=request.user,
                            resume_minute=data["resume_minute"],
                            scheduled_for=data["scheduled_for"],
                            venue=data["venue"],
                            note=data["additional_note"],
                        )
                        messages.success(request, "The match is live again from its approved minute.")
                    elif data["decision_type"] == AbandonedMatchDecision.DecisionType.AWARD:
                        award_official_result(
                            match_id=match.id,
                            actor=request.user,
                            home_score=data["official_home_score"],
                            away_score=data["official_away_score"],
                            note=data["additional_note"],
                        )
                        messages.success(request, "The official result was recorded.")
                    elif data["decision_type"] == AbandonedMatchDecision.DecisionType.CONFIRM:
                        confirm_score_at_abandonment(
                            match_id=match.id,
                            actor=request.user,
                            note=data["additional_note"],
                        )
                        messages.success(request, "The score at abandonment was confirmed.")
                    else:
                        void_abandoned_match(
                            match_id=match.id,
                            actor=request.user,
                            note=data["additional_note"],
                        )
                        messages.success(request, "The match was voided; no points were awarded.")
                except (AbandonedMatchResolutionError, ValidationError) as error:
                    form.add_error(None, " ".join(error.messages))
                else:
                    return redirect("match_detail", pk=match.id)

    return render(
        request,
        "league_manager/abandoned_match_resolution.html",
        {
            "league": league,
            "match": match,
            "form": form,
            "goals": goals,
            "cards": cards,
            "decisions": decisions,
            "current_decision": current_decision,
            "can_resolve": can_resolve,
            "can_reopen": can_reopen,
        },
    )


# ==========================================================
# MATCH AGENT MANAGEMENT
# ==========================================================

@login_required
def agent_list(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    memberships = (
        LeagueMembership.objects
        .filter(
            competition=league,
            role=LeagueMembership.Role.AGENT,
        )
        .select_related("user")
        .order_by(
            "-is_active",
            "user__first_name",
            "user__last_name",
            "user__username",
        )
    )

    assignment_counts = dict(
        MatchAdminAssignment.objects
        .filter(
            match__competition=league,
        )
        .values("admin_id")
        .annotate(total=Count("id"))
        .values_list("admin_id", "total")
    )

    for membership in memberships:
        membership.assignment_count = assignment_counts.get(
            membership.user_id,
            0,
        )

    is_owner = user_is_league_owner(
        request.user,
        league,
    )
    can_register_agents = user_manages_competition(
        request.user,
        league,
    )

    return render(
        request,
        "league_manager/agent_list.html",
        {
            "league": league,
            "agent_memberships": memberships,
            "is_owner": is_owner,
            "can_register_agents": can_register_agents,
        },
    )


@login_required
def agent_register(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    if not user_manages_competition(
        request.user,
        league,
    ):
        raise PermissionDenied(
            "Only the league owner or league admin can register match agents."
        )

    form = AgentRegistrationForm(
        request.POST or None,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        agent = form.cleaned_data["user"]

        existing_membership = LeagueMembership.objects.filter(
            user=agent,
            competition=league,
        ).first()

        was_reactivated = bool(
            existing_membership
            and not existing_membership.is_active
        )

        membership, created = LeagueMembership.objects.update_or_create(
            user=agent,
            competition=league,
            defaults={
                "role": LeagueMembership.Role.AGENT,
                "is_active": True,
            },
        )

        action = (
            ActivityLog.Action.AGENT_REACTIVATED
            if was_reactivated
            else ActivityLog.Action.AGENT_REGISTERED
        )

        description = (
            f"Match agent reactivated: {agent}"
            if was_reactivated
            else f"Match agent registered: {agent}"
        )

        record_activity(
            actor=request.user,
            competition=league,
            action=action,
            description=description,
            new_values={
                "user_id": agent.id,
                "username": agent.username,
                "email": agent.email,
                "active": True,
            },
        )

        messages.success(
            request,
            (
                f"{agent.get_full_name() or agent.username} "
                f"is now an active match agent."
            ),
        )

        return redirect(
            "league_manager:agent_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/agent_register.html",
        {
            "league": league,
            "form": form,
        },
    )


@login_required
@require_POST
@transaction.atomic
def agent_toggle_active(request, slug, membership_id):
    league = get_managed_league(
        request,
        slug,
    )

    if not user_is_league_owner(
        request.user,
        league,
    ):
        raise PermissionDenied(
            "Only the league owner can activate or deactivate agents."
        )

    membership = get_object_or_404(
        LeagueMembership.objects.select_related("user"),
        pk=membership_id,
        competition=league,
        role=LeagueMembership.Role.AGENT,
    )

    agent = membership.user

    if membership.is_active:
        assignments = MatchAdminAssignment.objects.filter(
            match__competition=league,
            admin=agent,
        )

        removed_assignment_count = assignments.count()
        assignments.delete()

        membership.is_active = False
        membership.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.AGENT_DEACTIVATED,
            description=(
                f"Match agent deactivated: {agent}. "
                f"{removed_assignment_count} assignment(s) removed."
            ),
            old_values={
                "active": True,
                "assignment_count": removed_assignment_count,
            },
            new_values={
                "active": False,
                "assignment_count": 0,
            },
        )

        messages.success(
            request,
            (
                f"{agent.get_full_name() or agent.username} "
                f"was deactivated and removed from assigned matches."
            ),
        )

    else:
        membership.is_active = True
        membership.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.AGENT_REACTIVATED,
            description=f"Match agent reactivated: {agent}",
            old_values={
                "active": False,
            },
            new_values={
                "active": True,
            },
        )

        messages.success(
            request,
            (
                f"{agent.get_full_name() or agent.username} "
                f"was reactivated."
            ),
        )

    return redirect(
        "league_manager:agent_list",
        slug=league.slug,
    )


@login_required
@transaction.atomic
def assign_agent(request, slug, match_id):
    league = get_managed_league(
        request,
        slug,
    )

    match = get_object_or_404(
        Match.objects.select_related(
            "competition",
            "home_team",
            "away_team",
        ),
        pk=match_id,
        competition=league,
    )

    if match.status in {
        "FINISHED",
        "ABANDONED",
    }:
        messages.error(
            request,
            "An agent cannot be assigned to a completed or abandoned match.",
        )

        return redirect(
            "league_manager:fixture_list",
            slug=league.slug,
        )

    current_assignment = (
        MatchAdminAssignment.objects
        .filter(match=match)
        .select_related("admin")
        .order_by("id")
        .first()
    )

    initial = {}

    if current_assignment:
        initial["agent"] = current_assignment.admin

    form = AgentAssignmentForm(
        request.POST or None,
        league=league,
        initial=initial,
    )

    if request.method == "POST" and form.is_valid():
        agent = form.cleaned_data["agent"]

        previous_agents = list(
            MatchAdminAssignment.objects
            .filter(match=match)
            .select_related("admin")
            .values(
                "admin_id",
                "admin__username",
                "admin__email",
            )
        )

        # The current model allows several assignment rows.
        # Remove all existing rows so this match has one controlling agent.
        MatchAdminAssignment.objects.filter(
            match=match,
        ).delete()

        MatchAdminAssignment.objects.create(
            match=match,
            admin=agent,
        )

        record_activity(
            actor=request.user,
            competition=league,
            match=match,
            action=ActivityLog.Action.AGENT_ASSIGNED,
            description=(
                f"{agent.get_full_name() or agent.username} "
                f"assigned to {match.home_team} vs {match.away_team}."
            ),
            old_values={
                "agents": previous_agents,
            },
            new_values={
                "agent_id": agent.id,
                "username": agent.username,
                "email": agent.email,
            },
        )

        messages.success(
            request,
            (
                f"{agent.get_full_name() or agent.username} "
                f"was assigned to this match."
            ),
        )

        return redirect(
            "league_manager:fixture_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/assign_agent.html",
        {
            "league": league,
            "match": match,
            "form": form,
            "current_assignment": current_assignment,
        },
    )


@login_required
@require_POST
@transaction.atomic
def unassign_agent(request, slug, match_id):
    league = get_managed_league(
        request,
        slug,
    )

    match = get_object_or_404(
        Match.objects.select_related(
            "competition",
            "home_team",
            "away_team",
        ),
        pk=match_id,
        competition=league,
    )

    assignments = (
        MatchAdminAssignment.objects
        .filter(match=match)
        .select_related("admin")
    )

    previous_agents = list(
        assignments.values(
            "admin_id",
            "admin__username",
            "admin__email",
        )
    )

    assignments.delete()

    if previous_agents:
        record_activity(
            actor=request.user,
            competition=league,
            match=match,
            action=ActivityLog.Action.AGENT_UNASSIGNED,
            description=(
                f"Agent unassigned from "
                f"{match.home_team} vs {match.away_team}."
            ),
            old_values={
                "agents": previous_agents,
            },
            new_values={
                "agents": [],
            },
        )

        messages.success(
            request,
            "The match agent was removed from this match.",
        )
    else:
        messages.info(
            request,
            "This match did not have an assigned agent.",
        )

    return redirect(
        "league_manager:fixture_list",
        slug=league.slug,
    )

# ==========================================================
# LEAGUE ADMINISTRATOR MANAGEMENT
# ==========================================================

@login_required
def staff_list(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    if not user_is_league_owner(
        request.user,
        league,
    ):
        raise PermissionDenied(
            "Only the league owner can manage league administrators."
        )

    staff_memberships = (
        LeagueMembership.objects
        .filter(
            competition=league,
            role__in=[
                LeagueMembership.Role.OWNER,
                LeagueMembership.Role.ADMIN,
            ],
        )
        .select_related("user")
        .order_by(
            "role",
            "-is_active",
            "user__first_name",
            "user__last_name",
            "user__username",
        )
    )

    return render(
        request,
        "league_manager/staff_list.html",
        {
            "league": league,
            "staff_memberships": staff_memberships,
        },
    )


@login_required
def staff_register(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    if not user_is_league_owner(
        request.user,
        league,
    ):
        raise PermissionDenied(
            "Only the league owner can add league administrators."
        )

    form = LeagueAdminRegistrationForm(
        request.POST or None,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        administrator = form.cleaned_data["user"]

        previous_membership = LeagueMembership.objects.filter(
            user=administrator,
            competition=league,
        ).first()

        was_reactivated = bool(
            previous_membership
            and previous_membership.role == LeagueMembership.Role.ADMIN
            and not previous_membership.is_active
        )

        membership, created = LeagueMembership.objects.update_or_create(
            user=administrator,
            competition=league,
            defaults={
                "role": LeagueMembership.Role.ADMIN,
                "is_active": True,
            },
        )

        display_name = (
            administrator.get_full_name()
            or administrator.username
        )

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.STAFF_ADDED,
            description=(
                f"League administrator reactivated: {display_name}"
                if was_reactivated
                else f"League administrator added: {display_name}"
            ),
            old_values={
                "active": False,
            } if was_reactivated else {},
            new_values={
                "user_id": administrator.id,
                "username": administrator.username,
                "email": administrator.email,
                "role": LeagueMembership.Role.ADMIN,
                "active": True,
            },
        )

        messages.success(
            request,
            (
                f"{display_name} was reactivated as a league administrator."
                if was_reactivated
                else f"{display_name} was added as a league administrator."
            ),
        )

        return redirect(
            "league_manager:staff_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/staff_register.html",
        {
            "league": league,
            "form": form,
        },
    )


@login_required
@require_POST
def staff_toggle_active(request, slug, membership_id):
    league = get_managed_league(
        request,
        slug,
    )

    if not user_is_league_owner(
        request.user,
        league,
    ):
        raise PermissionDenied(
            "Only the league owner can activate or deactivate administrators."
        )

    membership = get_object_or_404(
        LeagueMembership.objects.select_related("user"),
        pk=membership_id,
        competition=league,
        role=LeagueMembership.Role.ADMIN,
    )

    administrator = membership.user
    display_name = (
        administrator.get_full_name()
        or administrator.username
    )

    if membership.is_active:
        membership.is_active = False
        membership.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.STAFF_REMOVED,
            description=(
                f"League administrator deactivated: {display_name}"
            ),
            old_values={
                "active": True,
            },
            new_values={
                "active": False,
            },
        )

        messages.success(
            request,
            f"{display_name} was deactivated.",
        )

    else:
        membership.is_active = True
        membership.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.STAFF_ADDED,
            description=(
                f"League administrator reactivated: {display_name}"
            ),
            old_values={
                "active": False,
            },
            new_values={
                "active": True,
            },
        )

        messages.success(
            request,
            f"{display_name} was reactivated.",
        )

    return redirect(
        "league_manager:staff_list",
        slug=league.slug,
    )

# ==========================================================
# ACTIVITY HISTORY
# ==========================================================

@login_required
def activity_list(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    activities = (
        ActivityLog.objects
        .filter(competition=league)
        .select_related(
            "actor",
            "match",
            "match__home_team",
            "match__away_team",
        )
    )

    selected_action = request.GET.get(
        "action",
        "",
    ).strip()

    selected_actor = request.GET.get(
        "actor",
        "",
    ).strip()

    search_query = request.GET.get(
        "q",
        "",
    ).strip()

    if selected_action:
        valid_actions = {
            value
            for value, label in ActivityLog.Action.choices
        }

        if selected_action in valid_actions:
            activities = activities.filter(
                action=selected_action,
            )
        else:
            selected_action = ""

    if selected_actor:
        try:
            actor_id = int(selected_actor)
        except (TypeError, ValueError):
            actor_id = None

        if actor_id:
            activities = activities.filter(
                actor_id=actor_id,
            )
        else:
            selected_actor = ""

    if search_query:
        activities = activities.filter(
            Q(description__icontains=search_query)
            | Q(actor__username__icontains=search_query)
            | Q(actor__email__icontains=search_query)
            | Q(actor__first_name__icontains=search_query)
            | Q(actor__last_name__icontains=search_query)
            | Q(match__home_team__name__icontains=search_query)
            | Q(match__away_team__name__icontains=search_query)
        )

    actors = (
        ActivityLog.objects
        .filter(
            competition=league,
            actor__isnull=False,
        )
        .select_related("actor")
        .values(
            "actor_id",
            "actor__username",
            "actor__first_name",
            "actor__last_name",
            "actor__email",
        )
        .distinct()
        .order_by(
            "actor__first_name",
            "actor__last_name",
            "actor__username",
        )
    )

    paginator = Paginator(
        activities,
        25,
    )

    page_number = request.GET.get("page")

    page_obj = paginator.get_page(
        page_number
    )

    return render(
        request,
        "league_manager/activity_list.html",
        {
            "league": league,
            "page_obj": page_obj,
            "activities": page_obj.object_list,
            "action_choices": ActivityLog.Action.choices,
            "actors": actors,
            "selected_action": selected_action,
            "selected_actor": selected_actor,
            "search_query": search_query,
        },
    )

# ==========================================================
# RESULT ENTRY AND CORRECTION
# ==========================================================

@login_required
def result_update(request, slug):
    league = get_managed_league(request, slug)
    current_tab = "finished" if request.GET.get("tab") == "finished" else "pending"
    if current_tab == "finished" and request.method == "POST":
        messages.info(request, "Choose Edit Result next to the finished match you want to correct.")
        return redirect(f"{request.path}?tab=finished")

    base_matches = Match.objects.filter(competition=league).select_related(
        "stage", "group", "home_team", "away_team", "knockout_tie",
    )
    pending_matches = base_matches.filter(
        status__in=["UPCOMING", "LIVE"], match_date__lte=timezone.now(),
    )
    finished_matches = base_matches.filter(status="FINISHED")
    context = {
        "league": league,
        "current_tab": current_tab,
        "pending_count": pending_matches.count(),
        "finished_count": finished_matches.count(),
        "delayed_result_processing": league.matches.filter(
            result_processing__state="PENDING",
            result_processing__pending_since__lt=timezone.now() - timedelta(minutes=2),
        ).exists(),
    }

    if current_tab == "finished":
        can_edit = user_manages_competition(request.user, league)
        can_review = can_manage_abandonment_decisions(request.user, league)
        matches = finished_matches.annotate(has_official_decision=Exists(
            AbandonedMatchDecision.objects.filter(
                match_id=OuterRef("pk"), is_current=True,
                decision_type__in=[
                    AbandonedMatchDecision.DecisionType.AWARD,
                    AbandonedMatchDecision.DecisionType.CONFIRM,
                ],
            ),
        )).order_by("-match_date", "-id")
        context["finished_rows"] = [
            {
                "match": match,
                "can_edit": can_edit and finished_scorer_eligible(match),
                "official_decision": match.abandonment_resolution in OFFICIAL_ABANDONMENT_RESULT_RESOLUTIONS,
                "can_review": can_review and match.has_official_decision,
            }
            for match in matches
        ]
        return render(request, "league_manager/result_update.html", context)

    matches = pending_matches.order_by("match_date", "id")
    row_errors = {}
    submitted_values = {}
    if request.method == "POST":
        proposed_updates = []
        for match in matches:
            scores = {field: request.POST.get(f"{field}_{match.pk}", "").strip() for field in SCORE_FIELDS}
            if not any(scores.values()):
                continue
            submitted_values[match.pk] = scores
            if not scores["home_score"] or not scores["away_score"]:
                row_errors[match.pk] = "Both home and away scores are required."
                continue
            previous_result = result_snapshot(match)
            form = MatchResultForm(scores, instance=match)
            if not form.is_valid():
                row_errors[match.pk] = " ".join(
                    message for errors in form.errors.values() for message in errors
                )
                continue
            proposed_updates.append({
                "match": match, "result": form.normalized_result, "old_values": previous_result,
            })

        if row_errors:
            messages.error(request, "Some scores could not be saved. Correct the highlighted rows and try again.")
        elif not proposed_updates:
            messages.info(request, "Enter at least one final score.")
        else:
            try:
                _save_pending_results(request, league, proposed_updates)
            except ValidationError as error:
                record_blocked_bracket_correction(error, actor=request.user)
                message = " ".join(error.messages)
                messages.error(request, message)
                row_errors = {update["match"].pk: message for update in proposed_updates}
            except PermissionDenied:
                raise
            except OperationalError as error:
                logger.exception("Database error saving results for competition %s", league.pk)
                messages.error(request, RESULT_CONFLICT if is_lock_conflict(error) else RESULT_SAVE_FAILED)
            except Exception:
                logger.exception("Failed to save results for competition %s", league.pk)
                messages.error(request, RESULT_SAVE_FAILED)
            else:
                messages.success(request, RESULT_SAVED)
                return redirect(f"{request.path}?tab=pending")

    context["result_rows"] = [
        {
            "match": match,
            "home_value": submitted_values.get(match.pk, {}).get("home_score", ""),
            "away_value": submitted_values.get(match.pk, {}).get("away_score", ""),
            "regulation_home_value": submitted_values.get(match.pk, {}).get("regulation_home_score", ""),
            "regulation_away_value": submitted_values.get(match.pk, {}).get("regulation_away_score", ""),
            "penalty_home_value": submitted_values.get(match.pk, {}).get("penalty_home_score", ""),
            "penalty_away_value": submitted_values.get(match.pk, {}).get("penalty_away_score", ""),
            "error": row_errors.get(match.pk),
        }
        for match in matches
    ]
    return render(request, "league_manager/result_update.html", context)

# ==========================================================
# GROUP MANAGEMENT
# ==========================================================

@login_required
def group_list(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    group_stage = get_group_stage_for_league(
        league
    )

    groups = (
        Group.objects
        .filter(stage=group_stage)
        .prefetch_related("teams")
        .order_by("name")
    )

    return render(
        request,
        "league_manager/group_list.html",
        {
            "league": league,
            "group_stage": group_stage,
            "groups": groups,
        },
    )


@login_required
def group_create(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    group_stage = get_group_stage_for_league(
        league
    )

    form = GroupForm(
        request.POST or None,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        group = form.save(commit=False)
        group.stage = group_stage
        group.save()
        form.save_m2m()

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.GROUP_CREATED,
            description=f"Group created: {group.name}",
            new_values={
                "group_id": group.id,
                "name": group.name,
                "team_ids": list(
                    group.teams.values_list(
                        "id",
                        flat=True,
                    )
                ),
            },
        )

        messages.success(
            request,
            f"{group.name} was created successfully.",
        )

        if request.POST.get("save_and_add_another") == "1":
            return redirect(
                "league_manager:group_create",
                slug=league.slug,
            )

        return redirect(
            "league_manager:group_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/group_form.html",
        {
            "league": league,
            "form": form,
            "page_title": "Create Group",
            "submit_text": "Create Group",
            "is_create": True,
        },
    )


@login_required
def group_update(request, slug, group_id):
    league = get_managed_league(
        request,
        slug,
    )

    group_stage = get_group_stage_for_league(
        league
    )

    group = get_object_or_404(
        Group.objects.prefetch_related("teams"),
        pk=group_id,
        stage=group_stage,
    )

    old_values = {
        "group_id": group.id,
        "name": group.name,
        "team_ids": list(
            group.teams.values_list(
                "id",
                flat=True,
            )
        ),
    }

    form = GroupForm(
        request.POST or None,
        instance=group,
        league=league,
    )

    if request.method == "POST" and form.is_valid():
        updated_group = form.save()

        record_activity(
            actor=request.user,
            competition=league,
            action=ActivityLog.Action.GROUP_UPDATED,
            description=(
                f"Group updated: {updated_group.name}"
            ),
            old_values=old_values,
            new_values={
                "group_id": updated_group.id,
                "name": updated_group.name,
                "team_ids": list(
                    updated_group.teams.values_list(
                        "id",
                        flat=True,
                    )
                ),
            },
        )

        messages.success(
            request,
            f"{updated_group.name} was updated successfully.",
        )

        return redirect(
            "league_manager:group_list",
            slug=league.slug,
        )

    return render(
        request,
        "league_manager/group_form.html",
        {
            "league": league,
            "group": group,
            "form": form,
            "page_title": "Edit Group",
            "submit_text": "Save Changes",
            "is_create": False,
        },
    )

# ==========================================================
# KNOCKOUT ROUND MANAGEMENT
# ==========================================================

@login_required
@transaction.atomic
def knockout_rounds(request, slug):
    league = get_managed_league(
        request,
        slug,
    )

    if league.format_type not in {
        "GROUP_KNOCKOUT",
        "KNOCKOUT",
    }:
        raise PermissionDenied(
            "This competition does not contain a knockout stage."
        )

    # Round configuration changes the published fixture structure.  Results
    # processing and bracket progression use their dedicated workflows and do
    # not pass through this management endpoint.
    if request.method == "POST" and league.schedule_locked_at:
        messages.error(
            request,
            "This competition schedule is locked. Unlock it before changing knockout rounds or rules.",
        )
        return redirect("league_manager:knockout_rounds", slug=league.slug)

    existing_rounds = (
        Stage.objects
        .filter(
            competition=league,
            stage_type="KNOCKOUT",
        )
        .order_by("order", "id")
    )

    knockout_matches_exist = Match.objects.filter(
        competition=league,
        stage__stage_type="KNOCKOUT",
    ).exists()
    bracket_structure_exists = competition_has_bracket(league)
    knockout_structure_locked = knockout_matches_exist or bracket_structure_exists

    action = request.POST.get("action", "generate")
    form = KnockoutRoundsForm(
        request.POST if request.method == "POST" and action == "generate" else None,
        league=league,
        initial={
            "knockout_size": league.knockout_size,
        },
    )

    rule_form_rows = []
    for stage in existing_rounds:
        rule = FootballKnockoutRoundRule.objects.filter(stage=stage).first()
        rule_exists = rule is not None
        if rule is None:
            # Rendering this form must not create a default rule for legacy
            # data. Saving it is the administrator's explicit configuration.
            rule = FootballKnockoutRoundRule(stage=stage)
        started_or_finished = Match.objects.filter(stage=stage).exclude(
            status="UPCOMING"
        ).exists()
        # Legacy rounds with no rule still need an explicit first-time
        # configuration, even if their fixtures have already started.
        locked = rule_exists and started_or_finished
        rule_form_rows.append({
            "stage": stage,
            "locked": locked,
            "rule_exists": rule_exists,
            "form": KnockoutRoundRuleForm(
                request.POST if request.method == "POST" and action == "save_rules" else None,
                instance=rule,
                prefix=f"round-{stage.id}",
                locked=locked,
            ),
        })

    if request.method == "POST":
        if action == "save_rules":
            if not rule_form_rows:
                messages.error(request, "Configure the knockout rounds first.")
            else:
                valid_forms = [row["form"].is_valid() for row in rule_form_rows]
                if all(valid_forms):
                    changed_rounds = []
                    for row in rule_form_rows:
                        if row["locked"]:
                            continue
                        if row["rule_exists"] and not row["form"].has_changed():
                            continue
                        rule = row["form"].save()
                        changed_rounds.append({
                            "stage": row["stage"].name,
                            "number_of_legs": rule.number_of_legs,
                            "decider_policy": rule.decider_policy,
                        })

                    if changed_rounds:
                        record_activity(
                            actor=request.user,
                            competition=league,
                            action=ActivityLog.Action.KNOCKOUT_ROUNDS_CONFIGURED,
                            description="Football knockout round rules updated.",
                            new_values={"football_rules": changed_rounds},
                        )
                        messages.success(
                            request,
                            "The football knockout rules were saved successfully.",
                        )
                    else:
                        messages.info(request, "No unlocked round rules changed.")
                    return redirect(
                        "league_manager:knockout_rounds",
                        slug=league.slug,
                    )

        elif knockout_structure_locked:
            messages.error(
                request,
                (
                    "The knockout structure is locked because "
                    "knockout fixtures or a connected bracket already exist."
                ),
            )

            return redirect(
                "league_manager:knockout_rounds",
                slug=league.slug,
            )

        elif form.is_valid():
            old_values = {
                "knockout_size": league.knockout_size,
                "rounds": list(
                    existing_rounds.values_list(
                        "name",
                        flat=True,
                    )
                ),
            }

            try:
                created_rounds = configure_knockout_rounds(
                    league,
                    form.cleaned_data["knockout_size"],
                )
            except ValidationError as error:
                form.add_error(
                    None,
                    error,
                )
            else:
                league.refresh_from_db()

                new_round_names = [
                    stage.name
                    for stage in created_rounds
                ]

                record_activity(
                    actor=request.user,
                    competition=league,
                    action=(
                        ActivityLog.Action
                        .KNOCKOUT_ROUNDS_CONFIGURED
                    ),
                    description=(
                        "Knockout rounds configured: "
                        + ", ".join(new_round_names)
                    ),
                    old_values=old_values,
                    new_values={
                        "knockout_size": league.knockout_size,
                        "rounds": new_round_names,
                    },
                )

                messages.success(
                    request,
                    (
                        "The knockout rounds were configured "
                        "successfully."
                    ),
                )

                return redirect(
                    "league_manager:knockout_rounds",
                    slug=league.slug,
                )

    existing_rounds = (
        Stage.objects
        .filter(
            competition=league,
            stage_type="KNOCKOUT",
        )
        .annotate(
            fixture_count=Count("matches")
        )
        .order_by("order", "id")
    )

    return render(
        request,
        "league_manager/knockout_rounds.html",
        {
            "league": league,
            "form": form,
            "existing_rounds": existing_rounds,
            "knockout_matches_exist": knockout_structure_locked,
            "bracket_structure_exists": bracket_structure_exists,
            "rule_form_rows": rule_form_rows,
        },
    )


@login_required
def knockout_bracket(request, slug):
    """Create, seed, validate and generate the explicit connected bracket."""
    league = get_managed_league(request, slug)
    if league.format_type not in {"GROUP_KNOCKOUT", "KNOCKOUT"}:
        raise PermissionDenied("This competition does not contain a knockout stage.")

    # Seeding, linking, scheduling and explicit fixture generation all alter
    # the bracket's published structure. Match-result progression happens in
    # matches.bracket and remains available through its validated internal
    # workflow.
    if request.method == "POST" and league.schedule_locked_at:
        messages.error(
            request,
            "This competition schedule is locked. Unlock it before changing the knockout bracket.",
        )
        return redirect("league_manager:knockout_bracket", slug=league.slug)

    ties = list(
        bracket_ties_for_competition(league).select_related(
            "stage",
            "winner",
            "winner_destination",
        ).order_by("stage__order", "position", "id")
    )
    action = request.POST.get("action", "")

    first_round_slots = []
    if ties:
        first_stage_id = min(
            ties,
            key=lambda tie: (tie.stage.order, tie.stage_id),
        ).stage_id
        first_round_slots = list(
            FootballKnockoutTieSlot.objects.select_related(
                "tie",
                "tie__stage",
                "team",
            ).filter(
                tie__stage_id=first_stage_id,
                tie__position__isnull=False,
            ).order_by("tie__position", "slot")
        )

    seeding_form = (
        BracketSeedingForm(
            request.POST if request.method == "POST" and action == "seed_manual" else None,
            competition=league,
            slots=first_round_slots,
        )
        if ties
        else None
    )
    random_draw_form = (
        BracketRandomDrawForm(
            request.POST if request.method == "POST" and action == "seed_random" else None,
            competition=league,
        )
        if ties
        else None
    )
    schedule_form = (
        BracketScheduleForm(
            request.POST if request.method == "POST" and action == "save_schedule" else None,
            ties=ties,
        )
        if ties
        else None
    )
    link_form = (
        BracketLinkForm(
            request.POST if request.method == "POST" and action == "save_links" else None,
            ties=ties,
        )
        if ties
        else None
    )

    if request.method == "POST":
        if action == "create":
            if ties:
                messages.error(request, "A connected bracket already exists.")
            else:
                try:
                    created_ties = create_bracket_structure(league)
                except ValidationError as error:
                    messages.error(request, " ".join(error.messages))
                else:
                    links = [
                        {
                            "source_tie_id": tie.id,
                            "destination_tie_id": tie.winner_destination_id,
                            "destination_slot": tie.winner_destination_slot,
                        }
                        for tie in created_ties
                        if tie.winner_destination_id
                    ]
                    record_activity(
                        actor=request.user,
                        competition=league,
                        action=ActivityLog.Action.BRACKET_CREATED,
                        description="Connected knockout bracket created.",
                        new_values={"tie_ids": [tie.id for tie in created_ties]},
                    )
                    record_activity(
                        actor=request.user,
                        competition=league,
                        action=ActivityLog.Action.BRACKET_LINKED,
                        description="Initial bracket winner links created.",
                        new_values={"links": links},
                    )
                    messages.success(
                        request,
                        "The bracket structure and its explicit winner links were created.",
                    )
                    return redirect("league_manager:knockout_bracket", slug=league.slug)

        elif not ties:
            messages.error(request, "Create the connected bracket structure first.")

        elif action == "seed_manual":
            if seeding_form.is_valid():
                try:
                    seeded_ties = save_manual_seeding(
                        league,
                        seeding_form.selections(),
                    )
                except ValidationError as error:
                    seeding_form.add_error(None, error)
                else:
                    record_activity(
                        actor=request.user,
                        competition=league,
                        action=ActivityLog.Action.BRACKET_SEEDED,
                        description="Bracket teams were placed manually.",
                        new_values={
                            "team_ids": sorted(
                                {
                                    slot.team_id
                                    for slot in FootballKnockoutTieSlot.objects.filter(
                                        tie__in=seeded_ties,
                                        team__isnull=False,
                                    )
                                }
                            ),
                            "method": "manual",
                        },
                    )
                    messages.success(request, "The bracket was seeded successfully.")
                    return redirect("league_manager:knockout_bracket", slug=league.slug)

        elif action == "seed_random":
            if random_draw_form.is_valid():
                try:
                    random_seed_bracket(
                        league,
                        random_draw_form.cleaned_data["teams"],
                    )
                except ValidationError as error:
                    random_draw_form.add_error(None, error)
                else:
                    record_activity(
                        actor=request.user,
                        competition=league,
                        action=ActivityLog.Action.BRACKET_SEEDED,
                        description="Bracket teams were placed by random draw.",
                        new_values={
                            "team_ids": list(
                                random_draw_form.cleaned_data["teams"].values_list(
                                    "id",
                                    flat=True,
                                )
                            ),
                            "method": "random",
                        },
                    )
                    messages.success(request, "The random draw was saved successfully.")
                    return redirect("league_manager:knockout_bracket", slug=league.slug)

        elif action == "save_schedule":
            if schedule_form.is_valid():
                try:
                    save_bracket_schedule(league, schedule_form.cleaned_schedules)
                except ValidationError as error:
                    schedule_form.add_error(None, error)
                else:
                    messages.success(
                        request,
                        "Fixture kickoffs were saved. Fixtures will use these exact times.",
                    )
                    return redirect("league_manager:knockout_bracket", slug=league.slug)

        elif action == "save_links":
            if link_form.is_valid():
                previous_links = {
                    tie.id: (tie.winner_destination_id, tie.winner_destination_slot)
                    for tie in ties
                }
                try:
                    linked_ties = save_bracket_links(league, link_form.cleaned_links)
                except ValidationError as error:
                    link_form.add_error(None, error)
                else:
                    updated_links = {
                        tie.id: (tie.winner_destination_id, tie.winner_destination_slot)
                        for tie in linked_ties
                    }
                    if previous_links != updated_links:
                        record_activity(
                            actor=request.user,
                            competition=league,
                            action=ActivityLog.Action.BRACKET_LINKED,
                            description="Bracket winner links were updated.",
                            old_values={"links": previous_links},
                            new_values={"links": updated_links},
                        )
                        messages.success(request, "Bracket links were updated.")
                    else:
                        messages.info(request, "No bracket link changes were detected.")
                    return redirect("league_manager:knockout_bracket", slug=league.slug)

        elif action == "generate_fixtures":
            try:
                generated_ties = generate_bracket_matches(league, actor=request.user)
            except ValidationError as error:
                messages.error(request, " ".join(error.messages))
            else:
                match_count = Match.objects.filter(knockout_tie__in=generated_ties).count()
                messages.success(
                    request,
                    f"Bracket validation passed. {match_count} fixture(s) are now available.",
                )
                return redirect("league_manager:fixture_list", slug=league.slug)

        else:
            messages.error(request, "Choose a valid bracket operation.")

    slots_by_tie = {}
    for slot in FootballKnockoutTieSlot.objects.select_related("team").filter(
        tie__in=ties
    ):
        slots_by_tie.setdefault(slot.tie_id, {})[slot.slot] = slot

    bracket_rounds = []
    for tie in ties:
        if not bracket_rounds or bracket_rounds[-1]["stage"].id != tie.stage_id:
            bracket_rounds.append({"stage": tie.stage, "ties": []})
        tie_slots = slots_by_tie.get(tie.id, {})
        bracket_rounds[-1]["ties"].append({
            "tie": tie,
            "home_slot": tie_slots.get(FootballKnockoutTieSlot.Slot.HOME),
            "away_slot": tie_slots.get(FootballKnockoutTieSlot.Slot.AWAY),
        })

    return render(
        request,
        "league_manager/knockout_bracket.html",
        {
            "league": league,
            "has_bracket": bool(ties),
            "bracket_rounds": bracket_rounds,
            "seeding_form": seeding_form,
            "random_draw_form": random_draw_form,
            "schedule_form": schedule_form,
            "link_form": link_form,
            "first_round_slots": first_round_slots,
            "bracket_locked": bool(
                ties
                and (
                    any(tie.winner_id for tie in ties)
                    or Match.objects.filter(knockout_tie__in=ties).exists()
                )
            ),
        },
    )


@login_required
def team_coach_list(request, slug, team_id):
    league = get_managed_league(request, slug)
    if not user_is_league_owner(request.user, league):
        raise PermissionDenied("Only the league owner can manage team coaches.")

    team = get_object_or_404(Team, pk=team_id, competition=league)
    assignments = TeamCoachAssignment.objects.filter(team=team).select_related(
        "user"
    )

    return render(
        request,
        "league_manager/team_coach_list.html",
        {
            "league": league,
            "team": team,
            "coach_assignments": assignments,
        },
    )


@login_required
def team_coach_assign(request, slug, team_id):
    league = get_managed_league(request, slug)
    if not user_is_league_owner(request.user, league):
        raise PermissionDenied("Only the league owner can assign team coaches.")

    team = get_object_or_404(Team, pk=team_id, competition=league)
    form = TeamCoachRegistrationForm(request.POST or None, team=team)

    if request.method == "POST" and form.is_valid():
        coach = form.cleaned_data["user"]
        assignment, created = TeamCoachAssignment.objects.get_or_create(
            team=team,
            user=coach,
        )

        if not created:
            form.add_error("identifier", "This user is already a coach for this team.")
        else:
            display_name = coach.get_full_name() or coach.username
            record_activity(
                actor=request.user,
                competition=league,
                action=ActivityLog.Action.COACH_ASSIGNED,
                description=f"Team coach assigned: {display_name} to {team.name}",
                new_values={
                    "team_id": team.id,
                    "user_id": coach.id,
                    "username": coach.username,
                },
            )
            messages.success(request, f"{display_name} is now a coach for {team.name}.")
            return redirect(
                "league_manager:team_coach_list",
                slug=league.slug,
                team_id=team.id,
            )

    return render(
        request,
        "league_manager/team_coach_assign.html",
        {
            "league": league,
            "team": team,
            "form": form,
        },
    )


@login_required
@require_POST
def team_coach_remove(request, slug, team_id, assignment_id):
    league = get_managed_league(request, slug)
    if not user_is_league_owner(request.user, league):
        raise PermissionDenied("Only the league owner can remove team coaches.")

    team = get_object_or_404(Team, pk=team_id, competition=league)
    assignment = get_object_or_404(
        TeamCoachAssignment.objects.select_related("user"),
        pk=assignment_id,
        team=team,
    )
    coach = assignment.user
    assignment.delete()

    record_activity(
        actor=request.user,
        competition=league,
        action=ActivityLog.Action.COACH_REMOVED,
        description=f"Team coach removed: {coach} from {team.name}",
        old_values={"team_id": team.id, "user_id": coach.id},
    )
    messages.success(request, f"{coach.get_full_name() or coach.username} was removed as team coach.")
    return redirect(
        "league_manager:team_coach_list",
        slug=league.slug,
        team_id=team.id,
    )


@transaction.atomic
def _save_pending_results(request, league, updates):
    league = lock_result_competition(league.pk, nowait=True)
    if not user_manages_competition(request.user, league):
        raise PermissionDenied("You do not have permission to update these results.")
    saved = []
    for update in sorted(updates, key=lambda update: update["match"].pk):
        try:
            match = Match.objects.select_for_update(of=("self",), nowait=True).select_related(
                "competition", "stage", "home_team", "away_team", "knockout_tie",
            ).get(pk=update["match"].pk, competition=league)
        except Match.DoesNotExist as error:
            raise ValidationError(RESULT_CONFLICT) from error
        if match.competition_id != league.pk or result_snapshot(match) != update["old_values"]:
            raise ValidationError(RESULT_CONFLICT)
        result = update["result"]
        finalize_match_result_fast(
            match=match,
            home_score=result["home_score"],
            away_score=result["away_score"],
            regulation_home_score=result["regulation_home_score"],
            regulation_away_score=result["regulation_away_score"],
            penalty_home_score=result["penalty_home_score"],
            penalty_away_score=result["penalty_away_score"],
            actor=request.user,
        )
        record_activity(
            actor=request.user,
            competition=league,
            match=match,
            action=ActivityLog.Action.MATCH_FINISHED,
            description=f"Final result entered: {match.home_team} {match.home_score}-{match.away_score} {match.away_team}",
            old_values=update["old_values"],
            new_values=result_snapshot(match),
        )
        saved.append(match.pk)

    return saved
