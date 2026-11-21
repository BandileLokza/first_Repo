"""Safe fixture planning and generation for Table Only football leagues.

The important distinction in this module is between a *plan* and a database
write. Plans are constraint-checked first and may be previewed; only the
small save helpers at the bottom create ``Match`` rows.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from itertools import combinations
from random import SystemRandom
from time import monotonic

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction

from matches.models import Match

from .models import Stage


MAX_SCHEDULER_NODES = 40_000
MAX_SCHEDULER_SECONDS = 2.0
MAX_MATCHING_CANDIDATES = 48
MAX_ORIENTATION_NODES = 40_000
MAX_ORIENTATION_SECONDS = 3.0
LARGE_ORIENTATION_BEAM_WIDTH = 48
LARGE_CONTINUATION_SCHEDULE_VARIANTS = 6
PREVIEW_MAX_AGE_SECONDS = 15 * 60


@dataclass(frozen=True)
class PlannedFixture:
    """One not-yet-persisted fixture selected by the scheduler."""

    gameweek: int
    home_team_id: int
    away_team_id: int
    leg: int


@dataclass(frozen=True)
class FixturePlan:
    """An immutable generation result used by both preview and save paths."""

    action: str
    competition_id: int
    team_ids: tuple[int, ...]
    expected_gameweeks: tuple[int, ...]
    fixtures: tuple[PlannedFixture, ...]
    balance: dict


@dataclass(frozen=True)
class _FixtureSnapshot:
    """Minimal immutable fixture shape used for non-persisting look-ahead."""

    id: int
    stage_id: int
    group_id: int | None
    gameweek: int
    home_team_id: int
    away_team_id: int
    leg: int


class _SearchLimitExceeded(Exception):
    """Internal signal used to keep the bounded scheduler deterministic."""


def _table_stage(competition):
    stages = list(
        Stage.objects.filter(competition=competition, stage_type="LEAGUE").order_by("id")
    )
    if len(stages) != 1:
        raise ValidationError(
            "Fixture generation requires exactly one league-table stage."
        )
    return stages[0]


def _validate_competition(competition):
    if competition.format_type != "TABLE":
        raise ValidationError("Automatic fixtures are only available for Table Only leagues.")
    if competition.is_archived:
        raise ValidationError("Archived seasons are read-only; fixtures cannot be generated.")
    if competition.schedule_locked_at:
        raise ValidationError(
            "This competition's schedule is locked. Fixture generation and continuation "
            "are unavailable until a superuser unlocks it."
        )


def _teams_for_generation(competition, *, lock=False):
    queryset = competition.teams.order_by("name", "id")
    if lock:
        queryset = queryset.select_for_update()
    teams = list(queryset)
    if len(teams) < 2:
        raise ValidationError("At least two teams are required to generate fixtures.")
    return teams


def round_metrics(team_count):
    """Return the standard single-round shape for ``team_count`` teams."""

    return {
        "matches_per_gameweek": team_count // 2,
        "gameweeks": team_count - 1 if team_count % 2 == 0 else team_count,
        "fixtures": team_count * (team_count - 1) // 2,
    }


def _circle_pairings(teams):
    """Yield circle-method gameweeks without selecting home or away teams."""

    rotation = list(teams)
    if len(rotation) % 2:
        rotation.append(None)

    team_count = len(rotation)
    for round_index in range(team_count - 1):
        for pair_index in range(team_count // 2):
            first = rotation[pair_index]
            second = rotation[-(pair_index + 1)]
            if first is not None and second is not None:
                yield round_index + 1, pair_index, first, second
        rotation = [rotation[0], rotation[-1], *rotation[1:-1]]


def _pair(first_id, second_id):
    return frozenset((first_id, second_id))


def _pair_members(pairing):
    first, second = sorted(pairing)
    return first, second


def _has_baseline_standings_without_matches(competition):
    """Imported totals cannot safely substitute for fixture history."""

    from .models import Standing

    return Standing.objects.filter(competition=competition, played__gt=0).exists()


def _validate_first_round_history(competition, teams, matches):
    """Validate immutable first-leg history and return unordered pairings.

    Status, scores and kickoff dates intentionally do not participate in this
    validation. A saved fixture is a scheduling commitment whether it is
    upcoming, live, abandoned, or finished.
    """

    team_ids = {team.id for team in teams}
    metrics = round_metrics(len(teams))
    table_stage = _table_stage(competition)
    pairings = set()
    appearances = defaultdict(set)

    for match in matches:
        if match.leg != 1:
            raise ValidationError(
                "First-round continuation accepts only leg 1 fixtures. "
                "A partially entered second round must be completed manually."
            )
        if (
            match.stage_id != table_stage.id
            or match.group_id is not None
            or match.gameweek is None
            or match.gameweek < 1
            or match.gameweek > metrics["gameweeks"]
            or match.home_team_id not in team_ids
            or match.away_team_id not in team_ids
            or match.home_team_id == match.away_team_id
        ):
            raise ValidationError(
                "Historical first-round fixtures must be league-table leg 1 matches "
                "in the expected gameweek range and current league team set."
            )

        pairing = _pair(match.home_team_id, match.away_team_id)
        if pairing in pairings:
            raise ValidationError(
                "Historical fixtures contain a duplicate first-round matchup; "
                "correct it before generating fixtures."
            )
        pairings.add(pairing)

        if (
            match.home_team_id in appearances[match.gameweek]
            or match.away_team_id in appearances[match.gameweek]
        ):
            raise ValidationError(
                "Historical fixtures have a team appearing more than once in one "
                "gameweek; correct them before generating fixtures."
            )
        appearances[match.gameweek].update((match.home_team_id, match.away_team_id))

    expected = {
        _pair(first.id, second.id)
        for first, second in combinations(teams, 2)
    }
    if not pairings.issubset(expected):
        raise ValidationError("Historical fixtures do not represent the current league team set.")
    return pairings


def _validate_complete_first_round(competition, teams, matches):
    pairings = _validate_first_round_history(competition, teams, matches)
    metrics = round_metrics(len(teams))
    if len(pairings) != metrics["fixtures"]:
        raise ValidationError("The existing fixtures are not a complete single first round.")

    by_gameweek = defaultdict(list)
    for match in matches:
        by_gameweek[match.gameweek].append(match)
    expected_gameweeks = set(range(1, metrics["gameweeks"] + 1))
    if set(by_gameweek) != expected_gameweeks or any(
        len(by_gameweek[gameweek]) != metrics["matches_per_gameweek"]
        for gameweek in expected_gameweeks
    ):
        raise ValidationError(
            "The first round has every pairing but does not contain complete expected gameweeks."
        )
    return tuple(sorted(by_gameweek))


def _matching_candidates(free_team_ids, remaining_pairings, *, odd, limit):
    """Return a bounded deterministic set of matchings for one gameweek."""

    candidates = []
    free_team_ids = frozenset(free_team_ids)

    def available_opponents(team_id, unmatched):
        return sorted(
            other
            for other in unmatched
            if other != team_id and _pair(team_id, other) in remaining_pairings
        )

    def walk(unmatched, selected, bye_used):
        if len(candidates) >= limit:
            return
        if not unmatched:
            candidates.append(tuple(selected))
            return
        if len(unmatched) == 1:
            if odd and not bye_used:
                candidates.append(tuple(selected))
            return

        team_id = min(
            unmatched,
            key=lambda item: (len(available_opponents(item, unmatched)), item),
        )
        opponents = available_opponents(team_id, unmatched)
        opponents.sort(
            key=lambda item: (
                len(available_opponents(item, unmatched - {team_id})),
                item,
            )
        )
        for opponent_id in opponents:
            walk(
                unmatched - {team_id, opponent_id},
                [*selected, _pair(team_id, opponent_id)],
                bye_used,
            )
            if len(candidates) >= limit:
                return

        # In an odd-team round exactly one free team may receive a bye. Try
        # pairing first so the bye is reserved for a genuinely constrained case.
        if odd and not bye_used:
            walk(unmatched - {team_id}, selected, True)

    walk(free_team_ids, [], False)
    return candidates


def _schedule_remaining_pairings(
    team_ids,
    fixed_by_gameweek,
    remaining_pairings,
    gameweeks,
    *,
    schedule_variant=0,
):
    """Complete a partial one-factorisation with bounded backtracking."""

    team_ids = frozenset(team_ids)
    complete_pairings = {
        _pair(first_id, second_id) for first_id, second_id in combinations(team_ids, 2)
    }
    # A complete, unconstrained round robin has a known factorisation.  Do
    # not send it through the bounded continuation solver: this path is used
    # while checking a prospective second round and otherwise can hit the
    # wall-clock guard purely because the worker is busy.
    if (
        not fixed_by_gameweek
        and set(remaining_pairings) == complete_pairings
        and len(gameweeks) == round_metrics(len(team_ids))["gameweeks"]
    ):
        scheduled = defaultdict(list)
        for round_number, _index, first_id, second_id in _circle_pairings(sorted(team_ids)):
            scheduled[gameweeks[round_number - 1]].append(_pair(first_id, second_id))
        return {gameweek: tuple(scheduled[gameweek]) for gameweek in gameweeks}

    odd = len(team_ids) % 2 == 1
    free_by_gameweek = {}
    for gameweek in gameweeks:
        occupied = set()
        fixed = fixed_by_gameweek.get(gameweek, ())
        for match in fixed:
            occupied.update((match.home_team_id, match.away_team_id))
        free_by_gameweek[gameweek] = team_ids - occupied

    active_gameweeks = [
        gameweek for gameweek in gameweeks if len(free_by_gameweek[gameweek]) >= 2
    ]
    # In an odd-team league a fully fixed gameweek has one free team: that is
    # already this team's bye, so it must not be subtracted a second time from
    # the remaining-degree calculation below.
    fixed_bye_counts = defaultdict(int)
    for gameweek in gameweeks:
        if odd and len(free_by_gameweek[gameweek]) == 1:
            fixed_bye_counts[next(iter(free_by_gameweek[gameweek]))] += 1
    if any(count > 1 for count in fixed_bye_counts.values()):
        raise ValidationError(
            "Historical fixtures give a team more than one first-round bye."
        )
    active_gameweeks.sort(key=lambda gameweek: (len(free_by_gameweek[gameweek]), gameweek))
    deadline = monotonic() + MAX_SCHEDULER_SECONDS
    nodes = 0
    assignments = {}

    def feasible(available_pairings, index, bye_counts):
        future_gameweeks = active_gameweeks[index:]
        degrees = defaultdict(int)
        for pairing in available_pairings:
            first, second = _pair_members(pairing)
            degrees[first] += 1
            degrees[second] += 1
        for team_id in team_ids:
            available_slots = sum(
                team_id in free_by_gameweek[gameweek]
                for gameweek in future_gameweeks
            )
            required = available_slots - (
                1 - bye_counts[team_id] if odd else 0
            )
            if degrees[team_id] != required:
                return False
        return True

    def search(index, available_pairings, bye_counts):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_SCHEDULER_NODES or monotonic() > deadline:
            raise _SearchLimitExceeded
        if index == len(active_gameweeks):
            return not available_pairings and (
                not odd or all(bye_counts[team_id] == 1 for team_id in team_ids)
            )
        if not feasible(available_pairings, index, bye_counts):
            return False

        gameweek = active_gameweeks[index]
        candidates = _matching_candidates(
            free_by_gameweek[gameweek],
            available_pairings,
            odd=odd,
            limit=MAX_MATCHING_CANDIDATES,
        )
        if candidates and schedule_variant:
            # Retrying a continuation with a rotated candidate order explores
            # another valid edge-colouring of the proposed gameweeks without
            # touching manual rows. It is used only by the strict large-table
            # home/away planner after the first valid completion is not
            # orientable under the maximum-two rule.
            offset = (schedule_variant * (gameweek + index + 1)) % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        for matching in candidates:
            matching_set = set(matching)
            if not matching_set.issubset(available_pairings):
                continue
            next_bye_counts = bye_counts.copy()
            if odd:
                participants = {
                    team_id
                    for pairing in matching_set
                    for team_id in pairing
                }
                byes = free_by_gameweek[gameweek] - participants
                if len(byes) != 1:
                    continue
                bye_team = next(iter(byes))
                next_bye_counts[bye_team] += 1
                if next_bye_counts[bye_team] > 1:
                    continue
            assignments[gameweek] = tuple(matching)
            if search(index + 1, available_pairings - matching_set, next_bye_counts):
                return True
            assignments.pop(gameweek, None)
        return False

    try:
        found = search(0, set(remaining_pairings), fixed_bye_counts.copy())
    except _SearchLimitExceeded as error:
        raise ValidationError(
            "The imported fixture constraints exceeded the safe scheduling search "
            "limit. Review the fixed gameweeks or split the schedule into a smaller "
            "planning step."
        ) from error
    if not found:
        raise ValidationError(
            "The existing fixtures cannot be completed into the required number of "
            "full gameweeks without changing a fixed fixture."
        )
    return {gameweek: assignments.get(gameweek, ()) for gameweek in gameweeks}


def _orientation_cost(home_id, away_id, history, home_counts, games_per_team):
    minimum_home = games_per_team // 2
    maximum_home = (games_per_team + 1) // 2
    home_history = history[home_id]
    away_history = history[away_id]
    remaining_home = games_per_team - (len(home_history) + 1)
    remaining_away = games_per_team - (len(away_history) + 1)
    hard_violation = (
        int(home_counts[home_id] + 1 > maximum_home)
        + int(home_counts[home_id] + 1 + remaining_home < minimum_home)
        + int(home_counts[away_id] + remaining_away < minimum_home)
    )
    streak_violation = (
        int(len(home_history) >= 2 and home_history[-2:] == ["H", "H"])
        + int(len(away_history) >= 2 and away_history[-2:] == ["A", "A"])
    )
    repeat_venue = (
        int(bool(home_history) and home_history[-1] == "H")
        + int(bool(away_history) and away_history[-1] == "A")
    )
    return hard_violation, streak_violation, repeat_venue, home_counts[home_id], home_id, away_id


def _greedy_first_round_orientations(
    team_ids, fixed_by_gameweek, scheduled_by_gameweek, gameweeks
):
    """Fast deterministic fallback when the bounded optimiser cannot improve."""

    games_per_team = len(team_ids) - 1
    history = {team_id: [] for team_id in team_ids}
    home_counts = {team_id: 0 for team_id in team_ids}
    fixtures = []

    for gameweek in sorted(gameweeks):
        fixed = fixed_by_gameweek.get(gameweek, ())
        for match in fixed:
            history[match.home_team_id].append("H")
            history[match.away_team_id].append("A")
            home_counts[match.home_team_id] += 1
        for pairing in sorted(scheduled_by_gameweek.get(gameweek, ()), key=_pair_members):
            first_id, second_id = _pair_members(pairing)
            normal = _orientation_cost(
                first_id, second_id, history, home_counts, games_per_team
            )
            reverse = _orientation_cost(
                second_id, first_id, history, home_counts, games_per_team
            )
            home_id, away_id = (
                (first_id, second_id) if normal <= reverse else (second_id, first_id)
            )
            fixtures.append(PlannedFixture(gameweek, home_id, away_id, 1))
            history[home_id].append("H")
            history[away_id].append("A")
            home_counts[home_id] += 1
    return fixtures


def _balanced_first_round_orientations(
    team_ids, fixed_by_gameweek, scheduled_by_gameweek, gameweeks
):
    """Find a small-table orientation with balanced totals and no new 3-run.

    Pairings and gameweeks are already fixed by this stage; only each match's
    direction remains variable. The old greedy implementation chose a
    direction one match at a time, so a random circle draw could commit an
    apparently good choice that made a later three-home or three-away run
    inevitable. This bounded round-by-round search considers the remaining
    orientations together. It intentionally applies only to small tables,
    where exhaustive pruning is both cheap and most valuable for the manager
    preview; larger tables retain the existing bounded greedy fallback.
    """

    if len(team_ids) > 10:
        return None, False

    games_per_team = len(team_ids) - 1
    minimum_home = games_per_team // 2
    maximum_home = (games_per_team + 1) // 2
    ordered_gameweeks = tuple(sorted(gameweeks))
    all_fixed = [
        match
        for gameweek in ordered_gameweeks
        for match in fixed_by_gameweek.get(gameweek, ())
    ]
    generated_games = defaultdict(int)
    fixed_homes = defaultdict(int)
    for match in all_fixed:
        fixed_homes[match.home_team_id] += 1
    for gameweek in ordered_gameweeks:
        for pairing in scheduled_by_gameweek.get(gameweek, ()):
            first_id, second_id = _pair_members(pairing)
            generated_games[first_id] += 1
            generated_games[second_id] += 1

    # If immutable manual fixtures already make the preferred per-round home
    # totals impossible, do not reject a valid continuation on that soft
    # objective. We still seek a no-new-three-run orientation and report any
    # unavoidable imbalance in the preview.
    enforce_balance = all(
        fixed_homes[team_id] <= maximum_home
        and fixed_homes[team_id] + generated_games[team_id] >= minimum_home
        for team_id in team_ids
    )
    deadline = monotonic() + MAX_ORIENTATION_SECONDS
    nodes = 0
    limit_reached = False

    def append_fixed(history, homes, fixed):
        next_history = {team_id: list(values) for team_id, values in history.items()}
        next_homes = dict(homes)
        for match in fixed:
            next_history[match.home_team_id].append("H")
            next_history[match.away_team_id].append("A")
            next_homes[match.home_team_id] += 1
        return next_history, next_homes

    def round_options(gameweek, history, homes):
        pairings = tuple(
            sorted(scheduled_by_gameweek.get(gameweek, ()), key=_pair_members)
        )
        candidates = []

        def choose(index, entries, next_homes, repeat_cost):
            if index == len(pairings):
                candidates.append((repeat_cost, tuple(entries), next_homes))
                return
            first_id, second_id = _pair_members(pairings[index])
            choices = ((first_id, second_id), (second_id, first_id))
            for home_id, away_id in choices:
                if (
                    len(history[home_id]) >= 2
                    and history[home_id][-2:] == ["H", "H"]
                ) or (
                    len(history[away_id]) >= 2
                    and history[away_id][-2:] == ["A", "A"]
                ):
                    continue
                if enforce_balance and next_homes[home_id] + 1 > maximum_home:
                    continue
                updated_homes = dict(next_homes)
                updated_homes[home_id] += 1
                repeat = repeat_cost + int(
                    bool(history[home_id]) and history[home_id][-1] == "H"
                ) + int(
                    bool(history[away_id]) and history[away_id][-1] == "A"
                )
                choose(index + 1, [*entries, (home_id, away_id)], updated_homes, repeat)

        choose(0, [], dict(homes), 0)
        return sorted(
            candidates,
            key=lambda item: (item[0], item[1]),
        )

    def search(index, history, homes, fixtures):
        nonlocal nodes, limit_reached
        nodes += 1
        if nodes > MAX_ORIENTATION_NODES or monotonic() > deadline:
            limit_reached = True
            return None
        if index == len(ordered_gameweeks):
            if enforce_balance and any(
                not minimum_home <= homes[team_id] <= maximum_home
                for team_id in team_ids
            ):
                return None
            return fixtures

        gameweek = ordered_gameweeks[index]
        round_history, round_homes = append_fixed(
            history, homes, fixed_by_gameweek.get(gameweek, ())
        )
        for _cost, orientations, homes_after in round_options(
            gameweek, round_history, round_homes
        ):
            next_history = {
                team_id: list(values) for team_id, values in round_history.items()
            }
            round_fixtures = []
            for home_id, away_id in orientations:
                next_history[home_id].append("H")
                next_history[away_id].append("A")
                round_fixtures.append(PlannedFixture(gameweek, home_id, away_id, 1))
            result = search(
                index + 1,
                next_history,
                homes_after,
                [*fixtures, *round_fixtures],
            )
            if result is not None:
                return result
        return None

    return (
        search(
            0,
            {team_id: [] for team_id in team_ids},
            {team_id: 0 for team_id in team_ids},
            [],
        ),
        limit_reached,
    )


def _large_first_round_orientations(
    team_ids, fixed_by_gameweek, scheduled_by_gameweek, gameweeks
):
    """Use a bounded beam search for strict large-table home/away assignment.

    A state stores only the last two venues and current home total for each
    team. That is sufficient to enforce the maximum-two rule and final
    home/away bounds, while avoiding exponential enumeration of every whole
    round. No state that creates a third identical venue is retained.

    Returns ``(fixtures, status)`` where status is ``solved``, ``infeasible``
    for an exhaustively eliminated state space, or ``resource_limit`` when
    beam pruning/time limits mean infeasibility cannot be proven.
    """

    if len(team_ids) <= 10:
        return None, "not_applicable"

    games_per_team = len(team_ids) - 1
    minimum_home = games_per_team // 2
    maximum_home = (games_per_team + 1) // 2
    ordered_gameweeks = tuple(sorted(gameweeks))
    team_positions = {team_id: index for index, team_id in enumerate(team_ids)}

    # Count immutable future homes and unscheduled future appearances. They
    # give cheap, exact bounds for whether a partial orientation can still
    # finish with a valid per-round home total.
    future_fixed_homes = []
    future_generated_games = []
    remaining_fixed_homes = [0] * len(team_ids)
    remaining_generated_games = [0] * len(team_ids)
    by_gameweek = {}
    for gameweek in ordered_gameweeks:
        fixed = tuple(fixed_by_gameweek.get(gameweek, ()))
        pairings = tuple(sorted(scheduled_by_gameweek.get(gameweek, ()), key=_pair_members))
        by_gameweek[gameweek] = (fixed, pairings)
        for match in fixed:
            remaining_fixed_homes[team_positions[match.home_team_id]] += 1
        for pairing in pairings:
            first_id, second_id = _pair_members(pairing)
            remaining_generated_games[team_positions[first_id]] += 1
            remaining_generated_games[team_positions[second_id]] += 1
    for gameweek in ordered_gameweeks:
        fixed, pairings = by_gameweek[gameweek]
        for match in fixed:
            remaining_fixed_homes[team_positions[match.home_team_id]] -= 1
        for pairing in pairings:
            first_id, second_id = _pair_members(pairing)
            remaining_generated_games[team_positions[first_id]] -= 1
            remaining_generated_games[team_positions[second_id]] -= 1
        future_fixed_homes.append(tuple(remaining_fixed_homes))
        future_generated_games.append(tuple(remaining_generated_games))

    deadline = monotonic() + MAX_ORIENTATION_SECONDS
    truncated = False
    # histories, home_totals, repeat_cost, generated fixtures
    states = [
        (
            tuple("" for _team_id in team_ids),
            tuple(0 for _team_id in team_ids),
            0,
            tuple(),
        )
    ]

    def append_venue(histories, team_id, venue):
        position = team_positions[team_id]
        previous = histories[position]
        if previous[-2:] == venue * 2:
            return None
        updated = list(histories)
        updated[position] = (previous + venue)[-2:]
        return tuple(updated)

    def apply_fixed(histories, homes, fixed):
        next_histories = histories
        next_homes = list(homes)
        for match in fixed:
            next_histories = append_venue(next_histories, match.home_team_id, "H")
            if next_histories is None:
                return None
            next_histories = append_venue(next_histories, match.away_team_id, "A")
            if next_histories is None:
                return None
            next_homes[team_positions[match.home_team_id]] += 1
        return next_histories, tuple(next_homes)

    def orientation_options(histories, homes, pairings):
        options = []

        def choose(index, next_histories, next_homes, repeat_cost, fixtures):
            if index == len(pairings):
                options.append((repeat_cost, next_histories, next_homes, tuple(fixtures)))
                return
            first_id, second_id = _pair_members(pairings[index])
            for home_id, away_id in ((first_id, second_id), (second_id, first_id)):
                home_position = team_positions[home_id]
                away_position = team_positions[away_id]
                if next_homes[home_position] >= maximum_home:
                    continue
                home_history = next_histories[home_position]
                away_history = next_histories[away_position]
                after_home = append_venue(next_histories, home_id, "H")
                if after_home is None:
                    continue
                after_away = append_venue(after_home, away_id, "A")
                if after_away is None:
                    continue
                updated_homes = list(next_homes)
                updated_homes[home_position] += 1
                choose(
                    index + 1,
                    after_away,
                    tuple(updated_homes),
                    repeat_cost
                    + int(bool(home_history) and home_history[-1] == "H")
                    + int(bool(away_history) and away_history[-1] == "A"),
                    [*fixtures, (home_id, away_id)],
                )

        choose(0, histories, homes, 0, [])
        return options

    for index, gameweek in enumerate(ordered_gameweeks):
        if monotonic() > deadline:
            return None, "resource_limit"
        fixed, pairings = by_gameweek[gameweek]
        next_states = {}
        for histories, homes, accumulated_cost, generated in states:
            fixed_state = apply_fixed(histories, homes, fixed)
            if fixed_state is None:
                # The immutable history itself has a three-match venue run.
                continue
            histories_after_fixed, homes_after_fixed = fixed_state
            for round_cost, next_histories, next_homes, orientations in orientation_options(
                histories_after_fixed, homes_after_fixed, pairings
            ):
                future_homes = future_fixed_homes[index]
                future_games = future_generated_games[index]
                if any(
                    next_homes[position] + future_homes[position] > maximum_home
                    or next_homes[position] + future_homes[position] + future_games[position]
                    < minimum_home
                    for position in range(len(team_ids))
                ):
                    continue
                fixtures = generated + tuple(
                    PlannedFixture(gameweek, home_id, away_id, 1)
                    for home_id, away_id in orientations
                )
                key = (next_histories, next_homes)
                candidate = (accumulated_cost + round_cost, fixtures)
                current = next_states.get(key)
                if current is None or candidate[0] < current[0]:
                    next_states[key] = candidate

        if not next_states:
            return None, "resource_limit" if truncated else "infeasible"
        ranked = sorted(
            (
                cost,
                histories,
                homes,
                fixtures,
            )
            for (histories, homes), (cost, fixtures) in next_states.items()
        )
        if len(ranked) > LARGE_ORIENTATION_BEAM_WIDTH:
            truncated = True
            ranked = ranked[:LARGE_ORIENTATION_BEAM_WIDTH]
        states = [
            (histories, homes, cost, fixtures)
            for cost, histories, homes, fixtures in ranked
        ]

    solved = [
        state
        for state in states
        if all(minimum_home <= total <= maximum_home for total in state[1])
    ]
    if not solved:
        return None, "resource_limit" if truncated else "infeasible"
    return min(
        solved,
        key=lambda state: (
            state[2],
            tuple(
                (fixture.gameweek, fixture.home_team_id, fixture.away_team_id)
                for fixture in state[3]
            ),
        ),
    )[3], "solved"


def _orient_first_round(team_ids, fixed_by_gameweek, scheduled_by_gameweek, gameweeks):
    """Orient generated pairs while preserving fixed manual home/away rows."""

    optimised, limit_reached = _balanced_first_round_orientations(
        team_ids, fixed_by_gameweek, scheduled_by_gameweek, gameweeks
    )
    if optimised is not None:
        return optimised
    if len(team_ids) <= 10:
        detail = (
            "The home/away orientation search reached its safe resource limit."
            if limit_reached
            else "The fixed fixtures cannot be oriented without a three-match home/away streak."
        )
        raise ValidationError(f"{detail} No fixtures were generated.")

    optimised, status = _large_first_round_orientations(
        team_ids, fixed_by_gameweek, scheduled_by_gameweek, gameweeks
    )
    if optimised is not None:
        return optimised
    if status == "infeasible":
        detail = (
            "The fixed fixture history and current gameweek pairings cannot satisfy "
            "the maximum-two home/away rule."
        )
    else:
        detail = (
            "The home/away optimiser reached its safe resource limit before finding "
            "a compliant schedule; this is not an infeasibility finding."
        )
    raise ValidationError(f"{detail} No fixtures were generated.")


def _circle_large_first_round_orientations(draw, team_ids, gameweeks):
    """Orient a fresh large circle draw without an exponential round search.

    Alternating the direction of every pairing by gameweek gives the circle
    method a no-three-venue baseline. A small number of safe edge flips then
    corrects individual home totals. Unlike the old greedy path, this handles
    the venue *sequence* first and only changes directions when the complete
    per-team home/away bounds remain attainable.
    """

    games_per_team = len(team_ids) - 1
    minimum_home = games_per_team // 2
    maximum_home = (games_per_team + 1) // 2
    gameweek_index = {gameweek: index for index, gameweek in enumerate(gameweeks)}
    orientations = []
    for gameweek, _pair_index, first, second in _circle_pairings(draw):
        home_id, away_id = (
            (first.id, second.id)
            if gameweek % 2 else (second.id, first.id)
        )
        orientations.append([gameweek, home_id, away_id])

    sequences = {
        team_id: [None for _gameweek in gameweeks] for team_id in team_ids
    }
    home_counts = {team_id: 0 for team_id in team_ids}
    for gameweek, home_id, away_id in orientations:
        position = gameweek_index[gameweek]
        sequences[home_id][position] = "H"
        sequences[away_id][position] = "A"
        home_counts[home_id] += 1

    def no_three_at(team_id, position):
        values = sequences[team_id]
        for start in range(max(0, position - 2), min(position, len(values) - 3) + 1):
            window = values[start:start + 3]
            if window[0] is not None and window[0] == window[1] == window[2]:
                return False
        return True

    def imbalance():
        return sum(
            max(0, minimum_home - home_counts[team_id])
            + max(0, home_counts[team_id] - maximum_home)
            for team_id in team_ids
        )

    deadline = monotonic() + MAX_ORIENTATION_SECONDS
    nodes = 0

    def repair():
        nonlocal nodes
        nodes += 1
        if nodes > MAX_ORIENTATION_NODES or monotonic() > deadline:
            return False
        if not imbalance():
            return True

        under = sorted(
            (team_id for team_id in team_ids if home_counts[team_id] < minimum_home),
            key=lambda team_id: (home_counts[team_id], team_id),
        )
        over = sorted(
            (team_id for team_id in team_ids if home_counts[team_id] > maximum_home),
            key=lambda team_id: (-home_counts[team_id], team_id),
        )
        if under:
            recipient = under[0]
            donors = [
                team_id for team_id in team_ids if home_counts[team_id] > minimum_home
            ]
        elif over:
            donors = [over[0]]
            recipient = None
        else:
            return False

        candidates = []
        for index, (gameweek, home_id, away_id) in enumerate(orientations):
            if home_id not in donors:
                continue
            if recipient is not None and away_id != recipient:
                continue
            if recipient is None and home_counts[away_id] >= maximum_home:
                continue
            candidates.append((gameweek, index, home_id, away_id))
        candidates.sort(
            key=lambda item: (
                home_counts[item[3]],
                -home_counts[item[2]],
                item[0],
            )
        )
        for gameweek, index, donor, recipient in candidates:
            position = gameweek_index[gameweek]
            sequences[donor][position] = "A"
            sequences[recipient][position] = "H"
            if no_three_at(donor, position) and no_three_at(recipient, position):
                orientations[index][1], orientations[index][2] = recipient, donor
                home_counts[donor] -= 1
                home_counts[recipient] += 1
                if repair():
                    return True
                home_counts[donor] += 1
                home_counts[recipient] -= 1
                orientations[index][1], orientations[index][2] = donor, recipient
            sequences[donor][position] = "H"
            sequences[recipient][position] = "A"
        return False

    if not repair():
        raise ValidationError(
            "The large-league home/away optimiser could not balance the fresh "
            "circle draw within its safe resource limit. No fixtures were generated."
        )
    return [
        PlannedFixture(gameweek, home_id, away_id, 1)
        for gameweek, home_id, away_id in orientations
    ]


def _entries_balance(team_ids, entries):
    """Summarise home/away balance and report, rather than hide, streaks."""

    sequences = {team_id: [] for team_id in team_ids}
    homes = {team_id: 0 for team_id in team_ids}
    for gameweek, home_id, away_id in sorted(entries):
        sequences[home_id].append((gameweek, "H"))
        sequences[away_id].append((gameweek, "A"))
        homes[home_id] += 1

    teams = {}
    violations = []
    violation_details = []
    for team_id in team_ids:
        values_with_gameweeks = sorted(sequences[team_id])
        values = [value for _gameweek, value in values_with_gameweeks]
        longest = 0
        longest_home = 0
        longest_away = 0
        current = 0
        previous = None
        runs = []
        run_start = 0
        for index, value in enumerate(values):
            current = current + 1 if value == previous else 1
            if value != previous:
                run_start = index
            longest = max(longest, current)
            if value == "H":
                longest_home = max(longest_home, current)
            else:
                longest_away = max(longest_away, current)
            if index == len(values) - 1 or values[index + 1] != value:
                if current > 2:
                    runs.append({
                        "venue": value,
                        "gameweeks": tuple(
                            gameweek
                            for gameweek, _venue in values_with_gameweeks[run_start:index + 1]
                        ),
                    })
            previous = value
        teams[team_id] = {
            "home": homes[team_id],
            "away": len(values) - homes[team_id],
            "longest_home_streak": longest_home,
            "longest_away_streak": longest_away,
            "longest_streak": longest,
            "sequence": "".join(values),
        }
        if longest > 2:
            violations.append(team_id)
            violation_details.append({"team_id": team_id, "runs": tuple(runs)})
    return {
        "teams": teams,
        "streak_violation_team_ids": violations,
        "streak_violations": violation_details,
    }


def _assert_max_two_streaks(balance, *, team_names=None):
    """Prevent a plan or confirmation from accepting a prohibited venue run."""

    details = balance["streak_violations"]
    if not details:
        return
    affected = "; ".join(
        "team {team_id}: {runs}".format(
            team_id=(team_names or {}).get(item["team_id"], f"team {item['team_id']}"),
            runs=", ".join(
                "{venue} in Gameweeks {gameweeks}".format(
                    venue=run["venue"],
                    gameweeks=", ".join(str(gameweek) for gameweek in run["gameweeks"]),
                )
                for run in item["runs"]
            ),
        )
        for item in details
    )
    raise ValidationError(
        "The maximum-two home/away rule cannot be met without changing fixed "
        f"fixtures: {affected}. No fixtures were generated."
    )


def _order_second_round_matchings(scheduled, gameweeks, first_matches, orientations):
    """Order reverse matchings to reduce the round-boundary streak.

    Reverse home/away assignments are immutable, but when no second-round row
    exists the matching gameweeks themselves can safely be ordered. First try
    a bounded backtracking pass that rejects a third consecutive venue. This
    makes the "no more than two" preference real when the factorisation
    permits it, rather than merely hoping a greedy choice happens to find it.
    The greedy pass remains as a deterministic fallback for larger or
    genuinely constrained schedules.
    """

    history = defaultdict(list)
    for match in sorted(first_matches, key=lambda item: (item.gameweek, item.id)):
        history[match.home_team_id].append("H")
        history[match.away_team_id].append("A")
    matchings = tuple(tuple(scheduled[gameweek]) for gameweek in gameweeks)

    def score(matching):
        third_venue = 0
        repeat_venue = 0
        for pairing in matching:
            home_id, away_id = orientations[pairing]
            third_venue += int(
                len(history[home_id]) >= 2 and history[home_id][-2:] == ["H", "H"]
            )
            third_venue += int(
                len(history[away_id]) >= 2 and history[away_id][-2:] == ["A", "A"]
            )
            repeat_venue += int(bool(history[home_id]) and history[home_id][-1] == "H")
            repeat_venue += int(bool(history[away_id]) and history[away_id][-1] == "A")
        return (
            third_venue,
            repeat_venue,
            tuple(sorted(_pair_members(pair) for pair in matching)),
        )

    def apply(matching):
        for pairing in matching:
            home_id, away_id = orientations[pairing]
            history[home_id].append("H")
            history[away_id].append("A")

    def undo(matching):
        for pairing in matching:
            home_id, away_id = orientations[pairing]
            history[home_id].pop()
            history[away_id].pop()

    # The search only pursues zero-third-venue choices. It is deliberately
    # bounded so a large table never turns fixture generation into an
    # unbounded permutation search.
    deadline = monotonic() + MAX_SCHEDULER_SECONDS
    nodes = 0

    def find_streak_safe(remaining, selected):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_SCHEDULER_NODES or monotonic() > deadline:
            return None
        if not remaining:
            return list(selected)
        choices = sorted(remaining, key=score)
        for matching in choices:
            if score(matching)[0]:
                continue
            apply(matching)
            result = find_streak_safe(
                tuple(candidate for candidate in remaining if candidate != matching),
                [*selected, matching],
            )
            if result is not None:
                return result
            undo(matching)
        return None

    streak_safe = find_streak_safe(matchings, [])
    if streak_safe is not None:
        return dict(zip(gameweeks, streak_safe))

    # ``find_streak_safe`` undoes every explored branch before reporting no
    # solution, so ``history`` is again exactly the first-round history here.
    # Make the best deterministic choice at each remaining position.
    remaining = list(matchings)
    ordered = {}
    for gameweek in gameweeks:
        matching = min(remaining, key=score)
        remaining.remove(matching)
        ordered[gameweek] = matching
        apply(matching)
    return ordered


def _balance_objective(balance):
    streaks = [values["longest_streak"] for values in balance["teams"].values()]
    return (
        len(balance["streak_violation_team_ids"]),
        sum(max(0, streak - 2) for streak in streaks),
        sum(streaks),
    )


def _first_round_snapshots(competition, fixtures):
    """Give persisted and proposed first-round rows one validation shape."""

    stage_id = _table_stage(competition).id
    return tuple(
        _FixtureSnapshot(
            id=(fixture.id if getattr(fixture, "id", None) is not None else -(index + 1)),
            stage_id=getattr(fixture, "stage_id", stage_id),
            group_id=getattr(fixture, "group_id", None),
            gameweek=fixture.gameweek,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            leg=fixture.leg,
        )
        for index, fixture in enumerate(fixtures)
    )


def _assert_first_round_leaves_compliant_second_round(competition, teams, first_matches):
    """Verify a first-round proposal can finish a strict double round robin.

    Previewing a first round must not advertise the venue constraint as a
    season-wide success unless its immutable reverse fixtures can also be
    ordered without a third consecutive home or away match.
    """

    try:
        _build_second_round_plan(
            competition,
            teams,
            _first_round_snapshots(competition, first_matches),
            (),
        )
    except ValidationError as error:
        raise ValidationError(
            "The first-round plan is valid on its own but does not leave a "
            "compliant second-round schedule within the safe search limit. "
            "No fixtures were generated. "
            + " ".join(error.messages)
        ) from error


def _build_fresh_first_round_plan(competition, teams, *, shuffle=None):
    draw = list(teams)
    (shuffle or SystemRandom().shuffle)(draw)
    gameweeks = tuple(range(1, round_metrics(len(teams))["gameweeks"] + 1))
    scheduled = defaultdict(list)
    for gameweek, _index, first, second in _circle_pairings(draw):
        scheduled[gameweek].append(_pair(first.id, second.id))
    if len(teams) > 10 and len(teams) % 2 == 0:
        fixtures = _circle_large_first_round_orientations(
            draw, tuple(team.id for team in teams), gameweeks
        )
    else:
        fixtures = _orient_first_round(
            tuple(team.id for team in teams), {}, scheduled, gameweeks
        )
    balance = _entries_balance(
        tuple(team.id for team in teams),
        [(fixture.gameweek, fixture.home_team_id, fixture.away_team_id) for fixture in fixtures],
    )
    _assert_max_two_streaks(
        balance, team_names={team.id: team.name for team in teams}
    )
    plan = FixturePlan(
        action="first",
        competition_id=competition.id,
        team_ids=tuple(team.id for team in teams),
        expected_gameweeks=gameweeks,
        fixtures=tuple(fixtures),
        balance=balance,
    )
    _assert_first_round_leaves_compliant_second_round(competition, teams, plan.fixtures)
    return plan


def _build_remaining_first_round_plan(competition, teams, matches):
    existing_pairings = _validate_first_round_history(competition, teams, matches)
    metrics = round_metrics(len(teams))
    gameweeks = tuple(range(1, metrics["gameweeks"] + 1))
    expected_pairings = {
        _pair(first.id, second.id)
        for first, second in combinations(teams, 2)
    }
    remaining_pairings = expected_pairings - existing_pairings
    if not remaining_pairings:
        raise ValidationError("The first round is already complete.")
    fixed_by_gameweek = defaultdict(list)
    for match in matches:
        fixed_by_gameweek[match.gameweek].append(match)
    # A fixed opening can make the first deterministic factorisation
    # impossible to orient without a third identical venue, even though a
    # different ordering of the still-open pairings is compliant.  Try the
    # same small, bounded set of ordering variants for every table size; the
    # manual rows remain immutable in all variants.
    variants = range(LARGE_CONTINUATION_SCHEDULE_VARIANTS)
    orientation_errors = []
    fixtures = None
    for variant in variants:
        try:
            scheduled = _schedule_remaining_pairings(
                tuple(team.id for team in teams),
                fixed_by_gameweek,
                remaining_pairings,
                gameweeks,
                schedule_variant=variant,
            )
            fixtures = _orient_first_round(
                tuple(team.id for team in teams), fixed_by_gameweek, scheduled, gameweeks
            )
        except ValidationError as error:
            orientation_errors.append(error)
            continue
        break
    if fixtures is None:
        if orientation_errors:
            raise orientation_errors[-1]
        raise ValidationError(
            "No strict home/away continuation schedule could be constructed. "
            "No fixtures were generated."
        )
    all_entries = [
        (match.gameweek, match.home_team_id, match.away_team_id) for match in matches
    ] + [
        (fixture.gameweek, fixture.home_team_id, fixture.away_team_id) for fixture in fixtures
    ]
    balance = _entries_balance(tuple(team.id for team in teams), all_entries)
    _assert_max_two_streaks(
        balance, team_names={team.id: team.name for team in teams}
    )
    plan = FixturePlan(
        action="remaining_first",
        competition_id=competition.id,
        team_ids=tuple(team.id for team in teams),
        expected_gameweeks=gameweeks,
        fixtures=tuple(fixtures),
        balance=balance,
    )
    _assert_first_round_leaves_compliant_second_round(
        competition, teams, [*matches, *plan.fixtures]
    )
    return plan


def _build_second_round_plan(competition, teams, first_matches, second_matches):
    if second_matches:
        raise ValidationError(
            "A partially entered second round cannot be generated automatically. "
            "Preserve it and complete it manually until second-round continuation is configured."
        )
    first_gameweeks = _validate_complete_first_round(competition, teams, first_matches)
    metrics = round_metrics(len(teams))
    offset = max(first_gameweeks)
    gameweeks = tuple(range(offset + 1, offset + metrics["gameweeks"] + 1))
    reverse_orientations = {
        _pair(match.home_team_id, match.away_team_id): (match.away_team_id, match.home_team_id)
        for match in first_matches
    }
    generated_schedule = _schedule_remaining_pairings(
        tuple(team.id for team in teams), {}, set(reverse_orientations), gameweeks
    )
    candidates = [
        _order_second_round_matchings(
            generated_schedule, gameweeks, first_matches, reverse_orientations
        )
    ]
    # Mirroring the first-round matching order is another valid one-factorisation
    # and is often the best way to avoid a venue streak across the round boundary.
    mirrored_schedule = defaultdict(list)
    for match in first_matches:
        mirrored_schedule[offset + match.gameweek].append(
            _pair(match.home_team_id, match.away_team_id)
        )
    candidates.append(
        _order_second_round_matchings(
            mirrored_schedule, gameweeks, first_matches, reverse_orientations
        )
    )

    # A different circle draw yields a different valid one-factorisation of
    # the same reverse fixtures. For small tables, inspect a compact,
    # deterministic set of those alternatives before settling on a schedule.
    # This is what lets the generator honour the two-match venue-streak
    # preference when merely reordering one factorisation cannot do so. Large
    # leagues retain the bounded primary candidate above.
    if len(teams) <= 10:
        draw = list(teams)
        seen_draws = {tuple(team.id for team in draw)}
        inspected = 0
        for left in range(1, len(draw) - 1):
            for right in range(left + 1, len(draw)):
                alternative_draw = list(draw)
                alternative_draw[left], alternative_draw[right] = (
                    alternative_draw[right],
                    alternative_draw[left],
                )
                signature = tuple(team.id for team in alternative_draw)
                if signature in seen_draws:
                    continue
                seen_draws.add(signature)
                alternative = defaultdict(list)
                for round_number, _index, first, second in _circle_pairings(alternative_draw):
                    alternative[gameweeks[round_number - 1]].append(
                        _pair(first.id, second.id)
                    )
                candidates.append(
                    _order_second_round_matchings(
                        alternative, gameweeks, first_matches, reverse_orientations
                    )
                )
                inspected += 1
                if inspected >= 16:
                    break
            if inspected >= 16:
                break

    def schedule_balance(schedule):
        entries = [
            (match.gameweek, match.home_team_id, match.away_team_id)
            for match in first_matches
        ]
        entries.extend(
            (gameweek, *reverse_orientations[pairing])
            for gameweek in gameweeks
            for pairing in schedule[gameweek]
        )
        return _entries_balance(tuple(team.id for team in teams), entries)

    scheduled = min(
        candidates,
        key=lambda schedule: _balance_objective(schedule_balance(schedule)),
    )
    fixtures = []
    for gameweek in gameweeks:
        for pairing in sorted(scheduled[gameweek], key=_pair_members):
            home_id, away_id = reverse_orientations[pairing]
            fixtures.append(PlannedFixture(gameweek, home_id, away_id, 2))
    entries = [
        (match.gameweek, match.home_team_id, match.away_team_id) for match in first_matches
    ] + [
        (fixture.gameweek, fixture.home_team_id, fixture.away_team_id) for fixture in fixtures
    ]
    balance = _entries_balance(tuple(team.id for team in teams), entries)
    _assert_max_two_streaks(
        balance, team_names={team.id: team.name for team in teams}
    )
    return FixturePlan(
        action="second",
        competition_id=competition.id,
        team_ids=tuple(team.id for team in teams),
        expected_gameweeks=gameweeks,
        fixtures=tuple(fixtures),
        balance=balance,
    )


def _all_matches(competition, *, lock=False):
    # A replay is a resolution of an existing fixture, not a new round-robin
    # pairing.  The automatic scheduler must neither reject it as a duplicate
    # nor use it when calculating future gameweek balance.
    queryset = Match.objects.filter(
        competition=competition,
        replay_of__isnull=True,
    ).select_related(
        "stage", "home_team", "away_team"
    ).order_by("gameweek", "id")
    if lock:
        queryset = queryset.select_for_update()
    return list(queryset)


def _build_plan(competition, action, *, teams=None, matches=None, shuffle=None, lock=False):
    _validate_competition(competition)
    teams = teams if teams is not None else _teams_for_generation(competition, lock=lock)
    _table_stage(competition)
    matches = matches if matches is not None else _all_matches(competition, lock=lock)

    if action == "first":
        if matches:
            raise ValidationError(
                "Automatic first-round generation is unavailable because this league already has fixtures."
            )
        if _has_baseline_standings_without_matches(competition):
            raise ValidationError(
                "Previous matchups must be entered before fixtures can be safely generated; "
                "standings alone do not identify opponents."
            )
        return _build_fresh_first_round_plan(competition, teams, shuffle=shuffle)

    first_matches = [match for match in matches if match.leg == 1]
    second_matches = [match for match in matches if match.leg == 2]
    invalid_legs = [match for match in matches if match.leg not in {1, 2}]
    if invalid_legs:
        raise ValidationError("Fixtures must use leg 1 or leg 2 before automatic generation.")

    if action == "remaining_first":
        if second_matches:
            raise ValidationError(
                "First-round continuation is unavailable after second-round fixtures exist."
            )
        if not first_matches:
            if _has_baseline_standings_without_matches(competition):
                raise ValidationError(
                    "Previous matchups must be entered before remaining fixtures can be safely generated; "
                    "standings alone do not identify opponents."
                )
            raise ValidationError("There are no historical fixtures to continue from.")
        return _build_remaining_first_round_plan(competition, teams, first_matches)

    if action == "second":
        if not first_matches:
            raise ValidationError("Generate a complete first round before generating the second round.")
        return _build_second_round_plan(competition, teams, first_matches, second_matches)

    raise ValidationError("Unknown fixture-generation action.")


def _fixture_fingerprint(competition, teams, matches):
    rows = [
        (
            match.id,
            match.stage_id,
            match.group_id,
            match.home_team_id,
            match.away_team_id,
            match.gameweek,
            match.leg,
            match.status,
        )
        for match in matches
    ]
    payload = repr((
        competition.id,
        competition.is_archived,
        competition.schedule_locked_at.isoformat() if competition.schedule_locked_at else None,
        tuple(team.id for team in teams),
        rows,
    ))
    return sha256(payload.encode("utf-8")).hexdigest()


def _plan_payload(plan, fingerprint):
    return {
        "competition_id": plan.competition_id,
        "action": plan.action,
        "team_ids": list(plan.team_ids),
        "expected_gameweeks": list(plan.expected_gameweeks),
        "fixtures": [
            [fixture.gameweek, fixture.home_team_id, fixture.away_team_id, fixture.leg]
            for fixture in plan.fixtures
        ],
        "fingerprint": fingerprint,
    }


def build_generation_preview(competition, action):
    """Build a signed, non-mutating plan for manager confirmation."""

    teams = _teams_for_generation(competition)
    matches = _all_matches(competition)
    plan = _build_plan(competition, action, teams=teams, matches=matches)
    payload = _plan_payload(plan, _fixture_fingerprint(competition, teams, matches))
    team_names = {team.id: team.name for team in teams}
    existing_by_gameweek = defaultdict(int)
    for match in matches:
        existing_by_gameweek[match.gameweek] += 1
    proposed_by_gameweek = defaultdict(int)
    for fixture in plan.fixtures:
        proposed_by_gameweek[fixture.gameweek] += 1
    return {
        "token": signing.dumps(payload, compress=True),
        "action": action,
        "competition": competition,
        "team_names": team_names,
        "teams": list(teams),
        "existing": matches,
        "existing_fixture_count": len(matches),
        "existing_by_gameweek": dict(existing_by_gameweek),
        "proposed": plan.fixtures,
        "proposed_by_gameweek": dict(proposed_by_gameweek),
        "expected_gameweeks": plan.expected_gameweeks,
        "metrics": round_metrics(len(teams)),
        "balance": plan.balance,
    }


def _save_plan(
    competition, payload, teams, matches, *, first_round_lookahead_checked=False
):
    if payload.get("competition_id") != competition.id:
        raise ValidationError("This fixture preview belongs to another competition.")
    if payload.get("fingerprint") != _fixture_fingerprint(competition, teams, matches):
        raise ValidationError(
            "The fixture preview is stale because teams or fixtures changed. Create a new preview."
        )
    if tuple(payload.get("team_ids", ())) != tuple(team.id for team in teams):
        raise ValidationError("The fixture preview no longer matches the competition teams.")

    stage = _table_stage(competition)
    team_ids = {team.id for team in teams}
    grounds = {team.id: team.home_ground for team in teams}
    proposed = []
    for gameweek, home_id, away_id, leg in payload.get("fixtures", ()):
        if home_id == away_id or home_id not in team_ids or away_id not in team_ids:
            raise ValidationError("The fixture preview contains an invalid team assignment.")
        proposed.append(
            Match(
                competition=competition,
                stage=stage,
                home_team_id=home_id,
                away_team_id=away_id,
                gameweek=gameweek,
                match_date=None,
                location=grounds[home_id] or "",
                status="UPCOMING",
                phase="NONE",
                minute=0,
                leg=leg,
                generated_by_system=True,
            )
        )
    if not proposed:
        raise ValidationError("The fixture preview contains no fixtures to save.")
    _assert_max_two_streaks(
        _entries_balance(
            tuple(team.id for team in teams),
            [
                (match.gameweek, match.home_team_id, match.away_team_id)
                for match in matches
            ]
            + [
                (match.gameweek, match.home_team_id, match.away_team_id)
                for match in proposed
            ],
        ),
        team_names={team.id: team.name for team in teams},
    )
    if (
        payload.get("action") in {"first", "remaining_first"}
        and not first_round_lookahead_checked
    ):
        _assert_first_round_leaves_compliant_second_round(
            competition, teams, [*matches, *proposed]
        )
    Match.objects.bulk_create(proposed)
    return len(proposed)


def confirm_generation_preview(competition, token, *, action=None):
    """Atomically revalidate a signed preview and persist only its proposals."""

    try:
        payload = signing.loads(token, max_age=PREVIEW_MAX_AGE_SECONDS)
    except signing.BadSignature as error:
        raise ValidationError("The fixture preview is invalid or has expired. Create a new preview.") from error

    if action is not None and payload.get("action") != action:
        raise ValidationError(
            "This fixture preview does not match the requested generation action. "
            "Create a new preview."
        )

    with transaction.atomic():
        competition = type(competition).objects.select_for_update().get(pk=competition.pk)
        _validate_competition(competition)
        teams = _teams_for_generation(competition, lock=True)
        matches = _all_matches(competition, lock=True)
        return _save_plan(competition, payload, teams, matches)


def _save_generated_plan(competition, action, *, shuffle=None):
    with transaction.atomic():
        competition = type(competition).objects.select_for_update().get(pk=competition.pk)
        teams = _teams_for_generation(competition, lock=True)
        matches = _all_matches(competition, lock=True)
        plan = _build_plan(
            competition,
            action,
            teams=teams,
            matches=matches,
            shuffle=shuffle,
            lock=True,
        )
        return _save_plan(
            competition,
            _plan_payload(plan, _fixture_fingerprint(competition, teams, matches)),
            teams,
            matches,
            first_round_lookahead_checked=True,
        )


def generate_first_round(competition, *, shuffle=None):
    """Create a complete first round for a competition with no fixtures."""

    return _save_generated_plan(competition, "first", shuffle=shuffle)


def generate_remaining_first_round(competition, *, start_gameweek=None, shuffle=None):
    """Complete fixed first-round history without creating sparse gameweeks."""

    if start_gameweek is not None:
        raise ValidationError(
            "Choosing a later starting gameweek is no longer supported; the scheduler "
            "must complete the existing first-round gameweeks."
        )
    return _save_generated_plan(competition, "remaining_first", shuffle=shuffle)


def generate_second_round(competition):
    """Create a complete reverse second round after a complete first round."""

    return _save_generated_plan(competition, "second")


def round_generation_state(competition):
    """Return only actions that can produce a complete, safe round."""

    state = {
        "can_generate_first_round": False,
        "can_generate_remaining_first_round": False,
        "can_generate_second_round": False,
    }
    try:
        _validate_competition(competition)
        teams = _teams_for_generation(competition)
        _table_stage(competition)
    except ValidationError:
        return state

    matches = _all_matches(competition)
    if not matches:
        if not _has_baseline_standings_without_matches(competition):
            state["can_generate_first_round"] = True
        return state

    first_matches = [match for match in matches if match.leg == 1]
    second_matches = [match for match in matches if match.leg == 2]
    if len(first_matches) + len(second_matches) != len(matches) or second_matches:
        return state
    try:
        pairings = _validate_first_round_history(competition, teams, first_matches)
    except ValidationError:
        return state

    if len(pairings) < round_metrics(len(teams))["fixtures"]:
        state["can_generate_remaining_first_round"] = True
        return state
    try:
        _validate_complete_first_round(competition, teams, first_matches)
    except ValidationError:
        return state
    state["can_generate_second_round"] = True
    return state
