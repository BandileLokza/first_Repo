from typing import Optional

from django.db.models import Q

from .models import LeagueMembership, OrganisationAdministrator, RegionalAdministrator


MANAGER_ROLES = (
    LeagueMembership.Role.OWNER,
    LeagueMembership.Role.ADMIN,
)


def valid_authenticated_user(user) -> bool:
    return bool(
        user
        and user.is_authenticated
        and user.is_active
    )


def competition_is_read_only_for(user, competition) -> bool:
    """Whether an archived season is read-only for this actor.

    Archived league history remains public.  The existing superuser control
    paths intentionally remain available for exceptional corrections.
    """

    return bool(
        getattr(competition, "is_archived", False)
        and not getattr(user, "is_superuser", False)
    )


def user_is_regional_administrator(user, region) -> bool:
    """Whether the user has an active appointment for exactly this Region."""
    return bool(
        valid_authenticated_user(user)
        and region
        and RegionalAdministrator.objects.filter(
            user=user,
            region=region,
            is_active=True,
        ).exists()
    )


def can_manage_region(user, region) -> bool:
    return bool(
        valid_authenticated_user(user)
        and (getattr(user, "is_superuser", False) or user_is_regional_administrator(user, region))
    )


def user_is_organisation_administrator(user, organisation) -> bool:
    """Whether the user holds this exact non-regional scope appointment."""
    return bool(
        valid_authenticated_user(user)
        and organisation in OrganisationAdministrator.Organisation.values
        and OrganisationAdministrator.objects.filter(
            user=user,
            organisation=organisation,
            is_active=True,
        ).exists()
    )


def can_manage_organisation(user, organisation) -> bool:
    return bool(
        valid_authenticated_user(user)
        and (
            getattr(user, "is_superuser", False)
            or user_is_organisation_administrator(user, organisation)
        )
    )


def user_manages_series(user, series) -> bool:
    """Apply the smallest matching administrative scope to a league series."""
    if not series or not valid_authenticated_user(user):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if series.league_type == series.LeagueType.REGIONAL:
        return bool(series.region_id and can_manage_region(user, series.region))
    return can_manage_organisation(user, series.league_type)


def get_active_membership(
    user,
    competition,
) -> Optional[LeagueMembership]:
    if not valid_authenticated_user(user):
        return None

    return (
        LeagueMembership.objects
        .filter(
            user=user,
            competition=competition,
            is_active=True,
        )
        .first()
    )


def user_is_league_owner(user, competition) -> bool:
    if not valid_authenticated_user(user):
        return False

    if user.is_superuser:
        return True

    if competition_is_read_only_for(user, competition):
        return False

    return user_manages_series(user, competition.series) or LeagueMembership.objects.filter(
        user=user,
        competition=competition,
        role=LeagueMembership.Role.OWNER,
        is_active=True,
    ).exists()


def user_is_league_admin(user, competition) -> bool:
    if not valid_authenticated_user(user):
        return False

    if user.is_superuser:
        return True

    if competition_is_read_only_for(user, competition):
        return False

    return LeagueMembership.objects.filter(
        user=user,
        competition=competition,
        role=LeagueMembership.Role.ADMIN,
        is_active=True,
    ).exists()


def user_manages_competition(user, competition) -> bool:
    if not valid_authenticated_user(user):
        return False

    if user.is_superuser:
        return True

    if competition_is_read_only_for(user, competition):
        return False

    return user_manages_series(user, competition.series) or LeagueMembership.objects.filter(
        user=user,
        competition=competition,
        role__in=MANAGER_ROLES,
        is_active=True,
    ).exists()


def managed_competitions_queryset(user):
    """Return exactly the dashboard's direct management union.

    Keeping this in the permission layer makes search an additional filter on
    an authorized set, rather than a new lookup path that could disclose a
    competition by name.
    """

    from leagues.models import Competition

    if not valid_authenticated_user(user):
        return Competition.objects.none()
    if user.is_superuser:
        return Competition.objects.all()

    organisations = OrganisationAdministrator.objects.filter(
        user=user,
        is_active=True,
    ).values_list("organisation", flat=True)
    return Competition.objects.filter(
        Q(
            memberships__user=user,
            memberships__role__in=MANAGER_ROLES,
            memberships__is_active=True,
        )
        | Q(
            series__region__regional_administrators__user=user,
            series__region__regional_administrators__is_active=True,
        )
        | Q(series__league_type__in=organisations)
    ).distinct()


def can_delete_fixture(user, match) -> bool:
    """Deletion is deliberately narrower than normal league management."""

    if not valid_authenticated_user(user):
        return False
    if user.is_superuser:
        return True

    if competition_is_read_only_for(user, match.competition):
        return False

    series = getattr(match.competition, "series", None)
    return bool(
        series
        and series.league_type == series.LeagueType.REGIONAL
        and series.region_id
        and user_is_regional_administrator(user, series.region)
    )


def user_is_registered_agent(user, competition) -> bool:
    if not valid_authenticated_user(user):
        return False

    if competition_is_read_only_for(user, competition):
        return False

    return LeagueMembership.objects.filter(
        user=user,
        competition=competition,
        role=LeagueMembership.Role.AGENT,
        is_active=True,
    ).exists()


def user_is_assigned_agent(user, match) -> bool:
    """
    A match agent must satisfy both conditions:

    1. They have an active AGENT membership for this league.
    2. They are assigned to this exact match.
    """
    if not valid_authenticated_user(user):
        return False

    if competition_is_read_only_for(user, match.competition):
        return False

    from matches.models import MatchAdminAssignment

    has_agent_membership = LeagueMembership.objects.filter(
        user=user,
        competition=match.competition,
        role=LeagueMembership.Role.AGENT,
        is_active=True,
    ).exists()

    if not has_agent_membership:
        return False

    return MatchAdminAssignment.objects.filter(
        match=match,
        admin=user,
    ).exists()


def user_is_team_coach(user, team) -> bool:
    """Return whether a user has an active, team-specific coach assignment."""
    if not valid_authenticated_user(user):
        return False

    if competition_is_read_only_for(user, team.competition):
        return False

    from teams.models import TeamCoachAssignment

    return TeamCoachAssignment.objects.filter(
        user=user,
        team=team,
    ).exists()


def can_manage_team_lineup(user, match, team) -> bool:
    """Only a coach assigned to a participating team can manage its lineup."""
    if not valid_authenticated_user(user):
        return False

    if competition_is_read_only_for(user, match.competition):
        return False

    if team.id not in {match.home_team_id, match.away_team_id}:
        return False

    if match.status in {"FINISHED", "ABANDONED"}:
        return False

    return user_is_team_coach(user, team)


def can_unlock_team_lineups(user, match) -> bool:
    """Lineup unlocks are reserved for league managers before kickoff."""
    return (
        match.status == "UPCOMING"
        and user_manages_competition(user, match.competition)
    )

def can_control_match(user, match) -> bool:
    """
    Normal match-detail controls are available only while
    the match is active.

    Superuser:
        Can control any active match.

    League owner/admin:
        Can control active matches in their league.

    Match agent:
        Can control only their assigned active match.
    """
    if not valid_authenticated_user(user):
        return False

    if competition_is_read_only_for(user, match.competition):
        return False

    # Finished and abandoned matches must not be restarted
    # from the normal match-detail controls.
    if match.status in {"FINISHED", "ABANDONED"}:
        return False

    if user.is_superuser:
        return True

    if user_manages_competition(
        user,
        match.competition,
    ):
        return True

    return user_is_assigned_agent(user, match)

def can_correct_finished_result(user, match) -> bool:
    """
    Agents cannot correct completed match results.

    Only an EsportsLive superuser or a league owner/admin
    can perform an audited result correction.
    """
    if not valid_authenticated_user(user):
        return False

    if user.is_superuser:
        return True

    return user_manages_competition(
        user,
        match.competition,
    )


def can_manage_abandonment_decisions(user, competition) -> bool:
    """Authorize decisions and corrections within the administrator's scope."""
    if not valid_authenticated_user(user) or competition_is_read_only_for(user, competition):
        return False
    if user.is_superuser:
        return True
    series = competition.series
    if series and series.league_type == OrganisationAdministrator.Organisation.PLE:
        # PLE retains its existing competition management policy.
        return user_manages_competition(user, competition)
    return user_manages_series(user, series)


def can_resolve_abandoned_match(user, match) -> bool:
    return match.status == "ABANDONED" and can_manage_abandonment_decisions(user, match.competition)
