"""Write-side services and future processing boundaries for pool.

Views and model saves do not contain standings, bracket, ranking, or Celery
work.  Keeping those concerns here means later engines can be added without
changing the core competition-entry schema.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.text import slugify
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolCompetitionEntry,
    PoolKnockoutTie,
    PoolMatch,
    PoolManualResultRackSummary,
    PoolMatchResultRevision,
    PoolMatchScorerAssignment,
    PoolPlayer,
    PoolRack,
    PoolTeamMatchScheduledRack,
    PoolTeamRoster,
)


class PoolResultValidationError(ValidationError):
    """A domain validation error that manager forms can display safely."""


_UNCHANGED = object()
_NON_PLAYED_RESULT_METHODS = {
    PoolMatch.ResultMethod.WALKOVER,
    PoolMatch.ResultMethod.WITHDRAWAL,
    PoolMatch.ResultMethod.DISQUALIFICATION,
}
_TRANSITIONABLE_STATUSES = {
    PoolMatch.Status.POSTPONED,
    PoolMatch.Status.CANCELLED,
    PoolMatch.Status.ABANDONED,
}


_TIMED_COMPETITION_FORMATS = {
    PoolCompetition.FormatType.KNOCKOUT,
    PoolCompetition.FormatType.LEAGUE_KNOCKOUT,
}
_TIMER_EXPIRY_BLOCKED = object()


def _completion_reason_value(name: str) -> str:
    """Return a completion-reason value without coupling callers to choices.

    The timer fields intentionally live on ``PoolMatch``.  Keeping this tiny
    helper here lets the service use the model's named choice when available
    while retaining the persisted string as the actual service contract.
    """

    choices = getattr(PoolMatch, "CompletionReason", None)
    return getattr(choices, name, name)


def _match_timer_deadline(match: PoolMatch):
    """Return the persisted deadline only for a real applied match timer."""

    deadline = getattr(match, "timer_deadline", None)
    duration = getattr(match, "applied_time_limit_minutes", None)
    started_at = getattr(match, "timer_started_at", None)
    if not deadline or not duration or not started_at:
        return None
    return deadline


def _match_timer_is_due(match: PoolMatch, *, now=None) -> bool:
    """Whether this currently-live played fixture has reached its deadline."""

    return bool(
        _match_timer_has_elapsed(match, now=now)
        and match.status == PoolMatch.Status.LIVE
        and match.result_method == PoolMatch.ResultMethod.PLAYED
    )


def _match_timer_has_elapsed(match: PoolMatch, *, now=None) -> bool:
    """Whether the immutable applied deadline has elapsed in any match state.

    Unlike :func:`_match_timer_is_due`, this intentionally also answers for a
    finished fixture.  Result correction can reopen a race-target result, and
    that correction must use the original timer rather than silently creating
    a fresh unlimited live match after its deadline.
    """

    deadline = _match_timer_deadline(match)
    return bool(
        deadline
        and deadline <= (now or timezone.now())
    )


def _configured_match_time_limit_minutes(match: PoolMatch) -> int | None:
    """Return the current competition timer only when it is applicable.

    This is deliberately used solely when a fixture first starts.  Every
    later check consults the match's persisted snapshot, so a manager's
    competition-level edit can never rewrite a live countdown.
    """

    competition = match.competition
    if competition.format_type not in _TIMED_COMPETITION_FORMATS:
        return None
    value = getattr(competition, "match_time_limit_minutes", None)
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _timer_expiry_message(match: PoolMatch) -> str:
    if getattr(match, "time_expired_tied", False):
        if _team_tied_expiry_has_no_live_scheduled_rack(match):
            return (
                "The match time limit expired tied with no team rack live. "
                "No new scheduled rack may start; a competition manager must "
                "abandon the fixture or record a genuine withdrawal or "
                "disqualification."
            )
        return (
            "The match time limit has expired at a tied score. Only the next "
            "valid deciding rack may be recorded."
        )
    return "The match time limit has expired and the result was finalised."


def default_stage_definitions(competition: PoolCompetition):
    """Return the minimum explicit stages appropriate for a new competition."""
    if competition.format_type == PoolCompetition.FormatType.MONEY_MATCH:
        return [
            {
                "name": "Money Match",
                "stage_type": PoolCompetitionStage.StageType.MONEY,
                "order": 1,
            }
        ]
    if competition.format_type == PoolCompetition.FormatType.KNOCKOUT:
        return [
            {
                "name": "Knockout Stage",
                "stage_type": PoolCompetitionStage.StageType.KNOCKOUT,
                "order": 1,
            }
        ]
    if competition.format_type == PoolCompetition.FormatType.LEAGUE_KNOCKOUT:
        return [
            {
                "name": "League Stage",
                "stage_type": PoolCompetitionStage.StageType.LEAGUE,
                "order": 1,
            },
            {
                "name": "Knockout Stage",
                "stage_type": PoolCompetitionStage.StageType.KNOCKOUT,
                "order": 2,
            },
        ]
    return [
        {
            "name": "League Stage",
            "stage_type": PoolCompetitionStage.StageType.LEAGUE,
            "order": 1,
        }
    ]


@transaction.atomic
def ensure_default_stages(competition: PoolCompetition):
    """Create missing default stages without overwriting administrator edits."""
    if not competition.pk:
        raise ValueError("A competition must be saved before stages are created.")

    stages = []
    for definition in default_stage_definitions(competition):
        stage, _ = PoolCompetitionStage.objects.get_or_create(
            competition=competition,
            order=definition["order"],
            defaults={
                "name": definition["name"],
                "stage_type": definition["stage_type"],
            },
        )
        stages.append(stage)
    return stages


# More discoverable name for callers that do not need the idempotency detail.
create_default_stages = ensure_default_stages


@transaction.atomic
def ensure_competition_owner_membership(competition: PoolCompetition):
    """Synchronise the direct owner field with its membership representation.

    A newly created draft may have no owner yet.  Once an owner is assigned,
    that user gains the sole active OWNER membership; a previous owner remains
    an ADMIN so historical management access is not accidentally erased.
    """
    if not competition.pk:
        return None

    if not competition.owner_id:
        # A draft may deliberately be unassigned.  Do not leave a former
        # owner-role membership behind, because that would retain owner-level
        # permission after the direct owner field has been cleared.
        (
            PoolCompetitionMembership.objects.select_for_update()
            .filter(
                competition=competition,
                role=PoolCompetitionMembership.Role.OWNER,
            )
            .update(role=PoolCompetitionMembership.Role.ADMIN)
        )
        return None

    (
        PoolCompetitionMembership.objects.select_for_update()
        .filter(
            competition=competition,
            role=PoolCompetitionMembership.Role.OWNER,
        )
        .exclude(user_id=competition.owner_id)
        .update(role=PoolCompetitionMembership.Role.ADMIN)
    )
    membership, _ = PoolCompetitionMembership.objects.get_or_create(
        competition=competition,
        user_id=competition.owner_id,
        defaults={
            "role": PoolCompetitionMembership.Role.OWNER,
            "is_active": True,
        },
    )
    if (
        membership.role != PoolCompetitionMembership.Role.OWNER
        or not membership.is_active
    ):
        membership.role = PoolCompetitionMembership.Role.OWNER
        membership.is_active = True
        membership.save(update_fields=["role", "is_active", "updated_at"])
    return membership


def _money_match_slug(name):
    """Build a unique, readable slug without exposing a generic competition creator."""

    base_slug = slugify(name) or "money-match"
    slug = base_slug[:255]
    suffix = 2
    while PoolCompetition.objects.filter(slug=slug).exists():
        suffix_text = f"-{suffix}"
        slug = f"{base_slug[: 255 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return slug


def _validate_money_match_opponents(opponent_one, opponent_two):
    """Return the two player entries and labels for a singles Money Match."""

    if opponent_one is None or opponent_two is None:
        raise PoolResultValidationError("Select exactly two opponents.")
    if getattr(opponent_one, "pk", None) == getattr(opponent_two, "pk", None):
        raise PoolResultValidationError("A Money Match opponent cannot play itself.")
    if not isinstance(opponent_one, PoolPlayer) or not isinstance(opponent_two, PoolPlayer):
        raise PoolResultValidationError("Money Matches require two players.")
    return (
        opponent_one.full_name,
        opponent_two.full_name,
        ({"player": opponent_one}, {"player": opponent_two}),
    )


def _validated_money_match_race_target(race_target):
    if isinstance(race_target, bool):
        raise PoolResultValidationError("Race target must be a positive whole number.")
    try:
        race_target = int(race_target)
    except (TypeError, ValueError) as error:
        raise PoolResultValidationError("Race target must be a positive whole number.") from error
    if race_target < 1:
        raise PoolResultValidationError("Race target must be at least 1.")
    return race_target


def _validated_money_match_prize_pot(prize_pot):
    """Normalise the optional non-negative Money Match prize amount."""

    if prize_pot is _UNCHANGED:
        return _UNCHANGED
    if prize_pot in (None, ""):
        return None
    try:
        prize_pot = Decimal(str(prize_pot))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PoolResultValidationError("Prize pot must be a valid amount.") from error
    if not prize_pot.is_finite() or prize_pot < 0:
        raise PoolResultValidationError("Prize pot cannot be negative.")
    return prize_pot


def _money_match_stage(competition):
    stages = list(
        PoolCompetitionStage.objects.select_for_update()
        .filter(competition=competition)
        .order_by("order", "id")
    )
    money_stages = [
        stage
        for stage in stages
        if stage.stage_type == PoolCompetitionStage.StageType.MONEY
    ]
    if len(money_stages) == 1 and len(stages) == 1:
        return money_stages[0]
    if money_stages or stages:
        raise PoolResultValidationError(
            "This Money Match has an invalid stage configuration. It must have exactly one Money Match stage."
        )
    return PoolCompetitionStage.objects.create(
        competition=competition,
        name="Money Match",
        stage_type=PoolCompetitionStage.StageType.MONEY,
        order=1,
    )


def _locked_money_match_entries(competition):
    entries = list(
        # ``player`` is nullable for team entries, so select_related creates
        # an outer join.  PostgreSQL must lock only the entry row, not that
        # nullable joined side.
        PoolCompetitionEntry.objects.select_for_update(of=("self",))
        .filter(competition=competition)
        .select_related("player")
        .order_by("id")
    )
    if len(entries) > 2:
        raise PoolResultValidationError(
            "This Money Match has more than two entries. Remove the invalid extra entries before configuring opponents."
        )
    for entry in entries:
        if not entry.player_id or entry.team_id:
            raise PoolResultValidationError(
                "Money Matches require singles player entries."
            )
    return entries


def _locked_money_match_fixture(competition):
    fixtures = list(
        # ``PoolCompetitionEntry.player`` is nullable for team entries.  The
        # participant joins below are therefore outer joins, which PostgreSQL
        # cannot lock.  The competition row is locked first; lock only the
        # fixture rows here while retaining the loaded presentation data.
        PoolMatch.objects.select_for_update(of=("self",))
        .filter(competition=competition)
        .select_related("home_entry__player", "away_entry__player")
        .order_by("id")
    )
    money_fixtures = [fixture for fixture in fixtures if fixture.is_money_match_fixture]
    if len(money_fixtures) > 1:
        raise PoolResultValidationError(
            "This Money Match has more than one configured fixture. Resolve that invalid state before editing it."
        )
    if len(fixtures) != len(money_fixtures):
        raise PoolResultValidationError(
            "This Money Match has a fixture without the required Money Match marker. Resolve that invalid state before editing it."
        )
    return money_fixtures[0] if money_fixtures else None


def _money_match_has_started(pool_match):
    return bool(
        pool_match
        and (
            pool_match.status != PoolMatch.Status.UPCOMING
            or PoolRack.objects.filter(match=pool_match).exists()
        )
    )


def _ensure_money_match_fixture(competition, entries, *, scheduled_at=None):
    """Create the one fixture only after exactly two valid entries exist."""

    if len(entries) != 2:
        raise PoolResultValidationError(
            "Configure exactly two opponents before creating the Money Match fixture."
        )
    pool_match = _locked_money_match_fixture(competition)
    if pool_match is not None:
        return pool_match

    stage = _money_match_stage(competition)
    return PoolMatch.objects._create_locked_money_match_fixture(
        competition=competition,
        stage=stage,
        home_entry=entries[0],
        away_entry=entries[1],
        scheduled_at=scheduled_at,
        race_target=competition.default_race_target,
        venue=competition.venue,
    )


@transaction.atomic
def ensure_money_match_fixture(competition):
    """Recover a missing fixture for a valid, already-configured Money Match."""

    locked_competition = PoolCompetition.objects.select_for_update().get(pk=competition.pk)
    if not locked_competition.is_money_match:
        raise PoolResultValidationError("Only a Money Match can use this recovery service.")
    if locked_competition.participant_mode != PoolCompetition.ParticipantMode.SINGLES:
        raise PoolResultValidationError("Money Matches are available for singles players only.")
    entries = _locked_money_match_entries(locked_competition)
    return _ensure_money_match_fixture(locked_competition, entries)


@transaction.atomic
def configure_money_match(
    competition,
    *,
    opponent_one,
    opponent_two,
    race_target,
    scheduled_at=None,
    venue="",
    title="",
    scorer=None,
    visibility=None,
    assigned_by=None,
    money_match_prize_pot=_UNCHANGED,
):
    """Safely configure or recover the two entries and one fixture of a Money Match."""

    locked_competition = PoolCompetition.objects.select_for_update().get(pk=competition.pk)
    if not locked_competition.is_money_match:
        raise PoolResultValidationError("Only a Money Match can be configured here.")
    if locked_competition.participant_mode != PoolCompetition.ParticipantMode.SINGLES:
        raise PoolResultValidationError("Money Matches are available for singles players only.")

    display_one, display_two, entry_values = _validate_money_match_opponents(
        opponent_one, opponent_two
    )
    race_target = _validated_money_match_race_target(race_target)
    money_match_prize_pot = _validated_money_match_prize_pot(money_match_prize_pot)
    entries = _locked_money_match_entries(locked_competition)
    pool_match = _locked_money_match_fixture(locked_competition)
    if _money_match_has_started(pool_match):
        raise PoolResultValidationError(
            "Money Match opponents and details cannot change after play has started."
        )

    existing_participant_ids = {entry.player_id for entry in entries}
    desired_participant_ids = {opponent_one.pk, opponent_two.pk}
    entry_by_participant_id = {entry.player_id: entry for entry in entries}

    if existing_participant_ids != desired_participant_ids:
        if pool_match is not None:
            pool_match.delete()
            pool_match = None
        if entries:
            PoolCompetitionEntry.objects.filter(pk__in=[entry.pk for entry in entries]).delete()
        entries = [
            PoolCompetitionEntry(competition=locked_competition, **values)
            for values in entry_values
        ]
        for entry in entries:
            entry.save()
        entry_by_participant_id = {entry.player_id: entry for entry in entries}

    ordered_entries = [
        entry_by_participant_id[opponent_one.pk],
        entry_by_participant_id[opponent_two.pk],
    ]

    locked_competition.name = (title or "").strip() or f"{display_one} vs {display_two}"
    locked_competition.default_race_target = race_target
    locked_competition.start_date = scheduled_at.date() if scheduled_at else None
    locked_competition.venue = (venue or "").strip()
    if money_match_prize_pot is not _UNCHANGED:
        locked_competition.money_match_prize_pot = money_match_prize_pot
    if visibility is not None:
        locked_competition.visibility = visibility
    locked_competition.save()

    if pool_match is None:
        pool_match = _ensure_money_match_fixture(
            locked_competition,
            ordered_entries,
            scheduled_at=scheduled_at,
        )
    else:
        pool_match.home_entry = ordered_entries[0]
        pool_match.away_entry = ordered_entries[1]
        pool_match.race_target = race_target
        pool_match.scheduled_at = scheduled_at
        pool_match.venue = locked_competition.venue
        pool_match.save()

    assignments = PoolMatchScorerAssignment.objects.select_for_update().filter(
        match=pool_match
    )
    if scorer is None:
        assignments.update(is_active=False)
    else:
        assignments.exclude(user=scorer).update(is_active=False)
        PoolMatchScorerAssignment.objects.update_or_create(
            match=pool_match,
            user=scorer,
            defaults={"is_active": True, "assigned_by": assigned_by},
        )
    return locked_competition, pool_match


@transaction.atomic
def create_money_match(
    *,
    opponent_one,
    opponent_two,
    race_target,
    scheduled_at=None,
    venue="",
    title="",
    owner=None,
    scorer=None,
    visibility=PoolCompetition.Visibility.DRAFT,
    money_match_prize_pot=None,
):
    """Create the complete, one-fixture Money Match atomically.

    The model boundary repeats the important invariants.  This service gives
    the manager workflow one idempotency-safe transaction so a failed submit
    cannot leave a half-configured competition or an orphaned fixture.
    """

    display_one, display_two, _entry_values = _validate_money_match_opponents(
        opponent_one, opponent_two
    )
    race_target = _validated_money_match_race_target(race_target)
    money_match_prize_pot = _validated_money_match_prize_pot(money_match_prize_pot)
    name = (title or "").strip() or f"{display_one} vs {display_two}"
    competition = PoolCompetition(
        name=name,
        slug=_money_match_slug(name),
        participant_mode=PoolCompetition.ParticipantMode.SINGLES,
        format_type=PoolCompetition.FormatType.MONEY_MATCH,
        visibility=visibility,
        default_race_target=race_target,
        money_match_prize_pot=money_match_prize_pot,
        start_date=scheduled_at.date() if scheduled_at else None,
        venue=(venue or "").strip(),
        owner=owner,
    )
    competition.save()
    competition, pool_match = configure_money_match(
        competition,
        opponent_one=opponent_one,
        opponent_two=opponent_two,
        race_target=race_target,
        scheduled_at=scheduled_at,
        venue=venue,
        title=title,
        scorer=scorer,
        visibility=visibility,
        assigned_by=owner,
        money_match_prize_pot=money_match_prize_pot,
    )
    return competition, pool_match


def _match_pk(match: PoolMatch) -> int:
    if not isinstance(match, PoolMatch) or not match.pk:
        raise PoolResultValidationError("A saved pool match is required.")
    return match.pk


def _lock_match(match: PoolMatch) -> PoolMatch:
    """Return the current match row locked for one result operation."""
    # Bracket-aware lifecycle operations must take the bracket root first,
    # then the fixture row.  This is a local import to retain the pool
    # result/bracket service boundary without a module import cycle.
    try:
        from .brackets import lock_bracket_dependencies_for_match

        lock_bracket_dependencies_for_match(match)
    except PoolResultValidationError:
        raise
    except ValidationError as error:
        raise PoolResultValidationError(error.messages) from error
    try:
        return (
            # ``winner`` is nullable.  Lock only the fixture row so PostgreSQL
            # never attempts ``FOR UPDATE`` on the nullable side of its outer
            # join; rack rows are locked separately by the result operations.
            PoolMatch.objects.select_for_update(of=("self",))
            .select_related(
                "competition",
                "stage",
                "home_entry__player",
                "home_entry__team",
                "away_entry__player",
                "away_entry__team",
                "winner",
            )
            .get(pk=_match_pk(match))
        )
    except PoolMatch.DoesNotExist as exc:
        raise PoolResultValidationError(
            "This match was reset or removed before the scoring action could be applied."
        ) from exc


def _entry_id(entry) -> int | None:
    if isinstance(entry, PoolCompetitionEntry):
        return entry.pk
    return getattr(entry, "pk", entry)


def _match_entry_id(match: PoolMatch, entry, *, label: str) -> int:
    """Validate that an entry is one of the two locked match competitors."""
    entry_id = _entry_id(entry)
    if entry_id not in {match.home_entry_id, match.away_entry_id}:
        raise PoolResultValidationError(
            {label: "Select a competitor who is playing in this match."}
        )
    return entry_id


def _match_entry_life_id(match: PoolMatch, entry_id: int | None) -> int | None:
    """Return the side-specific tournament-life metadata for one entrant."""

    if entry_id == match.home_entry_id:
        return getattr(match, "home_entry_life_id", None)
    if entry_id == match.away_entry_id:
        return getattr(match, "away_entry_life_id", None)
    return None


def _player_id(player) -> int | None:
    if isinstance(player, PoolPlayer):
        return player.pk
    return getattr(player, "pk", player)


def _selected_rack_player_id(player, *, required: bool = True) -> int | None:
    """Return a saved player id or raise a form-safe domain validation error."""
    player_id = _player_id(player)
    if player_id is None or player_id == "":
        if required:
            raise PoolResultValidationError(
                {"winning_player": "Select the player who won this rack."}
            )
        return None
    try:
        selected_player = (
            PoolPlayer.objects.select_for_update()
            .only("pk")
            .filter(pk=player_id)
            .first()
        )
    except (TypeError, ValueError) as exc:
        raise PoolResultValidationError(
            {"winning_player": "Select a valid registered player."}
        ) from exc
    if selected_player is None:
        raise PoolResultValidationError(
            {"winning_player": "Select a valid registered player."}
        )
    return selected_player.pk


def _active_team_entry_for_rack_player(match: PoolMatch, player_id: int) -> int:
    """Resolve a team rack winner from its active competition-scoped roster.

    The lookup is deliberately limited to the two locked fixture entries.  A
    global player record, a roster in another competition, or an inactive
    roster membership is never enough to score a rack.  A player registered
    for both sides is ambiguous rather than silently awarded to an arbitrary
    team; managers must resolve that roster data before scoring.
    """
    entry_ids = [match.home_entry_id, match.away_entry_id]
    roster_entry_ids = list(
        PoolTeamRoster.objects.select_for_update()
        .filter(
            competition_entry_id__in=entry_ids,
            player_id=player_id,
            is_active=True,
        )
        .values_list("competition_entry_id", flat=True)
    )
    if not roster_entry_ids:
        raise PoolResultValidationError(
            {
                "winning_player": (
                    "Select a player registered on one of this match's active "
                    "competition rosters."
                )
            }
        )
    if len(roster_entry_ids) > 1:
        raise PoolResultValidationError(
            {
                "winning_player": (
                    "This player is active on both teams in this match. Resolve "
                    "the roster assignment before recording a rack."
                )
            }
        )
    return roster_entry_ids[0]


def _singles_entry_for_rack_player(match: PoolMatch, player_id: int) -> int:
    """Resolve a singles entry only when it represents the selected player."""
    entries = (
        (match.home_entry_id, match.home_entry.player_id),
        (match.away_entry_id, match.away_entry.player_id),
    )
    matching_entry_ids = [
        entry_id
        for entry_id, entry_player_id in entries
        if entry_player_id == player_id
    ]
    if len(matching_entry_ids) != 1:
        raise PoolResultValidationError(
            {
                "winning_player": (
                    "Select the player represented by one of this match's "
                    "singles entries."
                )
            }
        )
    return matching_entry_ids[0]


def _player_for_singles_entry(match: PoolMatch, entry_id: int) -> int:
    if entry_id == match.home_entry_id:
        player_id = match.home_entry.player_id
    else:
        player_id = match.away_entry.player_id
    if not player_id:
        # The competition-entry model prevents this, but retain a safe service
        # error rather than allowing a malformed imported row to lose audit
        # attribution silently.
        raise PoolResultValidationError(
            {"winner": "The selected singles entry has no player profile."}
        )
    return player_id


def _resolve_new_rack_attribution(
    match: PoolMatch,
    *,
    winner=None,
    winning_player=None,
) -> tuple[int, int]:
    """Return the authoritative entry/player pair for a newly recorded rack.

    Team scoring is player-first: the active roster membership determines the
    team entry, and a supplied entry is checked only as a consistency guard.
    Singles scoring remains entry-first and automatically records the player
    already represented by that entry, so existing singles callers do not
    need an extra player selection.
    """
    is_team_competition = (
        match.competition.participant_mode == PoolCompetition.ParticipantMode.TEAMS
    )
    if is_team_competition:
        winning_player_id = _selected_rack_player_id(winning_player)
        inferred_winner_id = _active_team_entry_for_rack_player(
            match,
            winning_player_id,
        )
        if winner is not None:
            selected_winner_id = _match_entry_id(match, winner, label="winner")
            if selected_winner_id != inferred_winner_id:
                raise PoolResultValidationError(
                    {
                        "winning_player": (
                            "The selected player is not registered for the team "
                            "chosen as rack winner."
                        )
                    }
                )
        return inferred_winner_id, winning_player_id

    if winner is not None:
        winner_id = _match_entry_id(match, winner, label="winner")
        winning_player_id = _player_for_singles_entry(match, winner_id)
        if winning_player is not None:
            supplied_player_id = _selected_rack_player_id(winning_player)
            if supplied_player_id != winning_player_id:
                raise PoolResultValidationError(
                    {
                        "winning_player": (
                            "The selected player does not match the singles entry "
                            "that won the rack."
                        )
                    }
                )
        return winner_id, winning_player_id

    winning_player_id = _selected_rack_player_id(winning_player)
    return (
        _singles_entry_for_rack_player(match, winning_player_id),
        winning_player_id,
    )


def _resolve_corrected_rack_attribution(
    match: PoolMatch,
    rack: PoolRack,
    *,
    winner=None,
    winning_player=_UNCHANGED,
) -> tuple[int, int | None]:
    """Resolve a correction without inventing attribution for legacy racks.

    A correction that only changes an outcome or breaker leaves a historical
    null player attribution untouched.  Team entry changes require a selected
    active roster player, while player-only corrections infer the winning team
    from that player's roster just like a new team rack.
    """
    if winner is None and winning_player is _UNCHANGED:
        return rack.winner_id, rack.winning_player_id

    if winning_player is not _UNCHANGED:
        if winning_player is None:
            if winner is None and rack.winning_player_id is None:
                # An old imported team rack can legitimately still have no
                # player attribution.  Keeping that legacy state while
                # correcting another field is not the same as clearing an
                # already-recorded player from a new rack.
                return rack.winner_id, None
            raise PoolResultValidationError(
                {
                    "winning_player": (
                        "Rack player attribution cannot be cleared. Leave this "
                        "unchanged for a historical unattributed rack."
                    )
                }
            )
        supplied_player_id = _selected_rack_player_id(winning_player)
        if supplied_player_id == rack.winning_player_id:
            supplied_winner_id = (
                _match_entry_id(match, winner, label="winner")
                if winner is not None
                else rack.winner_id
            )
            if supplied_winner_id == rack.winner_id:
                # A roster can be made inactive or removed after a historical
                # rack was recorded.  Preserve that existing attribution when
                # an outcome/breaker correction resubmits it unchanged; only
                # a *new* player/team selection must be active today.
                return rack.winner_id, rack.winning_player_id
        return _resolve_new_rack_attribution(
            match,
            winner=winner,
            winning_player=winning_player,
        )

    winner_id = _match_entry_id(match, winner, label="winner")
    if (
        match.competition.participant_mode
        == PoolCompetition.ParticipantMode.SINGLES
    ):
        return winner_id, _player_for_singles_entry(match, winner_id)
    if winner_id != rack.winner_id:
        raise PoolResultValidationError(
            {
                "winning_player": (
                    "Select an active registered player when changing a team "
                    "rack winner."
                )
            }
        )
    return winner_id, rack.winning_player_id


def _locked_rack(match: PoolMatch, rack) -> PoolRack:
    rack_id = getattr(rack, "pk", rack)
    try:
        return (
            PoolRack.objects.select_for_update()
            .select_related("match")
            .get(pk=rack_id, match=match)
        )
    except PoolRack.DoesNotExist as exc:
        raise PoolResultValidationError(
            "That rack does not belong to this match."
        ) from exc


def _serialise_rack(rack: PoolRack):
    return {
        "id": rack.pk,
        "rack_number": rack.rack_number,
        "winner_id": rack.winner_id,
        "winning_player_id": rack.winning_player_id,
        "outcome_type": rack.outcome_type,
        "breaker_id": rack.breaker_id,
        "recorded_by_id": rack.recorded_by_id,
        "recorded_at": rack.recorded_at.isoformat() if rack.recorded_at else None,
    }


def snapshot_match_result(match: PoolMatch):
    """Capture the complete result-facing state for an immutable audit entry."""
    racks = match.racks.order_by("rack_number", "pk").select_related("recorded_by")
    snapshot = {
        "status": match.status,
        "home_score": match.home_score,
        "away_score": match.away_score,
        "winner_id": match.winner_id,
        # Life IDs are nullable for ordinary/legacy fixtures, but recording
        # them alongside their real entry IDs makes a two-life knockout
        # correction fully reconstructable without ever treating a life as a
        # separate statistics participant.
        "home_entry_life_id": getattr(match, "home_entry_life_id", None),
        "away_entry_life_id": getattr(match, "away_entry_life_id", None),
        "winner_entry_life_id": getattr(match, "winner_entry_life_id", None),
        "result_method": match.result_method,
        "result_entry_method": getattr(match, "result_entry_method", ""),
        "completed_at": match.completed_at.isoformat() if match.completed_at else None,
        # Timer state is persisted on the match, not derived from a browser
        # clock.  Include it in every revision snapshot so a time-limit
        # completion and any later correction remain fully auditable.
        "applied_time_limit_minutes": getattr(
            match, "applied_time_limit_minutes", None
        ),
        "timer_snapshot_at": (
            match.timer_snapshot_at.isoformat()
            if getattr(match, "timer_snapshot_at", None)
            else None
        ),
        "timer_started_at": (
            match.timer_started_at.isoformat()
            if getattr(match, "timer_started_at", None)
            else None
        ),
        "timer_deadline": (
            match.timer_deadline.isoformat()
            if getattr(match, "timer_deadline", None)
            else None
        ),
        "time_expired_tied": bool(getattr(match, "time_expired_tied", False)),
        "completion_reason": getattr(match, "completion_reason", ""),
        "is_in_correction_mode": match.is_in_correction_mode,
        "racks": [_serialise_rack(rack) for rack in racks],
        "manual_rack_summaries": list(
            match.manual_rack_summaries.order_by("entry_id").values(
                "entry_id", "green_apples", "red_apples", "one_visits"
            )
        ) if match.pk else [],
    }
    # A team score sheet is not a second result source, but including its
    # lifecycle/snapshots in result revisions makes a later correction
    # auditable without fabricating 25 rack results.
    if (
        match.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
        and match.pk
    ):
        snapshot["team_scheduled_racks"] = list(
            PoolTeamMatchScheduledRack.objects.filter(match=match)
            .order_by("sequence_number", "pk")
            .values(
                "id",
                "sequence_number",
                "session_number",
                "home_position",
                "away_position",
                "status",
                "table_number",
                "home_player_id",
                "away_player_id",
                "result_id",
                "started_at",
                "completed_at",
            )
        )
        for row in snapshot["team_scheduled_racks"]:
            for key in ("started_at", "completed_at"):
                value = row[key]
                row[key] = value.isoformat() if value else None
    return snapshot


def _record_result_revision(
    match: PoolMatch,
    *,
    action: str,
    revised_by=None,
    reason: str = "",
    previous_result=None,
):
    """Write an immutable lifecycle audit record inside the caller's transaction."""
    return PoolMatchResultRevision.objects.create(
        match=match,
        revised_by=revised_by,
        action=action,
        reason=reason,
        previous_result=deepcopy(
            previous_result if previous_result is not None else snapshot_match_result(match)
        ),
        revised_result=snapshot_match_result(match),
    )


def _bracket_preflight(match, **kwargs):
    """Keep topology safety inside the existing transactional result boundary."""
    try:
        from .brackets import _preflight_result_change_for_locked_match

        return _preflight_result_change_for_locked_match(match, **kwargs)
    except PoolResultValidationError:
        raise
    except ValidationError as error:
        raise PoolResultValidationError(error.messages) from error


def _refresh_match_derived_state(match, *, actor=None, reason=""):
    """Synchronously refresh bracket/qualification projections after a result.

    Standings themselves are query-derived, so there is no editable table to
    rebuild.  The bracket graph is persistent and is deliberately refreshed
    inside this transaction: no Celery, Redis, or eventual consistency.
    """
    if match.competition.is_money_match:
        _synchronise_money_match_competition_state(match)
        return None

    # Every caller reaches this private helper from a result operation that
    # already owns the bracket dependency root and the current parent match
    # row through ``_lock_match``.  Re-taking those identical locks and
    # re-reading the fixture made a single live knockout completion pay the
    # full bracket-lock setup twice.  Keep the public bracket service's safe
    # default for direct callers; use its deliberately private fast path only
    # inside this established locked transaction boundary.
    try:
        from .brackets import _refresh_bracket_progression_for_locked_match

        return _refresh_bracket_progression_for_locked_match(
            match,
            actor=actor,
            reason=reason,
        )
    except PoolResultValidationError:
        raise
    except ValidationError as error:
        raise PoolResultValidationError(error.messages) from error


def _synchronise_money_match_competition_state(match):
    """Keep a Money Match's competition lifecycle aligned to its one fixture."""

    if not match.competition.is_money_match:
        return
    if match.status == PoolMatch.Status.FINISHED:
        state = PoolCompetition.OperationalState.COMPLETED
    elif (
        match.status in {PoolMatch.Status.LIVE, PoolMatch.Status.ABANDONED}
        or match.racks.exists()
    ):
        state = PoolCompetition.OperationalState.ACTIVE
    else:
        state = PoolCompetition.OperationalState.UPCOMING
    PoolCompetition.objects.filter(pk=match.competition_id).exclude(
        operational_state=state
    ).update(operational_state=state, updated_at=timezone.now())


def _mark_competition_active_for_played_match(match):
    """Move an event into Active once a real played fixture has begun.

    Visibility deliberately remains independent, and an explicitly completed
    competition is never reopened by a late correction or fixture write.  The
    conditional update avoids unnecessary writes after the first transition.
    """
    if (
        match.result_method == PoolMatch.ResultMethod.PLAYED
        and match.status in {PoolMatch.Status.LIVE, PoolMatch.Status.FINISHED}
    ):
        PoolCompetition.objects.filter(
            pk=match.competition_id,
            operational_state=PoolCompetition.OperationalState.UPCOMING,
        ).update(
            operational_state=PoolCompetition.OperationalState.ACTIVE,
            updated_at=timezone.now(),
        )


def _authoritative_scores(match: PoolMatch) -> tuple[int, int]:
    """Count the two competitors' rack wins and reject corrupt historic data."""
    home_score, away_score = match.rack_score_totals()
    total_racks = match.racks.count()
    if home_score + away_score != total_racks:
        raise PoolResultValidationError(
            "Rack history includes a winner who is not a match competitor."
        )
    return home_score, away_score


def _ensure_target_not_reached(match: PoolMatch, home_score: int, away_score: int):
    target = match.race_target
    if target is None:
        raise PoolResultValidationError("The match must have a positive race target.")
    if home_score > target or away_score > target:
        raise PoolResultValidationError(
            "Rack history cannot contain more wins than the race target."
        )
    if home_score == target and away_score == target:
        raise PoolResultValidationError(
            "Both competitors cannot reach the race target in the same match."
        )


def _mark_unneeded_team_racks(match: PoolMatch, *, stop_live_rows: bool = False):
    """Bring a terminal team score sheet into a truthful terminal state.

    Unstarted rows become ``NOT_REQUIRED``.  A terminal result can also leave
    another physical table genuinely live; in that case its immutable
    player/table snapshots are retained but it becomes ``STOPPED`` rather
    than being left incorrectly live or being given a fabricated rack result.
    """

    if match.competition.participant_mode == PoolCompetition.ParticipantMode.TEAMS:
        PoolTeamMatchScheduledRack.objects.filter(
            match=match,
            status=PoolTeamMatchScheduledRack.Status.PENDING,
        ).update(status=PoolTeamMatchScheduledRack.Status.NOT_REQUIRED)
        if stop_live_rows:
            PoolTeamMatchScheduledRack.objects.filter(
                match=match,
                status=PoolTeamMatchScheduledRack.Status.LIVE,
            ).update(status=PoolTeamMatchScheduledRack.Status.STOPPED)


def _team_tied_expiry_has_no_live_scheduled_rack(match: PoolMatch) -> bool:
    """Whether a team score sheet has no lawful deciding rack after expiry.

    Team score sheets persist rack-start state, so after a tied deadline only
    an already-live row may decide the fixture.  This predicate deliberately
    excludes legacy team fixtures with no score sheet; their generic scorer
    path has no persisted rack-start state and follows the normal deciding
    rack rule.
    """

    if (
        match.competition.participant_mode
        != PoolCompetition.ParticipantMode.TEAMS
    ):
        return False
    scheduled_rows = PoolTeamMatchScheduledRack.objects.filter(match=match)
    return scheduled_rows.exists() and not scheduled_rows.filter(
        status=PoolTeamMatchScheduledRack.Status.LIVE,
    ).exists()


def _restore_stopped_team_deciding_racks(match: PoolMatch):
    """Re-open only tables that were genuinely live at a timed finish.

    A manager correction can revise a time-limit leader back to a level score.
    The original deadline remains authoritative, so this is a deciding-rack
    state rather than a fresh match.  ``STOPPED`` rows are exactly the table
    snapshots that were live when that parent result was finalised; restoring
    them is safe, while the unstarted ``NOT_REQUIRED`` schedule must stay
    closed.
    """

    if (
        match.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
        and match.status == PoolMatch.Status.LIVE
        and getattr(match, "time_expired_tied", False)
    ):
        PoolTeamMatchScheduledRack.objects.filter(
            match=match,
            status=PoolTeamMatchScheduledRack.Status.STOPPED,
        ).update(status=PoolTeamMatchScheduledRack.Status.LIVE)


def _finish_time_limited_locked_match(
    match: PoolMatch,
    *,
    home_score: int,
    away_score: int,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Finish a locked live match at its authentic time-limit score.

    The caller has already checked the persisted deadline.  No artificial
    racks or race-target score are created; bracket and standings consumers
    therefore receive the same real score that was played.
    """

    if home_score == away_score:
        raise PoolResultValidationError(
            "A tied fixture needs a deciding rack before it can finish by time limit."
        )
    winner_id = (
        match.home_entry_id if home_score > away_score else match.away_entry_id
    )
    _bracket_preflight(
        match,
        prospective_winner=winner_id,
        prospective_eligible=True,
        actor=recorded_by,
    )
    previous_result = snapshot_match_result(match)
    match.home_score = home_score
    match.away_score = away_score
    match.status = PoolMatch.Status.FINISHED
    match.winner_id = winner_id
    match.winner_entry_life_id = _match_entry_life_id(match, winner_id)
    match.completed_at = timezone.now()
    match.is_in_correction_mode = False
    match.time_expired_tied = False
    match.completion_reason = _completion_reason_value("TIME_LIMIT")
    match.save()
    _mark_competition_active_for_played_match(match)
    _mark_unneeded_team_racks(match, stop_live_rows=True)
    _record_result_revision(
        match,
        action=PoolMatchResultRevision.Action.MATCH_COMPLETED,
        revised_by=recorded_by,
        reason=reason or "Match time limit expired.",
        previous_result=previous_result,
    )
    _refresh_match_derived_state(match, actor=recorded_by, reason=reason)
    return match


def _expire_locked_match_if_due(
    match: PoolMatch,
    *,
    recorded_by=None,
    reason: str = "",
) -> bool:
    """Apply deadline state while the parent ``PoolMatch`` row is locked.

    Returns ``True`` only when the persisted deadline was due and this helper
    applied/confirmed the expiry state.  A tied fixture deliberately remains
    LIVE with ``time_expired_tied=True`` so one already-live team rack (or the
    next singles rack) can decide it.
    """

    if not _match_timer_is_due(match):
        return False

    home_score, away_score = _authoritative_scores(match)
    _ensure_target_not_reached(match, home_score, away_score)

    # A legitimately reached race target always wins before time-limit logic.
    # This is mainly a defensive recovery path because normal rack recording
    # synchronises immediately after every result.
    if (
        home_score == match.race_target
        or away_score == match.race_target
    ):
        _synchronise_locked_match_score(match)
        return True

    if home_score != away_score:
        _finish_time_limited_locked_match(
            match,
            home_score=home_score,
            away_score=away_score,
            recorded_by=recorded_by,
            reason=reason,
        )
        return True

    if not getattr(match, "time_expired_tied", False):
        # Keep the deadline as the immutable server-side evidence that this
        # is the deciding-rack state.  It must not be recalculated from the
        # scheduled time or a later competition setting.
        match.time_expired_tied = True
        match.completion_reason = ""
        match.save(update_fields=[
            "time_expired_tied",
            "completion_reason",
            "updated_at",
        ])
    return True


@transaction.atomic
def expire_match_if_due(
    match: PoolMatch,
    *,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Public lazy-expiry boundary for authenticated scorer polling/actions.

    No background worker is needed for correctness: every mutating service
    uses the same locked helper, and callers may invoke this endpoint/service
    to make an untouched expired fixture reflect its authoritative state.
    """

    # Polling a match that is still before its deadline, or one that another
    # scorer has already completed, has no state to mutate.  Re-read only the
    # current fixture first: callers can legitimately hold an older instance
    # after a rack operation, so trusting its cached LIVE/UPCOMING state could
    # incorrectly skip a real expiry.  This cheap non-locking read lets normal
    # polls and completed retries return without contending for the bracket
    # root; a due live match still takes the authoritative bracket -> match
    # lock path below and is re-checked after that lock.
    try:
        current_match = PoolMatch.objects.get(pk=_match_pk(match))
    except PoolMatch.DoesNotExist as exc:
        raise PoolResultValidationError(
            "This match was reset or removed before the timer check could be applied."
        ) from exc
    if not _match_timer_is_due(current_match):
        return current_match

    locked_match = _lock_match(current_match)
    _expire_locked_match_if_due(
        locked_match,
        recorded_by=recorded_by,
        reason=reason,
    )
    return locked_match


def _synchronise_locked_match_score(
    match: PoolMatch,
    *,
    close_correction_when_finished: bool = True,
) -> PoolMatch:
    """Synchronise score/status/winner from the locked rack history.

    This private helper is called only while the parent match row is locked.
    A played match never stores a score independently of its rack records.
    """
    home_score, away_score = _authoritative_scores(match)
    match.home_score = home_score
    match.away_score = away_score

    if match.result_method != PoolMatch.ResultMethod.PLAYED:
        match.save()
        return match

    _ensure_target_not_reached(match, home_score, away_score)
    target = match.race_target
    race_target_reached = home_score == target or away_score == target
    timer_due = _match_timer_is_due(match)
    deciding_rack_state = bool(getattr(match, "time_expired_tied", False))
    time_limit_completion_reopened = bool(
        match.status == PoolMatch.Status.FINISHED
        and match.completion_reason == _completion_reason_value("TIME_LIMIT")
        and match.has_match_timer
    )
    timer_expired_finished_result_reopening = bool(
        match.status == PoolMatch.Status.FINISHED
        and not race_target_reached
        and match.has_match_timer
        and _match_timer_has_elapsed(match)
    )

    if (
        race_target_reached
        and not deciding_rack_state
        and not time_limit_completion_reopened
    ):
        # A target reached before an expiry-created deciding-rack state is
        # terminal even if the deadline happens to pass by the next server
        # interaction.  Once expiry has persisted a deciding rack, that rack
        # finishes by TIME_LIMIT even when it also reaches the race target.
        match.status = PoolMatch.Status.FINISHED
        match.winner_id = (
            match.home_entry_id if home_score == target else match.away_entry_id
        )
        match.winner_entry_life_id = _match_entry_life_id(
            match,
            match.winner_id,
        )
        match.completed_at = match.completed_at or timezone.now()
        match.time_expired_tied = False
        match.completion_reason = _completion_reason_value("RACE_TARGET")
        if close_correction_when_finished:
            match.is_in_correction_mode = False
    elif (
        deciding_rack_state
        or timer_due
        or time_limit_completion_reopened
        or timer_expired_finished_result_reopening
    ):
        # At expiry a leader wins at the actual score.  If level, retain a
        # persisted LIVE deciding-rack state; the next valid rack will return
        # here with a leader and immediately finish by the time limit.
        if home_score == away_score:
            match.status = PoolMatch.Status.LIVE
            match.winner = None
            match.winner_entry_life = None
            match.completed_at = None
            match.is_in_correction_mode = False
            match.time_expired_tied = True
            match.completion_reason = ""
        else:
            match.status = PoolMatch.Status.FINISHED
            match.winner_id = (
                match.home_entry_id
                if home_score > away_score
                else match.away_entry_id
            )
            match.winner_entry_life_id = _match_entry_life_id(
                match,
                match.winner_id,
            )
            match.completed_at = match.completed_at or timezone.now()
            match.time_expired_tied = False
            match.completion_reason = _completion_reason_value("TIME_LIMIT")
            if close_correction_when_finished:
                match.is_in_correction_mode = False
    else:
        match.status = PoolMatch.Status.LIVE
        match.winner = None
        match.winner_entry_life = None
        match.completed_at = None
        match.is_in_correction_mode = False
        match.time_expired_tied = False
        match.completion_reason = ""

    match.save()
    _mark_competition_active_for_played_match(match)
    if match.status == PoolMatch.Status.FINISHED:
        # Only the explicit persistent score-sheet rows change state; no fake
        # pool rack result is written for a rack that became unnecessary.  A
        # physical table may still have been live when a different table
        # clinched the race; preserve that row's snapshots as STOPPED so it
        # cannot add a stale result after the parent has finished.
        _mark_unneeded_team_racks(
            match,
            stop_live_rows=True,
        )
    else:
        _restore_stopped_team_deciding_racks(match)
    return match


@transaction.atomic
def synchronise_match_score(
    match: PoolMatch,
    *,
    close_correction_when_finished: bool = True,
) -> PoolMatch:
    """Public, transaction-safe score synchronisation boundary for later engines."""
    locked_match = _lock_match(match)
    updated_match = _synchronise_locked_match_score(
        locked_match,
        close_correction_when_finished=close_correction_when_finished,
    )
    _refresh_match_derived_state(updated_match)
    return updated_match


def _ensure_scoring_is_open(match: PoolMatch):
    if match.result_method != PoolMatch.ResultMethod.PLAYED:
        raise PoolResultValidationError(
            "Racks cannot be recorded for a non-played result."
        )
    if match.status != PoolMatch.Status.LIVE:
        raise PoolResultValidationError("Start the match before recording a rack.")
    if match.is_in_correction_mode:
        raise PoolResultValidationError(
            "Finish the active correction before recording another rack."
        )
    home_score, away_score = _authoritative_scores(match)
    _ensure_target_not_reached(match, home_score, away_score)
    if home_score == match.race_target or away_score == match.race_target:
        raise PoolResultValidationError(
            "This match has already reached its race target and must be completed."
        )


@transaction.atomic
def start_match(match: PoolMatch, *, recorded_by=None, reason: str = "") -> PoolMatch:
    """Move an upcoming or postponed played match into live scoring."""
    locked_match = _lock_match(match)
    if locked_match.status not in {
        PoolMatch.Status.UPCOMING,
        PoolMatch.Status.POSTPONED,
    }:
        raise PoolResultValidationError("Only an upcoming or postponed match can start.")
    if locked_match.result_method != PoolMatch.ResultMethod.PLAYED:
        raise PoolResultValidationError(
            "A non-played result cannot be started for rack scoring."
        )

    home_score, away_score = _authoritative_scores(locked_match)
    _ensure_target_not_reached(locked_match, home_score, away_score)
    if (
        home_score == locked_match.race_target
        or away_score == locked_match.race_target
    ):
        raise PoolResultValidationError(
            "A match that reached its race target cannot be started again."
        )

    if (
        locked_match.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
    ):
        # The team score sheet is generated only after both entry-scoped,
        # active five-player lineups have been revalidated under this parent
        # fixture lock.  Scheduled time is deliberately not part of that rule.
        from .team_operations import prepare_team_match_start

        prepare_team_match_start(
            locked_match,
            actor=recorded_by,
            reason=reason,
        )

    previous_result = snapshot_match_result(locked_match)
    # Snapshot the timer policy exactly once, at the real first-start
    # transition. ``timer_snapshot_at`` is deliberately written even when
    # there is no duration, so adding a competition timer later cannot attach
    # one to an already-started, postponed fixture.
    if getattr(locked_match, "timer_snapshot_at", None) is None:
        timer_snapshot_at = timezone.now()
        locked_match.timer_snapshot_at = timer_snapshot_at
        time_limit_minutes = _configured_match_time_limit_minutes(locked_match)
        if time_limit_minutes:
            timer_started_at = timer_snapshot_at
            locked_match.applied_time_limit_minutes = time_limit_minutes
            locked_match.timer_started_at = timer_started_at
            locked_match.timer_deadline = timer_started_at + timedelta(
                minutes=time_limit_minutes
            )
        else:
            locked_match.applied_time_limit_minutes = None
            locked_match.timer_started_at = None
            locked_match.timer_deadline = None
        locked_match.time_expired_tied = False
    locked_match.status = PoolMatch.Status.LIVE
    locked_match.winner = None
    locked_match.winner_entry_life = None
    locked_match.completed_at = None
    locked_match.is_in_correction_mode = False
    locked_match.completion_reason = ""
    _synchronise_locked_match_score(locked_match)
    _record_result_revision(
        locked_match,
        action=PoolMatchResultRevision.Action.MATCH_STARTED,
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    if locked_match.status == PoolMatch.Status.FINISHED:
        _refresh_match_derived_state(
            locked_match,
            actor=recorded_by,
            reason=reason or "Match reached its persisted timer deadline on restart.",
        )
    return locked_match


def record_rack(
    match: PoolMatch,
    *,
    winner=None,
    winning_player=None,
    outcome_type=PoolRack.OutcomeType.NORMAL,
    breaker=None,
    recorded_by=None,
    reason: str = "",
    request_token=None,
) -> PoolRack:
    """Record the next rack and atomically update the authoritative score.

    Team callers select ``winning_player`` only; the active competition roster
    determines the matching team entry.  Singles callers may continue to pass
    their winning entry as before, and the represented player is attributed
    automatically.
    """
    result = _record_rack_atomic(
        match,
        winner=winner,
        winning_player=winning_player,
        outcome_type=outcome_type,
        breaker=breaker,
        recorded_by=recorded_by,
        reason=reason,
        request_token=request_token,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        # The inner atomic operation has committed the authoritative expiry
        # state. Raise only after it exits, otherwise Django would roll the
        # automatic finalisation back with this normal validation response.
        refreshed_match = PoolMatch.objects.get(pk=_match_pk(match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _record_rack_atomic(
    match: PoolMatch,
    *,
    winner=None,
    winning_player=None,
    outcome_type=PoolRack.OutcomeType.NORMAL,
    breaker=None,
    recorded_by=None,
    reason: str = "",
    request_token=None,
) -> PoolRack | object:
    """Locked implementation for :func:`record_rack`."""
    locked_match = _lock_match(match)
    if request_token not in (None, ""):
        try:
            request_token = (
                request_token
                if isinstance(request_token, UUID)
                else UUID(str(request_token))
            )
        except (TypeError, ValueError, AttributeError) as error:
            raise PoolResultValidationError(
                {"request_token": "The scorer action identifier is invalid."}
            ) from error
        # Check this before timer/status validation. A browser retry after a
        # winning rack must receive its original confirmed rack, not a false
        # "match is finished" error or a second rack.
        existing = PoolRack.objects.filter(
            match=locked_match,
            request_token=request_token,
        ).first()
        if existing is not None:
            return existing
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=recorded_by,
        reason=reason,
    )
    if (
        locked_match.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
        and PoolTeamMatchScheduledRack.objects.filter(match=locked_match).exists()
    ):
        if timer_processed:
            return _TIMER_EXPIRY_BLOCKED
        raise PoolResultValidationError(
            "Use the team match operations score sheet to complete a live player-versus-player rack."
        )
    if timer_processed and locked_match.status != PoolMatch.Status.LIVE:
        return _TIMER_EXPIRY_BLOCKED
    _ensure_scoring_is_open(locked_match)
    winner_id, winning_player_id = _resolve_new_rack_attribution(
        locked_match,
        winner=winner,
        winning_player=winning_player,
    )
    breaker_id = (
        _match_entry_id(locked_match, breaker, label="breaker")
        if breaker is not None
        else None
    )
    if outcome_type not in PoolRack.OutcomeType.values:
        raise PoolResultValidationError({"outcome_type": "Select a valid rack outcome."})

    previous_result = snapshot_match_result(locked_match)
    previous_number = (
        locked_match.racks.select_for_update()
        .order_by("-rack_number")
        .values_list("rack_number", flat=True)
        .first()
    )
    try:
        # Keep a savepoint around the database backstop.  The match-row lock
        # serialises normal manager writes, while the unique constraint still
        # protects an unexpected concurrent or imported write.
        with transaction.atomic():
            rack = PoolRack(
                match=locked_match,
                rack_number=(previous_number or 0) + 1,
                winner_id=winner_id,
                winning_player_id=winning_player_id,
                outcome_type=outcome_type,
                breaker_id=breaker_id,
                recorded_by=recorded_by,
                request_token=request_token,
            )
            rack.save(_from_result_service=True)
    except IntegrityError as exc:
        if request_token is not None:
            existing = PoolRack.objects.filter(
                match=locked_match,
                request_token=request_token,
            ).first()
            if existing is not None:
                return existing
        raise PoolResultValidationError(
            "Another rack was recorded at the same time. Please try again."
        ) from exc
    _synchronise_locked_match_score(locked_match)
    _record_result_revision(
        locked_match,
        action=PoolMatchResultRevision.Action.RACK_RECORDED,
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    _refresh_match_derived_state(locked_match, actor=recorded_by, reason=reason)
    return rack


def _ensure_rack_can_be_corrected(match: PoolMatch):
    if match.result_method != PoolMatch.ResultMethod.PLAYED:
        raise PoolResultValidationError(
            "Racks cannot be changed for a non-played result."
        )
    if match.status == PoolMatch.Status.FINISHED:
        if not match.is_in_correction_mode:
            raise PoolResultValidationError(
                "Open result correction before changing a completed match."
            )
        return
    if match.status != PoolMatch.Status.LIVE:
        raise PoolResultValidationError("Only a live match can have racks corrected.")


def correct_rack(
    match: PoolMatch,
    rack,
    *,
    winner=None,
    winning_player=_UNCHANGED,
    outcome_type=None,
    breaker=_UNCHANGED,
    recorded_by=None,
    reason: str = "",
) -> PoolRack:
    """Correct one existing rack and resynchronise the match in one transaction."""
    result = _correct_rack_atomic(
        match,
        rack,
        winner=winner,
        winning_player=winning_player,
        outcome_type=outcome_type,
        breaker=breaker,
        recorded_by=recorded_by,
        reason=reason,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=_match_pk(match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _correct_rack_atomic(
    match: PoolMatch,
    rack,
    *,
    winner=None,
    winning_player=_UNCHANGED,
    outcome_type=None,
    breaker=_UNCHANGED,
    recorded_by=None,
    reason: str = "",
) -> PoolRack | object:
    """Locked implementation for :func:`correct_rack`."""
    locked_match = _lock_match(match)
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=recorded_by,
        reason=reason,
    )
    if timer_processed and (
        locked_match.status != PoolMatch.Status.LIVE
        or getattr(locked_match, "time_expired_tied", False)
    ):
        return _TIMER_EXPIRY_BLOCKED
    _ensure_rack_can_be_corrected(locked_match)
    locked_rack = _locked_rack(locked_match, rack)
    if PoolTeamMatchScheduledRack.objects.filter(
        match=locked_match,
        result=locked_rack,
    ).exists():
        raise PoolResultValidationError(
            "Correct this scheduled team rack through team match operations so its two player snapshots remain authoritative."
        )
    previous_result = snapshot_match_result(locked_match)
    previous_winner_id = locked_rack.winner_id

    resolved_winner_id, resolved_winning_player_id = _resolve_corrected_rack_attribution(
        locked_match,
        locked_rack,
        winner=winner,
        winning_player=winning_player,
    )
    if winner is not None or winning_player is not _UNCHANGED:
        locked_rack.winner_id = resolved_winner_id
        locked_rack.winning_player_id = resolved_winning_player_id
    if outcome_type is not None:
        if outcome_type not in PoolRack.OutcomeType.values:
            raise PoolResultValidationError(
                {"outcome_type": "Select a valid rack outcome."}
            )
        locked_rack.outcome_type = outcome_type
    if breaker is not _UNCHANGED:
        locked_rack.breaker_id = (
            _match_entry_id(locked_match, breaker, label="breaker")
            if breaker is not None
            else None
        )

    if locked_match.status == PoolMatch.Status.FINISHED:
        home_score, away_score = _authoritative_scores(locked_match)
        if previous_winner_id == locked_match.home_entry_id:
            home_score -= 1
        else:
            away_score -= 1
        prospective_winner_id = resolved_winner_id
        if prospective_winner_id == locked_match.home_entry_id:
            home_score += 1
        else:
            away_score += 1
        target = locked_match.race_target
        prospective_winner = (
            locked_match.home_entry_id
            if home_score == target
            else locked_match.away_entry_id
            if away_score == target
            else None
        )
        _bracket_preflight(
            locked_match,
            prospective_winner=prospective_winner,
            prospective_eligible=bool(prospective_winner),
            actor=recorded_by,
        )

    locked_rack.save(_from_result_service=True)
    _synchronise_locked_match_score(locked_match)
    _record_result_revision(
        locked_match,
        action=PoolMatchResultRevision.Action.RACK_CORRECTED,
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    _refresh_match_derived_state(locked_match, actor=recorded_by, reason=reason)
    return locked_rack


def undo_rack(
    match: PoolMatch,
    rack=None,
    *,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Undo the latest rack only, preserving a contiguous rack sequence."""
    result = _undo_rack_atomic(
        match,
        rack,
        recorded_by=recorded_by,
        reason=reason,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=_match_pk(match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _undo_rack_atomic(
    match: PoolMatch,
    rack=None,
    *,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch | object:
    """Locked implementation for :func:`undo_rack`."""
    locked_match = _lock_match(match)
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=recorded_by,
        reason=reason,
    )
    if timer_processed and (
        locked_match.status != PoolMatch.Status.LIVE
        or getattr(locked_match, "time_expired_tied", False)
    ):
        return _TIMER_EXPIRY_BLOCKED
    _ensure_rack_can_be_corrected(locked_match)
    latest_rack = (
        locked_match.racks.select_for_update().order_by("-rack_number", "-pk").first()
    )
    if latest_rack is None:
        raise PoolResultValidationError("There is no recorded rack to undo.")
    if PoolTeamMatchScheduledRack.objects.filter(
        match=locked_match,
        result=latest_rack,
    ).exists():
        raise PoolResultValidationError(
            "Scheduled team rack history cannot be undone through the generic rack path. Use the authorised team match correction workflow."
        )
    if rack is not None:
        locked_rack = _locked_rack(locked_match, rack)
        if locked_rack.pk != latest_rack.pk:
            raise PoolResultValidationError(
                "Only the latest rack can be undone; correct an earlier rack instead."
            )
        latest_rack = locked_rack

    previous_result = snapshot_match_result(locked_match)
    if locked_match.status == PoolMatch.Status.FINISHED:
        _bracket_preflight(
            locked_match,
            prospective_eligible=False,
            actor=recorded_by,
        )
    latest_rack.delete(_from_result_service=True)
    _synchronise_locked_match_score(locked_match)
    _record_result_revision(
        locked_match,
        action=PoolMatchResultRevision.Action.RACK_UNDONE,
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    _refresh_match_derived_state(locked_match, actor=recorded_by, reason=reason)
    return locked_match


@transaction.atomic
def open_result_correction(
    match: PoolMatch,
    *,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Unlock a completed result for one explicit authorised correction."""
    locked_match = _lock_match(match)
    if locked_match.status != PoolMatch.Status.FINISHED:
        raise PoolResultValidationError(
            "Only a completed match can be opened for result correction."
        )
    if locked_match.is_in_correction_mode:
        raise PoolResultValidationError("This result is already open for correction.")

    _bracket_preflight(
        locked_match,
        prospective_eligible=False,
        actor=recorded_by,
    )

    previous_result = snapshot_match_result(locked_match)
    locked_match.is_in_correction_mode = True
    locked_match.save()
    _record_result_revision(
        locked_match,
        action=PoolMatchResultRevision.Action.CORRECTION_OPENED,
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    _refresh_match_derived_state(locked_match, actor=recorded_by, reason=reason)
    return locked_match


def complete_played_match(
    match: PoolMatch,
    *,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Finish a played match only when its rack history reaches the target."""
    result = _complete_played_match_atomic(
        match,
        recorded_by=recorded_by,
        reason=reason,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=_match_pk(match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _complete_played_match_atomic(
    match: PoolMatch,
    *,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch | object:
    """Locked implementation for :func:`complete_played_match`."""
    locked_match = _lock_match(match)
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=recorded_by,
        reason=reason,
    )
    if timer_processed and getattr(locked_match, "time_expired_tied", False):
        return _TIMER_EXPIRY_BLOCKED
    if locked_match.result_method != PoolMatch.ResultMethod.PLAYED:
        raise PoolResultValidationError(
            "Use the non-played result workflow for this match."
        )
    if (
        locked_match.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
        and PoolTeamMatchScheduledRack.objects.filter(
            match=locked_match,
            status=PoolTeamMatchScheduledRack.Status.LIVE,
        ).exists()
    ):
        raise PoolResultValidationError(
            "Complete the live team tables before completing the parent fixture."
        )
    if locked_match.status == PoolMatch.Status.FINISHED and not locked_match.is_in_correction_mode:
        return locked_match
    if locked_match.status not in {PoolMatch.Status.LIVE, PoolMatch.Status.FINISHED}:
        raise PoolResultValidationError("Only a live match can be completed.")

    home_score, away_score = _authoritative_scores(locked_match)
    _ensure_target_not_reached(locked_match, home_score, away_score)
    if (
        home_score != locked_match.race_target
        and away_score != locked_match.race_target
    ):
        raise PoolResultValidationError(
            "A played match can finish only once a competitor reaches the race target."
        )

    prospective_winner = (
        locked_match.home_entry_id
        if home_score == locked_match.race_target
        else locked_match.away_entry_id
    )
    _bracket_preflight(
        locked_match,
        prospective_winner=prospective_winner,
        prospective_eligible=True,
        actor=recorded_by,
    )

    previous_result = snapshot_match_result(locked_match)
    _synchronise_locked_match_score(locked_match)
    _record_result_revision(
        locked_match,
        action=PoolMatchResultRevision.Action.MATCH_COMPLETED,
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    _refresh_match_derived_state(locked_match, actor=recorded_by, reason=reason)
    return locked_match


def record_manual_final_result(
    match: PoolMatch,
    *,
    home_score: int,
    away_score: int,
    home_outcomes=None,
    away_outcomes=None,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Persist one verified offline final score without creating PoolRack rows."""

    return _record_manual_final_result_atomic(
        match,
        home_score=home_score,
        away_score=away_score,
        home_outcomes=home_outcomes,
        away_outcomes=away_outcomes,
        recorded_by=recorded_by,
        reason=reason,
    )


def _validate_manual_final_score(match, home_score, away_score):
    if isinstance(home_score, bool) or isinstance(away_score, bool):
        raise PoolResultValidationError("Enter whole-number final scores.")
    try:
        home_score, away_score = int(home_score), int(away_score)
    except (TypeError, ValueError) as exc:
        raise PoolResultValidationError("Enter whole-number final scores.") from exc
    if home_score < 0 or away_score < 0:
        raise PoolResultValidationError("Final scores cannot be negative.")
    _ensure_target_not_reached(match, home_score, away_score)
    target = match.race_target
    if home_score == away_score:
        raise PoolResultValidationError("A final Pool result cannot be tied.")
    if max(home_score, away_score) != target:
        raise PoolResultValidationError(
            f"The winner must reach this fixture's race target of {target}."
        )
    if min(home_score, away_score) >= target:
        raise PoolResultValidationError(
            "The losing score must remain below the race target."
        )
    return home_score, away_score


def _normalise_manual_outcomes(values, *, score, side):
    values = values or {}
    normalised = {
        key: int(values.get(key) or 0)
        for key in ("green_apples", "red_apples", "one_visits")
    }
    if any(value < 0 for value in normalised.values()):
        raise PoolResultValidationError("Manual outcome totals cannot be negative.")
    if sum(normalised.values()) > score:
        raise PoolResultValidationError(
            f"{side} Green Apples, Red Apples, and One Visits cannot exceed the final score."
        )
    return normalised


@transaction.atomic
def _record_manual_final_result_atomic(
    match: PoolMatch,
    *,
    home_score: int,
    away_score: int,
    home_outcomes=None,
    away_outcomes=None,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Locked implementation for :func:`record_manual_final_result`."""

    locked_match = _lock_match(match)
    has_rack_history = locked_match.racks.select_for_update().exists()
    has_live_team_table = PoolTeamMatchScheduledRack.objects.filter(
        match=locked_match,
        status=PoolTeamMatchScheduledRack.Status.LIVE,
    ).exists()
    if has_rack_history or has_live_team_table or locked_match.status == PoolMatch.Status.LIVE:
        raise PoolResultValidationError(
            "This fixture has live or detailed rack history. Use the existing rack correction workflow instead of replacing it with an aggregate result."
        )
    if locked_match.status == PoolMatch.Status.CANCELLED:
        raise PoolResultValidationError("A cancelled match cannot receive a final result.")
    if locked_match.status == PoolMatch.Status.ABANDONED:
        raise PoolResultValidationError("An abandoned match cannot receive a manual final result.")
    if locked_match.status == PoolMatch.Status.FINISHED and (
        locked_match.result_entry_method
        != PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT
    ):
        raise PoolResultValidationError(
            "Only an existing manual final result can be updated here. Use the existing result correction workflow for a detailed result."
        )
    if locked_match.result_method != PoolMatch.ResultMethod.PLAYED:
        raise PoolResultValidationError(
            "A non-played result cannot be replaced with a manual played result."
        )

    home_score, away_score = _validate_manual_final_score(
        locked_match, home_score, away_score
    )
    home_outcomes = _normalise_manual_outcomes(
        home_outcomes, score=home_score, side="Home"
    )
    away_outcomes = _normalise_manual_outcomes(
        away_outcomes, score=away_score, side="Away"
    )
    prospective_winner = (
        locked_match.home_entry_id
        if home_score > away_score
        else locked_match.away_entry_id
    )
    _bracket_preflight(
        locked_match,
        prospective_winner=prospective_winner,
        prospective_eligible=True,
        actor=recorded_by,
    )
    previous_result = snapshot_match_result(locked_match)
    was_manual_result = (
        locked_match.status == PoolMatch.Status.FINISHED
        and locked_match.result_entry_method
        == PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT
    )
    locked_match.home_score = home_score
    locked_match.away_score = away_score
    locked_match.status = PoolMatch.Status.FINISHED
    locked_match.winner_id = prospective_winner
    locked_match.winner_entry_life_id = _match_entry_life_id(
        locked_match, prospective_winner
    )
    locked_match.completed_at = timezone.now()
    locked_match.result_method = PoolMatch.ResultMethod.PLAYED
    locked_match.result_entry_method = PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT
    locked_match.is_in_correction_mode = False
    locked_match.applied_time_limit_minutes = None
    locked_match.timer_snapshot_at = None
    locked_match.timer_started_at = None
    locked_match.timer_deadline = None
    locked_match.time_expired_tied = False
    locked_match.completion_reason = ""
    locked_match.save()
    for entry_id, outcomes in (
        (locked_match.home_entry_id, home_outcomes),
        (locked_match.away_entry_id, away_outcomes),
    ):
        PoolManualResultRackSummary.objects.update_or_create(
            match=locked_match,
            entry_id=entry_id,
            defaults=outcomes,
        )
    _mark_competition_active_for_played_match(locked_match)
    _mark_unneeded_team_racks(locked_match)
    _record_result_revision(
        locked_match,
        action=(
            PoolMatchResultRevision.Action.MANUAL_RESULT_CORRECTED
            if was_manual_result
            else PoolMatchResultRevision.Action.MANUAL_RESULT_ENTERED
        ),
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    _refresh_match_derived_state(locked_match, actor=recorded_by, reason=reason)
    return locked_match


def record_non_played_result(
    match: PoolMatch,
    *,
    result_method,
    winner,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Record a non-played result without manufacturing any rack history.

    A walkover is a pre-match decision and therefore requires an empty rack
    history and a 0-0 score.  A withdrawal or disqualification can occur after
    play has begun, so this service preserves the already-recorded racks and
    their derived score rather than inventing or deleting match history.  A
    bye is deliberately *not* a match result: the persistent bracket topology
    resolves it without creating a fixture, score, or rack record.
    """
    result = _record_non_played_result_atomic(
        match,
        result_method=result_method,
        winner=winner,
        recorded_by=recorded_by,
        reason=reason,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=_match_pk(match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _record_non_played_result_atomic(
    match: PoolMatch,
    *,
    result_method,
    winner,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch | object:
    """Locked implementation for :func:`record_non_played_result`."""
    locked_match = _lock_match(match)
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=recorded_by,
        reason=reason,
    )
    allow_expired_team_terminal_result = (
        timer_processed
        and getattr(locked_match, "time_expired_tied", False)
        and _team_tied_expiry_has_no_live_scheduled_rack(locked_match)
        and result_method in {
            PoolMatch.ResultMethod.WITHDRAWAL,
            PoolMatch.ResultMethod.DISQUALIFICATION,
        }
    )
    if timer_processed and not allow_expired_team_terminal_result:
        return _TIMER_EXPIRY_BLOCKED
    if (
        locked_match.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
        and PoolTeamMatchScheduledRack.objects.filter(
            match=locked_match,
            status=PoolTeamMatchScheduledRack.Status.LIVE,
        ).exists()
    ):
        # A live score-sheet row owns two immutable player snapshots.  Do not
        # finish the parent through a walkover/withdrawal/disqualification
        # while one of those individual tables is still in play: that would
        # strand a LIVE row whose authoritative result can no longer be
        # recorded.  The scorer must first complete the live tables (or use
        # the explicit result/correction workflow) under the same lock.
        raise PoolResultValidationError(
            "Complete the live team tables before recording a non-played result."
        )
    if result_method == PoolMatch.ResultMethod.BYE:
        raise PoolResultValidationError(
            "A bye is resolved by the bracket and cannot be recorded as a pool match result."
        )
    if result_method not in _NON_PLAYED_RESULT_METHODS:
        raise PoolResultValidationError(
            {"result_method": "Select a non-played result method."}
        )
    if (
        locked_match.status == PoolMatch.Status.FINISHED
        and not locked_match.is_in_correction_mode
    ):
        raise PoolResultValidationError(
            "Open result correction before replacing a completed result."
        )
    if locked_match.status == PoolMatch.Status.CANCELLED:
        raise PoolResultValidationError("A cancelled match cannot receive a result.")

    winner_id = _match_entry_id(locked_match, winner, label="winner")
    _bracket_preflight(
        locked_match,
        prospective_winner=winner_id,
        prospective_eligible=True,
        actor=recorded_by,
    )
    previous_result = snapshot_match_result(locked_match)
    home_score, away_score = _authoritative_scores(locked_match)
    if result_method == PoolMatch.ResultMethod.WALKOVER and (home_score or away_score):
        raise PoolResultValidationError(
            "A walkover or bye cannot have recorded racks or a score."
        )
    locked_match.home_score = home_score
    locked_match.away_score = away_score
    locked_match.status = PoolMatch.Status.FINISHED
    locked_match.result_method = result_method
    locked_match.result_entry_method = PoolMatch.ResultEntryMethod.LIVE_RACK_SCORING
    locked_match.winner_id = winner_id
    locked_match.winner_entry_life_id = _match_entry_life_id(
        locked_match,
        winner_id,
    )
    locked_match.completed_at = timezone.now()
    locked_match.is_in_correction_mode = False
    locked_match.time_expired_tied = False
    locked_match.completion_reason = ""
    locked_match.save()
    _mark_unneeded_team_racks(locked_match)
    _record_result_revision(
        locked_match,
        action=PoolMatchResultRevision.Action.NON_PLAYED_RESULT,
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    _refresh_match_derived_state(locked_match, actor=recorded_by, reason=reason)
    return locked_match


def transition_match_status(
    match: PoolMatch,
    *,
    status,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch:
    """Apply an allowed postpone, cancel, or abandon transition synchronously."""
    result = _transition_match_status_atomic(
        match,
        status=status,
        recorded_by=recorded_by,
        reason=reason,
    )
    if result is _TIMER_EXPIRY_BLOCKED:
        refreshed_match = PoolMatch.objects.get(pk=_match_pk(match))
        raise PoolResultValidationError(_timer_expiry_message(refreshed_match))
    return result


@transaction.atomic
def _transition_match_status_atomic(
    match: PoolMatch,
    *,
    status,
    recorded_by=None,
    reason: str = "",
) -> PoolMatch | object:
    """Locked implementation for :func:`transition_match_status`."""
    if status == PoolMatch.Status.LIVE:
        return start_match(match, recorded_by=recorded_by, reason=reason)
    if status not in _TRANSITIONABLE_STATUSES:
        raise PoolResultValidationError(
            "Use the dedicated result workflow to finish a pool match."
        )

    locked_match = _lock_match(match)
    timer_processed = _expire_locked_match_if_due(
        locked_match,
        recorded_by=recorded_by,
        reason=reason,
    )
    allow_expired_team_abandonment = (
        timer_processed
        and status == PoolMatch.Status.ABANDONED
        and getattr(locked_match, "time_expired_tied", False)
        and _team_tied_expiry_has_no_live_scheduled_rack(locked_match)
    )
    if timer_processed and not allow_expired_team_abandonment:
        return _TIMER_EXPIRY_BLOCKED
    if (
        locked_match.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
        and PoolTeamMatchScheduledRack.objects.filter(
            match=locked_match,
            status=PoolTeamMatchScheduledRack.Status.LIVE,
        ).exists()
    ):
        raise PoolResultValidationError(
            "Complete the live team tables before changing this fixture's status."
        )
    if (
        status in {PoolMatch.Status.CANCELLED, PoolMatch.Status.ABANDONED}
        and PoolKnockoutTie.objects.filter(match=locked_match).exists()
    ):
        raise PoolResultValidationError(
            "A bracket fixture cannot be cancelled or abandoned. Use the authorised result correction or bracket reset workflow."
        )
    if locked_match.status == PoolMatch.Status.FINISHED:
        raise PoolResultValidationError(
            "Open result correction before changing a completed match."
        )
    if locked_match.result_method != PoolMatch.ResultMethod.PLAYED:
        raise PoolResultValidationError(
            "A non-played result cannot be moved to another match status."
        )

    previous_result = snapshot_match_result(locked_match)
    home_score, away_score = _authoritative_scores(locked_match)
    _ensure_target_not_reached(locked_match, home_score, away_score)
    reached_target = (
        home_score == locked_match.race_target
        or away_score == locked_match.race_target
    )
    if reached_target:
        raise PoolResultValidationError(
            "A match at its race target must be completed, not transitioned."
        )

    if status == PoolMatch.Status.POSTPONED:
        if locked_match.status not in {
            PoolMatch.Status.UPCOMING,
            PoolMatch.Status.LIVE,
            PoolMatch.Status.POSTPONED,
        }:
            raise PoolResultValidationError("This match cannot be postponed now.")
        action = PoolMatchResultRevision.Action.MATCH_POSTPONED
    elif status == PoolMatch.Status.CANCELLED:
        if locked_match.status not in {
            PoolMatch.Status.UPCOMING,
            PoolMatch.Status.POSTPONED,
        }:
            raise PoolResultValidationError(
                "Only an upcoming or postponed match can be cancelled."
            )
        if locked_match.racks.exists():
            raise PoolResultValidationError(
                "A match with recorded racks must be abandoned or completed, not cancelled."
            )
        home_score = away_score = 0
        action = PoolMatchResultRevision.Action.MATCH_CANCELLED
    else:
        if locked_match.status not in {
            PoolMatch.Status.LIVE,
            PoolMatch.Status.POSTPONED,
        }:
            raise PoolResultValidationError("Only a started match can be abandoned.")
        action = PoolMatchResultRevision.Action.MATCH_ABANDONED

    locked_match.status = status
    locked_match.home_score = home_score
    locked_match.away_score = away_score
    locked_match.winner = None
    locked_match.winner_entry_life = None
    locked_match.completed_at = None
    locked_match.is_in_correction_mode = False
    locked_match.time_expired_tied = False
    locked_match.completion_reason = ""
    locked_match.save()
    if status in {PoolMatch.Status.CANCELLED, PoolMatch.Status.ABANDONED}:
        _mark_unneeded_team_racks(locked_match)
    _record_result_revision(
        locked_match,
        action=action,
        revised_by=recorded_by,
        reason=reason,
        previous_result=previous_result,
    )
    _refresh_match_derived_state(locked_match, actor=recorded_by, reason=reason)
    return locked_match


def revise_match_result(match: PoolMatch, *, revised_by=None, reason="", **changes):
    """Compatibility shim that refuses manual played-score mutations.

    Results must now flow through rack and lifecycle services so score, winner,
    status, and the audit history cannot drift apart.
    """
    result_method = changes.get("result_method")
    winner = changes.get("winner", changes.get("winner_id"))
    if result_method in _NON_PLAYED_RESULT_METHODS and winner is not None:
        return record_non_played_result(
            match,
            result_method=result_method,
            winner=winner,
            recorded_by=revised_by,
            reason=reason,
        )
    raise PoolResultValidationError(
        "Use the pool rack and result lifecycle services to revise this match."
    )


def result_processing_plan(match: PoolMatch):
    """Describe later work triggered by a result without doing it in phase one.

    This is a deliberately synchronous, side-effect-free boundary.  It keeps
    ordinary model/admin/public operations independent of Celery and Redis.
    """
    if match.status != PoolMatch.Status.FINISHED:
        return ()

    planned = ["player_statistics"]
    if match.stage.stage_type in {
        PoolCompetitionStage.StageType.LEAGUE,
        PoolCompetitionStage.StageType.GROUP,
    }:
        planned.append("league_table_recalculation")
    if match.stage.stage_type == PoolCompetitionStage.StageType.KNOCKOUT:
        planned.append("bracket_advancement")
    if match.competition.participant_mode == PoolCompetition.ParticipantMode.TEAMS:
        planned.append("team_fixture_recalculation")
    if not match.competition.is_money_match:
        planned.append("ranking_calculation")
    return tuple(planned)


def process_match_result_change(match: PoolMatch):
    """Phase-one processing hook; returns planned work and queues nothing."""
    return result_processing_plan(match)
