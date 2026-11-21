from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from leagues.models import Competition, Standing
from teams.models import Team
from .models import Goal, Match, Player, YellowCard
from .knockout import validate_official_result
from .result_outbox import lock_result_competition, request_result_processing


_UNSET = object()
OFFICIAL_ABANDONMENT_RESULT_RESOLUTIONS = frozenset({
    Match.AbandonmentResolution.AWARDED,
    Match.AbandonmentResolution.CONFIRMED,
})


class OfficialAbandonmentResultError(ValidationError):
    def __init__(self, match_id):
        super().__init__(
            "Reopen the official abandonment decision through the controlled "
            "abandoned-match workflow before correcting this result."
        )
        self.match_id = match_id


def _normalise_player_name(name):
    """Make harmless display-name variations compare as one roster entry."""
    return " ".join((name or "").split())


def resolve_goal_scorer(
    *,
    match,
    benefiting_team_id,
    goal_type,
    player=None,
    new_player_name="",
    allow_unattributed_own_goal=False,
):
    """Return the valid scorer, creating one in the match-derived team if needed.

    Callers must already hold the match lock.  Locking the expected team makes
    same-team name resolution serial, while keeping the transaction limited to
    the existing goal/result write.
    """
    if benefiting_team_id not in {match.home_team_id, match.away_team_id}:
        raise ValidationError("The scoring team must be playing in this match.")

    expected_team_id = benefiting_team_id
    if goal_type == Goal.GoalType.OWN_GOAL:
        expected_team_id = (
            match.away_team_id
            if benefiting_team_id == match.home_team_id
            else match.home_team_id
        )

    new_player_name = _normalise_player_name(new_player_name)
    if player and new_player_name:
        raise ValidationError("Select an existing player or enter a new player, not both.")
    if player:
        # Fetch again inside this transaction: never trust a browser-supplied
        # player id or an object resolved from an earlier queryset.
        player = Player.objects.select_related("team").filter(pk=player.pk).first()
        if not player or player.team_id != expected_team_id:
            raise ValidationError("Choose a player registered for the correct team.")
        return player

    if not new_player_name:
        if goal_type == Goal.GoalType.OWN_GOAL and not allow_unattributed_own_goal:
            raise ValidationError(
                "Select or enter the opposing player responsible for the own goal."
            )
        return None

    # Serialise matching/creation for this roster without trusting a submitted
    # team id.  The expected side above is derived solely from the locked match.
    team = Team.objects.select_for_update().get(pk=expected_team_id)
    for candidate in Player.objects.select_for_update().filter(team=team).order_by("id"):
        if _normalise_player_name(candidate.name).casefold() == new_player_name.casefold():
            return candidate
    return Player.objects.create(team=team, name=new_player_name)


def resolve_card_player(*, match, team_id, player=None, new_player_name=""):
    """Resolve a card recipient using the same roster rules as goal scorers.

    The team is derived from the locked match and is never trusted merely
    because a client submitted it.  This shares whitespace/case-folded player
    registration with the established goal workflow.
    """
    if team_id not in {match.home_team_id, match.away_team_id}:
        raise ValidationError("The selected team is not playing in this match.")
    return resolve_goal_scorer(
        match=match,
        benefiting_team_id=team_id,
        goal_type=Goal.GoalType.NORMAL,
        player=player,
        new_player_name=new_player_name,
    )


def recalculate_card_types(match, player_id=None):
    """Rebuild yellow/second-yellow status from active event history.

    A correction or cancellation cannot leave a later event permanently marked
    as a dismissal when its earlier caution no longer exists.  Direct reds are
    separate events and remain untouched.
    """
    cards = YellowCard.objects.select_for_update().filter(
        match=match,
        canceled=False,
        card_type__in=[
            YellowCard.CardType.YELLOW,
            YellowCard.CardType.SECOND_YELLOW_RED,
        ],
    )
    if player_id:
        cards = cards.filter(player_id=player_id)
        player_ids = [player_id]
    else:
        player_ids = list(cards.values_list("player_id", flat=True).distinct())

    for current_player_id in player_ids:
        player_cards = cards.filter(player_id=current_player_id).order_by("minute", "id")
        for index, card in enumerate(player_cards):
            desired = (
                YellowCard.CardType.YELLOW
                if index == 0
                else YellowCard.CardType.SECOND_YELLOW_RED
            )
            if card.card_type != desired:
                card.card_type = desired
                card.save(update_fields=["card_type"])


def apply_result_to_standings(home_standing, away_standing, home_score, away_score):
    home_standing.played += 1
    away_standing.played += 1

    home_standing.goals_for += home_score
    home_standing.goals_against += away_score

    away_standing.goals_for += away_score
    away_standing.goals_against += home_score

    if home_score > away_score:
        home_standing.won += 1
        home_standing.points += 3
        away_standing.lost += 1

    elif home_score < away_score:
        away_standing.won += 1
        away_standing.points += 3
        home_standing.lost += 1

    else:
        home_standing.drawn += 1
        away_standing.drawn += 1
        home_standing.points += 1
        away_standing.points += 1


def reverse_result_from_standings(home_standing, away_standing, home_score, away_score):
    home_standing.played -= 1
    away_standing.played -= 1

    home_standing.goals_for -= home_score
    home_standing.goals_against -= away_score

    away_standing.goals_for -= away_score
    away_standing.goals_against -= home_score

    if home_score > away_score:
        home_standing.won -= 1
        home_standing.points -= 3
        away_standing.lost -= 1

    elif home_score < away_score:
        away_standing.won -= 1
        away_standing.points -= 3
        home_standing.lost -= 1

    else:
        home_standing.drawn -= 1
        away_standing.drawn -= 1
        home_standing.points -= 1
        away_standing.points -= 1


@transaction.atomic
def apply_match_to_standings_incrementally(match):
    """Apply the current result and return its locked fixture.

    All table writers lock competition, fixture, then standings. A caller
    with an outer transaction retains these locks for related prediction
    writes, so a reopened result cannot be scored by a stale worker.
    """
    competition_id = Match.objects.values_list(
        "competition_id", flat=True
    ).get(pk=match.pk)
    lock_result_competition(competition_id)
    match = Match.objects.select_for_update(of=("self",)).select_related(
        "competition",
        "stage",
        "home_team",
        "away_team",
    ).get(pk=match.pk)
    if match.status != "FINISHED":
        return match

    if match.stage.stage_type not in ["LEAGUE", "GROUP"]:
        return match

    if match.table_updated:
        if match.table_home_score is None or match.table_away_score is None:
            raise ValidationError(
                "The standings contribution for this match is missing its applied score. "
                "Rebuild the competition standings before correcting the result."
            )
        if (match.table_home_score, match.table_away_score) == (match.home_score, match.away_score):
            return match

    standing_defaults = {
        "played": 0,
        "won": 0,
        "drawn": 0,
        "lost": 0,
        "goals_for": 0,
        "goals_against": 0,
        "points": 0,
    }
    # Opposite home/away fixtures must acquire shared standings in one order
    # or concurrent recalculation tasks can form an AB/BA lock cycle.
    standings_by_team_id = {}
    for team_id in sorted((match.home_team_id, match.away_team_id)):
        standing, created = Standing.objects.select_for_update().get_or_create(
            competition=match.competition,
            stage=match.stage,
            group_id=match.group_id,
            team_id=team_id,
            defaults=standing_defaults,
        )
        if created and match.table_updated:
            raise ValidationError(
                "An applied match is missing a team standing. "
                "Rebuild the competition standings before correcting the result."
            )
        standings_by_team_id[team_id] = standing
    home_standing = standings_by_team_id[match.home_team_id]
    away_standing = standings_by_team_id[match.away_team_id]

    if match.table_updated:
        reverse_result_from_standings(
            home_standing,
            away_standing,
            match.table_home_score,
            match.table_away_score,
        )
        match_totals = ("played", "won", "drawn", "lost", "goals_for", "goals_against", "points")
        if any(
            getattr(standing, field) < 0
            for standing in (home_standing, away_standing)
            for field in match_totals
        ):
            raise ValidationError(
                "The standings cannot safely reverse this match's applied score. "
                "Rebuild the competition standings before correcting the result."
            )

    apply_result_to_standings(
        home_standing,
        away_standing,
        match.home_score,
        match.away_score,
    )

    home_standing.save()
    away_standing.save()

    match.table_updated = True
    match.table_home_score = match.home_score
    match.table_away_score = match.away_score
    match.save(update_fields=[
        "table_updated",
        "table_home_score",
        "table_away_score",
    ])
    return match


def _prevent_invalidating_later_knockout_legs(match, normalized):
    """Keep a completed dependent leg from silently becoming invalid.

    Phase One deliberately does not rewrite or re-finalize dependent historical
    legs.  An earlier score is therefore immutable once a later leg is final,
    rather than leaving that later aggregate result inconsistent.
    """
    if (
        not match.pk
        or match.stage.stage_type != "KNOCKOUT"
        or not match.knockout_tie_id
    ):
        return

    result_fields = (
        "home_score",
        "away_score",
        "regulation_home_score",
        "regulation_away_score",
        "penalty_home_score",
        "penalty_away_score",
    )
    persisted_values = Match.objects.filter(pk=match.pk).values(*result_fields).first()
    changed = any(
        persisted_values[field_name] != normalized[field_name]
        for field_name in result_fields
    )
    if not changed:
        return

    later_leg_is_finished = match.knockout_tie.matches.filter(
        leg__gt=match.leg,
        status="FINISHED",
    ).exists()
    if later_leg_is_finished:
        raise ValidationError(
            "This earlier leg cannot be corrected because a later leg in the "
            "tie is already finished. Dependent historical results are not "
            "rewritten automatically."
        )


@transaction.atomic
def finalize_match_result_fast(
    match,
    home_score=None,
    away_score=None,
    regulation_home_score=_UNSET,
    regulation_away_score=_UNSET,
    penalty_home_score=_UNSET,
    penalty_away_score=_UNSET,
    actor=None,
):
    """Validate and finish a match through the one official result path."""
    preserved_abandon_reason = None
    current = None
    lock_result_competition(match.competition_id)
    if match.pk:
        # A submitted form may hold the fixture state from before abandonment
        # or a committee decision. Validate its current state under the same
        # row lock as the result write, without discarding submitted scores.
        current = (
            Match.objects.select_for_update(of=("self",))
            .only(
                "status", "abandonment_resolution", "abandon_reason",
                "home_score", "away_score", "regulation_home_score", "regulation_away_score",
                "penalty_home_score", "penalty_away_score", "resolution_method",
            )
            .get(pk=match.pk)
        )
        if current.status == "ABANDONED":
            raise ValidationError(
                "Resolve this abandoned match through the controlled "
                "abandoned-match workflow before recording a result."
            )
        if current.abandonment_resolution in OFFICIAL_ABANDONMENT_RESULT_RESOLUTIONS:
            raise OfficialAbandonmentResultError(current.pk)
        if current.abandonment_resolution == Match.AbandonmentResolution.RESUMED:
            preserved_abandon_reason = current.abandon_reason
    normalized = validate_official_result(
        match,
        home_score=match.home_score if home_score is None else home_score,
        away_score=match.away_score if away_score is None else away_score,
        regulation_home_score=(
            match.regulation_home_score
            if regulation_home_score is _UNSET
            else regulation_home_score
        ),
        regulation_away_score=(
            match.regulation_away_score
            if regulation_away_score is _UNSET
            else regulation_away_score
        ),
        penalty_home_score=(
            match.penalty_home_score
            if penalty_home_score is _UNSET
            else penalty_home_score
        ),
        penalty_away_score=(
            match.penalty_away_score
            if penalty_away_score is _UNSET
            else penalty_away_score
        ),
    )
    _prevent_invalidating_later_knockout_legs(match, normalized)

    for field_name, value in normalized.items():
        setattr(match, field_name, value)

    match.status = "FINISHED"
    match.phase = "NONE"
    match.phase_start_time = None
    match.minute = 0
    match.abandon_reason = preserved_abandon_reason

    result_fields = [
        "home_score",
        "away_score",
        "regulation_home_score",
        "regulation_away_score",
        "penalty_home_score",
        "penalty_away_score",
        "resolution_method",
        "status",
        "phase",
        "phase_start_time",
        "minute",
        "abandon_reason",
    ]
    if match.pk:
        match.save(update_fields=result_fields)
    else:
        match.save()

    # Bracket correctness and the durable processing request share the result transaction.
    from .bracket import advance_completed_tie_for_match

    advance_completed_tie_for_match(match, actor=actor)
    if current is None or current.status != "FINISHED" or any(
        getattr(current, field) != value for field, value in normalized.items()
    ):
        request_result_processing(match)

    return match


@transaction.atomic
def apply_official_match_result(
    match,
    home_score=None,
    away_score=None,
    *,
    actor=None,
    **result_values,
):
    """Backward-compatible public entry point for official result writers."""
    if home_score is not None:
        result_values["home_score"] = home_score
    if away_score is not None:
        result_values["away_score"] = away_score
    return finalize_match_result_fast(match, actor=actor, **result_values)


def recorded_goal_totals(match):
    """Count non-cancelled football goals by the team that received them."""
    return {
        match.home_team_id: match.goals.filter(
            team_id=match.home_team_id, canceled=False
        ).count(),
        match.away_team_id: match.goals.filter(
            team_id=match.away_team_id, canceled=False
        ).count(),
    }


def validate_result_against_recorded_goals(match, *, home_score, away_score):
    """Reject a result that would contradict already-recorded live goals."""
    totals = recorded_goal_totals(match)
    if any(totals.values()) and (
        totals[match.home_team_id] != home_score
        or totals[match.away_team_id] != away_score
    ):
        raise ValidationError(
            "This match already has goal events that do not match the submitted "
            "final score. Use the normal goal correction workflow first."
        )


@transaction.atomic
def save_quick_goal_details(match, rows, *, actor=None):
    """Attach optional, minute-less scorer records to a quick final result."""
    match = Match.objects.select_for_update(of=("self",)).select_related(
        "competition", "home_team", "away_team", "stage"
    ).get(pk=match.pk)
    if match.status != "FINISHED":
        raise ValidationError("Save the final result before adding goal details.")
    if match.abandonment_resolution in {
        Match.AbandonmentResolution.AWARDED,
        Match.AbandonmentResolution.CONFIRMED,
    }:
        raise ValidationError(
            "Goal details cannot be created for an official abandonment decision. "
            "Preserve the recorded match events and use the abandoned-match workflow "
            "to reopen an incorrect decision."
        )

    if match.goals.filter(canceled=False).exists():
        raise ValidationError(
            "Goal events already exist for this match. Use the normal goal "
            "correction workflow instead of adding a second set."
        )

    expected = {
        match.home_team_id: match.home_score,
        match.away_team_id: match.away_score,
    }
    actual = {match.home_team_id: 0, match.away_team_id: 0}
    prepared = []
    for row in rows:
        team_id = row["team_id"]
        goal_type = row["goal_type"]
        player_id = row.get("player_id")
        if team_id not in expected or goal_type not in Goal.GoalType.values:
            raise ValidationError("Invalid goal detail submitted.")
        player = None
        if player_id:
            player = Player.objects.filter(pk=player_id).first()
            if not player:
                raise ValidationError("Invalid scorer selected.")
        player = resolve_goal_scorer(
            match=match,
            benefiting_team_id=team_id,
            goal_type=goal_type,
            player=player,
            new_player_name=row.get("new_player_name", ""),
            allow_unattributed_own_goal=True,
        )
        actual[team_id] += 1
        prepared.append((team_id, player, goal_type))

    if actual != expected:
        raise ValidationError("Goal details must exactly match the saved final score.")

    for team_id, player, goal_type in prepared:
        Goal.objects.create(
            match=match,
            team_id=team_id,
            player=player,
            player_name=player.name if player else "",
            goal_type=goal_type,
            minute=None,
        )
    return match


def _require_control(match, flag, message):
    from .control_state import get_match_control_state

    state = get_match_control_state(match)
    if not getattr(state, flag):
        reason = state.unavailable_reason or message
        raise ValidationError(reason)


@transaction.atomic
def end_regulation(match, *, actor=None):
    lock_result_competition(match.competition_id)
    _require_control(
        match,
        "can_end_regulation",
        "Regulation can only end while the second half is running.",
    )
    match.minute = match.get_current_minute()
    match.regulation_home_score = match.home_score
    match.regulation_away_score = match.away_score
    match.penalty_home_score = None
    match.penalty_away_score = None
    match.resolution_method = ""
    match.phase = "REGULATION_ENDED"
    match.phase_start_time = None
    match.status = "LIVE"
    match.save(update_fields=[
        "minute",
        "regulation_home_score",
        "regulation_away_score",
        "penalty_home_score",
        "penalty_away_score",
        "resolution_method",
        "phase",
        "phase_start_time",
        "status",
    ])

    from .control_state import get_match_control_state

    if get_match_control_state(match).can_finish_match:
        finalize_match_result_fast(match, actor=actor)
    return match


@transaction.atomic
def finish_ready_match(match, *, actor=None):
    _require_control(
        match,
        "can_finish_match",
        "This match is not ready to be finished.",
    )
    return finalize_match_result_fast(match, actor=actor)


@transaction.atomic
def start_extra_time(match, *, actor=None):
    _require_control(
        match,
        "can_start_extra_time",
        "Extra time is not available for this match.",
    )
    match.phase = "EXTRA_FIRST_HALF"
    match.phase_start_time = timezone.now()
    match.status = "LIVE"
    match.minute = max(match.minute, 90)
    match.lineups_locked = True
    match.save(update_fields=[
        "phase",
        "phase_start_time",
        "status",
        "minute",
        "lineups_locked",
    ])
    return match


@transaction.atomic
def end_extra_time_first_half(match, *, actor=None):
    _require_control(
        match,
        "can_end_extra_time_first_half",
        "The first half of extra time is not running.",
    )
    match.minute = match.get_current_minute()
    match.phase = "EXTRA_BREAK"
    match.phase_start_time = None
    match.save(update_fields=["minute", "phase", "phase_start_time"])
    return match


@transaction.atomic
def start_extra_time_second_half(match, *, actor=None):
    _require_control(
        match,
        "can_start_extra_time_second_half",
        "The second half of extra time cannot start now.",
    )
    match.phase = "EXTRA_SECOND_HALF"
    match.phase_start_time = timezone.now()
    match.minute = max(match.minute, 105)
    match.lineups_locked = True
    match.save(update_fields=[
        "phase",
        "phase_start_time",
        "minute",
        "lineups_locked",
    ])
    return match


@transaction.atomic
def end_extra_time(match, *, actor=None):
    lock_result_competition(match.competition_id)
    _require_control(
        match,
        "can_end_extra_time",
        "Extra time can only end while its second half is running.",
    )
    match.minute = match.get_current_minute()
    match.phase = "EXTRA_ENDED"
    match.phase_start_time = None
    match.save(update_fields=["minute", "phase", "phase_start_time"])

    from .control_state import get_match_control_state

    if get_match_control_state(match).can_finish_match:
        finalize_match_result_fast(match, actor=actor)
    return match


@transaction.atomic
def start_penalty_shootout(match, *, actor=None):
    _require_control(
        match,
        "can_start_penalties",
        "A penalty shootout is not available for this match.",
    )
    match.phase = "PENALTIES"
    match.phase_start_time = None
    match.lineups_locked = True
    match.save(update_fields=["phase", "phase_start_time", "lineups_locked"])
    return match


@transaction.atomic
def finish_penalty_shootout(match, home_penalties, away_penalties, *, actor=None):
    _require_control(
        match,
        "can_finish_penalties",
        "A penalty shootout is not currently in progress.",
    )
    return finalize_match_result_fast(
        match,
        penalty_home_score=home_penalties,
        penalty_away_score=away_penalties,
        actor=actor,
    )


@transaction.atomic(savepoint=False)
def rebuild_league_standings(league):
    """Rebuild materialised standings with bounded database work.

    The former implementation reset every row and then called the incremental
    service for each completed match.  That service correctly protects one
    live result, but it also creates a transaction/savepoint and several row
    locks per fixture.  A deletion of a large season therefore multiplied
    round trips until the request could exhaust a synchronous worker timeout.

    A rebuild already owns the complete source of truth, so calculate it in
    memory and persist each affected standing and fixture set in bulk.  The
    same fields and 3/1/0 rules remain authoritative; ranking/tiebreakers are
    still applied at presentation time by ``leagues.standings``.
    """

    league = Competition.objects.select_for_update(of=("self",)).get(pk=league.pk)
    stage_ids = list(
        league.stages.filter(stage_type__in=["LEAGUE", "GROUP"])
        .values_list("id", flat=True)
    )
    if not stage_ids:
        return

    # Lock every existing table fixture before any standings. Otherwise a
    # result worker can hold a newly finished fixture while waiting for a
    # standing that the rebuild already owns. Keep the captured IDs as well:
    # later inserts must not be marked applied without entering these totals.
    locked_matches = list(
        Match.objects.select_for_update(of=("self",))
        .filter(competition=league, stage_id__in=stage_ids)
        .only(
            "id", "stage_id", "group_id", "home_team_id", "away_team_id",
            "home_score", "away_score", "status",
        )
        .order_by("id")
    )
    source_matches = [match for match in locked_matches if match.status == "FINISHED"]

    standings = list(
        Standing.objects.select_for_update()
        .filter(competition=league, stage_id__in=stage_ids)
    )
    by_scope = {
        (standing.stage_id, standing.group_id, standing.team_id): standing
        for standing in standings
    }
    missing = []
    for match in source_matches:
        for team_id in (match.home_team_id, match.away_team_id):
            key = (match.stage_id, match.group_id, team_id)
            if key not in by_scope:
                standing = Standing(
                    competition=league,
                    stage_id=match.stage_id,
                    group_id=match.group_id,
                    team_id=team_id,
                )
                by_scope[key] = standing
                missing.append(standing)
    if missing:
        Standing.objects.bulk_create(missing, batch_size=500)
        standings.extend(missing)

    for standing in standings:
        standing.played = 0
        standing.won = 0
        standing.drawn = 0
        standing.lost = 0
        standing.goals_for = 0
        standing.goals_against = 0
        standing.points = 0

    for match in source_matches:
        home_standing = by_scope[(match.stage_id, match.group_id, match.home_team_id)]
        away_standing = by_scope[(match.stage_id, match.group_id, match.away_team_id)]
        apply_result_to_standings(
            home_standing,
            away_standing,
            match.home_score,
            match.away_score,
        )

    if standings:
        Standing.objects.bulk_update(
            standings,
            [
                "played", "won", "drawn", "lost", "goals_for",
                "goals_against", "points",
            ],
            batch_size=500,
        )

    table_matches = Match.objects.filter(pk__in=[match.pk for match in locked_matches])
    table_matches.update(
        table_updated=False,
        table_home_score=None,
        table_away_score=None,
    )
    table_matches.filter(pk__in=[match.pk for match in source_matches]).update(
        table_updated=True,
        table_home_score=F("home_score"),
        table_away_score=F("away_score"),
    )
