"""Competition-scoped permissions for the isolated pool domain."""

from typing import Optional

from django.db.models import Q

from .models import (
    PoolCompetition,
    PoolCompetitionMembership,
    PoolCompetitionTeamStaffAssignment,
    PoolMatchScorerAssignment,
)


MANAGER_ROLES = (
    PoolCompetitionMembership.Role.OWNER,
    PoolCompetitionMembership.Role.ADMIN,
)


def _archived_for_normal_user(user, competition) -> bool:
    """Archived seasons stay viewable but cannot be operated by normal staff."""

    return bool(
        getattr(competition, "is_archived", False)
        and not getattr(user, "is_superuser", False)
    )


def managed_pool_competitions(user):
    """Return only competitions this user is authorised to manage.

    This is intentionally different from the public/preview queryset: a
    published competition never becomes manageable merely because it is
    visible.  Manager views should derive their dashboard and direct-object
    lookups from this queryset.
    """
    if not valid_authenticated_user(user):
        return PoolCompetition.objects.none()
    if getattr(user, "is_superuser", False):
        return PoolCompetition.objects.all()
    return PoolCompetition.objects.filter(
        Q(owner=user)
        | Q(
            memberships__user=user,
            memberships__is_active=True,
            memberships__role__in=MANAGER_ROLES,
        )
    ).distinct()


def user_has_pool_management_access(user) -> bool:
    """Whether a user can open the pool manager dashboard at all."""
    if not valid_authenticated_user(user):
        return False
    return bool(getattr(user, "is_superuser", False)) or managed_pool_competitions(
        user
    ).exists()


def valid_authenticated_user(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
    )


def get_active_pool_membership(
    user, competition: PoolCompetition
) -> Optional[PoolCompetitionMembership]:
    if not valid_authenticated_user(user):
        return None
    return (
        PoolCompetitionMembership.objects.filter(
            user=user,
            competition=competition,
            is_active=True,
        )
        .order_by("id")
        .first()
    )


def user_is_pool_competition_owner(user, competition: PoolCompetition) -> bool:
    if not valid_authenticated_user(user):
        return False
    if _archived_for_normal_user(user, competition):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if competition.owner_id == user.id:
        return True
    return PoolCompetitionMembership.objects.filter(
        user=user,
        competition=competition,
        role=PoolCompetitionMembership.Role.OWNER,
        is_active=True,
    ).exists()


def user_is_pool_competition_admin(user, competition: PoolCompetition) -> bool:
    if not valid_authenticated_user(user):
        return False
    if _archived_for_normal_user(user, competition):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return PoolCompetitionMembership.objects.filter(
        user=user,
        competition=competition,
        role=PoolCompetitionMembership.Role.ADMIN,
        is_active=True,
    ).exists()


def user_can_manage_competition(user, competition: PoolCompetition) -> bool:
    """Whether the user can manage this one pool competition only."""
    if not valid_authenticated_user(user):
        return False
    if _archived_for_normal_user(user, competition):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return competition.owner_id == user.id or PoolCompetitionMembership.objects.filter(
        user=user,
        competition=competition,
        role__in=MANAGER_ROLES,
        is_active=True,
    ).exists()


def user_manages_pool_competition(user, competition: PoolCompetition) -> bool:
    """Compatibility-friendly public name for manager checks in views."""
    return user_can_manage_competition(user, competition)


def user_can_preview_competition(user, competition: PoolCompetition) -> bool:
    """Draft competition previews are limited to that competition's managers."""
    return user_can_manage_competition(user, competition)


def user_is_pool_team_staff(user, competition_entry) -> bool:
    """Return whether a user has an active coach/captain assignment for one entry.

    This intentionally does *not* feed the manager queryset.  Team staff can
    submit their own fixture lineups but never inherit result scoring,
    standings, bracket, roster, or unrelated-team access.
    """

    if not valid_authenticated_user(user) or not getattr(competition_entry, "pk", None):
        return False
    return PoolCompetitionTeamStaffAssignment.objects.filter(
        competition_entry=competition_entry,
        user=user,
        is_active=True,
    ).exists()


def user_can_manage_team_lineup(user, pool_match, competition_entry) -> bool:
    """Narrow entry-scoped access for coaches/captains and manager overrides."""

    if not valid_authenticated_user(user):
        return False
    if not getattr(pool_match, "pk", None) or not getattr(competition_entry, "pk", None):
        return False
    if _archived_for_normal_user(user, pool_match.competition):
        return False
    if competition_entry.pk not in {
        pool_match.home_entry_id,
        pool_match.away_entry_id,
    }:
        return False
    if competition_entry.competition_id != pool_match.competition_id:
        return False
    return user_can_manage_competition(user, pool_match.competition) or user_is_pool_team_staff(
        user,
        competition_entry,
    )
def user_is_pool_match_scorer(user, pool_match) -> bool:
    """Whether this user is an active scorer for this exact pool fixture."""

    if not valid_authenticated_user(user) or not getattr(pool_match, "pk", None):
        return False

    return PoolMatchScorerAssignment.objects.filter(
        match=pool_match,
        user=user,
        is_active=True,
    ).exists()


def user_can_score_pool_match(user, pool_match) -> bool:
    """Managers may score normally; assigned scorers may score only this match."""

    if not valid_authenticated_user(user):
        return False
    if _archived_for_normal_user(user, pool_match.competition):
        return False

    if user_can_manage_competition(user, pool_match.competition):
        return True

    return user_is_pool_match_scorer(user, pool_match)
