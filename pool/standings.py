"""Query-derived pool standings and deterministic tie-breaking.

Pool tables are intentionally *not* persisted.  A table is reconstructed from
the authoritative pool-match result at read time, which means an opened result
correction immediately disappears from the table without a background task or
an eventually-consistent counter to repair.

The only writes in this module are organiser-managed standings policy
configuration (tie-breakers and points bands). Result services remain the sole
write boundary for fixtures and rack history.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from django.core.exceptions import ValidationError
from django.db import transaction

from .baselines import baseline_values_by_entry
from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolCompetitionTieBreaker,
    PoolCompetitionMarginPointRule,
    PoolGroup,
    PoolGroupEntry,
    PoolKnockoutTieSlot,
    PoolMatch,
    competition_has_generated_group_qualification_bracket,
)


Criterion = PoolCompetitionTieBreaker.Criterion

# Table points are always primary and deliberately have no model row.  The
# configured rows below describe the criteria used *after* table points.
DEFAULT_POOL_TIE_BREAKERS = (
    (1, Criterion.MATCHES_WON),
    (2, Criterion.RACK_DIFFERENCE),
    (3, Criterion.RACKS_WON),
    (4, Criterion.HEAD_TO_HEAD),
)
DEFAULT_TIE_BREAKERS = tuple(
    criterion for _order, criterion in DEFAULT_POOL_TIE_BREAKERS
)
_TABLE_POINTS_CRITERION = "__POOL_TABLE_POINTS__"

_TABLE_STAGE_TYPES = frozenset(
    {
        PoolCompetitionStage.StageType.LEAGUE,
        PoolCompetitionStage.StageType.GROUP,
    }
)
_ELIGIBLE_RESULT_METHODS = frozenset(
    {
        PoolMatch.ResultMethod.PLAYED,
        PoolMatch.ResultMethod.WALKOVER,
        PoolMatch.ResultMethod.WITHDRAWAL,
        PoolMatch.ResultMethod.DISQUALIFICATION,
    }
)


class PoolStandingsValidationError(ValidationError):
    """A standings scope or source-result invariant was not satisfied."""


@dataclass(frozen=True, slots=True)
class PoolStandingRow:
    """One read-only, competition-entry scoped pool table row.

    The aliases mirror both normal standings language (``played``/``won``)
    and the existing pool statistics language (``matches_played``/
    ``racks_won``), so templates do not have to translate domain terms.
    """

    competition: PoolCompetition
    stage: PoolCompetitionStage
    group: PoolGroup | None
    entry: PoolCompetitionEntry
    position: int = 0
    played: int = 0
    won: int = 0
    lost: int = 0
    points: int = 0
    racks_for: int = 0
    racks_against: int = 0
    recent_form: tuple[str, ...] = ()

    @property
    def competition_id(self):
        return self.competition.pk

    @property
    def stage_id(self):
        return self.stage.pk

    @property
    def group_id(self):
        return self.group.pk if self.group is not None else None

    @property
    def entry_id(self):
        return self.entry.pk

    @property
    def matches_played(self):
        return self.played

    @property
    def matches_won(self):
        return self.won

    @property
    def matches_lost(self):
        return self.lost

    @property
    def racks_won(self):
        return self.racks_for

    @property
    def racks_lost(self):
        return self.racks_against

    @property
    def rack_difference(self):
        return self.racks_for - self.racks_against

    @property
    def display_name(self):
        return self.entry.display_name

    @property
    def participant(self):
        """The player or team backing this competition-specific entry."""
        return self.entry.participant


# A short public alias is convenient for callers that do not need to know this
# is a derived, non-model row.  Do not replace this with a persisted model.
PoolStanding = PoolStandingRow


@dataclass(frozen=True, slots=True)
class PoolHeadToHeadRow:
    """Read-only mini-table totals for a tie-breaking subgroup."""

    entry_id: int
    played: int = 0
    won: int = 0
    lost: int = 0
    points: int = 0
    racks_for: int = 0
    racks_against: int = 0

    @property
    def rack_difference(self):
        return self.racks_for - self.racks_against

    @property
    def value(self):
        """A single deterministic value for the configured H2H criterion."""
        return (self.points, self.won, self.rack_difference, self.racks_for)


@dataclass(frozen=True, slots=True)
class PoolMarginPointRuleSpec:
    """One validated, competition-local played-match margin allocation band.

    This small immutable value object lets the configuration service validate a
    complete proposed policy before it replaces database rows.  It is not a
    standings model and never stores calculated totals.
    """

    minimum_margin: int
    maximum_margin: int | None
    winner_points: int
    loser_points: int


@dataclass(slots=True)
class _MutableStandingRow:
    """Private accumulator, converted to :class:`PoolStandingRow` at return."""

    entry: PoolCompetitionEntry
    played: int = 0
    won: int = 0
    lost: int = 0
    points: int = 0
    racks_for: int = 0
    racks_against: int = 0


def _coerce_margin_rule_spec(rule) -> PoolMarginPointRuleSpec:
    """Normalize model rows, mappings, and four-value sequences for validation."""

    if isinstance(rule, PoolMarginPointRuleSpec):
        return rule
    if isinstance(rule, PoolCompetitionMarginPointRule):
        return PoolMarginPointRuleSpec(
            minimum_margin=rule.minimum_margin,
            maximum_margin=rule.maximum_margin,
            winner_points=rule.winner_points,
            loser_points=rule.loser_points,
        )
    if isinstance(rule, Mapping):
        try:
            return PoolMarginPointRuleSpec(
                minimum_margin=rule["minimum_margin"],
                maximum_margin=rule.get("maximum_margin"),
                winner_points=rule["winner_points"],
                loser_points=rule["loser_points"],
            )
        except KeyError as exc:
            raise PoolStandingsValidationError(
                "Each margin band needs a minimum margin and winner/loser points."
            ) from exc
    if isinstance(rule, (tuple, list)) and len(rule) == 4:
        return PoolMarginPointRuleSpec(*rule)
    raise PoolStandingsValidationError(
        "Each margin band must provide minimum margin, maximum margin, winner points, and loser points."
    )


def _valid_nonnegative_whole_number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def maximum_meaningful_pool_margin(
    competition: PoolCompetition,
    *,
    pending_race_target: int | None = None,
    pending_race_targets: Iterable[int] = (),
    exclude_match_ids: Iterable[int] = (),
) -> int:
    """Return the greatest winning margin a table fixture may legitimately use.

    Margin-points policy is competition-wide rather than tied to one group,
    because league and group stages share its points configuration.  The
    competition default protects fixtures created in the future, while saved
    league/group fixture overrides protect the fixtures that already exist.
    Knockout-only targets deliberately do not affect a league/group table.

    ``pending_race_target`` / ``pending_race_targets`` let :class:`PoolMatch`
    validate unsaved fixture targets before they reach the database.
    ``exclude_match_ids`` replaces or removes persisted fixtures while an
    edit/delete is being checked.  Together they make prospective target
    changes safe instead of validating against a soon-to-be-stale row.  This
    is the same deterministic scope used for policy configuration and result
    allocation; no template or caller has to guess a target.
    """

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError(
            "A saved pool competition is required to determine its margin policy range."
        )

    try:
        excluded_ids = {
            match_id
            for match_id in (exclude_match_ids or ())
            if isinstance(match_id, int) and not isinstance(match_id, bool)
        }
        additional_targets = list(pending_race_targets or ())
    except TypeError as exc:
        raise PoolStandingsValidationError(
            "Prospective pool match targets must be ordered collections."
        ) from exc

    table_matches = PoolMatch.objects.filter(
            competition_id=competition.pk,
            stage__stage_type__in=_TABLE_STAGE_TYPES,
            race_target__isnull=False,
        )
    if excluded_ids:
        table_matches = table_matches.exclude(pk__in=excluded_ids)

    targets = [competition.default_race_target]
    targets.extend(table_matches.values_list("race_target", flat=True))
    if pending_race_target is not None:
        targets.append(pending_race_target)
    targets.extend(additional_targets)

    valid_targets = [
        target
        for target in targets
        if isinstance(target, int) and not isinstance(target, bool) and target >= 1
    ]
    if not valid_targets:
        raise PoolStandingsValidationError(
            "A league or group race target must be a whole number of at least 1."
        )
    return max(valid_targets)


# A shorter public alias keeps callers readable without duplicating target
# lookup rules in models, services, or manager views.
maximum_pool_standings_margin = maximum_meaningful_pool_margin


def validate_pool_margin_point_rules(
    competition: PoolCompetition,
    rules,
    *,
    require_complete: bool = True,
    maximum_margin: int | None = None,
    allow_legacy_open_ended: bool = False,
) -> tuple[PoolMarginPointRuleSpec, ...]:
    """Validate one deterministic played-match margin policy.

    Cross-row range validation lives here rather than in forms or templates so
    manager configuration, head-to-head calculation, qualification, and any
    later API all agree.  A newly configured policy must cover exactly margins
    ``1`` through the greatest meaningful league/group race target; a Race to
    4 policy therefore ends at margin 4 rather than inventing a ``5+`` band.

    ``allow_legacy_open_ended`` is intentionally read-only compatibility for
    policies created before bounded ranges existed.  Callers that *write* a
    policy leave it false, so no new artificial open-ended row can be saved.
    """

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError(
            "A saved pool competition is required to configure margin points."
        )
    if maximum_margin is None:
        maximum_margin = maximum_meaningful_pool_margin(competition)
    if (
        not isinstance(maximum_margin, int)
        or isinstance(maximum_margin, bool)
        or maximum_margin < 1
    ):
        raise PoolStandingsValidationError(
            "The maximum meaningful winning margin must be a whole number of at least 1."
        )

    try:
        supplied_rules = list(rules or ())
    except TypeError as exc:
        raise PoolStandingsValidationError(
            "Margin point rules must be an ordered collection."
        ) from exc

    specs: list[PoolMarginPointRuleSpec] = []
    errors: list[str] = []
    for rule in supplied_rules:
        if (
            isinstance(rule, PoolCompetitionMarginPointRule)
            and rule.competition_id
            and rule.competition_id != competition.pk
        ):
            errors.append("Margin point rules must belong to this competition.")
            continue
        try:
            spec = _coerce_margin_rule_spec(rule)
        except PoolStandingsValidationError as error:
            errors.extend(error.messages)
            continue

        if not _valid_nonnegative_whole_number(spec.minimum_margin) or spec.minimum_margin < 1:
            errors.append("Each minimum winning margin must be a whole number of at least 1.")
        if spec.maximum_margin is not None and (
            not _valid_nonnegative_whole_number(spec.maximum_margin)
            or spec.maximum_margin < spec.minimum_margin
        ):
            errors.append(
                "A maximum winning margin must be blank or a whole number no lower than its minimum."
            )
        if not _valid_nonnegative_whole_number(spec.winner_points):
            errors.append("Winner points cannot be negative.")
        if not _valid_nonnegative_whole_number(spec.loser_points):
            errors.append("Loser points cannot be negative.")
        specs.append(spec)

    if errors:
        raise PoolStandingsValidationError(errors)
    if not specs:
        if require_complete:
            raise PoolStandingsValidationError(
                "Margin-based points need at least one winning-margin band."
            )
        return ()

    ordered = tuple(sorted(specs, key=lambda item: (item.minimum_margin, item.maximum_margin is None, item.maximum_margin or 0)))
    if len({spec.minimum_margin for spec in ordered}) != len(ordered):
        raise PoolStandingsValidationError(
            "Each winning-margin band must begin at a unique margin."
        )

    expected_minimum = 1
    for index, spec in enumerate(ordered):
        if spec.minimum_margin < expected_minimum:
            raise PoolStandingsValidationError(
                "Winning-margin bands cannot overlap."
            )
        if spec.minimum_margin > expected_minimum:
            raise PoolStandingsValidationError(
                "Winning-margin bands cannot contain gaps."
            )
        is_final = index == len(ordered) - 1
        if spec.maximum_margin is None:
            if not is_final:
                raise PoolStandingsValidationError(
                    "Only the final winning-margin band may have no maximum."
                )
            if allow_legacy_open_ended:
                return ordered
            raise PoolStandingsValidationError(
                "Margin-point rules must end at the maximum meaningful winning margin "
                f"of {maximum_margin}; do not add an open-ended band."
            )
        if spec.maximum_margin > maximum_margin:
            raise PoolStandingsValidationError(
                "A winning-margin rule cannot extend beyond the maximum meaningful "
                f"margin of {maximum_margin}."
            )
        expected_minimum = spec.maximum_margin + 1
        if expected_minimum > maximum_margin:
            if not is_final:
                raise PoolStandingsValidationError(
                    "No winning-margin rule may follow the maximum meaningful margin."
                )
            return ordered

    # Reaching here means every supplied band ended before the largest real
    # table margin.  Never silently fall back to fixed points for a malformed
    # active policy.
    if require_complete:
        raise PoolStandingsValidationError(
            "Margin-based points must cover every winning margin from 1 through "
            f"{maximum_margin}."
        )
    return ordered


validate_margin_point_rules = validate_pool_margin_point_rules


def derive_pool_margin_point_rules_from_thresholds(
    competition: PoolCompetition,
    thresholds,
) -> tuple[PoolMarginPointRuleSpec, ...]:
    """Translate the manager's simple threshold cards into stored ranges.

    Organisers provide only the upper edge of each rule.  The first band
    always starts at one rack and every following start is derived from the
    preceding threshold.  The final card must end at the greatest meaningful
    winning margin for league/group fixtures, so a Race to 4 policy ends at
    4 rather than creating a fictional ``5+`` range.

    ``None`` remains a compatibility shorthand for the *last* threshold from
    older callers.  It is normalised to the real maximum before validation;
    forms never accept a blank final threshold and no newly persisted rule is
    open-ended.
    """

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError(
            "A saved pool competition is required to configure margin points."
        )
    try:
        supplied_thresholds = list(thresholds or ())
    except TypeError as exc:
        raise PoolStandingsValidationError(
            "Played-match point rules must be an ordered collection."
        ) from exc

    if not supplied_thresholds:
        raise PoolStandingsValidationError(
            "Provide at least one played-match points rule."
        )

    maximum_margin = maximum_meaningful_pool_margin(competition)

    def values_for(rule):
        if not isinstance(rule, Mapping):
            raise PoolStandingsValidationError(
                "Each played-match rule must include a threshold and both point values."
            )
        try:
            return (
                rule.get("up_to_margin"),
                rule["winner_points"],
                rule["loser_points"],
            )
        except KeyError as exc:
            raise PoolStandingsValidationError(
                "Each played-match rule needs winner and loser points."
            ) from exc

    current_minimum = 1
    derived_rules: list[PoolMarginPointRuleSpec] = []
    for index, threshold_rule in enumerate(supplied_thresholds):
        up_to_margin, winner_points, loser_points = values_for(threshold_rule)
        is_final = index == len(supplied_thresholds) - 1

        if not _valid_nonnegative_whole_number(winner_points):
            raise PoolStandingsValidationError("Winner points cannot be negative.")
        if not _valid_nonnegative_whole_number(loser_points):
            raise PoolStandingsValidationError("Loser points cannot be negative.")

        if is_final and up_to_margin in (None, ""):
            up_to_margin = maximum_margin

        if not _valid_nonnegative_whole_number(up_to_margin) or up_to_margin < 1:
            raise PoolStandingsValidationError(
                "Each winning-margin threshold must be a whole number of at least 1."
            )
        if up_to_margin < current_minimum:
            raise PoolStandingsValidationError(
                "Each winning-margin threshold must be greater than the previous threshold."
            )
        if up_to_margin > maximum_margin:
            raise PoolStandingsValidationError(
                "A winning-margin threshold cannot exceed the maximum meaningful "
                f"margin of {maximum_margin}."
            )
        if is_final and up_to_margin != maximum_margin:
            raise PoolStandingsValidationError(
                "The final played-match rule must cover the maximum meaningful "
                f"winning margin of {maximum_margin}."
            )
        derived_rules.append(
            PoolMarginPointRuleSpec(
                minimum_margin=current_minimum,
                maximum_margin=up_to_margin,
                winner_points=winner_points,
                loser_points=loser_points,
            )
        )
        current_minimum = up_to_margin + 1

    return validate_pool_margin_point_rules(
        competition,
        derived_rules,
        require_complete=True,
        maximum_margin=maximum_margin,
    )


derive_margin_point_rules_from_thresholds = (
    derive_pool_margin_point_rules_from_thresholds
)


def configured_pool_margin_point_rules(
    competition: PoolCompetition,
    *,
    require_complete: bool | None = None,
    maximum_margin: int | None = None,
) -> tuple[PoolCompetitionMarginPointRule, ...]:
    """Return one competition's persisted bands after validating their policy.

    Existing deployments may still contain the earlier final open-ended row.
    It remains readable and safely covers a bounded target until a manager
    saves the policy again, at which point the replacement service writes a
    finite final range.
    """

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError(
            "A saved pool competition is required to read margin point rules."
        )
    rules = tuple(
        PoolCompetitionMarginPointRule.objects.filter(competition_id=competition.pk)
        .order_by("minimum_margin", "id")
    )
    if require_complete is None:
        require_complete = (
            competition.points_mode
            == PoolCompetition.PointsMode.MARGIN_BASED
        )
    validate_pool_margin_point_rules(
        competition,
        rules,
        require_complete=require_complete,
        maximum_margin=maximum_margin,
        allow_legacy_open_ended=True,
    )
    return rules


configured_margin_point_rules = configured_pool_margin_point_rules


def point_allocation_for_pool_match(
    competition: PoolCompetition,
    pool_match: PoolMatch,
    *,
    _competition_is_current: bool = False,
) -> tuple[int, int]:
    """Return the winner/loser table allocation for one eligible result.

    Only genuinely played, authoritative rack results consult margin bands.
    Walkovers, withdrawals, and disqualifications deliberately retain the
    competition's normal fixed allocation even when genuine rack history is
    preserved for statistical purposes.
    """

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError(
            "A saved pool competition is required to allocate standings points."
        )
    if not isinstance(pool_match, PoolMatch) or not pool_match.pk:
        raise PoolStandingsValidationError(
            "A saved pool match is required to allocate standings points."
        )
    if pool_match.competition_id != competition.pk:
        raise PoolStandingsValidationError(
            "A pool match must belong to the supplied competition."
        )
    participant_ids = {pool_match.home_entry_id, pool_match.away_entry_id}
    if (
        pool_match.status != PoolMatch.Status.FINISHED
        or pool_match.is_in_correction_mode
        or pool_match.result_method not in _ELIGIBLE_RESULT_METHODS
        or pool_match.winner_id not in participant_ids
    ):
        raise PoolStandingsValidationError(
            "Table points can be allocated only for a final, eligible pool result."
        )

    # Public callers may keep a competition object over a manager policy
    # update.  Refresh by default so this public allocation helper cannot
    # return stale table points; the standings loop passes its freshly loaded
    # root explicitly to avoid one query per fixture.
    if not _competition_is_current:
        competition = PoolCompetition.objects.get(pk=competition.pk)

    normal_allocation = (competition.points_for_win, competition.points_for_loss)
    if (
        competition.points_mode != PoolCompetition.PointsMode.MARGIN_BASED
        or pool_match.result_method != PoolMatch.ResultMethod.PLAYED
    ):
        return normal_allocation

    margin = abs(pool_match.home_score - pool_match.away_score)
    if margin < 1:
        raise PoolStandingsValidationError(
            "A played finished pool match must have a positive winning rack margin."
        )

    for rule in configured_pool_margin_point_rules(
        competition,
        require_complete=True,
    ):
        if margin < rule.minimum_margin:
            continue
        if rule.maximum_margin is None or margin <= rule.maximum_margin:
            return rule.winner_points, rule.loser_points
    raise PoolStandingsValidationError(
        "No margin-based points rule covers this played match result."
    )


points_for_pool_match = point_allocation_for_pool_match


@transaction.atomic
def replace_pool_standings_points_configuration(
    competition: PoolCompetition,
    *,
    points_mode,
    points_for_win,
    points_for_loss,
    margin_rules=(),
    allow_legacy_open_ended: bool = False,
) -> tuple[PoolCompetition, list[PoolCompetitionMarginPointRule]]:
    """Atomically replace one competition's complete standings-points policy.

    The parent competition is locked before its bands, matching bracket/root
    locking.  The policy is validated in full before any rows are deleted, so
    a malformed submitted formset cannot leave an active competition with a
    gap, overlap, or missing terminal race margin.  The explicit
    ``allow_legacy_open_ended`` escape hatch is only for a manager preserving
    pre-bounded rows; ordinary callers and all new threshold submissions are
    rejected instead of creating an artificial open-ended band.
    """

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError(
            "A saved pool competition is required to configure standings points."
        )
    points_mode = getattr(points_mode, "value", points_mode)
    if points_mode not in PoolCompetition.PointsMode.values:
        raise PoolStandingsValidationError("Select a valid standings points system.")
    if not _valid_nonnegative_whole_number(points_for_win):
        raise PoolStandingsValidationError("Win points cannot be negative.")
    if not _valid_nonnegative_whole_number(points_for_loss):
        raise PoolStandingsValidationError("Loss points cannot be negative.")

    supplied_rules = list(margin_rules or ())
    require_complete = (
        points_mode == PoolCompetition.PointsMode.MARGIN_BASED
        or bool(supplied_rules)
    )
    desired_rules = validate_pool_margin_point_rules(
        competition,
        supplied_rules,
        require_complete=require_complete,
        allow_legacy_open_ended=allow_legacy_open_ended,
    )
    # A previous release stored its final band as open-ended.  Preserve its
    # allocation for every meaningful current table margin, but canonicalise
    # it on any deliberate manager/service save so new data never carries a
    # fictional beyond-target range.
    maximum_margin = maximum_meaningful_pool_margin(competition)
    if any(rule.maximum_margin is None for rule in desired_rules):
        desired_rules = tuple(
            replace(rule, maximum_margin=maximum_margin)
            if rule.maximum_margin is None
            else rule
            for rule in desired_rules
        )
        desired_rules = validate_pool_margin_point_rules(
            competition,
            desired_rules,
            require_complete=require_complete,
            maximum_margin=maximum_margin,
        )

    locked_competition = PoolCompetition.objects.select_for_update().get(pk=competition.pk)
    existing_rules = list(
        PoolCompetitionMarginPointRule.objects.select_for_update()
        .filter(competition_id=locked_competition.pk)
        .order_by("minimum_margin", "id")
    )
    if competition_has_generated_group_qualification_bracket(locked_competition.pk):
        raise PoolStandingsValidationError(
            "Reset the generated qualification bracket before changing standings points."
        )

    # The desired complete policy is already validated and the competition is
    # locked above.  Use the narrowly scoped private bypass so ordinary bulk
    # deletes remain unable to leave an active margin policy incomplete.
    PoolCompetitionMarginPointRule.objects.filter(
        competition_id=locked_competition.pk
    )._delete_for_atomic_replacement()
    created_rules = PoolCompetitionMarginPointRule.objects.bulk_create(
        [
            PoolCompetitionMarginPointRule(
                competition=locked_competition,
                minimum_margin=rule.minimum_margin,
                maximum_margin=rule.maximum_margin,
                winner_points=rule.winner_points,
                loser_points=rule.loser_points,
            )
            for rule in desired_rules
        ]
    )
    locked_competition.points_mode = points_mode
    locked_competition.points_for_win = points_for_win
    locked_competition.points_for_loss = points_for_loss
    locked_competition.save()
    return locked_competition, created_rules


replace_standings_points_configuration = replace_pool_standings_points_configuration


def validate_pool_tie_breaker_configuration(configuration) -> tuple[tuple[int, str], ...]:
    """Normalize one optional, complete ordering of pool tie-breakers.

    ``configuration`` may contain ``(order, criterion)`` pairs (the stable
    service format) or an ordered list of criterion values (useful to forms).
    Empty configuration is valid: read queries then use the safe defaults.
    Table points are deliberately absent because they are always primary.
    """

    if configuration is None:
        configuration = ()

    try:
        supplied = list(configuration)
    except TypeError as exc:
        raise PoolStandingsValidationError(
            "Tie-breaker configuration must be an ordered collection."
        ) from exc

    normalized: list[tuple[int, str]] = []
    errors: list[str] = []
    valid_criteria = set(Criterion.values)

    for default_order, item in enumerate(supplied, start=1):
        if isinstance(item, (tuple, list)) and len(item) == 2:
            order, criterion = item
        else:
            order, criterion = default_order, item

        criterion = getattr(criterion, "value", criterion)
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            errors.append("Each tie-breaker order must be a positive whole number.")
            continue
        if criterion not in valid_criteria:
            errors.append("Select a valid pool tie-breaker criterion.")
            continue
        normalized.append((order, criterion))

    orders = [order for order, _criterion in normalized]
    criteria = [criterion for _order, criterion in normalized]
    if len(orders) != len(set(orders)):
        errors.append("Each tie-breaker order may only be used once.")
    if len(criteria) != len(set(criteria)):
        errors.append("Each tie-breaker criterion may only be selected once.")
    if len(normalized) > len(valid_criteria):
        errors.append("Too many pool tie-breaker criteria were supplied.")

    # Gaps create ambiguous form/configuration order and provide no benefit;
    # replacing rows rather than swapping them makes this safe to enforce.
    if orders and sorted(orders) != list(range(1, len(orders) + 1)):
        errors.append("Tie-breaker orders must start at 1 and be consecutive.")

    if errors:
        raise PoolStandingsValidationError(errors)
    return tuple(sorted(normalized, key=lambda value: value[0]))


# Familiar project-style aliases keep the standalone pool module easy to use
# beside ``leagues.services`` without coupling either sport's models.
validate_tie_breaker_configuration = validate_pool_tie_breaker_configuration


@transaction.atomic
def replace_pool_tie_breakers(
    competition: PoolCompetition,
    configuration,
) -> tuple[list[PoolCompetitionTieBreaker], list[PoolCompetitionTieBreaker]]:
    """Atomically replace a competition's optional tie-breaker rows.

    Deleting after both the parent competition and its configuration rows are
    locked avoids transient unique-order collisions when administrators swap
    two criteria.  The returned tuple is ``(created_rows, replaced_rows)``;
    it is empty/empty when the desired configuration is already current.
    """

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError(
            "A saved pool competition is required to configure tie-breakers."
        )

    desired = validate_pool_tie_breaker_configuration(configuration)
    locked_competition = PoolCompetition.objects.select_for_update().get(
        pk=competition.pk
    )
    if PoolKnockoutTieSlot.objects.filter(
        tie__bracket__competition_id=locked_competition.pk,
        source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
    ).exists():
        raise PoolStandingsValidationError(
            "Reset the generated qualification bracket before changing tie-breakers."
        )
    existing_rows = list(
        PoolCompetitionTieBreaker.objects.select_for_update()
        .filter(competition_id=locked_competition.pk)
        .order_by("order", "id")
    )
    existing = tuple((row.order, row.criterion) for row in existing_rows)
    if existing == desired:
        return [], []

    PoolCompetitionTieBreaker.objects.filter(
        competition_id=locked_competition.pk
    ).delete()
    created_rows = PoolCompetitionTieBreaker.objects.bulk_create(
        [
            PoolCompetitionTieBreaker(
                competition=locked_competition,
                order=order,
                criterion=criterion,
            )
            for order, criterion in desired
        ]
    )
    return created_rows, existing_rows


replace_tie_breakers = replace_pool_tie_breakers


@transaction.atomic
def ensure_default_pool_tie_breakers(
    competition: PoolCompetition,
) -> list[PoolCompetitionTieBreaker]:
    """Create the default ordering only when this competition has none.

    An explicit partial configuration is respected.  A parent-row lock makes
    this idempotent under concurrent competition setup requests.
    """

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        return []

    locked_competition = PoolCompetition.objects.select_for_update().get(
        pk=competition.pk
    )
    if PoolCompetitionTieBreaker.objects.filter(
        competition_id=locked_competition.pk
    ).exists():
        return []
    return PoolCompetitionTieBreaker.objects.bulk_create(
        [
            PoolCompetitionTieBreaker(
                competition=locked_competition,
                order=order,
                criterion=criterion,
            )
            for order, criterion in DEFAULT_POOL_TIE_BREAKERS
        ]
    )


ensure_default_tie_breakers = ensure_default_pool_tie_breakers


def configured_pool_tie_breakers(competition: PoolCompetition) -> tuple[str, ...]:
    """Return ordered post-points criteria, falling back safely when absent."""

    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError(
            "A saved pool competition is required to read tie-breakers."
        )

    configuration = list(
        competition.tie_breakers.order_by("order", "id").values_list(
            "order", "criterion"
        )
    )
    if not configuration:
        return DEFAULT_TIE_BREAKERS
    try:
        normalized = validate_pool_tie_breaker_configuration(configuration)
    except PoolStandingsValidationError:
        # Historic/imported configuration should never make a public table
        # unavailable.  Manager writes remain strictly validated by the
        # replacement service above; reads simply use the documented default.
        return DEFAULT_TIE_BREAKERS
    return tuple(criterion for _order, criterion in normalized)


configured_tie_breakers = configured_pool_tie_breakers


def groups_for_stage(stage: PoolCompetitionStage):
    """Return groups in one valid group stage in their public table order."""

    stage = _require_saved_stage(stage)
    if stage.stage_type != PoolCompetitionStage.StageType.GROUP:
        raise PoolStandingsValidationError(
            "Only a group stage can have group standings."
        )
    return PoolGroup.objects.filter(stage_id=stage.pk).order_by("order", "id")


stage_groups = groups_for_stage


def standings_for_group(group: PoolGroup) -> list[PoolStandingRow]:
    """Build one exact group table from eligible finalised group fixtures."""

    group = _require_saved_group(group)
    return standings_for_stage(group.stage, group=group)


group_standings = standings_for_group


def standings_for_stage_groups(
    stage: PoolCompetitionStage,
) -> list[tuple[PoolGroup, list[PoolStandingRow]]]:
    """Build every table in a group stage, retaining the group beside its rows."""

    stage = _require_saved_stage(stage)
    if stage.stage_type != PoolCompetitionStage.StageType.GROUP:
        raise PoolStandingsValidationError(
            "This helper is available only for group stages."
        )
    return [(group, standings_for_group(group)) for group in groups_for_stage(stage)]


group_standings_for_stage = standings_for_stage_groups


def standings_for_stage(
    stage: PoolCompetitionStage,
    *,
    group: PoolGroup | None = None,
) -> list[PoolStandingRow]:
    """Derive a single league or group table from its exact fixture scope.

    A league table consists of all competition entries and fixtures with no
    group.  A group stage requires its explicit ``group`` argument, so results
    can never leak between Group A and Group B.
    """

    stage = _require_saved_stage(stage)
    # A stage can retain a cached ``competition`` relation across a manager
    # policy save.  Reload the small configuration root so a caller holding a
    # long-lived stage instance still receives the current authoritative
    # points policy rather than stale fixed values.
    competition = PoolCompetition.objects.get(pk=stage.competition_id)
    group = _validate_standings_scope(stage, group)
    entries = _entries_for_scope(stage, group)
    matches = eligible_pool_matches_for_stage(stage, group=group)
    _validate_source_matches(entries, matches, stage=stage, group=group)

    # Imported baseline totals are part of the displayed overall table, just
    # like football's baseline-inclusive totals.  They are intentionally not
    # passed to the head-to-head mini-table below: aggregate imports cannot
    # truthfully tell us who played whom before eSportsLive recorded fixtures.
    baselines = baseline_values_by_entry(
        stage,
        group=group,
        entry_ids=[entry.pk for entry in entries],
    )
    mutable_rows = {
        entry.pk: _MutableStandingRow(
            entry=entry,
            **{
                field: getattr(baselines.get(entry.pk), field, 0)
                for field in (
                    "played",
                    "won",
                    "lost",
                    "points",
                    "racks_for",
                    "racks_against",
                )
            },
        )
        for entry in entries
    }
    for pool_match in matches:
        home = mutable_rows[pool_match.home_entry_id]
        away = mutable_rows[pool_match.away_entry_id]
        _apply_eligible_match(
            home,
            away,
            pool_match,
            competition=competition,
        )
    recent_form = _recent_form_by_entry(matches)

    rows = [
        PoolStandingRow(
            competition=competition,
            stage=stage,
            group=group,
            entry=mutable.entry,
            played=mutable.played,
            won=mutable.won,
            lost=mutable.lost,
            points=mutable.points,
            racks_for=mutable.racks_for,
            racks_against=mutable.racks_against,
            recent_form=recent_form.get(mutable.entry.pk, ()),
        )
        for mutable in mutable_rows.values()
    ]
    ranked_rows = rank_pool_standings(
        competition,
        rows,
        source_matches=matches,
    )
    return [replace(row, position=position) for position, row in enumerate(ranked_rows, 1)]


def eligible_pool_matches_for_stage(
    stage: PoolCompetitionStage,
    *,
    group: PoolGroup | None = None,
) -> list[PoolMatch]:
    """Return authoritative matches eligible for exactly one table scope.

    A finalised match is deliberately omitted while result correction is open;
    it re-enters only after the result service finalises the corrected result.
    Byes have no opponent and never create league/group table statistics.
    """

    stage = _require_saved_stage(stage)
    group = _validate_standings_scope(stage, group)
    matches = PoolMatch.objects.filter(
        competition_id=stage.competition_id,
        stage_id=stage.pk,
        status=PoolMatch.Status.FINISHED,
        is_in_correction_mode=False,
        winner__isnull=False,
        result_method__in=_ELIGIBLE_RESULT_METHODS,
    )
    if group is None:
        matches = matches.filter(group__isnull=True)
    else:
        matches = matches.filter(group_id=group.pk)

    return list(
        matches.select_related(
            "competition",
            "stage",
            "group",
            "home_entry__player",
            "home_entry__team",
            "away_entry__player",
            "away_entry__team",
            "winner",
        ).order_by("completed_at", "pk")
    )


eligible_matches_for_stage = eligible_pool_matches_for_stage


def rank_pool_standings(
    competition: PoolCompetition,
    standings: Iterable[PoolStandingRow],
    *,
    source_matches: Iterable[PoolMatch] | None = None,
) -> list[PoolStandingRow]:
    """Rank one already-derived scope with recursive head-to-head resolution."""

    rows = list(standings)
    if not rows:
        return []
    _validate_standing_rows(competition, rows)
    if len(rows) == 1:
        return rows

    first_row = rows[0]
    if source_matches is None:
        matches = eligible_pool_matches_for_stage(
            first_row.stage,
            group=first_row.group,
        )
    else:
        matches = list(source_matches)
        _validate_source_matches(
            [row.entry for row in rows],
            matches,
            stage=first_row.stage,
            group=first_row.group,
        )

    # Points are the fixed primary criterion and therefore intentionally do
    # not appear in ``PoolCompetitionTieBreaker`` rows.
    criteria = (_TABLE_POINTS_CRITERION, *configured_pool_tie_breakers(competition))
    first_head_to_head_index = next(
        (
            index
            for index, criterion in enumerate(criteria)
            if criterion == Criterion.HEAD_TO_HEAD
        ),
        None,
    )
    return _rank_tied_rows(
        rows,
        criteria,
        matches,
        competition=competition,
        criterion_index=0,
        first_head_to_head_index=first_head_to_head_index,
    )


rank_standings = rank_pool_standings


def head_to_head_mini_table(
    standings: Sequence[PoolStandingRow],
    source_matches: Iterable[PoolMatch],
    *,
    competition: PoolCompetition | None = None,
) -> dict[int, PoolHeadToHeadRow]:
    """Derive a mini-table for exactly the supplied tied rows.

    Only finalised meetings between members of the tied subgroup are used. If
    no such meeting exists, all entries receive the same all-zero value and
    the ranking caller continues to the next configured criterion.
    """

    rows = list(standings)
    if not rows:
        return {}
    if competition is None:
        competition = rows[0].competition
    # This exported helper may be called independently of ``standings_for_stage``
    # with a competition object retained across a manager policy update.  Reload
    # the small configuration root so its mini-table always uses the same current
    # points policy as the main table and qualification service.
    competition = PoolCompetition.objects.get(pk=competition.pk)
    _validate_standing_rows(competition, rows)
    matches = list(source_matches)
    _validate_source_matches(
        [row.entry for row in rows],
        matches,
        stage=rows[0].stage,
        group=rows[0].group,
        allow_outside_entries=True,
    )

    entry_ids = {row.entry_id for row in rows}
    direct_matches = [
        pool_match
        for pool_match in matches
        if pool_match.home_entry_id in entry_ids
        and pool_match.away_entry_id in entry_ids
    ]
    # A group schedule may still be in progress.  Use all finalised meetings
    # among the tied members; only the complete absence of a direct meeting is
    # non-separating.  This deliberately supports a three-way mini-table even
    # before every possible pair has played.
    if not direct_matches:
        return {entry_id: PoolHeadToHeadRow(entry_id=entry_id) for entry_id in entry_ids}

    mutable_rows = {
        entry_id: _MutableStandingRow(entry=next(
            row.entry for row in rows if row.entry_id == entry_id
        ))
        for entry_id in entry_ids
    }
    for pool_match in direct_matches:
        _apply_eligible_match(
            mutable_rows[pool_match.home_entry_id],
            mutable_rows[pool_match.away_entry_id],
            pool_match,
            competition=competition,
        )

    return {
        entry_id: PoolHeadToHeadRow(
            entry_id=entry_id,
            played=mutable.played,
            won=mutable.won,
            lost=mutable.lost,
            points=mutable.points,
            racks_for=mutable.racks_for,
            racks_against=mutable.racks_against,
        )
        for entry_id, mutable in mutable_rows.items()
    }


def _require_saved_stage(stage: PoolCompetitionStage) -> PoolCompetitionStage:
    if not isinstance(stage, PoolCompetitionStage) or not stage.pk:
        raise PoolStandingsValidationError("A saved pool competition stage is required.")
    if not stage.competition_id:
        raise PoolStandingsValidationError("The pool stage must belong to a competition.")
    return stage


def _require_saved_group(group: PoolGroup) -> PoolGroup:
    if not isinstance(group, PoolGroup) or not group.pk:
        raise PoolStandingsValidationError("A saved pool group is required.")
    if not group.stage_id:
        raise PoolStandingsValidationError("The pool group must belong to a stage.")
    return group


def _validate_standings_scope(
    stage: PoolCompetitionStage,
    group: PoolGroup | None,
) -> PoolGroup | None:
    if stage.stage_type not in _TABLE_STAGE_TYPES:
        raise PoolStandingsValidationError(
            "Standings are available only for league and group stages."
        )
    if stage.stage_type == PoolCompetitionStage.StageType.LEAGUE:
        if group is not None:
            raise PoolStandingsValidationError("A league-stage table cannot select a group.")
        return None

    if group is None:
        raise PoolStandingsValidationError(
            "Select an explicit group to calculate a group-stage table."
        )
    group = _require_saved_group(group)
    if group.stage_id != stage.pk:
        raise PoolStandingsValidationError(
            "The selected group does not belong to this pool stage."
        )
    return group


def _entries_for_scope(
    stage: PoolCompetitionStage,
    group: PoolGroup | None,
) -> list[PoolCompetitionEntry]:
    if group is None:
        return list(
            PoolCompetitionEntry.objects.filter(competition_id=stage.competition_id)
            .select_related("player", "team")
            .order_by("seed", "id")
        )

    memberships = (
        PoolGroupEntry.objects.filter(stage_id=stage.pk, group_id=group.pk)
        .select_related("entry__player", "entry__team")
        .order_by("sort_order", "entry__seed", "entry__id")
    )
    return [membership.entry for membership in memberships]


def _validate_standing_rows(
    competition: PoolCompetition,
    rows: Sequence[PoolStandingRow],
) -> None:
    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolStandingsValidationError("A saved pool competition is required.")
    first = rows[0]
    scope = (first.competition_id, first.stage_id, first.group_id)
    if scope[0] != competition.pk:
        raise PoolStandingsValidationError(
            "Standings must belong to the supplied pool competition."
        )

    entry_ids = set()
    for row in rows:
        if not isinstance(row, PoolStandingRow):
            raise PoolStandingsValidationError(
                "Pool standings ranking requires derived pool standing rows."
            )
        if (row.competition_id, row.stage_id, row.group_id) != scope:
            raise PoolStandingsValidationError(
                "Pool standings ranking requires exactly one table scope."
            )
        if row.entry.competition_id != competition.pk:
            raise PoolStandingsValidationError(
                "Each standing entry must belong to the supplied competition."
            )
        if row.entry_id in entry_ids:
            raise PoolStandingsValidationError(
                "A pool table may contain an entry only once."
            )
        entry_ids.add(row.entry_id)


def _validate_source_matches(
    entries: Iterable[PoolCompetitionEntry],
    matches: Iterable[PoolMatch],
    *,
    stage: PoolCompetitionStage,
    group: PoolGroup | None,
    allow_outside_entries: bool = False,
) -> None:
    """Fail closed if corrupt/imported data could leak into a table scope."""

    entry_ids = {entry.pk for entry in entries}
    for pool_match in matches:
        if (
            pool_match.competition_id != stage.competition_id
            or pool_match.stage_id != stage.pk
            or pool_match.group_id != (group.pk if group is not None else None)
        ):
            raise PoolStandingsValidationError(
                "A source match is outside the requested pool standings scope."
            )
        if pool_match.status != PoolMatch.Status.FINISHED:
            raise PoolStandingsValidationError(
                "Only finished matches can be used for pool standings."
            )
        if pool_match.is_in_correction_mode:
            raise PoolStandingsValidationError(
                "A result open for correction cannot be used for pool standings."
            )
        if pool_match.result_method not in _ELIGIBLE_RESULT_METHODS:
            raise PoolStandingsValidationError(
                "This result method is not eligible for pool standings."
            )
        participant_ids = {pool_match.home_entry_id, pool_match.away_entry_id}
        if pool_match.winner_id not in participant_ids:
            raise PoolStandingsValidationError(
                "An eligible pool result must name one match participant as winner."
            )
        if not allow_outside_entries and not participant_ids.issubset(entry_ids):
            raise PoolStandingsValidationError(
                "A source match contains an entry outside this pool table."
            )


def _apply_eligible_match(
    home: _MutableStandingRow,
    away: _MutableStandingRow,
    pool_match: PoolMatch,
    *,
    competition: PoolCompetition,
) -> None:
    """Apply one already-validated, eligible result to two mutable rows."""

    if pool_match.home_score < 0 or pool_match.away_score < 0:
        raise PoolStandingsValidationError("Pool match scores cannot be negative.")

    home.played += 1
    away.played += 1
    home.racks_for += pool_match.home_score
    home.racks_against += pool_match.away_score
    away.racks_for += pool_match.away_score
    away.racks_against += pool_match.home_score

    winner_points, loser_points = point_allocation_for_pool_match(
        competition,
        pool_match,
        _competition_is_current=True,
    )
    if pool_match.winner_id == pool_match.home_entry_id:
        home.won += 1
        away.lost += 1
        home.points += winner_points
        away.points += loser_points
    elif pool_match.winner_id == pool_match.away_entry_id:
        away.won += 1
        home.lost += 1
        away.points += winner_points
        home.points += loser_points
    else:  # Defensive backstop for callers of the public mini-table helper.
        raise PoolStandingsValidationError(
            "An eligible pool result must name one match participant as winner."
        )


def _recent_form_by_entry(matches: Iterable[PoolMatch]) -> dict[int, tuple[str, ...]]:
    """Return up to five latest eligible W/L results for each exact table entry."""
    per_entry: dict[int, list[tuple[object, int, str]]] = defaultdict(list)
    for pool_match in matches:
        completed_at = pool_match.completed_at or pool_match.updated_at
        for entry_id in (pool_match.home_entry_id, pool_match.away_entry_id):
            if not entry_id:
                continue
            result = "W" if pool_match.winner_id == entry_id else "L"
            per_entry[entry_id].append((completed_at, pool_match.pk, result))
    return {
        entry_id: tuple(
            result
            for _completed_at, _pk, result in sorted(
                values,
                key=lambda value: (value[0], value[1]),
                reverse=True,
            )[:5]
        )
        for entry_id, values in per_entry.items()
    }


def _rank_tied_rows(
    rows: Sequence[PoolStandingRow],
    criteria: Sequence[str],
    source_matches: Sequence[PoolMatch],
    *,
    competition: PoolCompetition,
    criterion_index: int,
    first_head_to_head_index: int | None,
) -> list[PoolStandingRow]:
    if len(rows) < 2:
        return list(rows)
    if criterion_index >= len(criteria):
        return _deterministic_entry_order(rows)

    criterion = criteria[criterion_index]
    values = _criterion_values(
        criterion,
        rows,
        source_matches,
        competition=competition,
    )
    buckets: dict[object, list[PoolStandingRow]] = defaultdict(list)
    for row in rows:
        buckets[values[row.entry_id]].append(row)

    if len(buckets) == 1:
        return _rank_tied_rows(
            rows,
            criteria,
            source_matches,
            competition=competition,
            criterion_index=criterion_index + 1,
            first_head_to_head_index=first_head_to_head_index,
        )

    ranked: list[PoolStandingRow] = []
    for value in sorted(buckets, reverse=True):
        subgroup = buckets[value]
        if len(subgroup) == 1:
            ranked.extend(subgroup)
            continue

        # A multi-entry head-to-head table may split a group only partly.  In
        # that case restart head-to-head for the smaller remaining subgroup,
        # as the mini-table must be recalculated against just those entrants.
        if (
            criterion == Criterion.HEAD_TO_HEAD
            and len(rows) > 2
            and len(subgroup) < len(rows)
            and first_head_to_head_index is not None
        ):
            next_index = first_head_to_head_index
        else:
            next_index = criterion_index + 1
        ranked.extend(
            _rank_tied_rows(
                subgroup,
                criteria,
                source_matches,
                competition=competition,
                criterion_index=next_index,
                first_head_to_head_index=first_head_to_head_index,
            )
        )
    return ranked


def _criterion_values(
    criterion: str,
    rows: Sequence[PoolStandingRow],
    source_matches: Sequence[PoolMatch],
    *,
    competition: PoolCompetition,
) -> dict[int, object]:
    if criterion == _TABLE_POINTS_CRITERION:
        return {row.entry_id: row.points for row in rows}
    if criterion == Criterion.MATCHES_WON:
        return {row.entry_id: row.won for row in rows}
    if criterion == Criterion.RACK_DIFFERENCE:
        return {row.entry_id: row.rack_difference for row in rows}
    if criterion == Criterion.RACKS_WON:
        return {row.entry_id: row.racks_for for row in rows}
    if criterion == Criterion.HEAD_TO_HEAD:
        mini_table = head_to_head_mini_table(
            rows,
            source_matches,
            competition=competition,
        )
        return {entry_id: row.value for entry_id, row in mini_table.items()}

    # ``configured_pool_tie_breakers`` validates direct database data before
    # this point. Keep this branch defensive for programmatic callers.
    raise PoolStandingsValidationError("Unknown pool tie-breaker criterion.")


def _deterministic_entry_order(
    rows: Sequence[PoolStandingRow],
) -> list[PoolStandingRow]:
    """Last-resort stable order: seed, then public name, then primary key."""

    def key(row: PoolStandingRow):
        seed = row.entry.seed
        return (
            seed is None,
            seed if seed is not None else 0,
            row.entry.display_name.casefold(),
            row.entry_id,
        )

    return sorted(rows, key=key)
