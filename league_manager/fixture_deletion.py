"""Shared, transaction-safe football fixture deletion workflow.

The League Manager and the Django admin both delete fixtures through this
module.  Keeping the policy here prevents an administrative bulk action from
bypassing the audit trail, knockout safeguards, or standings rebuild that the
League Manager already provides.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError

from leagues.models import Competition
from matches.models import AbandonedMatchDecision, FootballKnockoutTie, Match
from matches.services import rebuild_league_standings

from .models import ActivityLog
from .permissions import can_delete_fixture


class FixtureDeletionBlocked(ValidationError):
    """Raised when any fixture in an all-or-nothing deletion is ineligible."""


def fixture_deletion_block_reason(
    fixture,
    *,
    knockout_tie=None,
    has_linked_replays=None,
    has_abandonment_history=None,
):
    """Return the reason this fixture cannot be safely removed, if any."""

    if has_linked_replays is None:
        has_linked_replays = Match.objects.filter(replay_of_id=fixture.pk).exists()
    if has_linked_replays:
        return (
            "This abandoned fixture has linked replay history and cannot be deleted. "
            "Keep the audited resolution record instead."
        )

    if has_abandonment_history is None:
        has_abandonment_history = AbandonedMatchDecision.objects.filter(
            Q(match_id=fixture.pk) | Q(replay_match_id=fixture.pk)
        ).exists()
    if has_abandonment_history:
        return (
            "This fixture has official abandonment decision history and cannot be deleted. "
            "Keep the audited decision record instead."
        )

    tie = knockout_tie
    if tie is None and fixture.knockout_tie_id:
        tie = fixture.knockout_tie
    if not tie:
        return None
    if tie.position is not None:
        return (
            "Connected knockout-bracket fixtures cannot be deleted individually. "
            "Regenerate or correct the bracket through its safeguarded workflow."
        )
    if tie.winner_id:
        return (
            "This knockout fixture cannot be deleted because its tie already has "
            "a recorded winner."
        )
    return None


def fixture_deletion_snapshot(fixture):
    """Keep enough context for an audit entry after the fixture is gone."""

    series = fixture.competition.series
    return {
        "match_id": fixture.id,
        "stage_id": fixture.stage_id,
        "stage": fixture.stage.name if fixture.stage else None,
        "group_id": fixture.group_id,
        "group": fixture.group.name if fixture.group else None,
        "home_team_id": fixture.home_team_id,
        "home_team": fixture.home_team.name,
        "away_team_id": fixture.away_team_id,
        "away_team": fixture.away_team.name,
        "match_date": fixture.match_date.isoformat() if fixture.match_date else None,
        "location": fixture.location or "",
        "gameweek": fixture.gameweek,
        "leg": fixture.leg,
        "status": fixture.status,
        "generated_by_system": fixture.generated_by_system,
        "competition_id": fixture.competition_id,
        "competition": fixture.competition.name,
        "series_id": series.id,
        "series": series.name,
        "league_type": series.league_type,
        "region": series.region.name if series.region_id else None,
        "organisation": series.league_type if not series.region_id else None,
        "schedule_locked": bool(fixture.competition.schedule_locked_at),
        "schedule_locked_at": (
            fixture.competition.schedule_locked_at.isoformat()
            if fixture.competition.schedule_locked_at
            else None
        ),
    }


def _normalise_fixture_ids(fixture_ids):
    """Return unique integer primary keys while retaining selection order."""

    normalized = []
    seen = set()
    for fixture_id in fixture_ids:
        try:
            fixture_id = int(fixture_id)
        except (TypeError, ValueError):
            raise FixtureDeletionBlocked("The selected fixture list is invalid.")
        if fixture_id not in seen:
            seen.add(fixture_id)
            normalized.append(fixture_id)
    if not normalized:
        raise FixtureDeletionBlocked("Select at least one fixture to delete.")
    return normalized


def _fixtures_queryset():
    return Match.objects.select_related(
        "competition",
        "competition__series",
        "competition__series__region",
        "stage",
        "group",
        "home_team",
        "away_team",
        "knockout_tie",
    ).order_by("competition_id", "pk")


def _fixture_label(fixture):
    return f"Fixture #{fixture.pk} ({fixture.home_team.name} vs {fixture.away_team.name})"


def _validate_fixture_set(actor, fixtures, expected_ids, *, knockout_ties=None):
    """Validate every selected fixture before anything is deleted."""

    by_id = {fixture.pk: fixture for fixture in fixtures}
    missing_ids = [fixture_id for fixture_id in expected_ids if fixture_id not in by_id]
    if missing_ids:
        formatted_ids = ", ".join(str(fixture_id) for fixture_id in missing_ids)
        raise FixtureDeletionBlocked(
            f"The selected fixture or fixtures no longer exist: {formatted_ids}."
        )

    replay_source_ids = set(
        Match.objects.filter(replay_of_id__in=by_id).values_list("replay_of_id", flat=True)
    )
    abandonment_history_ids = {
        fixture_id
        for decision_ids in AbandonedMatchDecision.objects.filter(
            Q(match_id__in=by_id) | Q(replay_match_id__in=by_id)
        ).values_list("match_id", "replay_match_id")
        for fixture_id in decision_ids
        if fixture_id is not None
    }
    reasons = []
    for fixture in fixtures:
        if not can_delete_fixture(actor, fixture):
            reasons.append(
                f"{_fixture_label(fixture)}: you do not have permission to delete it."
            )
            continue
        reason = fixture_deletion_block_reason(
            fixture,
            knockout_tie=(knockout_ties or {}).get(fixture.knockout_tie_id),
            has_linked_replays=fixture.pk in replay_source_ids,
            has_abandonment_history=fixture.pk in abandonment_history_ids,
        )
        if reason:
            reasons.append(f"{_fixture_label(fixture)}: {reason}")

    if reasons:
        raise FixtureDeletionBlocked(reasons)


def validate_fixture_deletion(*, actor, fixture_ids):
    """Validate a proposed selection for the confirmation step.

    The final deletion validates again while holding row locks, so a change
    between the confirmation page and its POST cannot result in a partial
    operation.
    """

    fixture_ids = _normalise_fixture_ids(fixture_ids)
    fixtures = list(_fixtures_queryset().filter(pk__in=fixture_ids))
    _validate_fixture_set(actor, fixtures, fixture_ids)
    return fixtures


def delete_fixtures(*, actor, fixture_ids):
    """Delete an all-or-nothing fixture selection and rebuild only affected tables."""

    fixture_ids = _normalise_fixture_ids(fixture_ids)
    with transaction.atomic():
        # Lock parent competitions first in a stable order. This serialises the
        # deletion with schedule, generation, and result work for each league.
        competition_ids = list(
            Match.objects.filter(pk__in=fixture_ids)
            .order_by("competition_id")
            .values_list("competition_id", flat=True)
            .distinct()
        )
        competitions = {
            competition.pk: competition
            for competition in Competition.objects.select_for_update(of=("self",))
            .select_related("series", "series__region")
            .filter(pk__in=competition_ids)
            .order_by("pk")
        }

        # ``of=("self",)`` avoids trying to lock the nullable knockout-tie
        # side of the join on PostgreSQL. Ties are locked explicitly below.
        fixtures = list(
            _fixtures_queryset()
            .select_for_update(of=("self",))
            .filter(pk__in=fixture_ids)
        )
        _validate_fixture_set(actor, fixtures, fixture_ids)

        fixture_competition_ids = {fixture.competition_id for fixture in fixtures}
        extra_competition_ids = fixture_competition_ids.difference(competitions)
        if extra_competition_ids:
            competitions.update(
                {
                    competition.pk: competition
                    for competition in Competition.objects.select_for_update(of=("self",))
                    .select_related("series", "series__region")
                    .filter(pk__in=extra_competition_ids)
                    .order_by("pk")
                }
            )
        for fixture in fixtures:
            # Use the locked parent row for permission and audit state.
            fixture.competition = competitions[fixture.competition_id]

        knockout_tie_ids = {
            fixture.knockout_tie_id
            for fixture in fixtures
            if fixture.knockout_tie_id is not None
        }
        knockout_ties = {
            tie.pk: tie
            for tie in FootballKnockoutTie.objects.select_for_update().filter(
                pk__in=knockout_tie_ids
            )
        }
        _validate_fixture_set(
            actor,
            fixtures,
            fixture_ids,
            knockout_ties=knockout_ties,
        )

        # A durable per-fixture audit is preserved even for an admin bulk
        # operation. Match is intentionally set before the cascade; its
        # SET_NULL relation leaves the snapshot intact afterwards.
        ActivityLog.objects.bulk_create(
            [
                ActivityLog(
                    actor=actor,
                    competition=fixture.competition,
                    match=fixture,
                    action=ActivityLog.Action.FIXTURE_DELETED,
                    description=(
                        f"Fixture deleted: {fixture.home_team.name} vs "
                        f"{fixture.away_team.name}"
                    ),
                    old_values=fixture_deletion_snapshot(fixture),
                )
                for fixture in fixtures
            ]
        )

        # A table rebuild is needed only when a deleted source could have
        # contributed to the materialised standings.  ABANDONED fixtures
        # normally never do; ``table_updated`` protects legacy/manual records
        # where an already-applied result was later changed to abandoned.
        standings_rebuild_ids = {
            fixture.competition_id
            for fixture in fixtures
            if fixture.stage.stage_type in {"LEAGUE", "GROUP"}
            and (fixture.status == "FINISHED" or fixture.table_updated)
        }

        # Django's collector cascades match-owned goals, cards, lineups,
        # assignments, predictions, and related event history. Teams, players,
        # competitions, and unrelated fixtures are not deletion targets.
        try:
            Match.objects.filter(pk__in=fixture_ids).delete()
        except ProtectedError as error:
            raise FixtureDeletionBlocked(
                "A selected fixture is linked to protected match history and cannot be deleted."
            ) from error

        # Standings are materialized. Rebuild once per affected competition,
        # after every selected fixture has gone, rather than per match. An
        # unresolved abandonment cannot be a source row, so skipping it does
        # not retain stale points; if any selected source did contribute, the
        # whole competition is rebuilt from the remaining finished fixtures.
        for competition_id in sorted(standings_rebuild_ids):
            rebuild_league_standings(competitions[competition_id])

    return len(fixtures)
