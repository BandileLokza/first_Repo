"""Conservative reconciliation for moving football teams between seasons."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from matches.models import Match
from teams.models import Team

from .models import Competition, Group, Standing


STANDING_ACTIVITY_FIELDS = (
    "played", "won", "drawn", "lost", "goals_for", "goals_against", "points",
    "baseline_played", "baseline_won", "baseline_drawn", "baseline_lost",
    "baseline_goals_for", "baseline_goals_against", "baseline_points",
)


def standing_has_activity(standing):
    """Whether a row contains results that must never be moved automatically."""

    return any(getattr(standing, field) != 0 for field in STANDING_ACTIVITY_FIELDS)


def competition_has_team_matches(competition, team):
    return Match.objects.filter(competition=competition).filter(
        Q(home_team=team) | Q(away_team=team)
    ).exists()


def _destination_table_stage(competition):
    stages = list(
        competition.stages.filter(stage_type="LEAGUE").order_by("order", "id")[:2]
    )
    if len(stages) != 1:
        raise ValidationError(
            "The destination Table Only competition must have exactly one league stage."
        )
    return stages[0]


def ensure_destination_standing(team, competition):
    """Create only the normal zero TABLE row for a moved/repaired team."""

    if competition.format_type != "TABLE":
        return None
    stage = _destination_table_stage(competition)
    return Standing.objects.get_or_create(
        competition=competition,
        stage=stage,
        group=None,
        team=team,
    )[0]


def validate_team_competition_move(team, destination_competition):
    """Raise a clear error if moving this team could rewrite league history."""

    if team.competition_id == destination_competition.id:
        return
    old_competition = team.competition
    if competition_has_team_matches(old_competition, team):
        raise ValidationError(
            "This team cannot be moved to another competition because fixtures "
            "already exist for it in the current competition."
        )
    old_standings = Standing.objects.filter(
        competition=old_competition, team=team
    )
    if any(standing_has_activity(standing) for standing in old_standings):
        raise ValidationError(
            "This team cannot be moved to another competition because its current "
            "competition standings contain recorded statistics."
        )
    # Validate destination structure before removing any old zero-value rows.
    if destination_competition.format_type == "TABLE":
        _destination_table_stage(destination_competition)


@transaction.atomic
def move_team_to_competition(team, destination_competition, *, actor=None):
    """Safely move a team without rewriting fixtures, results, or player data."""

    team = Team.objects.select_for_update().select_related("competition").get(pk=team.pk)
    destination = Competition.objects.select_for_update().get(pk=destination_competition.pk)
    if team.competition_id == destination.id:
        return team

    old_competition = team.competition
    old_standings = list(
        Standing.objects.select_for_update().filter(
            competition=old_competition, team=team
        )
    )
    if competition_has_team_matches(old_competition, team):
        raise ValidationError(
            "This team cannot be moved to another competition because fixtures "
            "already exist for it in the current competition."
        )
    if any(standing_has_activity(standing) for standing in old_standings):
        raise ValidationError(
            "This team cannot be moved to another competition because its current "
            "competition standings contain recorded statistics."
        )
    if destination.format_type == "TABLE":
        _destination_table_stage(destination)

    # Remove only stale structures owned by the competition being left.
    old_groups = list(
        Group.objects.select_for_update().filter(
            stage__competition=old_competition,
            teams=team,
        )
    )
    for group in old_groups:
        group.teams.remove(team)
    Standing.objects.filter(pk__in=[standing.pk for standing in old_standings]).delete()

    team.competition = destination
    team.save(update_fields=["competition"])
    ensure_destination_standing(team, destination)
    return team
