"""Safe round-robin fixture planning for Pool league and group tables.

The manager layer deliberately stays out of this module.  It validates the
selected table, asks for a preview, signs that preview, and later confirms the
same signed plan.  Confirmation is serialised through the competition row and
creates each :class:`~pool.models.PoolMatch` through ``save()`` so normal model
validation remains authoritative.

This module is intentionally conservative.  A baseline with historical totals
or any manually-created fixture makes the played-pairing history unknowable, so
automatic generation is refused rather than risking an incorrect schedule.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolGroup,
    PoolGroupEntry,
    PoolLeagueAuditEvent,
    PoolMatch,
    PoolStandingBaseline,
)


PREVIEW_VERSION = 1
"""Schema version for signed league-fixture preview tokens."""

PREVIEW_SALT = "pool.league-fixtures.preview.v1"
"""Dedicated signing salt; tokens cannot be confused with other Pool data."""

DEFAULT_PREVIEW_MAX_AGE_SECONDS = 30 * 60
"""The default period for which a manager can confirm a preview."""


class LeagueFixtureValidationError(ValidationError):
    """A user-facing failure while planning or confirming league fixtures.

    Views may catch this subclass to show a clear manager message without
    accidentally treating an unsafe schedule as a generic server error.
    """


@dataclass(frozen=True)
class LeagueFixtureScope:
    """One exact league table: a league stage or one group within a group stage."""

    competition: PoolCompetition
    stage: PoolCompetitionStage
    group: PoolGroup | None = None

    @property
    def competition_id(self) -> int:
        return self.competition.pk

    @property
    def stage_id(self) -> int:
        return self.stage.pk

    @property
    def group_id(self) -> int | None:
        return self.group.pk if self.group else None

    @property
    def label(self) -> str:
        """Human-facing table name suitable for manager messages."""

        return self.group.name if self.group else self.stage.name

    def as_dict(self) -> dict[str, int | None]:
        """Return the stable, JSON-safe scope identity used in preview tokens."""

        return {
            "competition_id": self.competition_id,
            "stage_id": self.stage_id,
            "group_id": self.group_id,
        }


@dataclass(frozen=True)
class PlannedLeagueFixture:
    """One unsaved fixture in a round-robin preview."""

    gameweek: int
    home_entry_id: int
    away_entry_id: int

    def as_dict(self) -> dict[str, int]:
        """Return a JSON-safe fixture row."""

        return {
            "gameweek": self.gameweek,
            "home_entry_id": self.home_entry_id,
            "away_entry_id": self.away_entry_id,
        }


@dataclass(frozen=True)
class PlannedLeagueGameweek:
    """A display-friendly gameweek containing its planned fixtures."""

    number: int
    fixtures: tuple[PlannedLeagueFixture, ...]


@dataclass(frozen=True)
class LeagueFixturePreview:
    """An unsigned in-memory plan which can be signed for later confirmation."""

    scope: LeagueFixtureScope
    leg: int
    entry_ids: tuple[int, ...]
    fixtures: tuple[PlannedLeagueFixture, ...]

    @property
    def fixture_count(self) -> int:
        """Number of matches represented by this preview."""

        return len(self.fixtures)

    @property
    def gameweek_count(self) -> int:
        """Number of gameweeks represented by this preview."""

        return len(self.gameweeks)

    @property
    def gameweeks(self) -> tuple[PlannedLeagueGameweek, ...]:
        """Group preview fixtures in display order without adding database work."""

        grouped: dict[int, list[PlannedLeagueFixture]] = defaultdict(list)
        for fixture in self.fixtures:
            grouped[fixture.gameweek].append(fixture)
        return tuple(
            PlannedLeagueGameweek(
                number=gameweek,
                fixtures=tuple(
                    sorted(
                        grouped[gameweek],
                        key=lambda fixture: (
                            fixture.home_entry_id,
                            fixture.away_entry_id,
                        ),
                    )
                ),
            )
            for gameweek in sorted(grouped)
        )

    def as_dict(self) -> dict[str, object]:
        """Return a template/API-friendly representation of this preview."""

        return {
            "scope": self.scope.as_dict(),
            "leg": self.leg,
            "entry_ids": list(self.entry_ids),
            "fixture_count": self.fixture_count,
            "gameweek_count": self.gameweek_count,
            "fixtures": [fixture.as_dict() for fixture in self.fixtures],
            "gameweeks": [
                {
                    "gameweek": gameweek.number,
                    "fixtures": [fixture.as_dict() for fixture in gameweek.fixtures],
                }
                for gameweek in self.gameweeks
            ],
        }

    def token_payload(self) -> dict[str, object]:
        """Return only the stable JSON data that must be protected by signing."""

        return {
            "version": PREVIEW_VERSION,
            "scope": self.scope.as_dict(),
            "leg": self.leg,
            # Keep this canonical so confirmation can reliably detect entry
            # additions/removals regardless of preview shuffle order.
            "entry_ids": sorted(self.entry_ids),
            "fixtures": [fixture.as_dict() for fixture in self.fixtures],
        }


def _error(message: str | Sequence[str]) -> LeagueFixtureValidationError:
    """Build the one service-specific validation exception consistently."""

    return LeagueFixtureValidationError(message)


def _positive_int(value, *, label: str) -> int:
    """Return a strict positive integer, rejecting bools and malformed tokens."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _error(f"The fixture preview contains an invalid {label}.")
    return value


def _saved_instance(value, model, *, label: str):
    """Require a persisted model instance before accessing its table scope."""

    if not isinstance(value, model) or not value.pk:
        raise _error(f"A saved {label} is required.")
    return value


def _scope_from_objects(
    competition: PoolCompetition,
    stage: PoolCompetitionStage,
    group: PoolGroup | None,
) -> LeagueFixtureScope:
    """Validate already-loaded scope objects without issuing another query."""

    _saved_instance(competition, PoolCompetition, label="competition")
    _saved_instance(stage, PoolCompetitionStage, label="stage")
    if group is not None:
        _saved_instance(group, PoolGroup, label="group")

    if stage.competition_id != competition.pk:
        raise _error("The selected league stage does not belong to this competition.")
    if competition.format_type == PoolCompetition.FormatType.KNOCKOUT:
        raise _error("Automatic fixtures are unavailable for knockout competitions.")
    if stage.stage_type not in {
        PoolCompetitionStage.StageType.LEAGUE,
        PoolCompetitionStage.StageType.GROUP,
    }:
        raise _error("Automatic fixtures require a league or group-table stage.")

    if stage.stage_type == PoolCompetitionStage.StageType.GROUP:
        if group is None:
            raise _error("Select a group table before generating fixtures.")
        if group.stage_id != stage.pk:
            raise _error("The selected group does not belong to this stage.")
    elif group is not None:
        raise _error("A league stage does not use a group table.")

    return LeagueFixtureScope(competition=competition, stage=stage, group=group)


def get_league_fixture_scope(
    competition: PoolCompetition,
    stage: PoolCompetitionStage,
    group: PoolGroup | None = None,
) -> LeagueFixtureScope:
    """Load and validate one scheduler scope from manager-selected objects.

    A group stage always needs an explicit group.  A normal league stage must
    not receive one.  The returned objects are freshly read so an old manager
    page cannot plan against a stage that has changed since it was rendered.
    """

    competition = _saved_instance(competition, PoolCompetition, label="competition")
    stage = _saved_instance(stage, PoolCompetitionStage, label="stage")
    if group is not None:
        group = _saved_instance(group, PoolGroup, label="group")

    try:
        current_competition = PoolCompetition.objects.get(pk=competition.pk)
        current_stage = PoolCompetitionStage.objects.get(pk=stage.pk)
        current_group = (
            PoolGroup.objects.get(pk=group.pk) if group is not None else None
        )
    except (PoolCompetition.DoesNotExist, PoolCompetitionStage.DoesNotExist, PoolGroup.DoesNotExist) as exc:
        raise _error("This league table no longer exists. Reload the page and try again.") from exc

    return _scope_from_objects(current_competition, current_stage, current_group)


# A descriptive alias makes view code read naturally while retaining one
# implementation for external callers and tests.
validate_league_fixture_scope = get_league_fixture_scope


def get_league_fixture_entries(scope: LeagueFixtureScope) -> list[PoolCompetitionEntry]:
    """Return the exact entrants that belong to a fixture-generation table.

    League stages use all competition entrants.  Group stages use only the
    explicit memberships in the selected group, preserving their configured
    sort order for an understandable preview before the order is shuffled.
    """

    if not isinstance(scope, LeagueFixtureScope):
        raise _error("A validated league fixture scope is required.")

    if scope.group_id:
        return list(
            PoolCompetitionEntry.objects.filter(
                group_memberships__stage_id=scope.stage_id,
                group_memberships__group_id=scope.group_id,
            )
            .select_related("player", "team")
            .order_by("group_memberships__sort_order", "seed", "id")
        )
    return list(
        PoolCompetitionEntry.objects.filter(competition_id=scope.competition_id)
        .select_related("player", "team")
        .order_by("seed", "id")
    )


# Short aliases keep integration code readable and make the retrieval contract
# discoverable without duplicating data-access logic.
get_scope_entries = get_league_fixture_entries
league_fixture_entries = get_league_fixture_entries


def get_league_fixture_scope_and_entries(
    competition: PoolCompetition,
    stage: PoolCompetitionStage,
    group: PoolGroup | None = None,
) -> tuple[LeagueFixtureScope, list[PoolCompetitionEntry]]:
    """Validate a table selection and return its current eligible entrants."""

    scope = get_league_fixture_scope(competition, stage, group)
    return scope, get_league_fixture_entries(scope)


def _scope_matches(scope: LeagueFixtureScope):
    """Return base-table-only matches in exactly one league/group table."""

    queryset = PoolMatch.objects.filter(
        competition_id=scope.competition_id,
        stage_id=scope.stage_id,
    )
    if scope.group_id:
        return queryset.filter(group_id=scope.group_id)
    return queryset.filter(group__isnull=True)


def _scope_baselines(scope: LeagueFixtureScope):
    """Return baseline rows belonging to exactly one scheduling table."""

    queryset = PoolStandingBaseline.objects.filter(
        competition_id=scope.competition_id,
        stage_id=scope.stage_id,
    )
    if scope.group_id:
        return queryset.filter(group_id=scope.group_id)
    return queryset.filter(group__isnull=True)


def _round_robin_gameweek_count(entrant_count: int) -> int:
    """Return the number of first-leg weeks, including the odd-entry bye week."""

    if entrant_count < 2:
        return 0
    # With an odd number of entrants, the synthetic bye creates an even-sized
    # circle.  Every real entrant therefore has one bye across *n* gameweeks.
    return entrant_count if entrant_count % 2 else entrant_count - 1


def _fixture_values(queryset) -> list[dict[str, object]]:
    """Materialise only fields needed to prove a persisted schedule is safe."""

    return list(
        queryset.order_by("gameweek", "pk").values(
            "id",
            "home_entry_id",
            "away_entry_id",
            "gameweek",
            "league_leg",
            "is_generated_league_fixture",
            "status",
        )
    )


def _round_robin_issues(
    rows: Iterable[Mapping[str, object]],
    entry_ids: Iterable[int],
    *,
    expected_gameweeks: int,
) -> list[str]:
    """Return structural defects in one full single-leg schedule.

    It is intentionally indifferent to home/away orientation.  Orientation is
    checked separately for a second round against the saved first round.
    """

    rows = list(rows)
    entry_id_set = set(entry_ids)
    entrant_count = len(entry_id_set)
    expected_fixture_count = entrant_count * (entrant_count - 1) // 2
    expected_per_gameweek = entrant_count // 2
    issues: list[str] = []

    if entrant_count < 2:
        issues.append("At least two entrants are required.")
        return issues
    if len(rows) != expected_fixture_count:
        issues.append(
            f"Expected {expected_fixture_count} fixtures but found {len(rows)}."
        )

    rows_by_gameweek: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    pairings: set[tuple[int, int]] = set()
    for row in rows:
        gameweek = row.get("gameweek")
        home_entry_id = row.get("home_entry_id")
        away_entry_id = row.get("away_entry_id")
        if (
            isinstance(gameweek, bool)
            or not isinstance(gameweek, int)
            or gameweek < 1
        ):
            issues.append("Every generated fixture must have a positive gameweek.")
            continue
        if home_entry_id not in entry_id_set or away_entry_id not in entry_id_set:
            issues.append("A fixture references an entrant outside this table.")
            continue
        if home_entry_id == away_entry_id:
            issues.append("A fixture pairs an entrant with itself.")
            continue
        rows_by_gameweek[gameweek].append(row)
        pairing = tuple(sorted((home_entry_id, away_entry_id)))
        if pairing in pairings:
            issues.append("A pairing appears more than once in the same league leg.")
        pairings.add(pairing)

    actual_gameweeks = set(rows_by_gameweek)
    expected_gameweek_numbers = set(range(1, expected_gameweeks + 1))
    if actual_gameweeks != expected_gameweek_numbers:
        issues.append(
            "Generated gameweeks must run consecutively from Gameweek 1."
        )

    for gameweek, gameweek_rows in rows_by_gameweek.items():
        if len(gameweek_rows) != expected_per_gameweek:
            issues.append(
                f"Gameweek {gameweek} has an unexpected number of fixtures."
            )
        seen_this_gameweek: set[int] = set()
        for row in gameweek_rows:
            for entry_id in (row.get("home_entry_id"), row.get("away_entry_id")):
                if entry_id in seen_this_gameweek:
                    issues.append(
                        f"An entrant appears more than once in Gameweek {gameweek}."
                    )
                if isinstance(entry_id, int):
                    seen_this_gameweek.add(entry_id)

    if len(pairings) != expected_fixture_count:
        issues.append("The schedule does not contain every unique entrant pairing once.")

    # Repeating a message does not help a manager diagnose an invalid historic
    # schedule, especially when a bulk import created many bad rows.
    return list(dict.fromkeys(issues))


def _first_round_rows(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return first-leg rows from a materialised scope fixture list."""

    return [
        dict(row)
        for row in rows
        if row.get("is_generated_league_fixture") and row.get("league_leg") == 1
    ]


def _second_round_rows(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return second-leg rows from a materialised scope fixture list."""

    return [
        dict(row)
        for row in rows
        if row.get("is_generated_league_fixture") and row.get("league_leg") == 2
    ]


def _canonical_fixture_tuples(
    fixtures: Iterable[PlannedLeagueFixture | Mapping[str, object]],
) -> tuple[tuple[int, int, int], ...]:
    """Canonicalise a schedule for exact, order-independent comparison."""

    canonical = []
    for fixture in fixtures:
        if isinstance(fixture, PlannedLeagueFixture):
            canonical.append(
                (fixture.gameweek, fixture.home_entry_id, fixture.away_entry_id)
            )
        else:
            canonical.append(
                (
                    fixture.get("gameweek"),
                    fixture.get("home_entry_id"),
                    fixture.get("away_entry_id"),
                )
            )
    # ``repr`` keeps this diagnostic helper safe even for a malformed legacy
    # row containing (for example) ``None`` alongside integers.  A normal
    # schedule still compares as the same tuple values; malformed data simply
    # compares unequal and is reported as unsafe rather than raising TypeError.
    return tuple(sorted(canonical, key=repr))


def _expected_second_round(
    first_round_rows: Iterable[Mapping[str, object]],
    *,
    gameweek_offset: int,
) -> tuple[PlannedLeagueFixture, ...]:
    """Reverse every saved first-leg fixture at the matching later gameweek."""

    fixtures = [
        PlannedLeagueFixture(
            gameweek=int(row["gameweek"]) + gameweek_offset,
            home_entry_id=int(row["away_entry_id"]),
            away_entry_id=int(row["home_entry_id"]),
        )
        for row in first_round_rows
    ]
    return tuple(
        sorted(
            fixtures,
            key=lambda fixture: (
                fixture.gameweek,
                fixture.home_entry_id,
                fixture.away_entry_id,
            ),
        )
    )


def _baseline_summary(scope: LeagueFixtureScope) -> tuple[int, int]:
    """Return ``(record_count, non_zero_record_count)`` for one table."""

    rows = _scope_baselines(scope).values(
        "played",
        "won",
        "lost",
        "racks_for",
        "racks_against",
        "points",
    )
    record_count = 0
    historical_count = 0
    for row in rows:
        record_count += 1
        if any(row[field] for field in row):
            historical_count += 1
    return record_count, historical_count


def league_fixture_status(scope: LeagueFixtureScope) -> dict[str, object]:
    """Summarise generation state and all reasons a new preview is blocked.

    The result deliberately exposes plain values suitable for templates or a
    small JSON manager endpoint.  ``can_generate_*`` is advisory only; the
    same checks are repeated while confirmation holds database row locks.
    """

    if not isinstance(scope, LeagueFixtureScope):
        raise _error("A validated league fixture scope is required.")

    entries = get_league_fixture_entries(scope)
    entry_ids = [entry.pk for entry in entries]
    entrant_count = len(entry_ids)
    expected_gameweeks = _round_robin_gameweek_count(entrant_count)
    expected_fixture_count = entrant_count * (entrant_count - 1) // 2
    rows = _fixture_values(_scope_matches(scope))
    first_rows = _first_round_rows(rows)
    second_rows = _second_round_rows(rows)
    manual_rows = [row for row in rows if not row["is_generated_league_fixture"]]
    malformed_generated_rows = [
        row
        for row in rows
        if row["is_generated_league_fixture"] and row["league_leg"] not in {1, 2}
    ]
    first_issues = (
        _round_robin_issues(
            first_rows,
            entry_ids,
            expected_gameweeks=expected_gameweeks,
        )
        if first_rows
        else []
    )

    second_issues: list[str] = []
    if second_rows:
        if not first_rows or first_issues:
            second_issues.append(
                "Second-round fixtures exist without a valid generated first round."
            )
        else:
            # The generic validator works with a one-based leg.  Shift the
            # persisted second-leg numbers back temporarily, then separately
            # prove their real numbering continues after the first leg.
            shifted_second_rows = [
                {
                    **row,
                    "gameweek": (
                        row["gameweek"] - expected_gameweeks
                        if isinstance(row["gameweek"], int)
                        and not isinstance(row["gameweek"], bool)
                        else row["gameweek"]
                    ),
                }
                for row in second_rows
            ]
            second_issues.extend(
                _round_robin_issues(
                    shifted_second_rows,
                    entry_ids,
                    expected_gameweeks=expected_gameweeks,
                )
            )
            expected_second = _expected_second_round(
                first_rows,
                gameweek_offset=expected_gameweeks,
            )
            if _canonical_fixture_tuples(second_rows) != _canonical_fixture_tuples(
                expected_second
            ):
                second_issues.append(
                    "Second-round fixtures do not exactly reverse the saved first round."
                )
            expected_second_gameweeks = set(
                range(expected_gameweeks + 1, expected_gameweeks * 2 + 1)
            )
            actual_second_gameweeks = {
                row["gameweek"]
                for row in second_rows
                if isinstance(row["gameweek"], int)
            }
            if actual_second_gameweeks != expected_second_gameweeks:
                second_issues.append(
                    "Second-round gameweeks do not continue after the first round."
                )
    second_issues = list(dict.fromkeys(second_issues))

    baseline_record_count, historical_baseline_count = _baseline_summary(scope)
    common_blockers: list[str] = []
    if entrant_count < 2:
        common_blockers.append("At least two entrants are required before fixtures can be generated.")
    if manual_rows:
        common_blockers.append(
            "Automatic generation is blocked because this table already has manually created fixtures."
        )
    if malformed_generated_rows:
        common_blockers.append(
            "Automatic generation is blocked because this table has incomplete generated-fixture metadata."
        )
    if historical_baseline_count:
        common_blockers.append(
            "Automatic generation is blocked because imported baseline standings do not identify historical pairings. Enter remaining fixtures manually."
        )

    first_blockers = list(common_blockers)
    if first_rows:
        first_blockers.append("First-round fixtures have already been generated for this table.")
    if second_rows:
        first_blockers.append("Second-round fixtures already exist for this table.")
    if first_rows and first_issues:
        first_blockers.append("The existing generated first round is structurally invalid.")

    second_blockers = list(common_blockers)
    if not first_rows:
        second_blockers.append("Generate a valid first round before generating reverse fixtures.")
    elif first_issues:
        second_blockers.append("The generated first round is structurally invalid.")
    if second_rows:
        second_blockers.append("Second-round fixtures have already been generated for this table.")
    if second_rows and second_issues:
        second_blockers.append("The existing generated second round is structurally invalid.")

    first_generated = bool(first_rows) and not first_issues
    second_generated = bool(second_rows) and not second_issues
    result: dict[str, object] = {
        "scope": scope.as_dict(),
        "entrant_count": entrant_count,
        "entrant_ids": entry_ids,
        "first_round_gameweeks": expected_gameweeks,
        "first_round_matches": expected_fixture_count,
        "total_fixture_count": len(rows),
        "manual_fixture_count": len(manual_rows),
        "has_manual_fixtures": bool(manual_rows),
        "malformed_generated_fixture_count": len(malformed_generated_rows),
        "baseline_record_count": baseline_record_count,
        "historical_baseline_count": historical_baseline_count,
        "baseline_blocks_generation": bool(historical_baseline_count),
        "first_round": {
            "exists": bool(first_rows),
            "generated": first_generated,
            "match_count": len(first_rows),
            "expected_match_count": expected_fixture_count,
            "issues": first_issues,
        },
        "second_round": {
            "exists": bool(second_rows),
            "generated": second_generated,
            "match_count": len(second_rows),
            "expected_match_count": expected_fixture_count,
            "issues": second_issues,
        },
        "first_round_generated": first_generated,
        "second_round_generated": second_generated,
        "can_generate_first_round": not first_blockers,
        "can_generate_second_round": not second_blockers,
        "first_round_blocking_reasons": first_blockers,
        "second_round_blocking_reasons": second_blockers,
        "blocking_reasons": list(dict.fromkeys(first_blockers + second_blockers)),
    }
    return result


# Short, manager-oriented alias.
get_league_fixture_status = league_fixture_status


def _assert_plan_allowed(status: Mapping[str, object], *, leg: int) -> None:
    """Raise the relevant safety explanation before a preview is made/saved."""

    blockers_key = (
        "first_round_blocking_reasons" if leg == 1 else "second_round_blocking_reasons"
    )
    blockers = status.get(blockers_key, [])
    if blockers:
        raise _error(list(blockers))


def _circle_schedule(entry_ids: Sequence[int]) -> tuple[PlannedLeagueFixture, ...]:
    """Build a single-leg circle-method schedule from a shuffled entrant order."""

    positions: list[int | None] = list(entry_ids)
    if len(positions) % 2:
        positions.append(None)  # The empty position is the weekly bye.
    slot_count = len(positions)
    fixtures: list[PlannedLeagueFixture] = []

    for gameweek in range(1, slot_count):
        for index in range(slot_count // 2):
            left = positions[index]
            right = positions[-(index + 1)]
            if left is None or right is None:
                continue
            # Alternate the anchor pairing's orientation.  The remaining
            # orientation follows the shuffled circle; the second leg reverses
            # every one of these persisted first-leg fixtures exactly.
            if index == 0 and gameweek % 2 == 0:
                home_entry_id, away_entry_id = right, left
            else:
                home_entry_id, away_entry_id = left, right
            fixtures.append(
                PlannedLeagueFixture(
                    gameweek=gameweek,
                    home_entry_id=home_entry_id,
                    away_entry_id=away_entry_id,
                )
            )
        # Keep the first position fixed and rotate all remaining positions.
        positions = [positions[0], positions[-1], *positions[1:-1]]

    return tuple(fixtures)


def _validated_preview(
    *,
    scope: LeagueFixtureScope,
    leg: int,
    entry_ids: Iterable[int],
    fixtures: Iterable[PlannedLeagueFixture],
) -> LeagueFixturePreview:
    """Create a preview only when its JSON-level schedule is structurally sound."""

    if leg not in {1, 2}:
        raise _error("A fixture preview must be for league leg 1 or 2.")
    canonical_entry_ids = tuple(sorted(_positive_int(entry_id, label="entrant") for entry_id in entry_ids))
    if len(canonical_entry_ids) != len(set(canonical_entry_ids)):
        raise _error("A fixture preview cannot contain the same entrant twice.")
    if len(canonical_entry_ids) < 2:
        raise _error("At least two entrants are required before fixtures can be generated.")

    fixtures = tuple(fixtures)
    rows = [fixture.as_dict() for fixture in fixtures]
    expected_gameweeks = _round_robin_gameweek_count(len(canonical_entry_ids))
    if leg == 2:
        # A second-leg token is structurally a full round robin, but its
        # numbering starts after the first leg.  Shift only for the generic
        # shape check; exact reverse orientation is checked against the DB at
        # planning and confirmation time.
        shifted_rows = [
            {
                **row,
                "gameweek": row["gameweek"] - expected_gameweeks,
            }
            for row in rows
        ]
        issues = _round_robin_issues(
            shifted_rows,
            canonical_entry_ids,
            expected_gameweeks=expected_gameweeks,
        )
    else:
        issues = _round_robin_issues(
            rows,
            canonical_entry_ids,
            expected_gameweeks=expected_gameweeks,
        )
    if issues:
        raise _error(issues)
    return LeagueFixturePreview(
        scope=scope,
        leg=leg,
        entry_ids=canonical_entry_ids,
        fixtures=fixtures,
    )


def plan_first_round_preview(
    scope: LeagueFixtureScope,
    *,
    randomizer: Callable[[list[int]], object] | None = None,
) -> LeagueFixturePreview:
    """Create an unsaved, shuffled, single-leg round-robin preview.

    ``randomizer`` is an optional test hook.  It receives the mutable entrant
    list and may shuffle it in place (like :func:`random.shuffle`) or return a
    replacement order containing the same entrants.
    """

    if not isinstance(scope, LeagueFixtureScope):
        raise _error("A validated league fixture scope is required.")
    status = league_fixture_status(scope)
    _assert_plan_allowed(status, leg=1)
    entry_ids = [entry.pk for entry in get_league_fixture_entries(scope)]
    shuffled_ids = list(entry_ids)
    if randomizer is None:
        random.shuffle(shuffled_ids)
    else:
        replacement = randomizer(shuffled_ids)
        if replacement is not None:
            shuffled_ids = list(replacement)
        if sorted(shuffled_ids) != sorted(entry_ids):
            raise _error("The fixture randomizer changed the table entrants.")
    return _validated_preview(
        scope=scope,
        leg=1,
        entry_ids=entry_ids,
        fixtures=_circle_schedule(shuffled_ids),
    )


def plan_second_round_preview(scope: LeagueFixtureScope) -> LeagueFixturePreview:
    """Create an unsaved reverse-fixture preview from the exact first round."""

    if not isinstance(scope, LeagueFixtureScope):
        raise _error("A validated league fixture scope is required.")
    status = league_fixture_status(scope)
    _assert_plan_allowed(status, leg=2)
    entry_ids = [entry.pk for entry in get_league_fixture_entries(scope)]
    first_rows = _first_round_rows(_fixture_values(_scope_matches(scope)))
    return _validated_preview(
        scope=scope,
        leg=2,
        entry_ids=entry_ids,
        fixtures=_expected_second_round(
            first_rows,
            gameweek_offset=_round_robin_gameweek_count(len(entry_ids)),
        ),
    )


def serialize_fixture_preview(preview: LeagueFixturePreview) -> str:
    """Sign a fixture preview for one short-lived, tamper-evident confirmation."""

    if not isinstance(preview, LeagueFixturePreview):
        raise _error("A league fixture preview is required before it can be signed.")
    # Re-validate the in-memory object too.  This prevents a caller from using
    # dataclass construction to turn signing into an arbitrary fixture writer.
    _validated_preview(
        scope=preview.scope,
        leg=preview.leg,
        entry_ids=preview.entry_ids,
        fixtures=preview.fixtures,
    )
    return signing.dumps(preview.token_payload(), salt=PREVIEW_SALT, compress=True)


def _preview_max_age(max_age: int | None) -> int:
    """Return a valid configured/token-specific expiry period."""

    if max_age is None:
        max_age = getattr(
            settings,
            "POOL_LEAGUE_FIXTURE_PREVIEW_MAX_AGE_SECONDS",
            DEFAULT_PREVIEW_MAX_AGE_SECONDS,
        )
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 1:
        return DEFAULT_PREVIEW_MAX_AGE_SECONDS
    return max_age


def _preview_from_payload(payload: object) -> LeagueFixturePreview:
    """Strictly parse a signed JSON payload into a validated preview object."""

    if not isinstance(payload, Mapping) or payload.get("version") != PREVIEW_VERSION:
        raise _error("This fixture preview is not recognised. Generate a new preview.")
    scope_data = payload.get("scope")
    if not isinstance(scope_data, Mapping):
        raise _error("This fixture preview has no valid competition scope.")
    competition_id = _positive_int(scope_data.get("competition_id"), label="competition")
    stage_id = _positive_int(scope_data.get("stage_id"), label="stage")
    raw_group_id = scope_data.get("group_id")
    if raw_group_id is None:
        group_id = None
    else:
        group_id = _positive_int(raw_group_id, label="group")
    leg = payload.get("leg")
    if leg not in {1, 2}:
        raise _error("This fixture preview has an invalid league leg.")
    raw_entry_ids = payload.get("entry_ids")
    raw_fixtures = payload.get("fixtures")
    if not isinstance(raw_entry_ids, list) or not isinstance(raw_fixtures, list):
        raise _error("This fixture preview has an invalid schedule.")

    try:
        competition = PoolCompetition.objects.get(pk=competition_id)
        stage = PoolCompetitionStage.objects.get(pk=stage_id)
        group = PoolGroup.objects.get(pk=group_id) if group_id else None
    except (PoolCompetition.DoesNotExist, PoolCompetitionStage.DoesNotExist, PoolGroup.DoesNotExist) as exc:
        raise _error("This fixture preview refers to a table that no longer exists.") from exc
    scope = _scope_from_objects(competition, stage, group)

    fixtures: list[PlannedLeagueFixture] = []
    for raw_fixture in raw_fixtures:
        if not isinstance(raw_fixture, Mapping):
            raise _error("This fixture preview contains an invalid fixture row.")
        fixtures.append(
            PlannedLeagueFixture(
                gameweek=_positive_int(raw_fixture.get("gameweek"), label="gameweek"),
                home_entry_id=_positive_int(
                    raw_fixture.get("home_entry_id"), label="home entrant"
                ),
                away_entry_id=_positive_int(
                    raw_fixture.get("away_entry_id"), label="away entrant"
                ),
            )
        )
    return _validated_preview(
        scope=scope,
        leg=leg,
        entry_ids=raw_entry_ids,
        fixtures=fixtures,
    )


def deserialize_fixture_preview(
    token: str,
    *,
    max_age: int | None = None,
) -> LeagueFixturePreview:
    """Verify, expire-check, and parse a manager's signed fixture preview."""

    if not isinstance(token, str) or not token:
        raise _error("A fixture preview token is required.")
    try:
        payload = signing.loads(
            token,
            salt=PREVIEW_SALT,
            max_age=_preview_max_age(max_age),
        )
    except signing.SignatureExpired as exc:
        raise _error("This fixture preview has expired. Generate a fresh preview.") from exc
    except signing.BadSignature as exc:
        raise _error("This fixture preview is invalid. Generate a fresh preview.") from exc
    return _preview_from_payload(payload)


def _locked_scope(preview: LeagueFixturePreview) -> LeagueFixtureScope:
    """Reload a preview scope under locks in PostgreSQL-safe lock order."""

    try:
        competition = PoolCompetition.objects.select_for_update().get(
            pk=preview.scope.competition_id
        )
        stage = PoolCompetitionStage.objects.select_for_update().get(
            pk=preview.scope.stage_id
        )
        group = (
            PoolGroup.objects.select_for_update().get(pk=preview.scope.group_id)
            if preview.scope.group_id
            else None
        )
    except (PoolCompetition.DoesNotExist, PoolCompetitionStage.DoesNotExist, PoolGroup.DoesNotExist) as exc:
        raise _error("This league table changed before fixtures could be generated.") from exc
    return _scope_from_objects(competition, stage, group)


def _locked_scope_entry_ids(scope: LeagueFixtureScope) -> list[int]:
    """Lock membership/entry rows so confirmation sees one stable table list."""

    if scope.group_id:
        membership_entry_ids = list(
            PoolGroupEntry.objects.select_for_update(of=("self",))
            .filter(stage_id=scope.stage_id, group_id=scope.group_id)
            .order_by("sort_order", "entry_id")
            .values_list("entry_id", flat=True)
        )
        entry_ids = list(
            PoolCompetitionEntry.objects.select_for_update(of=("self",))
            .filter(competition_id=scope.competition_id, pk__in=membership_entry_ids)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if len(entry_ids) != len(membership_entry_ids):
            raise _error("The group entrants changed before fixtures could be generated.")
        return entry_ids
    return list(
        PoolCompetitionEntry.objects.select_for_update(of=("self",))
        .filter(competition_id=scope.competition_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _lock_scope_state(scope: LeagueFixtureScope) -> list[dict[str, object]]:
    """Lock mutable table rows before status is revalidated and matches are saved."""

    # Existing baseline rows can be edited independently of fixture creation.
    # Lock them after the stage/competition roots so a non-zero imported table
    # cannot race past the conservative mid-season safeguard.
    list(_scope_baselines(scope).select_for_update(of=("self",)).values_list("pk", flat=True))
    return _fixture_values(_scope_matches(scope).select_for_update(of=("self",)))


def _assert_preview_entries_current(
    preview: LeagueFixturePreview,
    current_entry_ids: Iterable[int],
) -> None:
    """Reject a stale preview when table membership has changed."""

    if tuple(sorted(current_entry_ids)) != preview.entry_ids:
        raise _error("The table entrants changed after this preview. Generate a fresh preview.")


def _assert_expected_scope(
    preview: LeagueFixturePreview,
    expected_scope: LeagueFixtureScope | None,
) -> None:
    """Keep a token submitted to one manager route inside that route's table."""

    if expected_scope is None:
        return
    if not isinstance(expected_scope, LeagueFixtureScope):
        raise _error("A validated expected fixture scope is required.")
    if preview.scope.as_dict() != expected_scope.as_dict():
        raise _error("This fixture preview belongs to a different competition table.")


def _assert_preview_matches_first_round(
    preview: LeagueFixturePreview,
    *,
    current_entry_ids: Iterable[int],
) -> None:
    """Prove a first-leg token remains a complete current round robin."""

    _assert_preview_entries_current(preview, current_entry_ids)
    # Re-run the validation through a freshly constructed preview to avoid
    # trusting a token parsed before concurrent state was locked.
    _validated_preview(
        scope=preview.scope,
        leg=1,
        entry_ids=current_entry_ids,
        fixtures=preview.fixtures,
    )


def _assert_preview_matches_second_round(
    preview: LeagueFixturePreview,
    *,
    current_entry_ids: Iterable[int],
    first_round_rows: Iterable[Mapping[str, object]],
) -> None:
    """Prove a second-leg token is exactly the saved first round reversed."""

    _assert_preview_entries_current(preview, current_entry_ids)
    entry_ids = tuple(sorted(current_entry_ids))
    expected = _expected_second_round(
        first_round_rows,
        gameweek_offset=_round_robin_gameweek_count(len(entry_ids)),
    )
    if _canonical_fixture_tuples(preview.fixtures) != _canonical_fixture_tuples(expected):
        raise _error(
            "The first-round fixtures changed after this preview. Generate a fresh reverse-fixture preview."
        )
    _validated_preview(
        scope=preview.scope,
        leg=2,
        entry_ids=entry_ids,
        fixtures=preview.fixtures,
    )


def _audit_fixture_generation(
    *,
    scope: LeagueFixtureScope,
    leg: int,
    actor,
    matches: Sequence[PoolMatch],
) -> None:
    """Record a compact immutable audit event for an accepted fixture plan."""

    action = (
        PoolLeagueAuditEvent.Action.FIRST_ROUND_GENERATED
        if leg == 1
        else PoolLeagueAuditEvent.Action.SECOND_ROUND_GENERATED
    )
    leg_label = "first" if leg == 1 else "second"
    PoolLeagueAuditEvent(
        competition=scope.competition,
        stage=scope.stage,
        group=scope.group,
        actor_id=getattr(actor, "pk", None) or None,
        action=action,
        message=(
            f"Generated {leg_label}-round fixtures: "
            f"{len(matches)} matches across {len({match.gameweek for match in matches})} gameweeks."
        ),
        payload={
            "leg": leg,
            "fixture_ids": [match.pk for match in matches],
            "gameweeks": sorted({match.gameweek for match in matches}),
            "entry_ids": sorted(
                {entry_id for match in matches for entry_id in (match.home_entry_id, match.away_entry_id)}
            ),
        },
    ).save()


def confirm_fixture_preview(
    token: str,
    *,
    actor=None,
    scope: LeagueFixtureScope | None = None,
    max_age: int | None = None,
) -> list[PoolMatch]:
    """Transactionally revalidate and persist the exact signed fixture preview.

    The caller is still responsible for the normal manager permission check.
    Manager views should pass their already-authorised ``scope`` so a signed
    token cannot be submitted through a route for another competition.  This
    service validates schedule correctness and data safety, locks the exact
    scope rows without nullable joins, rechecks the signed preview, then calls
    ``PoolMatch.save`` for every fixture while reusing the batch's policy lock.
    A refresh or duplicate submit
    therefore sees the new leg and fails safely instead of creating a copy.
    """

    preview = deserialize_fixture_preview(token, max_age=max_age)
    _assert_expected_scope(preview, scope)
    try:
        with transaction.atomic():
            scope = _locked_scope(preview)
            current_entry_ids = _locked_scope_entry_ids(scope)
            locked_rows = _lock_scope_state(scope)

            # Build status after the scope's mutable rows are locked.  This
            # repeats the preview-time checks and catches manual fixtures,
            # imported historical totals, or a duplicate submission.
            status = league_fixture_status(scope)
            _assert_plan_allowed(status, leg=preview.leg)
            if preview.leg == 1:
                _assert_preview_matches_first_round(
                    preview,
                    current_entry_ids=current_entry_ids,
                )
            else:
                _assert_preview_matches_second_round(
                    preview,
                    current_entry_ids=current_entry_ids,
                    first_round_rows=_first_round_rows(locked_rows),
                )

            # The competition root is already locked by ``_locked_scope``.
            # Every generated fixture uses the same default target and table
            # scope, so validate the prospective margin policy once instead
            # of re-locking/recalculating it for every individual save.
            locked_competitions = {scope.competition_id: scope.competition}
            PoolMatch._validate_active_margin_policy_for_prospective_scopes(
                {
                    scope.competition_id: {
                        "exclude_match_ids": set(),
                        "pending_race_targets": [
                            scope.competition.default_race_target
                        ],
                    }
                },
                locked_competitions=locked_competitions,
            )

            created_matches: list[PoolMatch] = []
            for fixture in preview.fixtures:
                pool_match = PoolMatch(
                    competition=scope.competition,
                    stage=scope.stage,
                    group=scope.group,
                    home_entry_id=fixture.home_entry_id,
                    away_entry_id=fixture.away_entry_id,
                    scheduled_at=None,
                    round_label=(
                        f"{PoolMatch.fixture_round_term_for(scope.competition, scope.stage)} "
                        f"{fixture.gameweek}"
                    ),
                    gameweek=fixture.gameweek,
                    league_leg=preview.leg,
                    is_generated_league_fixture=True,
                )
                try:
                    # Do not bulk-create: retain all ordinary model validation
                    # and save behaviour. The policy root was locked and
                    # validated once for this homogeneous batch above.
                    pool_match.save(
                        _locked_competitions=locked_competitions,
                        _skip_margin_policy_validation=True,
                        _skip_database_constraint_validation=True,
                    )
                except ValidationError as exc:
                    raise _error(exc.messages) from exc
                created_matches.append(pool_match)

            _audit_fixture_generation(
                scope=scope,
                leg=preview.leg,
                actor=actor,
                matches=created_matches,
            )
            return created_matches
    except IntegrityError as exc:
        # A direct database writer that bypasses model/service locks should not
        # result in a partial schedule.  The atomic block rolls it all back.
        raise _error(
            "Fixture generation could not complete because this table changed concurrently. Reload and generate a fresh preview."
        ) from exc


# Friendly aliases for manager views that use shorter action names.
confirm_league_fixture_preview = confirm_fixture_preview
sign_fixture_preview = serialize_fixture_preview
unsign_fixture_preview = deserialize_fixture_preview


def preview_entries_by_id(scope: LeagueFixtureScope) -> dict[int, PoolCompetitionEntry]:
    """Return a small display map for rendering a preview without N+1 lookups."""

    return {entry.pk: entry for entry in get_league_fixture_entries(scope)}


__all__ = [
    "DEFAULT_PREVIEW_MAX_AGE_SECONDS",
    "LeagueFixturePreview",
    "LeagueFixtureScope",
    "LeagueFixtureValidationError",
    "PlannedLeagueFixture",
    "PlannedLeagueGameweek",
    "PREVIEW_SALT",
    "PREVIEW_VERSION",
    "confirm_fixture_preview",
    "confirm_league_fixture_preview",
    "deserialize_fixture_preview",
    "get_league_fixture_entries",
    "get_league_fixture_scope",
    "get_league_fixture_scope_and_entries",
    "get_league_fixture_status",
    "get_scope_entries",
    "league_fixture_entries",
    "league_fixture_status",
    "plan_first_round_preview",
    "plan_second_round_preview",
    "preview_entries_by_id",
    "serialize_fixture_preview",
    "sign_fixture_preview",
    "unsign_fixture_preview",
    "validate_league_fixture_scope",
]
