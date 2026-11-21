"""Safe football league-season archive and cloning services.

Each season remains a separate ``Competition``.  These services intentionally
never reset, recalculate, or copy result-derived data from the source season.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from league_manager.models import LeagueMembership
from matches.models import Match, Player
from teams.models import Team

from .models import Competition, CompetitionSeries, Standing
from .services import (
    ensure_competition_structure,
    replace_competition_tie_breakers,
)


ARCHIVABLE_FORMAT = "TABLE"
UNRESOLVED_MATCH_STATUSES = ("UPCOMING", "LIVE")
COPIED_MEMBERSHIP_ROLES = (
    LeagueMembership.Role.OWNER,
    LeagueMembership.Role.ADMIN,
)

# A roster is normally small, but an imported league can contain hundreds of
# players.  Batching keeps a season rollover to a bounded number of database
# round trips without holding an unbounded player list in application memory.
BULK_CREATE_BATCH_SIZE = 500


def _require_superuser(actor):
    if not (
        actor
        and actor.is_authenticated
        and actor.is_active
        and actor.is_superuser
    ):
        raise PermissionDenied(
            "Only a superuser can archive a league season or create its next season."
        )


def _validate_table_competition(competition):
    if competition.format_type != ARCHIVABLE_FORMAT:
        raise ValidationError(
            "Only Table Only football competitions can use season archiving."
        )


@transaction.atomic
def archive_competition_season(competition, *, actor):
    """Archive one completed TABLE competition without touching its history."""

    _require_superuser(actor)

    competition = (
        Competition.objects.select_for_update(of=("self",))
        .select_related("series")
        .get(pk=competition.pk)
    )
    _validate_table_competition(competition)

    if competition.is_archived:
        raise ValidationError("This football league season is already archived.")

    # Lock existing fixtures before deciding eligibility, so a concurrent result
    # or fixture update cannot slip through the archive decision mid-transaction.
    unresolved_matches = list(
        Match.objects.select_for_update()
        .filter(competition=competition)
        .filter(
            Q(status__in=UNRESOLVED_MATCH_STATUSES)
            | Q(status="ABANDONED", abandonment_resolution="")
        )
        .values_list("id", flat=True)
    )
    if unresolved_matches:
        raise ValidationError(
            "This football league cannot be archived while it has live, upcoming, or unresolved abandoned matches."
        )

    from matches.models import MatchResultProcessingOutbox

    if MatchResultProcessingOutbox.objects.filter(
        match__competition=competition, state=MatchResultProcessingOutbox.State.PENDING,
    ).exists():
        raise ValidationError(
            "Wait for pending standings and prediction updates before archiving this season."
        )
    competition.is_archived = True
    competition.archived_at = timezone.now()
    competition.archived_by = actor
    competition.save(
        update_fields=["is_archived", "archived_at", "archived_by"]
    )
    return competition


@transaction.atomic
def create_next_competition_season(source_competition, *, actor, season, slug):
    """Create a clean new TABLE season under an archived competition's series."""

    _require_superuser(actor)

    source_competition = (
        Competition.objects.select_for_update()
        .select_related("series")
        .get(pk=source_competition.pk)
    )
    _validate_table_competition(source_competition)

    if not source_competition.is_archived:
        raise ValidationError(
            "Archive this football league season before creating its next season."
        )

    season = (season or "").strip()
    slug = (slug or "").strip()
    if not season:
        raise ValidationError("Enter a new season label.")
    if not slug:
        raise ValidationError("Enter a new competition slug.")

    # Serialize competing next-season requests for this same series.  The
    # database constraint on (series, season) remains the final race-safe guard.
    series = CompetitionSeries.objects.select_for_update().get(
        pk=source_competition.series_id
    )
    if Competition.objects.filter(series=series, season__iexact=season).exists():
        raise ValidationError("This league series already has that season label.")
    if Competition.objects.filter(slug=slug).exists():
        raise ValidationError("This football competition slug is already in use.")

    next_competition = Competition(
        name=source_competition.name,
        slug=slug,
        season=season,
        series=series,
        format_type=ARCHIVABLE_FORMAT,
        predictions_enabled=source_competition.predictions_enabled,
    )
    # The series is locked and persisted above, while the two uniqueness
    # checks have just run under that lock. Keep field/model validation but do
    # not repeat those lookups; the database constraints remain race-safe.
    next_competition.full_clean(
        exclude={"series"},
        validate_unique=False,
        validate_constraints=False,
    )
    next_competition.save()

    # Team post-save standing creation requires the new TABLE stage to exist.
    # We create the equivalent standing rows explicitly below because the
    # batched team insert deliberately bypasses Django post-save signals.
    stages = ensure_competition_structure(next_competition)
    league_stage = next(
        stage for stage in stages if stage.stage_type == "LEAGUE"
    )

    source_tie_breakers = list(
        source_competition.tie_breakers.order_by("order", "id").values_list(
            "order",
            "criterion",
        )
    )
    if source_tie_breakers:
        replace_competition_tie_breakers(next_competition, source_tie_breakers)

    source_teams = list(source_competition.teams.order_by("id"))
    cloned_teams = []
    team_by_source_id = {}
    for source_team in source_teams:
        next_team = Team(
            name=source_team.name,
            home_ground=source_team.home_ground,
            logo=source_team.logo,
            competition=next_competition,
        )
        # Team has no custom save behaviour or uniqueness constraint. Keep
        # validation of copied fields while omitting the redundant foreign-key
        # existence check: ``next_competition`` was inserted above in this
        # transaction and the database foreign key is the final guard.
        next_team.full_clean(
            exclude={"competition"},
            validate_unique=False,
            validate_constraints=False,
        )
        cloned_teams.append(next_team)
        team_by_source_id[source_team.pk] = next_team

    Team.objects.bulk_create(cloned_teams, batch_size=BULK_CREATE_BATCH_SIZE)

    # ``Team`` normally receives a post-save signal which creates this row.
    # bulk_create() does not emit that signal, so reproduce the required,
    # zero-value TABLE standing explicitly and atomically.
    Standing.objects.bulk_create(
        [
            Standing(
                competition=next_competition,
                stage=league_stage,
                team=next_team,
            )
            for next_team in cloned_teams
        ],
        batch_size=BULK_CREATE_BATCH_SIZE,
    )

    # Player has neither custom save logic nor lifecycle signals. Stream it
    # rather than prefetching every roster into memory, and insert fixed-size
    # batches.  The source-team to new-team map guarantees that every cloned
    # player points to the corresponding *new* Team row.
    player_batch = []
    source_players = (
        Player.objects.filter(team__competition=source_competition)
        .order_by("team_id", "id")
        .iterator(chunk_size=BULK_CREATE_BATCH_SIZE)
    )
    for source_player in source_players:
        next_player = Player(
            name=source_player.name,
            photo=source_player.photo,
            team=team_by_source_id[source_player.team_id],
            jersey_number=source_player.jersey_number,
            starting_goals=0,
        )
        # Retain validation of copied fields. ``team`` is intentionally
        # excluded because it is a known, just-inserted clone target; asking
        # full_clean() to prove that foreign key exists creates one SELECT per
        # player. The database foreign key retains the integrity guarantee.
        next_player.full_clean(
            exclude={"team"},
            validate_unique=False,
            validate_constraints=False,
        )
        player_batch.append(next_player)
        if len(player_batch) == BULK_CREATE_BATCH_SIZE:
            Player.objects.bulk_create(
                player_batch,
                batch_size=BULK_CREATE_BATCH_SIZE,
            )
            player_batch.clear()

    if player_batch:
        Player.objects.bulk_create(
            player_batch,
            batch_size=BULK_CREATE_BATCH_SIZE,
        )

    # Preserve only league-level owner/admin access.  Agents are deliberately
    # not copied, and coach assignments remain an explicit new-season decision.
    source_memberships = source_competition.memberships.filter(
        role__in=COPIED_MEMBERSHIP_ROLES
    ).order_by("id")
    next_memberships = [
        LeagueMembership(
            user_id=source_membership.user_id,
            competition=next_competition,
            role=source_membership.role,
            is_active=source_membership.is_active,
        )
        for source_membership in source_memberships
    ]
    # The source constraint guarantees these are unique. The destination is a
    # freshly-created competition, and its database constraint remains the
    # final integrity guard for future model changes or concurrent writes.
    LeagueMembership.objects.bulk_create(
        next_memberships,
        batch_size=BULK_CREATE_BATCH_SIZE,
    )

    return next_competition
