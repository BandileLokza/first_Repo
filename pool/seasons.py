"""Small, transactional lifecycle services for archived pool league seasons.

Historical results are never reset here.  A fresh competition is created in
the same series and receives only the deliberately safe setup data.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolMatch,
    PoolTeamRoster,
)
from .standings import (
    replace_pool_standings_points_configuration,
    replace_pool_tie_breakers,
)


class PoolSeasonValidationError(ValidationError):
    """A clear domain error for the archive and next-season workflows."""


_UNRESOLVED_MATCH_STATUSES = (
    PoolMatch.Status.UPCOMING,
    PoolMatch.Status.LIVE,
    PoolMatch.Status.POSTPONED,
)


def _require_superuser(actor):
    if not getattr(actor, "is_authenticated", False) or not getattr(
        actor, "is_superuser", False
    ):
        raise PoolSeasonValidationError(
            "Only a superuser can archive a season or create its next season."
        )


def archive_eligibility_errors(competition):
    """Return all safe, user-facing reasons a pool season cannot archive."""

    errors = []
    if competition.format_type != PoolCompetition.FormatType.LEAGUE:
        errors.append("Only pool LEAGUE competitions can be archived.")
    if competition.operational_state != PoolCompetition.OperationalState.COMPLETED:
        errors.append("Complete the pool league before archiving its season.")
    if competition.matches.filter(status__in=_UNRESOLVED_MATCH_STATUSES).exists():
        errors.append(
            "Resolve every upcoming, live, or postponed fixture before archiving."
        )
    if competition.is_archived:
        errors.append("This pool season is already archived.")
    return errors


def ensure_pool_competition_can_archive(competition):
    errors = archive_eligibility_errors(competition)
    if errors:
        raise PoolSeasonValidationError(errors)


def archive_pool_competition(competition, *, actor):
    """Archive a completed pool league without changing any historic data."""

    _require_superuser(actor)
    if not getattr(competition, "pk", None):
        raise PoolSeasonValidationError("A saved pool competition is required.")

    with transaction.atomic():
        locked = PoolCompetition.objects.select_for_update().get(pk=competition.pk)
        ensure_pool_competition_can_archive(locked)
        # Use a direct update deliberately: the archive operation must change
        # archive metadata only, without rerunning provisioning signals.
        PoolCompetition.objects.filter(pk=locked.pk).update(
            is_archived=True,
            archived_at=timezone.now(),
            archived_by=actor,
        )
        locked.refresh_from_db()
        return locked


def _validate_next_season_source(source):
    if source.format_type != PoolCompetition.FormatType.LEAGUE:
        raise PoolSeasonValidationError(
            "Only archived pool LEAGUE competitions can start a next season."
        )
    if not source.is_archived:
        raise PoolSeasonValidationError(
            "Archive this pool season before creating its next season."
        )
    if source.stages.exclude(
        stage_type=PoolCompetitionStage.StageType.LEAGUE
    ).exists():
        raise PoolSeasonValidationError(
            "This league has additional stage topology and cannot be safely cloned "
            "as a league-only season."
        )


def _validate_next_season_identity(source, *, season, slug):
    season = (season or "").strip()
    slug = (slug or "").strip()
    errors = []
    if not season:
        errors.append("Enter a new season label.")
    elif len(season) > 20:
        errors.append("Season labels must be 20 characters or fewer.")
    if not slug:
        errors.append("Enter a new competition slug.")
    elif PoolCompetition.objects.filter(slug=slug).exists():
        errors.append("This competition slug is already in use.")
    if season and PoolCompetition.objects.filter(
        series=source.series,
        season=season,
    ).exists():
        errors.append("This series already has that season label.")
    if errors:
        raise PoolSeasonValidationError(errors)
    return season, slug


def _copy_pool_memberships(source, target):
    """Carry only competition owners/admins, never operational assignments."""

    memberships = PoolCompetitionMembership.objects.filter(
        competition=source,
        role__in=(
            PoolCompetitionMembership.Role.OWNER,
            PoolCompetitionMembership.Role.ADMIN,
        ),
    ).order_by("id")
    for membership in memberships:
        # The PoolCompetition post-save signal has already synchronised the
        # direct owner on the fresh competition.  Retain that one invariant
        # instead of overwriting it with historical membership state.
        if membership.role == PoolCompetitionMembership.Role.OWNER:
            continue
        PoolCompetitionMembership.objects.update_or_create(
            competition=target,
            user=membership.user,
            defaults={
                "role": PoolCompetitionMembership.Role.ADMIN,
                "is_active": membership.is_active,
            },
        )


def _copy_pool_configuration(source, target):
    """Copy validated league policy through its existing service boundaries."""

    tie_breakers = list(
        source.tie_breakers.order_by("order", "id").values_list("order", "criterion")
    )
    replace_pool_tie_breakers(target, tie_breakers)

    margin_rules = ()
    if source.points_mode == PoolCompetition.PointsMode.MARGIN_BASED:
        margin_rules = tuple(
            source.margin_point_rules.order_by("minimum_margin", "id").values_list(
                "minimum_margin",
                "maximum_margin",
                "winner_points",
                "loser_points",
            )
        )
    replace_pool_standings_points_configuration(
        target,
        points_mode=source.points_mode,
        points_for_win=source.points_for_win,
        points_for_loss=source.points_for_loss,
        margin_rules=margin_rules,
    )


def _copy_pool_entries_and_rosters(source, target):
    """Reuse global people/teams while creating fresh season-scoped rows."""

    source_entries = list(
        source.entries.select_related("player", "team").order_by("id")
    )
    entry_map = {}
    for source_entry in source_entries:
        target_entry = PoolCompetitionEntry.objects.create(
            competition=target,
            player=source_entry.player if source_entry.player_id else None,
            team=source_entry.team if source_entry.team_id else None,
        )
        entry_map[source_entry.pk] = target_entry

    if source.participant_mode != PoolCompetition.ParticipantMode.TEAMS:
        return

    rosters = PoolTeamRoster.objects.filter(
        competition_entry__in=source_entries
    ).select_related("player").order_by("competition_entry_id", "sort_order", "id")
    for roster in rosters:
        PoolTeamRoster.objects.create(
            competition_entry=entry_map[roster.competition_entry_id],
            player=roster.player,
            is_active=roster.is_active,
            sort_order=roster.sort_order,
        )


def create_next_pool_season(source, *, season, slug, actor):
    """Create a zero-result league season under the source's existing series."""

    _require_superuser(actor)
    if not getattr(source, "pk", None):
        raise PoolSeasonValidationError("A saved pool competition is required.")

    try:
        with transaction.atomic():
            source = (
                PoolCompetition.objects.select_for_update()
                .select_related("series", "owner")
                .get(pk=source.pk)
            )
            _validate_next_season_source(source)
            season, slug = _validate_next_season_identity(
                source,
                season=season,
                slug=slug,
            )

            # A fresh season begins privately and with no calendar or result
            # lifecycle copied from its history.  A margin policy is applied
            # only after creation through the validated replacement service.
            target = PoolCompetition(
                name=source.name,
                slug=slug,
                series=source.series,
                season=season,
                description=source.description,
                logo=source.logo,
                cover_image=source.cover_image,
                participant_mode=source.participant_mode,
                format_type=PoolCompetition.FormatType.LEAGUE,
                visibility=PoolCompetition.Visibility.DRAFT,
                operational_state=PoolCompetition.OperationalState.UPCOMING,
                default_race_target=source.default_race_target,
                points_for_win=source.points_for_win,
                points_for_loss=source.points_for_loss,
                points_mode=PoolCompetition.PointsMode.FIXED,
                third_place_match_enabled=False,
                match_time_limit_minutes=None,
                allow_two_lives=False,
                start_date=None,
                end_date=None,
                venue=source.venue,
                rules=source.rules,
                owner=source.owner,
            )
            target.save()
            _copy_pool_configuration(source, target)
            _copy_pool_memberships(source, target)
            _copy_pool_entries_and_rosters(source, target)
            return target
    except IntegrityError as error:
        raise PoolSeasonValidationError(
            "A competition with this season label or slug already exists."
        ) from error
