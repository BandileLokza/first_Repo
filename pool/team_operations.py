"""Transactional operations for the local five-player team match format.

This module deliberately keeps a team score sheet separate from
``PoolRack``.  A scheduled row records the fixed position pairing and its
live lifecycle; a ``PoolRack`` is only created when that individual race has
actually been completed.  This lets several physical tables complete in a
different order without changing the immutable score-sheet sequence.
"""

from __future__ import annotations

from collections.abc import Mapping

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    PoolTeamMatchAuditEvent,
    PoolTeamMatchLineup,
    PoolTeamMatchLineupMember,
    PoolTeamMatchLineupPosition,
    PoolTeamMatchScheduledRack,
    PoolTeamMatchSubstitution,
    PoolTeamRoster,
)
from .services import (
    PoolResultValidationError,
    _authoritative_scores,
    _bracket_preflight,
    _ensure_rack_can_be_corrected,
    _ensure_scoring_is_open,
    _expire_locked_match_if_due,
    _lock_match,
    _locked_rack,
    _record_result_revision,
    _refresh_match_derived_state,
    _synchronise_locked_match_score,
    _TIMER_EXPIRY_BLOCKED,
    _timer_expiry_message,
    snapshot_match_result,
)


# The local score sheet is intentionally data, rather than inferred from a
# match/rack id.  Positions are stable; the player occupying one is resolved
# only when a scheduled rack starts.
TEAM_MATCH_SCORE_SHEET = (
    (1, 1, 1), (1, 2, 2), (1, 3, 3), (1, 4, 4), (1, 5, 5),
    (2, 2, 3), (2, 3, 4), (2, 4, 5), (2, 5, 1), (2, 1, 2),
    (3, 3, 5), (3, 4, 1), (3, 5, 2), (3, 1, 3), (3, 2, 4),
    (4, 4, 2), (4, 5, 3), (4, 1, 4), (4, 2, 5), (4, 3, 1),
    (5, 5, 4), (5, 1, 5), (5, 2, 1), (5, 3, 2), (5, 4, 3),
)
TEAM_LINEUP_POSITIONS = frozenset(range(1, 6))


def is_team_fixture(match) -> bool:
    """Return whether a fixture uses the local team-match operations path."""

    return bool(
        match
        and getattr(match, "competition", None)
        and match.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
    )


def _entry_id(value):
    return getattr(value, "pk", value)


def _player_id(value):
    return getattr(value, "pk", value)


def _require_team_fixture(match):
    if not is_team_fixture(match):
        raise PoolResultValidationError("Team match operations are only available for team fixtures.")


def _require_match_entry(match, entry):
    entry_id = _entry_id(entry)
    if entry_id not in {match.home_entry_id, match.away_entry_id}:
        raise PoolResultValidationError("Select one of the two team entries in this match.")
    return entry_id


def _audit(match, *, action, actor=None, team_entry=None, payload=None):
    """Append a narrow, match-operation audit record when available."""

    return PoolTeamMatchAuditEvent.objects.create(
        match=match,
        team_entry_id=_entry_id(team_entry) if team_entry is not None else None,
        action=action,
        actor=actor,
        payload=payload or {},
    )


def _normalise_players(values, *, label):
    """Return stable integer player ids from form/service input."""

    result = []
    for value in values or ():
        player_id = _player_id(value)
        if not isinstance(player_id, int) or isinstance(player_id, bool):
            raise PoolResultValidationError({label: "Select valid registered players."})
        result.append(player_id)
    return result


def _normalise_starters(starters):
    """Accept five stable positions, allowing an honest vacant position."""

    if not isinstance(starters, Mapping):
        raise PoolResultValidationError({"starters": "Submit all five numbered starting positions."})
    normalised = {}
    for raw_position, raw_player in starters.items():
        try:
            position = int(raw_position)
        except (TypeError, ValueError) as exc:
            raise PoolResultValidationError({"starters": "Starting positions must be numbered 1 to 5."}) from exc
        if raw_player in (None, ""):
            player_id = None
        else:
            player_id = _player_id(raw_player)
            if not isinstance(player_id, int) or isinstance(player_id, bool):
                raise PoolResultValidationError({"starters": "Select valid registered players or leave a position vacant."})
        normalised[position] = player_id
    if set(normalised) != TEAM_LINEUP_POSITIONS:
        raise PoolResultValidationError({"starters": "A lineup needs exactly positions 1 through 5."})
    occupied_player_ids = [player_id for player_id in normalised.values() if player_id]
    if not occupied_player_ids:
        raise PoolResultValidationError({"starters": "A lineup needs at least one eligible starting player."})
    if len(set(occupied_player_ids)) != len(occupied_player_ids):
        raise PoolResultValidationError({"starters": "Starting players must be unique."})
    return normalised


def _active_roster_player_ids(entry_id, player_ids):
    player_ids = set(player_ids)
    if not player_ids:
        return set()
    return set(
        PoolTeamRoster.objects.select_for_update()
        .filter(
            competition_entry_id=entry_id,
            player_id__in=player_ids,
            is_active=True,
            player__is_active=True,
        )
        .values_list("player_id", flat=True)
    )


def _validate_lineup_players(match, entry_id, starters, substitutes):
    starter_ids = [player_id for player_id in starters.values() if player_id]
    all_player_ids = starter_ids + list(substitutes)
    if len(set(substitutes)) != len(substitutes):
        raise PoolResultValidationError({"substitutes": "Each substitute may be selected only once."})
    if set(starter_ids).intersection(substitutes):
        raise PoolResultValidationError({"substitutes": "A starting player cannot also be a substitute."})
    active_ids = _active_roster_player_ids(entry_id, all_player_ids)
    if active_ids != set(all_player_ids):
        raise PoolResultValidationError(
            {"starters": "Every starter and substitute must be active on this competition team's roster."}
        )


def _lineup_rows_locked(match, *, require_complete=True):
    """Return both lineups and current position maps under the match lock."""

    entries = (match.home_entry_id, match.away_entry_id)
    lineups = {
        lineup.team_entry_id: lineup
        for lineup in PoolTeamMatchLineup.objects.select_for_update()
        .filter(match=match, team_entry_id__in=entries)
        .order_by("team_entry_id", "pk")
    }
    if require_complete and set(lineups) != set(entries):
        missing = [
            "home" if entry_id == match.home_entry_id else "away"
            for entry_id in entries
            if entry_id not in lineups
        ]
        raise PoolResultValidationError(
                "Both teams need a submitted lineup before this match can start (missing %s lineup)."
            % " and ".join(missing)
        )

    position_rows = list(
        PoolTeamMatchLineupPosition.objects.select_for_update(of=("self",))
        .select_related("player", "lineup")
        .filter(lineup__match=match, lineup__team_entry_id__in=entries)
        .order_by("lineup__team_entry_id", "position", "pk")
    )
    positions_by_entry = {entry_id: {} for entry_id in entries}
    for row in position_rows:
        positions_by_entry.setdefault(row.lineup.team_entry_id, {})[row.position] = row

    if require_complete:
        all_lineup_player_ids = set()
        for entry_id in entries:
            lineup = lineups[entry_id]
            position_map = positions_by_entry.get(entry_id, {})
            if set(position_map) != TEAM_LINEUP_POSITIONS:
                raise PoolResultValidationError(
                    "Each submitted lineup must contain exactly positions 1 through 5."
                )
            player_ids = [
                row.player_id for row in position_map.values() if row.player_id
            ]
            if not player_ids:
                raise PoolResultValidationError(
                    "Each submitted lineup needs at least one eligible starting player."
                )
            if len(set(player_ids)) != len(player_ids):
                raise PoolResultValidationError("Starting players must be unique within a lineup.")
            squad_ids = set(
                PoolTeamMatchLineupMember.objects.select_for_update()
                .filter(lineup=lineup)
                .values_list("player_id", flat=True)
            )
            if not set(player_ids).issubset(squad_ids):
                raise PoolResultValidationError("Every starting player must belong to the submitted match squad.")
            active_ids = _active_roster_player_ids(entry_id, squad_ids)
            if not set(player_ids).issubset(active_ids):
                raise PoolResultValidationError(
                    "A starting player is no longer active on this competition team's roster."
                )
            if all_lineup_player_ids.intersection(player_ids):
                raise PoolResultValidationError(
                    "The same player cannot be submitted for both competing teams."
                )
            all_lineup_player_ids.update(player_ids)
    return lineups, positions_by_entry


@transaction.atomic
def submit_team_match_lineup(
    match,
    team_entry,
    *,
    starters,
    substitutes=(),
    actor=None,
    allow_live_correction=False,
    reason="",
):
    """Create or replace one side's match-specific squad and five starters.

    Scheduled time is intentionally irrelevant.  Staff can edit while the
    fixture is UPCOMING or POSTPONED.  A live correction is an explicit
    manager-only service operation and only succeeds before any scheduled
    rack has begun; callers must pass ``allow_live_correction=True``.
    """

    locked_match = _lock_match(match)
    _require_team_fixture(locked_match)
    entry_id = _require_match_entry(locked_match, team_entry)
    editable_statuses = {locked_match.Status.UPCOMING, locked_match.Status.POSTPONED}
    has_started_schedule_activity = PoolTeamMatchScheduledRack.objects.filter(
        match=locked_match,
        status__in=(
            PoolTeamMatchScheduledRack.Status.LIVE,
            PoolTeamMatchScheduledRack.Status.COMPLETED,
            PoolTeamMatchScheduledRack.Status.VOID,
            PoolTeamMatchScheduledRack.Status.STOPPED,
        ),
    ).exists()
    live_safe_correction = (
        allow_live_correction
        and locked_match.status == locked_match.Status.LIVE
        and not has_started_schedule_activity
    )
    if (
        (locked_match.status not in editable_statuses or has_started_schedule_activity)
        and not live_safe_correction
    ):
        raise PoolResultValidationError(
            "Lineups are locked once the match is live. Use an authorised substitution for match-day changes."
        )

    starters = _normalise_starters(starters)
    substitutes = _normalise_players(substitutes, label="substitutes")
    _validate_lineup_players(locked_match, entry_id, starters, substitutes)
    opponent_entry_id = (
        locked_match.away_entry_id
        if entry_id == locked_match.home_entry_id
        else locked_match.home_entry_id
    )
    opponent_squad_ids = set(
        PoolTeamMatchLineupMember.objects.select_for_update()
        .filter(
            lineup__match=locked_match,
            lineup__team_entry_id=opponent_entry_id,
        )
        .values_list("player_id", flat=True)
    )
    if opponent_squad_ids.intersection((*starters.values(), *substitutes)):
        raise PoolResultValidationError(
            {"starters": "A player cannot be submitted for both competing teams."}
        )

    lineup, created = PoolTeamMatchLineup.objects.select_for_update().get_or_create(
        match=locked_match,
        team_entry_id=entry_id,
        defaults={"submitted_by": actor},
    )
    # Position and squad rows are match-owned mutable submission state before
    # play.  Removing them inside the locked service avoids transient unique
    # position/player collisions while an organiser corrects a lineup.
    PoolTeamMatchLineupPosition.objects.filter(lineup=lineup).delete()
    PoolTeamMatchLineupMember.objects.filter(lineup=lineup).delete()
    PoolTeamMatchLineupMember.objects.bulk_create(
        [
            PoolTeamMatchLineupMember(lineup=lineup, player_id=player_id)
            for player_id in (*[player_id for player_id in starters.values() if player_id], *substitutes)
        ]
    )
    PoolTeamMatchLineupPosition.objects.bulk_create(
        [
            PoolTeamMatchLineupPosition(
                lineup=lineup,
                position=position,
                player_id=starters[position],
            )
            for position in sorted(starters)
        ]
    )
    lineup.submitted_by = actor
    lineup.save(update_fields=("submitted_by", "updated_at"))
    _audit(
        locked_match,
        action=(
            PoolTeamMatchAuditEvent.Action.LINEUP_SAVED
            if created
            else PoolTeamMatchAuditEvent.Action.LINEUP_CORRECTED
        ),
        actor=actor,
        team_entry=entry_id,
        payload={
            "positions": {str(position): starters[position] for position in sorted(starters)},
            "substitute_ids": list(substitutes),
            "reason": reason,
        },
    )
    return lineup


@transaction.atomic
def ensure_team_match_schedule(locked_match, *, actor=None, reason=""):
    """Validate both lineups and create the immutable 25-row score sheet."""

    # ``start_match`` already holds this parent row, but this public service is
    # also useful to authorised maintenance/import paths.  Taking the same
    # root lock here makes a direct call just as idempotent: two requests can
    # never both observe an empty score sheet and create duplicate topology.
    locked_match = _lock_match(locked_match)
    _require_team_fixture(locked_match)
    _lineup_rows_locked(locked_match, require_complete=True)
    existing = list(
        PoolTeamMatchScheduledRack.objects.select_for_update()
        .filter(match=locked_match)
        .order_by("sequence_number", "pk")
    )
    if existing:
        expected = [
            (index, session, home, away)
            for index, (session, home, away) in enumerate(TEAM_MATCH_SCORE_SHEET, start=1)
        ]
        actual = [
            (row.sequence_number, row.session_number, row.home_position, row.away_position)
            for row in existing
        ]
        if actual != expected:
            raise PoolResultValidationError(
                "The saved team score sheet is incomplete or corrupt. It cannot be regenerated after creation."
            )
        return existing
    rows = [
        PoolTeamMatchScheduledRack(
            match=locked_match,
            sequence_number=index,
            session_number=session,
            home_position=home_position,
            away_position=away_position,
        )
        for index, (session, home_position, away_position) in enumerate(
            TEAM_MATCH_SCORE_SHEET,
            start=1,
        )
    ]
    PoolTeamMatchScheduledRack.objects.bulk_create(rows)
    _audit(
        locked_match,
        action=PoolTeamMatchAuditEvent.Action.SCHEDULE_GENERATED,
        actor=actor,
        payload={"rack_count": len(rows), "reason": reason},
    )
    return list(
        PoolTeamMatchScheduledRack.objects.filter(match=locked_match).order_by("sequence_number")
    )


def prepare_team_match_start(locked_match, *, actor=None, reason=""):
    """Hook called by ``services.start_match`` while its parent row is locked."""

    return ensure_team_match_schedule(locked_match, actor=actor, reason=reason)


def _current_position_maps(locked_match):
    _, positions = _lineup_rows_locked(locked_match, require_complete=True)
    return {
        entry_id: {
            position: row.player
            for position, row in position_rows.items()
        }
        for entry_id, position_rows in positions.items()
    }


def _locked_schedule_rows(locked_match):
    return list(
        PoolTeamMatchScheduledRack.objects.select_for_update(of=("self",))
        .select_related("home_player", "away_player", "result")
        .filter(match=locked_match)
        .order_by("sequence_number", "pk")
    )


def _live_player_ids(schedule_rows):
    return {
        player_id
        for row in schedule_rows
        if row.status == PoolTeamMatchScheduledRack.Status.LIVE
        for player_id in (row.home_player_id, row.away_player_id)
        if player_id is not None
    }


def _globally_live_player_ids_locked(player_ids):
    """Return candidate players already on a live table in any team fixture.

    A physical player can belong to more than one competition roster and can
    therefore appear in two different parent fixtures.  Locking the candidate
    ``PoolPlayer`` rows in deterministic order gives every team-rack start the
    same cross-fixture serialisation point; the subsequent query can then
    safely reject or skip a player who is already on another live schedule.
    This stays deliberately inside the service layer, alongside the existing
    parent-match and table locks, rather than relying on a UI-only check.
    """

    player_ids = sorted(
        {
            player_id
            for player_id in player_ids
            if isinstance(player_id, int) and not isinstance(player_id, bool)
        }
    )
    if not player_ids:
        return set()
    # Force evaluation while taking locks in primary-key order.  Every start
    # path that could assign these snapshots uses this helper, avoiding a
    # cross-fixture player double-booking race.
    list(
        PoolPlayer.objects.select_for_update()
        .filter(pk__in=player_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    live_pairs = PoolTeamMatchScheduledRack.objects.filter(
        status=PoolTeamMatchScheduledRack.Status.LIVE,
    ).filter(
        Q(home_player_id__in=player_ids) | Q(away_player_id__in=player_ids)
    ).values_list("home_player_id", "away_player_id")
    return {
        player_id
        for home_player_id, away_player_id in live_pairs
        for player_id in (home_player_id, away_player_id)
        if player_id in player_ids
    }


def _can_start_or_resolve_pending_team_rack(locked_match, schedule_rows):
    """Whether a live parent fixture may fill an unoccupied physical table.

    A team's distance from the race target is intentionally irrelevant here.
    A configured multi-table fixture keeps its full physical-table capacity
    until the first rack actually reaches the target.  Completion then
    atomically stops any other live schedule rows, rather than predicting a
    result and throttling legitimate concurrent play.
    """

    return locked_match.status == locked_match.Status.LIVE


def _eligible_pending_rows(locked_match, schedule_rows, position_maps):
    """Return ordered (row, home player, away player) eligible candidates."""

    if not _can_start_or_resolve_pending_team_rack(locked_match, schedule_rows):
        return []
    busy_player_ids = _live_player_ids(schedule_rows)
    candidates = []
    for row in schedule_rows:
        if row.status != row.Status.PENDING:
            continue
        home_player = position_maps[locked_match.home_entry_id].get(row.home_position)
        away_player = position_maps[locked_match.away_entry_id].get(row.away_position)
        if home_player is None or away_player is None:
            continue
        if home_player.pk in busy_player_ids or away_player.pk in busy_player_ids:
            continue
        candidates.append((row, home_player, away_player))
    return candidates


def _next_due_vacancy_row(locked_match, schedule_rows, position_maps, candidates):
    """Return the earliest due unfilled pairing without jumping a playable rack.

    A normal rack remains preferred whenever it appears first in the original
    schedule.  If that rack is currently blocked by a live player, the next
    vacant pairing can be resolved instead, matching the existing flexible
    multi-table scheduler without silently awarding every future vacancy at
    match start.
    """

    if not _can_start_or_resolve_pending_team_rack(locked_match, schedule_rows):
        return None
    first_playable_sequence = candidates[0][0].sequence_number if candidates else None
    for row in schedule_rows:
        if row.status != row.Status.PENDING:
            continue
        home_player = position_maps[locked_match.home_entry_id].get(row.home_position)
        away_player = position_maps[locked_match.away_entry_id].get(row.away_position)
        if home_player is not None and away_player is not None:
            continue
        if (
            first_playable_sequence is None
            or row.sequence_number < first_playable_sequence
        ):
            return row, home_player, away_player
    return None


def _resolve_due_team_vacancy(
    locked_match,
    row,
    *,
    home_player,
    away_player,
    actor=None,
):
    """Persist one due vacancy as a truthful team result or an explicit void.

    This runs under the parent fixture and schedule-row locks held by the
    caller.  It deliberately does not manufacture a player attribution or a
    physical-table snapshot for a rack that was never contested.
    """

    if home_player is None and away_player is None:
        row.status = row.Status.VOID
        row.table_number = None
        row.home_player = None
        row.away_player = None
        row.started_at = None
        row.completed_at = timezone.now()
        row.result = None
        row.save()
        _audit(
            locked_match,
            action=PoolTeamMatchAuditEvent.Action.RACK_COMPLETED,
            actor=actor,
            payload={
                "scheduled_rack_id": row.pk,
                "sequence_number": row.sequence_number,
                "lineup_vacancy": True,
                "void": True,
            },
        )
        return row

    _ensure_scoring_is_open(locked_match)
    winner_id = (
        locked_match.home_entry_id
        if home_player is not None
        else locked_match.away_entry_id
    )
    previous_result = snapshot_match_result(locked_match)
    previous_number = (
        locked_match.racks.select_for_update()
        .order_by("-rack_number", "-pk")
        .values_list("rack_number", flat=True)
        .first()
    )
    try:
        with transaction.atomic():
            result = PoolRack(
                match=locked_match,
                rack_number=(previous_number or 0) + 1,
                winner_id=winner_id,
                winning_player_id=None,
                outcome_type=PoolRack.OutcomeType.LINEUP_VACANCY,
                recorded_by=actor,
            )
            result.save(
                _from_result_service=True,
                _from_team_vacancy_service=True,
            )
    except IntegrityError as exc:
        raise PoolResultValidationError(
            "Another scorer recorded a rack at the same time. Please try again."
        ) from exc

    row.result = result
    row.status = row.Status.COMPLETED
    row.table_number = None
    row.home_player = home_player
    row.away_player = away_player
    row.started_at = None
    row.completed_at = timezone.now()
    row.save()
    _synchronise_locked_match_score(locked_match)
    locked_match.refresh_from_db()
    if locked_match.status == locked_match.Status.FINISHED:
        PoolTeamMatchScheduledRack.objects.filter(
            match=locked_match,
            status=PoolTeamMatchScheduledRack.Status.PENDING,
        ).update(status=PoolTeamMatchScheduledRack.Status.NOT_REQUIRED)
    _record_result_revision(
        locked_match,
        action=locked_match.result_revisions.model.Action.RACK_RECORDED,
        revised_by=actor,
        reason="Scheduled lineup vacancy.",
        previous_result=previous_result,
    )
    _audit(
        locked_match,
        action=PoolTeamMatchAuditEvent.Action.RACK_COMPLETED,
        actor=actor,
        payload={
            "scheduled_rack_id": row.pk,
            "sequence_number": row.sequence_number,
            "result_rack_id": result.pk,
            "result_rack_number": result.rack_number,
            "winner_entry_id": winner_id,
            "outcome_type": PoolRack.OutcomeType.LINEUP_VACANCY,
            "lineup_vacancy": True,
            "vacant_side": "away" if home_player is not None else "home",
        },
    )
    _refresh_match_derived_state(locked_match, actor=actor, reason="Scheduled lineup vacancy.")
    return row


def team_match_schedule_state(match):
    """Read-model data for public and manager templates without mutations."""

    if not is_team_fixture(match):
        return {
            "schedules": (),
            "live": (),
            "pending": (),
            "completed": (),
            "void": (),
            "stopped": (),
            "not_required": (),
        }
    schedules = list(
        PoolTeamMatchScheduledRack.objects.filter(match=match)
        .select_related("home_player", "away_player", "result__winning_player", "result__winner__team")
        .order_by("sequence_number", "pk")
    )
    lineups = {
        lineup.team_entry_id: lineup
        for lineup in PoolTeamMatchLineup.objects.filter(match=match)
    }
    positions = {
        entry_id: {
            row.position: row.player
            for row in PoolTeamMatchLineupPosition.objects.filter(lineup=lineup).select_related("player")
        }
        for entry_id, lineup in lineups.items()
    }
    for row in schedules:
        if row.status == row.Status.PENDING:
            row.current_home_player = positions.get(match.home_entry_id, {}).get(row.home_position)
            row.current_away_player = positions.get(match.away_entry_id, {}).get(row.away_position)
        else:
            row.current_home_player = row.home_player
            row.current_away_player = row.away_player
        # The schedule read model intentionally exposes both the resolved
        # player objects used by templates and their stable ids used by
        # service/API callers.  Pending rows are position-based until they
        # start, so these values are derived rather than persisted snapshots.
        row.current_home_player_id = getattr(row.current_home_player, "pk", None)
        row.current_away_player_id = getattr(row.current_away_player, "pk", None)
    live = [
        row
        for row in schedules
        if row.status == row.Status.LIVE
    ]
    pending = [
        row
        for row in schedules
        if row.status == row.Status.PENDING
    ]
    completed = [
        row
        for row in schedules
        if row.status == row.Status.COMPLETED
    ]
    void = [
        row
        for row in schedules
        if row.status == row.Status.VOID
    ]
    not_required = [
        row
        for row in schedules
        if row.status == row.Status.NOT_REQUIRED
    ]
    stopped = [
        row
        for row in schedules
        if row.status == row.Status.STOPPED
    ]

    return {
        "schedules": schedules,
        "live": live,
        "pending": pending,
        "up_next": pending[:5],
        "completed": completed,
        "void": void,
        "stopped": stopped,
        "not_required": not_required,
        "positions": positions,
    }


def start_next_eligible_team_rack(match, *, table_number, scheduled_rack=None, actor=None):
    """Start the earliest eligible pending score-sheet rack on a free table."""

    result = _start_next_eligible_team_rack_atomic(
        match,
        table_number=table_number,
        scheduled_rack=scheduled_rack,
        actor=actor,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=getattr(match, "pk", match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _start_next_eligible_team_rack_atomic(
    match,
    *,
    table_number,
    scheduled_rack=None,
    actor=None,
):
    """Locked implementation for :func:`start_next_eligible_team_rack`."""

    locked_match = _lock_match(match)
    _require_team_fixture(locked_match)
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=actor,
    )
    # After the deadline a team fixture may only complete a rack which was
    # already LIVE at expiry. Never assign a new table/snapshot in either a
    # leader-finalised or tied deciding-rack state.
    if timer_processed or getattr(locked_match, "time_expired_tied", False):
        return _TIMER_EXPIRY_BLOCKED
    if locked_match.status != locked_match.Status.LIVE:
        raise PoolResultValidationError("Start the parent team match before starting an individual rack.")
    if locked_match.result_method != locked_match.ResultMethod.PLAYED:
        raise PoolResultValidationError("Individual racks are unavailable for a non-played result.")
    try:
        table_number = int(table_number)
    except (TypeError, ValueError) as exc:
        raise PoolResultValidationError({"table_number": "Select a valid physical table."}) from exc
    if not 1 <= table_number <= locked_match.table_count:
        raise PoolResultValidationError({"table_number": "This table is not available for this fixture."})

    schedule_rows = _locked_schedule_rows(locked_match)
    if not schedule_rows:
        raise PoolResultValidationError("This team match has no generated score sheet.")
    if any(row.status == row.Status.LIVE and row.table_number == table_number for row in schedule_rows):
        raise PoolResultValidationError({"table_number": "That physical table already has a live rack."})
    position_maps = _current_position_maps(locked_match)
    candidates = _eligible_pending_rows(locked_match, schedule_rows, position_maps)
    # A player may legitimately be rostered in more than one team entry.  The
    # score sheet's local busy-player check is necessary but insufficient in
    # that case: honour a live snapshot in another team fixture as well, and
    # move on to the next original sequence row whose two players are free.
    globally_live_player_ids = _globally_live_player_ids_locked(
        player.pk
        for _candidate, home_player, away_player in candidates
        for player in (home_player, away_player)
    )
    candidates = [
        candidate
        for candidate in candidates
        if candidate[1].pk not in globally_live_player_ids
        and candidate[2].pk not in globally_live_player_ids
    ]
    due_vacancy = _next_due_vacancy_row(
        locked_match,
        schedule_rows,
        position_maps,
        candidates,
    )
    if due_vacancy is not None:
        row, home_player, away_player = due_vacancy
        requested_id = _entry_id(scheduled_rack)
        if requested_id is not None and requested_id != row.pk:
            raise PoolResultValidationError(
                "Resolve the earliest due score-sheet vacancy first; blocked racks keep their original number."
            )
        return _resolve_due_team_vacancy(
            locked_match,
            row,
            home_player=home_player,
            away_player=away_player,
            actor=actor,
        )
    if not candidates:
        raise PoolResultValidationError(
            "No pending rack can start yet. A player may be busy or no rack is required."
        )
    row, home_player, away_player = candidates[0]
    requested_id = _entry_id(scheduled_rack)
    if requested_id is not None and requested_id != row.pk:
        raise PoolResultValidationError(
            "Start the earliest eligible score-sheet rack first; blocked racks keep their original number."
        )
    row.status = row.Status.LIVE
    row.table_number = table_number
    row.home_player = home_player
    row.away_player = away_player
    row.started_at = timezone.now()
    row.completed_at = None
    row.save()
    _audit(
        locked_match,
        action=PoolTeamMatchAuditEvent.Action.RACK_STARTED,
        actor=actor,
        payload={
            "scheduled_rack_id": row.pk,
            "sequence_number": row.sequence_number,
            "table_number": table_number,
            "home_player_id": home_player.pk,
            "away_player_id": away_player.pk,
        },
    )
    return row


def _team_rack_winner(locked_match, scheduled_rack, winning_player):
    player_id = _player_id(winning_player)
    if player_id == scheduled_rack.home_player_id:
        return locked_match.home_entry_id, player_id
    if player_id == scheduled_rack.away_player_id:
        return locked_match.away_entry_id, player_id
    raise PoolResultValidationError(
        {"winning_player": "Select one of the two players currently contesting this rack."}
    )


def complete_team_match_rack(
    match,
    scheduled_rack,
    *,
    winning_player,
    outcome_type=PoolRack.OutcomeType.NORMAL,
    actor=None,
    reason="",
):
    """Complete one live player-v-player rack and synchronise the parent score."""

    result = _complete_team_match_rack_atomic(
        match,
        scheduled_rack,
        winning_player=winning_player,
        outcome_type=outcome_type,
        actor=actor,
        reason=reason,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=getattr(match, "pk", match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _complete_team_match_rack_atomic(
    match,
    scheduled_rack,
    *,
    winning_player,
    outcome_type=PoolRack.OutcomeType.NORMAL,
    actor=None,
    reason="",
):
    """Locked implementation for :func:`complete_team_match_rack`."""

    locked_match = _lock_match(match)
    _require_team_fixture(locked_match)
    row = (
        PoolTeamMatchScheduledRack.objects.select_for_update(of=("self",))
        .select_related("home_player", "away_player", "result")
        .filter(pk=_entry_id(scheduled_rack), match=locked_match)
        .first()
    )
    if row is None:
        raise PoolResultValidationError("That scheduled rack does not belong to this match.")
    if row.status == row.Status.COMPLETED and row.result_id:
        # Repeated POST/browser retry is intentionally idempotent: the already
        # recorded authoritative result is returned rather than counted twice.
        return row
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=actor,
        reason=reason,
    )
    if (
        (timer_processed and locked_match.status != locked_match.Status.LIVE)
        or (
            locked_match.status != locked_match.Status.LIVE
            and getattr(locked_match, "completion_reason", "")
            == "TIME_LIMIT"
        )
    ):
        return _TIMER_EXPIRY_BLOCKED
    if row.status != row.Status.LIVE:
        raise PoolResultValidationError("Only a live scheduled rack can be completed.")
    if not row.home_player_id or not row.away_player_id:
        raise PoolResultValidationError("This live rack has no player snapshots and cannot be scored safely.")
    if outcome_type not in PoolRack.OutcomeType.values:
        raise PoolResultValidationError({"outcome_type": "Select a valid rack outcome."})
    if outcome_type == PoolRack.OutcomeType.LINEUP_VACANCY:
        raise PoolResultValidationError(
            {"outcome_type": "Lineup-vacancy outcomes are resolved only when a due position is empty."}
        )
    _ensure_scoring_is_open(locked_match)
    winner_id, winning_player_id = _team_rack_winner(locked_match, row, winning_player)
    previous_result = snapshot_match_result(locked_match)
    previous_number = (
        locked_match.racks.select_for_update()
        .order_by("-rack_number", "-pk")
        .values_list("rack_number", flat=True)
        .first()
    )
    try:
        with transaction.atomic():
            result = PoolRack(
                match=locked_match,
                rack_number=(previous_number or 0) + 1,
                winner_id=winner_id,
                winning_player_id=winning_player_id,
                outcome_type=outcome_type,
                recorded_by=actor,
            )
            result.save(_from_result_service=True)
    except IntegrityError as exc:
        raise PoolResultValidationError("Another scorer recorded a rack at the same time. Please try again.") from exc

    row.result = result
    row.status = row.Status.COMPLETED
    # Keep the physical table as useful completed-history context.  Only LIVE
    # rows participate in the unique-live-table constraint, so this table is
    # immediately available for the next rack despite the retained value.
    row.completed_at = timezone.now()
    row.save()
    _synchronise_locked_match_score(locked_match)
    locked_match.refresh_from_db()
    if locked_match.status == locked_match.Status.FINISHED:
        PoolTeamMatchScheduledRack.objects.filter(
            match=locked_match,
            status=PoolTeamMatchScheduledRack.Status.PENDING,
        ).update(status=PoolTeamMatchScheduledRack.Status.NOT_REQUIRED)
    _record_result_revision(
        locked_match,
        action=locked_match.result_revisions.model.Action.RACK_RECORDED,
        revised_by=actor,
        reason=reason,
        previous_result=previous_result,
    )
    _audit(
        locked_match,
        action=PoolTeamMatchAuditEvent.Action.RACK_COMPLETED,
        actor=actor,
        payload={
            "scheduled_rack_id": row.pk,
            "sequence_number": row.sequence_number,
            "result_rack_id": result.pk,
            "result_rack_number": result.rack_number,
            "winning_player_id": winning_player_id,
            "winner_entry_id": winner_id,
            "outcome_type": outcome_type,
            "reason": reason,
        },
    )
    _refresh_match_derived_state(locked_match, actor=actor, reason=reason)
    return row


@transaction.atomic
def substitute_team_match_player(
    match,
    team_entry,
    *,
    position,
    incoming_player,
    actor=None,
    reason="",
):
    """Move an eligible submitted squad player into one current position.

    Re-entry is deliberately flexible for this phase: any submitted squad
    player can return when active on this competition roster, not occupying a
    different current position, and not currently live on another table.
    """

    locked_match = _lock_match(match)
    _require_team_fixture(locked_match)
    if locked_match.status != locked_match.Status.LIVE:
        raise PoolResultValidationError("Substitutions are only available while the team match is live.")
    entry_id = _require_match_entry(locked_match, team_entry)
    try:
        position = int(position)
    except (TypeError, ValueError) as exc:
        raise PoolResultValidationError({"position": "Select a position from 1 to 5."}) from exc
    if position not in TEAM_LINEUP_POSITIONS:
        raise PoolResultValidationError({"position": "Select a position from 1 to 5."})
    incoming_id = _player_id(incoming_player)
    if not isinstance(incoming_id, int) or isinstance(incoming_id, bool):
        raise PoolResultValidationError({"incoming_player": "Select a valid incoming player."})

    lineups, position_maps = _lineup_rows_locked(locked_match, require_complete=True)
    lineup = lineups[entry_id]
    current_row = position_maps[entry_id][position]
    if not current_row.player_id:
        raise PoolResultValidationError(
            {"position": "Fill this vacant position; it cannot be substituted out."}
        )
    if incoming_id == current_row.player_id:
        raise PoolResultValidationError({"incoming_player": "This player already occupies the selected position."})
    if not PoolTeamMatchLineupMember.objects.select_for_update().filter(
        lineup=lineup,
        player_id=incoming_id,
    ).exists():
        raise PoolResultValidationError({"incoming_player": "The incoming player is not in this submitted match squad."})
    if incoming_id not in _active_roster_player_ids(entry_id, {incoming_id}):
        raise PoolResultValidationError({"incoming_player": "The incoming player is not active on this competition team's roster."})
    if incoming_id in _globally_live_player_ids_locked({incoming_id}):
        raise PoolResultValidationError(
            {"incoming_player": "The incoming player is already live in another team fixture."}
        )
    for other_position, row in position_maps[entry_id].items():
        if other_position != position and row.player_id == incoming_id:
            raise PoolResultValidationError({"incoming_player": "The incoming player already occupies another current position."})
    live_rows = _locked_schedule_rows(locked_match)
    if incoming_id in _live_player_ids(live_rows):
        raise PoolResultValidationError({"incoming_player": "The incoming player is already live on another table."})

    outgoing_id = current_row.player_id
    current_row.player_id = incoming_id
    current_row.save()
    substitution = PoolTeamMatchSubstitution.objects.create(
        match=locked_match,
        team_entry_id=entry_id,
        position=position,
        outgoing_player_id=outgoing_id,
        incoming_player_id=incoming_id,
        actor=actor,
        reason=reason,
    )
    _audit(
        locked_match,
        action=PoolTeamMatchAuditEvent.Action.SUBSTITUTION,
        actor=actor,
        team_entry=entry_id,
        payload={
            "substitution_id": substitution.pk,
            "position": position,
            "outgoing_player_id": outgoing_id,
            "incoming_player_id": incoming_id,
            "reason": reason,
        },
    )
    return substitution


def fill_team_match_vacancy(
    match,
    team_entry,
    *,
    position,
    incoming_player,
    actor=None,
    reason="",
):
    """Place an eligible roster player into a genuinely vacant live position.

    This is intentionally distinct from a substitution: there is no outgoing
    player and no substitution record.  Already completed/void scheduled rows
    retain their historic snapshots; future pending rows continue to resolve
    the current position dynamically.
    """

    result = _fill_team_match_vacancy_atomic(
        match,
        team_entry,
        position=position,
        incoming_player=incoming_player,
        actor=actor,
        reason=reason,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=getattr(match, "pk", match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _fill_team_match_vacancy_atomic(
    match,
    team_entry,
    *,
    position,
    incoming_player,
    actor=None,
    reason="",
):
    locked_match = _lock_match(match)
    _require_team_fixture(locked_match)
    if locked_match.status != locked_match.Status.LIVE:
        raise PoolResultValidationError(
            "A vacant team position can be filled only while the match is live."
        )
    entry_id = _require_match_entry(locked_match, team_entry)
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=actor,
        reason=reason,
    )
    if timer_processed or getattr(locked_match, "time_expired_tied", False):
        return _TIMER_EXPIRY_BLOCKED
    try:
        position = int(position)
    except (TypeError, ValueError) as exc:
        raise PoolResultValidationError(
            {"position": "Select a vacant position from 1 to 5."}
        ) from exc
    if position not in TEAM_LINEUP_POSITIONS:
        raise PoolResultValidationError(
            {"position": "Select a vacant position from 1 to 5."}
        )
    incoming_id = _player_id(incoming_player)
    if not isinstance(incoming_id, int) or isinstance(incoming_id, bool):
        raise PoolResultValidationError(
            {"incoming_player": "Select a valid incoming player."}
        )

    lineups, position_maps = _lineup_rows_locked(locked_match, require_complete=True)
    lineup = lineups[entry_id]
    current_row = position_maps[entry_id].get(position)
    if current_row is None:
        raise PoolResultValidationError(
            {"position": "This lineup has no stable row for that position."}
        )
    if current_row.player_id:
        raise PoolResultValidationError(
            {"position": "Use a normal substitution for an occupied position."}
        )
    if incoming_id not in _active_roster_player_ids(entry_id, {incoming_id}):
        raise PoolResultValidationError(
            {"incoming_player": "The incoming player is not active on this competition team's roster."}
        )
    if incoming_id in _globally_live_player_ids_locked({incoming_id}):
        raise PoolResultValidationError(
            {"incoming_player": "The incoming player is already live in another team fixture."}
        )
    for position_rows in position_maps.values():
        if any(
            row.player_id == incoming_id
            for row in position_rows.values()
            if row.player_id
        ):
            raise PoolResultValidationError(
                {"incoming_player": "This player already occupies a current position in this fixture."}
            )
    schedule_rows = _locked_schedule_rows(locked_match)
    if incoming_id in _live_player_ids(schedule_rows):
        raise PoolResultValidationError(
            {"incoming_player": "The incoming player is already live on another table."}
        )

    PoolTeamMatchLineupMember.objects.select_for_update().get_or_create(
        lineup=lineup,
        player_id=incoming_id,
    )
    current_row.player_id = incoming_id
    current_row.save()
    _audit(
        locked_match,
        action=PoolTeamMatchAuditEvent.Action.LINEUP_CORRECTED,
        actor=actor,
        team_entry=entry_id,
        payload={
            "vacancy_fill": True,
            "position": position,
            "incoming_player_id": incoming_id,
            "reason": reason,
        },
    )
    return current_row


def correct_completed_team_match_rack(
    match,
    scheduled_rack,
    *,
    winning_player,
    outcome_type=None,
    actor=None,
    reason="",
):
    """Correct an attributed scheduled result without allowing a third player."""

    result = _correct_completed_team_match_rack_atomic(
        match,
        scheduled_rack,
        winning_player=winning_player,
        outcome_type=outcome_type,
        actor=actor,
        reason=reason,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=getattr(match, "pk", match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _correct_completed_team_match_rack_atomic(
    match,
    scheduled_rack,
    *,
    winning_player,
    outcome_type=None,
    actor=None,
    reason="",
):
    """Locked implementation for :func:`correct_completed_team_match_rack`."""

    locked_match = _lock_match(match)
    row = (
        PoolTeamMatchScheduledRack.objects.select_for_update(of=("self",))
        .select_related("result", "home_player", "away_player")
        .filter(pk=_entry_id(scheduled_rack), match=locked_match)
        .first()
    )
    if row is None or not row.result_id:
        raise PoolResultValidationError("That scheduled rack has no completed result to correct.")
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=actor,
        reason=reason,
    )
    if timer_processed and (
        locked_match.status != locked_match.Status.LIVE
        or getattr(locked_match, "time_expired_tied", False)
    ):
        return _TIMER_EXPIRY_BLOCKED
    _ensure_rack_can_be_corrected(locked_match)
    rack = _locked_rack(locked_match, row.result)
    if rack.outcome_type == PoolRack.OutcomeType.LINEUP_VACANCY:
        raise PoolResultValidationError(
            "A lineup-vacancy result is historical score-sheet evidence and cannot be corrected as a player-v-player rack."
        )
    winner_id, winning_player_id = _team_rack_winner(locked_match, row, winning_player)
    if outcome_type is not None and outcome_type not in PoolRack.OutcomeType.values:
        raise PoolResultValidationError({"outcome_type": "Select a valid rack outcome."})
    if outcome_type == PoolRack.OutcomeType.LINEUP_VACANCY:
        raise PoolResultValidationError(
            {"outcome_type": "Lineup-vacancy outcomes cannot be assigned through player-rack correction."}
        )
    previous_result = snapshot_match_result(locked_match)
    winner_changed = rack.winner_id != winner_id
    prospective_winner = None
    if winner_changed:
        home_score, away_score = _authoritative_scores(locked_match)
        if rack.winner_id == locked_match.home_entry_id:
            home_score -= 1
        else:
            away_score -= 1
        if winner_id == locked_match.home_entry_id:
            home_score += 1
        else:
            away_score += 1
        prospective_winner = (
            locked_match.home_entry_id if home_score == locked_match.race_target else
            locked_match.away_entry_id if away_score == locked_match.race_target else None
        )
        # A completed score-sheet row can be corrected while its parent is
        # live.  Do not let that correction turn the parent terminal while a
        # different physical table is still live: the survivor would have a
        # frozen player snapshot but no longer be allowed to submit its
        # authoritative result.  Normal scoring cannot reach this state
        # because the near-clinch launch boundary prevents it; correction must
        # enforce the same invariant explicitly.
        if prospective_winner and PoolTeamMatchScheduledRack.objects.filter(
            match=locked_match,
            status=PoolTeamMatchScheduledRack.Status.LIVE,
        ).exclude(pk=row.pk).exists():
            raise PoolResultValidationError(
                "Complete the other live team tables before a correction can finish this fixture."
            )

    # If this correction changes an already-finished winner, preserve the
    # Phase Three bracket dependency guard before making any historical write.
    if locked_match.status == locked_match.Status.FINISHED and winner_changed:
        _bracket_preflight(
            locked_match,
            prospective_winner=prospective_winner,
            prospective_eligible=bool(prospective_winner),
            actor=actor,
        )
    rack.winner_id = winner_id
    rack.winning_player_id = winning_player_id
    if outcome_type is not None:
        rack.outcome_type = outcome_type
    rack.save(_from_result_service=True)
    _synchronise_locked_match_score(locked_match)
    locked_match.refresh_from_db()
    if (
        locked_match.status == locked_match.Status.LIVE
        and not getattr(locked_match, "time_expired_tied", False)
    ):
        # Opening/correcting a final result can legitimately put the parent
        # below its target again.  Restore only unplayed score-sheet rows;
        # live/completed snapshots remain historical truth.
        PoolTeamMatchScheduledRack.objects.filter(
            match=locked_match,
            status=PoolTeamMatchScheduledRack.Status.NOT_REQUIRED,
        ).update(status=PoolTeamMatchScheduledRack.Status.PENDING)
    _record_result_revision(
        locked_match,
        action=locked_match.result_revisions.model.Action.RACK_CORRECTED,
        revised_by=actor,
        reason=reason,
        previous_result=previous_result,
    )
    # The existing immutable result revision records the before/after result
    # snapshot for a correction.  Do not label it as a new scheduled-rack
    # completion in the narrower team-operation audit trail.
    _refresh_match_derived_state(locked_match, actor=actor, reason=reason)
    return rack
