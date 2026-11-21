"""Competition-specific standings ranking and head-to-head resolution.

This module deliberately ranks already-calculated ``Standing`` rows.  Match
result services remain responsible for updating the tracked statistics, while
this service applies a competition's configured order at display time.
"""

from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Aggregate, Case, JSONField, OuterRef, QuerySet, Subquery, When
from django.db.models.functions import JSONObject

from matches.models import Match

from .models import CompetitionTieBreaker, Standing
from .services import DEFAULT_COMPETITION_TIE_BREAKERS


Criterion = CompetitionTieBreaker.Criterion

DEFAULT_TIE_BREAKERS = tuple(
    criterion
    for _position, criterion in DEFAULT_COMPETITION_TIE_BREAKERS
)

HEAD_TO_HEAD_CRITERIA = frozenset(
    {
        Criterion.HEAD_TO_HEAD_POINTS,
        Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE,
        Criterion.HEAD_TO_HEAD_GOALS_SCORED,
        Criterion.HEAD_TO_HEAD_AWAY_GOALS,
    }
)


class _ResultArray(Aggregate):
    function = "JSONB_AGG"
    output_field = JSONField()

    def as_sqlite(self, compiler, connection, **extra_context):
        return self.as_sql(compiler, connection, function="JSON_GROUP_ARRAY", **extra_context)


@dataclass(frozen=True)
class _AppliedMatch:
    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int


@dataclass
class _MiniTableRow:
    """In-memory statistics for one team within a tied mini-table."""

    points: int = 0
    goals_for: int = 0
    goals_against: int = 0
    away_goals: int = 0

    @property
    def goal_difference(self):
        return self.goals_for - self.goals_against


def configured_tie_breakers(competition):
    """Return a safe, deterministic criterion sequence for ``competition``.

    Database and Django-admin validation normally guarantee this shape.  The
    defensive normalization keeps old/programmatically-created competitions
    safe while the data migration is rolled out: points is always primary and
    an absent configuration preserves the former points/GD/GF behavior.
    """

    configured = list(
        competition.tie_breakers.order_by("order", "id").values_list(
            "criterion",
            flat=True,
        )
    )
    if not configured:
        return DEFAULT_TIE_BREAKERS

    valid_values = {value for value, _label in Criterion.choices}
    remaining = [
        criterion
        for criterion in configured
        if criterion in valid_values and criterion != Criterion.POINTS
    ]
    return (Criterion.POINTS, *remaining)


def rank_standings(competition, standings):
    """Return ``standings`` in the configured order for one table scope.

    ``standings`` must contain rows from one competition/stage/group table.
    Overall criteria read the baseline-inclusive ``Standing.total_*`` values.
    Head-to-head criteria use only completed match records in the same scope.
    """

    criteria = configured_tie_breakers(competition)
    if HEAD_TO_HEAD_CRITERIA.intersection(criteria):
        rows = _standings_with_applied_matches(competition, standings)
        scoped_matches = [
            _AppliedMatch(**match)
            for row in rows
            for match in (row.applied_matches or [])
        ]
    else:
        rows = list(standings)
        scoped_matches = []

    if len(rows) < 2:
        return rows
    _validate_standing_scope(competition, rows)
    first_head_to_head_index = next(
        (
            index
            for index, criterion in enumerate(criteria)
            if criterion in HEAD_TO_HEAD_CRITERIA
        ),
        None,
    )

    return _rank_tied_group(
        rows,
        criteria,
        scoped_matches,
        criterion_index=0,
        first_head_to_head_index=first_head_to_head_index,
    )


def _validate_standing_scope(competition, rows):
    scope = (
        rows[0].competition_id,
        rows[0].stage_id,
        rows[0].group_id,
    )
    if scope[0] != competition.id:
        raise ValueError("Standings must belong to the supplied competition.")

    for row in rows[1:]:
        if (
            row.competition_id,
            row.stage_id,
            row.group_id,
        ) != scope:
            raise ValueError(
                "Standings ranking requires one competition, stage and group scope."
            )


def _standings_with_applied_matches(competition, standings):
    """Read totals and head-to-head inputs in a single database statement."""
    if not isinstance(standings, QuerySet):
        original_rows = list(standings)
        if not original_rows:
            return []
        _validate_standing_scope(competition, original_rows)
        standings = Standing.objects.filter(
            pk__in=[row.pk for row in original_rows],
        ).select_related("team")

    matches = Match.objects.filter(
        competition_id=OuterRef("competition_id"),
        stage_id=OuterRef("stage_id"),
        home_team_id=OuterRef("team_id"),
        status="FINISHED",
        table_updated=True,
        table_home_score__isnull=False,
        table_away_score__isnull=False,
    ).order_by().values("home_team_id").annotate(
        score_rows=_ResultArray(JSONObject(
            home_team_id="home_team_id",
            away_team_id="away_team_id",
            home_score="table_home_score",
            away_score="table_away_score",
        )),
    ).values("score_rows")
    # Each match appears once, under its home standing. The mini-table still
    # limits both teams to the selected standings and the current tied group.
    return list(standings.annotate(
        applied_matches=Case(
            When(group_id__isnull=True, then=Subquery(matches.filter(group_id__isnull=True))),
            default=Subquery(matches.filter(group_id=OuterRef("group_id"))),
            output_field=JSONField(),
        ),
    ))


def _rank_tied_group(
    rows,
    criteria,
    scoped_matches,
    *,
    criterion_index,
    first_head_to_head_index,
):
    if len(rows) < 2:
        return rows

    if criterion_index >= len(criteria):
        return _deterministic_order(rows)

    criterion = criteria[criterion_index]
    values = _criterion_values(criterion, rows, scoped_matches)
    buckets = defaultdict(list)
    for row in rows:
        buckets[values[row.team_id]].append(row)

    # The criterion cannot currently distinguish this tied group.  For
    # incomplete head-to-head schedules this is intentional: use the completed
    # meetings that exist, then continue safely to the next configured rule.
    if len(buckets) == 1:
        return _rank_tied_group(
            rows,
            criteria,
            scoped_matches,
            criterion_index=criterion_index + 1,
            first_head_to_head_index=first_head_to_head_index,
        )

    ranked = []
    for value in sorted(buckets, key=_value_sort_key, reverse=True):
        subgroup = buckets[value]
        if len(subgroup) == 1:
            ranked.extend(subgroup)
            continue

        # A multi-team head-to-head result can split one team away while a
        # smaller subgroup remains level.  Rebuild that subgroup's mini-table
        # from the first configured head-to-head criterion.  The subgroup is
        # strictly smaller after a split, so this cannot recurse forever.
        if (
            criterion in HEAD_TO_HEAD_CRITERIA
            and len(rows) > 2
            and first_head_to_head_index is not None
        ):
            next_index = first_head_to_head_index
        else:
            next_index = criterion_index + 1

        ranked.extend(
            _rank_tied_group(
                subgroup,
                criteria,
                scoped_matches,
                criterion_index=next_index,
                first_head_to_head_index=first_head_to_head_index,
            )
        )

    return ranked


def _criterion_values(criterion, rows, scoped_matches):
    if criterion == Criterion.POINTS:
        return {row.team_id: row.total_points for row in rows}
    if criterion == Criterion.OVERALL_GOAL_DIFFERENCE:
        return {row.team_id: row.total_goal_difference for row in rows}
    if criterion == Criterion.OVERALL_GOALS_SCORED:
        return {row.team_id: row.total_goals_for for row in rows}
    if criterion == Criterion.WINS:
        return {row.team_id: row.total_won for row in rows}
    if criterion in HEAD_TO_HEAD_CRITERIA:
        mini_table = _mini_table(rows, scoped_matches)
        if criterion == Criterion.HEAD_TO_HEAD_POINTS:
            return {team_id: entry.points for team_id, entry in mini_table.items()}
        if criterion == Criterion.HEAD_TO_HEAD_GOAL_DIFFERENCE:
            return {
                team_id: entry.goal_difference
                for team_id, entry in mini_table.items()
            }
        if criterion == Criterion.HEAD_TO_HEAD_GOALS_SCORED:
            return {team_id: entry.goals_for for team_id, entry in mini_table.items()}
        return {team_id: entry.away_goals for team_id, entry in mini_table.items()}

    # Yellow-card records alone do not establish a fair-play policy and there
    # is no red-card/disciplinary-point data.  FAIR_PLAY therefore remains a
    # recognized but non-separating criterion until the event model is richer.
    # DRAWING_OF_LOTS is likewise deliberately manual: never randomize rows.
    return {row.team_id: None for row in rows}


def _mini_table(rows, scoped_matches):
    team_ids = {row.team_id for row in rows}
    table = {team_id: _MiniTableRow() for team_id in team_ids}

    for match in scoped_matches:
        if (
            match.home_team_id not in team_ids
            or match.away_team_id not in team_ids
        ):
            continue

        home = table[match.home_team_id]
        away = table[match.away_team_id]
        home.goals_for += match.home_score
        home.goals_against += match.away_score
        away.goals_for += match.away_score
        away.goals_against += match.home_score
        away.away_goals += match.away_score

        # These are the same 3/1/0 values used by
        # matches.services.apply_result_to_standings.
        if match.home_score > match.away_score:
            home.points += 3
        elif match.home_score < match.away_score:
            away.points += 3
        else:
            home.points += 1
            away.points += 1

    return table


def _value_sort_key(value):
    """Keep uncomputable/manual criteria tied below numeric values."""

    return value is not None, value if value is not None else 0


def _deterministic_order(rows):
    return sorted(
        rows,
        key=lambda row: (row.team.name.casefold(), row.team_id),
    )
