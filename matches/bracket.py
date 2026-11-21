"""Transactional services for the connected football knockout bracket.

Phase One owns football result validation.  This module owns only the explicit
bracket graph: tied slots, forward links, byes and safe downstream updates.
"""

from collections import defaultdict
from random import SystemRandom

from django.core.exceptions import ValidationError
from django.db import transaction
from leagues.models import Stage

from .knockout import get_round_rule, resolve_completed_knockout_tie
from .models import (
    FootballKnockoutTie,
    FootballKnockoutTieSlot,
    Goal,
    Match,
    MatchLineup,
    YellowCard,
)


class BracketCorrectionBlocked(ValidationError):
    """A result change would rewrite meaningful downstream bracket data."""

    def __init__(self, message, *, source_tie=None, destination_tie=None, match=None):
        super().__init__(message)
        self.source_tie = source_tie
        self.destination_tie = destination_tie
        self.match = match


def record_blocked_bracket_correction(error, *, actor=None):
    """Audit a rejected rewrite after the result transaction has rolled back."""
    if not isinstance(error, BracketCorrectionBlocked) or not error.source_tie:
        return None
    source_tie = error.source_tie
    match = error.match
    return _record_activity(
        actor=actor,
        competition=source_tie.stage.competition,
        match=match,
        action="BRACKET_CORRECTION_BLOCKED",
        description=" ".join(error.messages),
        values={
            "source_tie_id": source_tie.id,
            "destination_tie_id": (
                error.destination_tie.id if error.destination_tie else None
            ),
            "destination_match_id": match.id if match else None,
        },
    )


def bracket_ties_for_competition(competition):
    """Return only explicit Phase Two ties, leaving legacy ties untouched."""
    return FootballKnockoutTie.objects.filter(
        stage__competition=competition,
        position__isnull=False,
    )


def competition_has_bracket(competition):
    return bracket_ties_for_competition(competition).exists()


def _record_activity(*, actor, competition, action, description, match=None, values=None):
    """Keep the bracket service independent from management view imports."""
    from league_manager.models import ActivityLog

    ActivityLog.objects.create(
        actor=actor,
        competition=competition,
        match=match,
        action=action,
        description=description,
        new_values=values or {},
    )


def _ordered_bracket_ties(competition, *, lock=False):
    queryset = bracket_ties_for_competition(competition).select_related(
        "stage",
        "stage__competition",
        "team_one",
        "team_two",
        "winner",
        "winner_destination",
    ).order_by("stage__order", "position", "id")
    if lock:
        queryset = queryset.select_for_update()
    return list(queryset)


def _slot_map(tie, *, lock=False):
    queryset = FootballKnockoutTieSlot.objects.select_related("team").filter(
        tie=tie,
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    return {slot.slot: slot for slot in queryset}


def _tie_slot(tie, slot_name, *, lock=False):
    queryset = FootballKnockoutTieSlot.objects.select_related("team").filter(
        tie=tie,
        slot=slot_name,
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    return queryset.get()


def _bracket_matches(tie, *, lock=False):
    queryset = Match.objects.select_related(
        "home_team",
        "away_team",
        "stage",
    ).filter(knockout_tie=tie).order_by("leg", "id")
    if lock:
        queryset = queryset.select_for_update()
    return list(queryset)


def _first_round_stage_id(ties):
    return min(ties, key=lambda tie: (tie.stage.order, tie.stage.id)).stage_id


def _is_terminal_tie(tie):
    if tie.winner_destination_id or tie.position is None:
        return False
    return not bracket_ties_for_competition(tie.stage.competition).filter(
        stage__order__gt=tie.stage.order,
    ).exists()


def _ensure_bracket_is_editable(competition):
    ties = bracket_ties_for_competition(competition)
    if ties.filter(winner__isnull=False).exists():
        raise ValidationError(
            "Bracket seeding is locked after a tie has advanced a winner."
        )
    if Match.objects.filter(knockout_tie__in=ties).exists():
        raise ValidationError(
            "Bracket seeding is locked after bracket fixtures have been generated."
        )


@transaction.atomic
def create_bracket_structure(competition):
    """Create an empty canonical bracket from the configured knockout stages."""
    existing_ties = bracket_ties_for_competition(competition).select_for_update()
    if existing_ties.exists():
        raise ValidationError("A connected bracket already exists for this competition.")

    stages = list(
        Stage.objects.select_for_update().filter(
            competition=competition,
            stage_type="KNOCKOUT",
        ).order_by("order", "id")
    )
    if not stages:
        raise ValidationError("Configure the knockout rounds before creating a bracket.")
    if len({stage.order for stage in stages}) != len(stages):
        raise ValidationError("Knockout rounds must have distinct ordering values.")

    expected_size = 2 ** len(stages)
    if competition.knockout_size and competition.knockout_size != expected_size:
        raise ValidationError(
            "Configured knockout stages do not match the competition knockout size."
        )

    if FootballKnockoutTie.objects.filter(stage__in=stages).exists():
        raise ValidationError(
            "A connected bracket cannot be created around existing knockout ties or fixtures."
        )
    if Match.objects.filter(stage__in=stages).exists():
        raise ValidationError(
            "A connected bracket cannot be created around existing knockout fixtures."
        )

    for stage in stages:
        get_round_rule(stage)

    ties_by_stage = {}
    for index, stage in enumerate(stages):
        tie_count = 2 ** (len(stages) - index - 1)
        stage_ties = []
        for position in range(1, tie_count + 1):
            tie = FootballKnockoutTie.objects.create(stage=stage, position=position)
            FootballKnockoutTieSlot.objects.bulk_create([
                FootballKnockoutTieSlot(
                    tie=tie,
                    slot=FootballKnockoutTieSlot.Slot.HOME,
                ),
                FootballKnockoutTieSlot(
                    tie=tie,
                    slot=FootballKnockoutTieSlot.Slot.AWAY,
                ),
            ])
            stage_ties.append(tie)
        ties_by_stage[stage.id] = stage_ties

    for source_stage, destination_stage in zip(stages, stages[1:]):
        sources = ties_by_stage[source_stage.id]
        destinations = ties_by_stage[destination_stage.id]
        for index, destination in enumerate(destinations):
            for source, slot_name in (
                (sources[index * 2], FootballKnockoutTie.DestinationSlot.HOME),
                (sources[index * 2 + 1], FootballKnockoutTie.DestinationSlot.AWAY),
            ):
                source.winner_destination = destination
                source.winner_destination_slot = slot_name
                source.save(
                    update_fields=[
                        "winner_destination",
                        "winner_destination_slot",
                    ]
                )

    validate_bracket(competition, require_entrants=False)
    return _ordered_bracket_ties(competition)


def validate_bracket(competition, *, require_entrants=True):
    """Validate the full explicit graph before fixtures are generated."""
    ties = _ordered_bracket_ties(competition)
    if not ties:
        raise ValidationError("Create a bracket structure before validating it.")

    errors = []
    tie_ids = {tie.id for tie in ties}
    first_stage_id = _first_round_stage_id(ties)
    slots_by_tie = {tie.id: _slot_map(tie) for tie in ties}
    incoming = {}
    terminal_ties = []
    stage_teams = defaultdict(set)

    for tie in ties:
        try:
            get_round_rule(tie.stage)
        except ValidationError as error:
            errors.extend(error.messages)

        slot_map = slots_by_tie[tie.id]
        if set(slot_map) != {
            FootballKnockoutTieSlot.Slot.HOME,
            FootballKnockoutTieSlot.Slot.AWAY,
        }:
            errors.append(f"{tie.stage.name} tie {tie.position} must have home and away slots.")
            continue

        for slot in slot_map.values():
            if slot.team_id:
                if slot.team_id in stage_teams[tie.stage_id]:
                    errors.append(
                        f"{slot.team.name} appears more than once in {tie.stage.name}."
                    )
                stage_teams[tie.stage_id].add(slot.team_id)

        if tie.stage_id == first_stage_id and require_entrants:
            home_slot = slot_map[FootballKnockoutTieSlot.Slot.HOME]
            away_slot = slot_map[FootballKnockoutTieSlot.Slot.AWAY]
            entrant_count = int(bool(home_slot.team_id)) + int(bool(away_slot.team_id))
            bye_count = int(home_slot.is_bye) + int(away_slot.is_bye)
            if entrant_count == 0 or (entrant_count == 1 and bye_count != 1):
                errors.append(
                    f"{tie.stage.name} tie {tie.position} needs two teams or one team and one explicit bye."
                )
            if entrant_count == 2 and bye_count:
                errors.append(
                    f"{tie.stage.name} tie {tie.position} cannot combine teams with a bye."
                )
            if tie.is_bye != bool(bye_count):
                errors.append(
                    f"{tie.stage.name} tie {tie.position} has inconsistent bye state."
                )
        elif any(slot.is_bye for slot in slot_map.values()):
            errors.append("Byes are only valid in the first bracket round.")

        if tie.winner_destination_id:
            if tie.winner_destination_id not in tie_ids:
                errors.append("A bracket tie points outside this competition bracket.")
            else:
                key = (tie.winner_destination_id, tie.winner_destination_slot)
                if key in incoming:
                    errors.append("Two ties populate the same destination bracket slot.")
                incoming[key] = tie
        else:
            terminal_ties.append(tie)

    if len(terminal_ties) != 1:
        errors.append("A complete bracket must have exactly one final tie.")
    elif terminal_ties[0].stage.order != max(tie.stage.order for tie in ties):
        errors.append("The final tie must be in the latest knockout round.")

    for tie in ties:
        if tie.stage_id == first_stage_id:
            continue
        for slot_name in (
            FootballKnockoutTieSlot.Slot.HOME,
            FootballKnockoutTieSlot.Slot.AWAY,
        ):
            if (tie.id, slot_name) not in incoming:
                errors.append(
                    f"{tie.stage.name} tie {tie.position} has a missing source link."
                )

    if errors:
        raise ValidationError(errors)
    return ties


def _set_slot_team(slot, team, *, allow_schedule_locked_progression=False):
    slot.team = team
    slot.is_bye = False
    slot.save(
        update_fields=["team", "is_bye"],
        _allow_schedule_locked_bracket_progression=allow_schedule_locked_progression,
    )


def _set_tie_participants_from_slots(tie, *, allow_existing_matches=False):
    slots = _slot_map(tie, lock=True)
    home = slots[FootballKnockoutTieSlot.Slot.HOME].team
    away = slots[FootballKnockoutTieSlot.Slot.AWAY].team
    team_ids = [team.id for team in (home, away) if team]

    if len(team_ids) == 2:
        team_one_id, team_two_id = sorted(team_ids)
    elif len(team_ids) == 1:
        team_one_id, team_two_id = team_ids[0], None
    else:
        team_one_id = team_two_id = None

    if (
        tie.team_one_id == team_one_id
        and tie.team_two_id == team_two_id
    ):
        return

    if _bracket_matches(tie, lock=True) and not allow_existing_matches:
        raise ValidationError(
            "Bracket participants cannot be replaced after fixtures are generated."
        )

    if allow_existing_matches:
        tie.team_one_id = team_one_id
        tie.team_two_id = team_two_id
        tie.save(
            update_fields=["team_one", "team_two"],
            _allow_schedule_locked_bracket_progression=True,
        )
        return

    tie.team_one_id = team_one_id
    tie.team_two_id = team_two_id
    tie.save(update_fields=["team_one", "team_two"])


def _ensure_tie_matches(tie, *, allow_existing_rewrite=False):
    slots = _slot_map(tie, lock=True)
    home_team = slots[FootballKnockoutTieSlot.Slot.HOME].team
    away_team = slots[FootballKnockoutTieSlot.Slot.AWAY].team
    if tie.is_bye or not home_team or not away_team:
        return []

    rule = get_round_rule(tie.stage)
    existing_matches = _bracket_matches(tie, lock=True)
    expected_legs = set(range(1, rule.number_of_legs + 1))
    scheduled_for = _scheduled_for_legs(tie, rule)

    _set_tie_participants_from_slots(
        tie,
        allow_existing_matches=allow_existing_rewrite,
    )

    if existing_matches:
        if {match.leg for match in existing_matches} != expected_legs:
            raise ValidationError(
                f"{tie.stage.name} tie {tie.position} has an invalid fixture set."
            )
        for match in existing_matches:
            desired_home = home_team if match.leg == 1 else away_team
            desired_away = away_team if match.leg == 1 else home_team
            if (
                match.home_team_id != desired_home.id
                or match.away_team_id != desired_away.id
            ):
                if not allow_existing_rewrite:
                    raise ValidationError(
                        "Bracket fixtures cannot be changed outside safeguarded progression."
                    )
                match.home_team = desired_home
                match.away_team = desired_away
                match.save(
                    update_fields=["home_team", "away_team"],
                    # This is the one validated system path permitted to
                    # refresh future generated participants after an upstream
                    # bracket correction.  Model validation verifies the
                    # precise current HOME/AWAY slots before accepting it.
                    _allow_generated_bracket_participant_rewrite=True,
                )
        return existing_matches

    created = []
    for leg in sorted(expected_legs):
        match = Match(
            competition=tie.stage.competition,
            stage=tie.stage,
            home_team=home_team if leg == 1 else away_team,
            away_team=away_team if leg == 1 else home_team,
            knockout_tie=tie,
            leg=leg,
            match_date=scheduled_for[leg],
            status="UPCOMING",
            phase="NONE",
            generated_by_system=True,
        )
        # An established bracket may legitimately materialise its next
        # fixture after the competition schedule is published. The model
        # validates this private exception against the exact tie participants;
        # ordinary fixture creation has no corresponding bypass.
        match.save(_allow_schedule_locked_bracket_creation=True)
        created.append(match)
    return created


def _scheduled_for_legs(tie, rule):
    """Return the explicit kickoff for every configured leg, or fail safely."""
    scheduled_for = {1: tie.leg_one_scheduled_for}
    if rule.number_of_legs == 2:
        scheduled_for[2] = tie.leg_two_scheduled_for

    missing_legs = [str(leg) for leg, kickoff in scheduled_for.items() if not kickoff]
    if missing_legs:
        leg_label = ", ".join(missing_legs)
        raise ValidationError(
            f"Set the scheduled kickoff for leg {leg_label} of "
            f"{tie.stage.name} tie {tie.position} before generating fixtures."
        )
    return scheduled_for


def _destination_blocker(destination_tie):
    """Return the first downstream dependency that makes a rewrite unsafe."""
    if destination_tie.winner_id:
        matches = _bracket_matches(destination_tie, lock=True)
        return (
            matches[0] if matches else None,
            "the downstream tie has already resolved",
        )

    from predictions.models import Prediction

    for match in _bracket_matches(destination_tie, lock=True):
        if match.status != "UPCOMING" or match.phase != "NONE":
            return match, "the downstream match has started or finished"
        if match.minute or match.abandon_reason or match.lineups_locked:
            return match, "the downstream match has recorded match activity"
        if any([
            match.regulation_home_score is not None,
            match.regulation_away_score is not None,
            match.penalty_home_score is not None,
            match.penalty_away_score is not None,
            bool(match.resolution_method),
        ]):
            return match, "the downstream match has recorded result data"
        if Goal.objects.filter(match=match).exists():
            return match, "the downstream match has goals"
        if YellowCard.objects.filter(match=match).exists():
            return match, "the downstream match has disciplinary records"
        if MatchLineup.objects.filter(match=match).exists():
            return match, "the downstream match has submitted lineups"
        if Prediction.objects.filter(prediction_match__match=match).exists():
            return match, "the downstream match has submitted predictions"
    return None, None


def _advance_winner(tie, winner, resolution_method, *, actor=None, deciding_match=None):
    """Persist one winner and safely place it in its explicit destination slot."""
    tie = FootballKnockoutTie.objects.select_for_update(of=("self",)).select_related(
        "stage",
        "stage__competition",
        "winner",
        "winner_destination",
    ).get(pk=tie.pk)
    old_winner_id = tie.winner_id
    winner_changed = old_winner_id != winner.id
    destination_slot_changed = False

    # Record the completed source first.  The schedule-lock exception below
    # then proves that its exact winner is the only entrant allowed into the
    # linked downstream slot. This whole workflow is transactionally atomic.
    if winner_changed or tie.winner_resolution_method != resolution_method:
        tie.winner = winner
        tie.winner_resolution_method = resolution_method
        tie.save(update_fields=["winner", "winner_resolution_method"])

    destination = None
    if tie.winner_destination_id:
        destination = FootballKnockoutTie.objects.select_for_update(of=("self",)).select_related(
            "stage",
            "stage__competition",
            "winner",
        ).get(pk=tie.winner_destination_id)
        slot = _tie_slot(
            destination,
            tie.winner_destination_slot,
            lock=True,
        )
        if slot.team_id and slot.team_id != winner.id:
            if old_winner_id is None:
                raise ValidationError(
                    "The destination bracket slot is already occupied by a different team."
                )
            blocked_match, reason = _destination_blocker(destination)
            if reason:
                match_label = (
                    f"{blocked_match.home_team} vs {blocked_match.away_team}"
                    if blocked_match
                    else f"{destination.stage.name} tie {destination.position}"
                )
                raise BracketCorrectionBlocked(
                    "This result correction would replace a team in "
                    f"{match_label}, but {reason}. Downstream data was left unchanged.",
                    source_tie=tie,
                    destination_tie=destination,
                    match=blocked_match,
                )
            _set_slot_team(
                slot,
                winner,
                allow_schedule_locked_progression=True,
            )
            destination_slot_changed = True
        elif not slot.team_id:
            _set_slot_team(
                slot,
                winner,
                allow_schedule_locked_progression=True,
            )
            destination_slot_changed = True

    if destination_slot_changed:
        _set_tie_participants_from_slots(
            destination,
            allow_existing_matches=True,
        )
        _ensure_tie_matches(destination, allow_existing_rewrite=True)

    if not winner_changed and not destination_slot_changed:
        return tie

    from league_manager.models import ActivityLog

    values = {
        "tie_id": tie.id,
        "winner_id": winner.id,
        "resolution_method": resolution_method,
    }
    if destination:
        values.update({
            "destination_tie_id": destination.id,
            "destination_slot": tie.winner_destination_slot,
        })

    if resolution_method == FootballKnockoutTie.WinnerResolutionMethod.BYE:
        action = ActivityLog.Action.BYE_ADVANCED
        description = f"{winner.name} advanced by bye from {tie.stage.name}."
    elif old_winner_id and winner_changed:
        action = ActivityLog.Action.BRACKET_CORRECTION_RESOLVED
        description = f"Bracket winner corrected to {winner.name} in {tie.stage.name}."
    else:
        action = ActivityLog.Action.KNOCKOUT_WINNER_ADVANCED
        description = f"{winner.name} advanced from {tie.stage.name}."
    _record_activity(
        actor=actor,
        competition=tie.stage.competition,
        match=deciding_match,
        action=action,
        description=description,
        values=values,
    )

    if _is_terminal_tie(tie):
        _record_activity(
            actor=actor,
            competition=tie.stage.competition,
            match=deciding_match,
            action=ActivityLog.Action.KNOCKOUT_CHAMPION_DECLARED,
            description=f"{winner.name} was declared knockout champion.",
            values={"tie_id": tie.id, "champion_id": winner.id},
        )
    return tie


@transaction.atomic
def advance_completed_tie_for_match(match, *, actor=None):
    """Advance a persisted deciding match synchronously, or safely no-op."""
    if not match.pk or not match.knockout_tie_id:
        return None

    match = Match.objects.select_for_update(of=("self",)).select_related(
        "stage",
        "knockout_tie",
    ).get(pk=match.pk)
    if match.stage.stage_type != "KNOCKOUT":
        return None

    tie = FootballKnockoutTie.objects.select_for_update(of=("self",)).select_related(
        "stage",
        "stage__competition",
    ).get(pk=match.knockout_tie_id)
    if tie.position is None:
        return None

    resolved = resolve_completed_knockout_tie(tie)
    if resolved is None:
        return None
    return _advance_winner(
        tie,
        resolved["winner"],
        resolved["resolution_method"],
        actor=actor,
        deciding_match=resolved["deciding_match"],
    )


def _advance_explicit_bye(tie, *, actor=None):
    slots = _slot_map(tie, lock=True)
    home_slot = slots[FootballKnockoutTieSlot.Slot.HOME]
    away_slot = slots[FootballKnockoutTieSlot.Slot.AWAY]
    teams = [slot.team for slot in (home_slot, away_slot) if slot.team_id]
    bye_count = int(home_slot.is_bye) + int(away_slot.is_bye)
    if not tie.is_bye or len(teams) != 1 or bye_count != 1:
        return None
    _set_tie_participants_from_slots(tie)
    return _advance_winner(
        tie,
        teams[0],
        FootballKnockoutTie.WinnerResolutionMethod.BYE,
        actor=actor,
    )


@transaction.atomic
def generate_bracket_matches(competition, *, actor=None):
    """Validate, advance explicit byes, and create only scheduled fixtures."""
    ties = validate_bracket(competition)
    for tie in ties:
        if not tie.is_bye:
            _scheduled_for_legs(tie, get_round_rule(tie.stage))
    while True:
        changed = False
        for tie in _ordered_bracket_ties(competition, lock=True):
            tie.refresh_from_db()
            if tie.winner_id:
                continue
            if tie.is_bye:
                if _advance_explicit_bye(tie, actor=actor):
                    changed = True
                continue
            slots = _slot_map(tie, lock=True)
            if (
                slots[FootballKnockoutTieSlot.Slot.HOME].team_id
                and slots[FootballKnockoutTieSlot.Slot.AWAY].team_id
            ):
                before_count = len(_bracket_matches(tie, lock=True))
                _ensure_tie_matches(tie)
                if not before_count:
                    changed = True
        if not changed:
            break
    return _ordered_bracket_ties(competition)


@transaction.atomic
def save_manual_seeding(competition, selections):
    """Persist validated first-round team/bye choices without generating matches."""
    _ensure_bracket_is_editable(competition)
    ties = _ordered_bracket_ties(competition, lock=True)
    if not ties:
        raise ValidationError("Create a bracket structure before seeding it.")

    first_stage_id = _first_round_stage_id(ties)
    first_round_slots = list(
        FootballKnockoutTieSlot.objects.select_for_update().select_related(
            "tie",
            "team",
        ).filter(
            tie__stage_id=first_stage_id,
            tie__position__isnull=False,
        ).order_by("tie__position", "slot")
    )
    slots_by_id = {slot.id: slot for slot in first_round_slots}
    if set(selections) != set(slots_by_id):
        raise ValidationError("Every first-round bracket slot must be submitted.")

    chosen_team_ids = []
    for slot_id, team in selections.items():
        slot = slots_by_id[slot_id]
        if team is not None:
            if team.competition_id != competition.id:
                raise ValidationError("Every entrant must belong to this competition.")
            chosen_team_ids.append(team.id)
        slot.team = team
        slot.is_bye = team is None

    if len(chosen_team_ids) != len(set(chosen_team_ids)):
        raise ValidationError("A team can only be seeded once in the first round.")

    for tie in [tie for tie in ties if tie.stage_id == first_stage_id]:
        slots = [slot for slot in first_round_slots if slot.tie_id == tie.id]
        entrants = [slot for slot in slots if slot.team_id]
        if not entrants:
            raise ValidationError(
                f"{tie.stage.name} tie {tie.position} cannot contain two byes."
            )
        if len(entrants) == 1 and sum(slot.is_bye for slot in slots) != 1:
            raise ValidationError(
                f"{tie.stage.name} tie {tie.position} needs an explicit bye."
            )
        if len(entrants) == 2 and any(slot.is_bye for slot in slots):
            raise ValidationError(
                f"{tie.stage.name} tie {tie.position} cannot combine entrants and a bye."
            )

    # Clear old values first so an authorized pre-generation reseed can swap
    # two teams without transient duplicate-team validation failures.
    FootballKnockoutTieSlot.objects.filter(
        pk__in=slots_by_id,
    ).update(team=None, is_bye=False)

    for slot in first_round_slots:
        slot.full_clean()
        slot.save(update_fields=["team", "is_bye"])

    for tie in [tie for tie in ties if tie.stage_id == first_stage_id]:
        is_bye = any(
            slot.is_bye for slot in first_round_slots if slot.tie_id == tie.id
        )
        # Keep the tie's own validation true at every save while an authorized
        # pre-generation reseed changes between a two-team tie and a bye.
        if not is_bye and tie.is_bye:
            tie.is_bye = False
            tie.save(update_fields=["is_bye"])
        _set_tie_participants_from_slots(tie)
        if is_bye and not tie.is_bye:
            tie.is_bye = True
            tie.save(update_fields=["is_bye"])

    validate_bracket(competition)
    return _ordered_bracket_ties(competition)


def random_seed_bracket(competition, teams):
    """Create a fair first-round draw while distributing byes one per tie."""
    ties = _ordered_bracket_ties(competition)
    if not ties:
        raise ValidationError("Create a bracket structure before seeding it.")
    first_stage_id = _first_round_stage_id(ties)
    first_round_ties = [tie for tie in ties if tie.stage_id == first_stage_id]
    teams = list(teams)
    if len({team.id for team in teams}) != len(teams):
        raise ValidationError("A team can only be entered once in the draw.")
    if len(teams) < len(first_round_ties) or len(teams) > len(first_round_ties) * 2:
        raise ValidationError(
            "The draw needs enough teams to avoid an empty first-round tie."
        )
    if any(team.competition_id != competition.id for team in teams):
        raise ValidationError("Every entrant must belong to this competition.")

    SystemRandom().shuffle(teams)
    ordered_slots = []
    for slot_name in (
        FootballKnockoutTieSlot.Slot.HOME,
        FootballKnockoutTieSlot.Slot.AWAY,
    ):
        for tie in first_round_ties:
            ordered_slots.append(
                FootballKnockoutTieSlot.objects.get(tie=tie, slot=slot_name)
            )

    selections = {
        slot.id: (teams[index] if index < len(teams) else None)
        for index, slot in enumerate(ordered_slots)
    }
    return save_manual_seeding(competition, selections)


@transaction.atomic
def save_bracket_schedule(competition, schedules):
    """Save all explicit bracket kickoffs before any fixture is generated.

    A connected bracket deliberately has no invented ``now`` kickoff.  Storing
    the future dates on ties means a later round can be scheduled before its
    entrants are known, while Match rows still appear only when both slots are
    populated.
    """
    _ensure_bracket_is_editable(competition)
    ties = _ordered_bracket_ties(competition, lock=True)
    ties_by_id = {tie.id: tie for tie in ties}
    if set(schedules) != set(ties_by_id):
        raise ValidationError("Provide fixture times for every bracket tie.")

    for tie_id, values in schedules.items():
        tie = ties_by_id[tie_id]
        try:
            leg_one_scheduled_for = values[1]
        except KeyError as error:
            raise ValidationError(
                f"Provide the first-leg kickoff for {tie.stage.name} tie {tie.position}."
            ) from error
        if not leg_one_scheduled_for:
            raise ValidationError(
                f"Provide the first-leg kickoff for {tie.stage.name} tie {tie.position}."
            )
        rule = get_round_rule(tie.stage)
        leg_two_scheduled_for = values.get(2)
        if rule.number_of_legs == 2 and not leg_two_scheduled_for:
            raise ValidationError(
                f"Provide the second-leg kickoff for {tie.stage.name} tie {tie.position}."
            )
        if rule.number_of_legs == 1 and leg_two_scheduled_for:
            raise ValidationError(
                f"{tie.stage.name} is a single-leg round."
            )

        tie.leg_one_scheduled_for = leg_one_scheduled_for
        tie.leg_two_scheduled_for = leg_two_scheduled_for
        tie.full_clean()
        tie.save(
            update_fields=["leg_one_scheduled_for", "leg_two_scheduled_for"]
        )

    return _ordered_bracket_ties(competition)


@transaction.atomic
def save_bracket_links(competition, links):
    """Safely update the explicit winner graph before fixtures exist.

    ``links`` maps every bracket tie id to either ``(destination, slot)`` or
    ``None`` for the final.  The service—not the browser—validates the full
    graph, so links cannot cross competitions, go backwards, or share a slot.
    """
    _ensure_bracket_is_editable(competition)
    ties = _ordered_bracket_ties(competition, lock=True)
    ties_by_id = {tie.id: tie for tie in ties}
    if set(links) != set(ties_by_id):
        raise ValidationError("Every bracket tie must have a submitted link.")

    destination_slots = set()
    normalized_links = {}
    for source_id, target in links.items():
        source = ties_by_id[source_id]
        if target is None:
            normalized_links[source.id] = None
        else:
            destination, slot_name = target
            if destination.id not in ties_by_id:
                raise ValidationError("Bracket links must stay within this competition.")
            if destination.id == source.id:
                raise ValidationError("A tie cannot feed itself.")
            if destination.stage.order <= source.stage.order:
                raise ValidationError("A tie can only feed a later knockout round.")
            if slot_name not in {
                FootballKnockoutTie.DestinationSlot.HOME,
                FootballKnockoutTie.DestinationSlot.AWAY,
            }:
                raise ValidationError("Select a valid destination bracket slot.")
            key = (destination.id, slot_name)
            if key in destination_slots:
                raise ValidationError("Two ties cannot populate the same destination slot.")
            destination_slots.add(key)
            normalized_links[source.id] = (destination, slot_name)

    # Clear every existing link first.  This makes a valid HOME/AWAY slot swap
    # possible under the database's one-source-per-destination-slot constraint.
    FootballKnockoutTie.objects.filter(pk__in=ties_by_id).update(
        winner_destination=None,
        winner_destination_slot="",
    )

    for source_id, target in normalized_links.items():
        source = ties_by_id[source_id]
        if target is None:
            source.winner_destination = None
            source.winner_destination_slot = ""
        else:
            source.winner_destination, source.winner_destination_slot = target
        source.full_clean()
        source.save(
            update_fields=["winner_destination", "winner_destination_slot"]
        )

    validate_bracket(competition, require_entrants=False)
    return _ordered_bracket_ties(competition)
