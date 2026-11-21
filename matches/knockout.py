"""Football-specific knockout configuration and result validation helpers.

This module deliberately keeps football tie logic separate from a future
sport-neutral bracket implementation.  It is the single source of truth for
round rules, aggregate scoring and official knockout-result validation.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import (
    FootballKnockoutRoundRule,
    FootballKnockoutTie,
    Match,
)


def get_round_rule(stage):
    """Return configured football rules for a knockout stage.

    Legacy knockout stages are intentionally not given an inferred rule.  An
    administrator must configure them explicitly, which avoids accidentally
    treating ambiguous historical fixtures as a one- or two-leg tie.
    """
    if stage.stage_type != "KNOCKOUT":
        return None

    try:
        return stage.football_knockout_rule
    except FootballKnockoutRoundRule.DoesNotExist as error:
        raise ValidationError(
            "Configure the football rules for this knockout round first."
        ) from error


def create_default_round_rule(stage):
    """Create the default rule only for a newly configured knockout stage."""
    if stage.stage_type != "KNOCKOUT":
        return None
    rule, _ = FootballKnockoutRoundRule.objects.get_or_create(stage=stage)
    return rule


def _pair_query(stage, team_one_id, team_two_id):
    return Match.objects.filter(stage=stage).filter(
        Q(home_team_id=team_one_id, away_team_id=team_two_id)
        | Q(home_team_id=team_two_id, away_team_id=team_one_id)
    )


def validate_fixture_knockout_config(match):
    """Validate a new or edited knockout fixture without saving it."""
    if not match.stage_id or match.stage.stage_type != "KNOCKOUT":
        return

    if match.leg is None or match.leg < 1:
        raise ValidationError({"leg": "A match leg must be at least 1."})

    rule = get_round_rule(match.stage)
    if match.leg not in range(1, rule.number_of_legs + 1):
        raise ValidationError({
            "leg": (
                f"{match.stage.name} is configured for "
                f"{rule.get_number_of_legs_display().lower()}."
            )
        })

    if not match.home_team_id or not match.away_team_id:
        return

    duplicates = _pair_query(
        match.stage,
        match.home_team_id,
        match.away_team_id,
    ).filter(leg=match.leg)
    if match.pk:
        duplicates = duplicates.exclude(pk=match.pk)
    if duplicates.exists():
        raise ValidationError(
            "This knockout pairing already has a fixture for this leg."
        )


def _unambiguous_unlinked_pair_matches(match, tie, rule):
    """Return safe legacy fixtures that may be attached to ``tie``.

    A fixture is inferred only if it is the same pairing, all its leg numbers
    are valid for the configured round, and each leg occurs once.  Any
    duplicate or out-of-range historical data stays unlinked for an
    administrator to correct explicitly.
    """
    candidates = list(
        _pair_query(match.stage, tie.team_one_id, tie.team_two_id)
        .filter(knockout_tie__isnull=True)
        .exclude(pk=match.pk)
        .order_by("leg", "id")
    )
    all_pair_matches = list(
        _pair_query(match.stage, tie.team_one_id, tie.team_two_id)
        .exclude(pk=match.pk)
        .order_by("leg", "id")
    )
    leg_numbers = [item.leg for item in all_pair_matches]
    valid_legs = set(range(1, rule.number_of_legs + 1))
    if (
        any(leg not in valid_legs for leg in leg_numbers)
        or len(leg_numbers) != len(set(leg_numbers))
    ):
        return []

    used_legs = set(tie.matches.values_list("leg", flat=True))
    if any(item.leg in used_legs for item in candidates):
        return []
    return candidates


@transaction.atomic
def assign_match_to_knockout_tie(match):
    """Attach a configured knockout fixture to its canonical football tie.

    New fixtures are paired immediately.  Older unlinked fixtures are joined
    only when that inference is demonstrably unambiguous.
    """
    if not match.stage_id or match.stage.stage_type != "KNOCKOUT":
        match.knockout_tie = None
        return None

    validate_fixture_knockout_config(match)
    if not match.home_team_id or not match.away_team_id:
        return None

    team_one_id, team_two_id = sorted((match.home_team_id, match.away_team_id))
    tie, _ = FootballKnockoutTie.objects.get_or_create(
        stage=match.stage,
        team_one_id=team_one_id,
        team_two_id=team_two_id,
    )
    rule = get_round_rule(match.stage)

    for older_match in _unambiguous_unlinked_pair_matches(match, tie, rule):
        older_match.knockout_tie = tie
        older_match.save(update_fields=["knockout_tie"])

    match.knockout_tie = tie
    return tie


def require_configured_knockout_match(match):
    if match.stage.stage_type != "KNOCKOUT":
        raise ValidationError("This action is only available for knockout matches.")
    rule = get_round_rule(match.stage)
    if not match.knockout_tie_id:
        raise ValidationError(
            "This knockout fixture is not linked to a tie. Edit and save the "
            "fixture after confirming its round configuration."
        )
    if match.leg not in range(1, rule.number_of_legs + 1):
        raise ValidationError("The fixture leg does not match the round rules.")
    return rule


def is_deciding_leg(match, rule=None):
    rule = rule or require_configured_knockout_match(match)
    return rule.number_of_legs == 1 or match.leg == rule.number_of_legs


def _validate_previous_legs(match, rule):
    if rule.number_of_legs == 1:
        return

    previous_legs = list(
        match.knockout_tie.matches.filter(leg__lt=match.leg)
        .exclude(pk=match.pk)
        .order_by("leg", "id")
    )
    required_leg_numbers = set(range(1, match.leg))
    found_leg_numbers = {item.leg for item in previous_legs}
    if found_leg_numbers != required_leg_numbers:
        raise ValidationError(
            "All earlier legs in this knockout tie must exist before this result."
        )
    if any(item.status != "FINISHED" for item in previous_legs):
        raise ValidationError(
            "All earlier legs in this knockout tie must be finished first."
        )


def aggregate_scores(match, home_score=None, away_score=None):
    """Return aggregate totals keyed to the tie's canonical team order."""
    rule = require_configured_knockout_match(match)
    _validate_previous_legs(match, rule)

    team_one_total = 0
    team_two_total = 0
    previous_matches = match.knockout_tie.matches.exclude(pk=match.pk).filter(
        leg__lt=match.leg,
        status="FINISHED",
    )
    for item in previous_matches:
        if item.home_team_id == match.knockout_tie.team_one_id:
            team_one_total += item.home_score
            team_two_total += item.away_score
        else:
            team_one_total += item.away_score
            team_two_total += item.home_score

    current_home = match.home_score if home_score is None else home_score
    current_away = match.away_score if away_score is None else away_score
    if match.home_team_id == match.knockout_tie.team_one_id:
        team_one_total += current_home
        team_two_total += current_away
    else:
        team_one_total += current_away
        team_two_total += current_home

    return team_one_total, team_two_total


def aggregate_display(match):
    """Return an aggregate summary for a configured two-leg tie, if useful."""
    if not match.knockout_tie_id or match.stage.stage_type != "KNOCKOUT":
        return None
    try:
        rule = require_configured_knockout_match(match)
    except (TypeError, ValidationError):
        return None
    if (
        rule.number_of_legs != FootballKnockoutRoundRule.Legs.TWO
        or match.leg != rule.number_of_legs
    ):
        return None

    try:
        team_one_score, team_two_score = aggregate_scores(match)
    except ValidationError:
        return None
    return {
        "team_one": match.knockout_tie.team_one,
        "team_two": match.knockout_tie.team_two,
        "team_one_score": team_one_score,
        "team_two_score": team_two_score,
    }


def resolve_completed_knockout_tie(tie):
    """Return the persisted Phase One winner once every required leg is final.

    This deliberately builds on the existing round-rule, deciding-leg and
    aggregate helpers.  It is the single winner resolver used by connected
    bracket progression; it never infers a winner from fixture ordering or
    team names.
    """
    if not tie.team_one_id or not tie.team_two_id:
        return None

    rule = get_round_rule(tie.stage)
    expected_legs = set(range(1, rule.number_of_legs + 1))
    matches = list(
        tie.matches.select_related("home_team", "away_team").order_by("leg", "id")
    )
    matches_by_leg = {match.leg: match for match in matches}

    if (
        len(matches_by_leg) != rule.number_of_legs
        or len(matches) != rule.number_of_legs
        or set(matches_by_leg) != expected_legs
    ):
        return None
    if any(match.status != "FINISHED" for match in matches_by_leg.values()):
        return None

    deciding_match = matches_by_leg[rule.number_of_legs]
    require_configured_knockout_match(deciding_match)

    if (
        deciding_match.penalty_home_score is not None
        and deciding_match.penalty_away_score is not None
    ):
        if deciding_match.penalty_home_score == deciding_match.penalty_away_score:
            raise ValidationError("Penalty shootout scores cannot be tied.")
        winner = (
            deciding_match.home_team
            if deciding_match.penalty_home_score > deciding_match.penalty_away_score
            else deciding_match.away_team
        )
    elif rule.number_of_legs == FootballKnockoutRoundRule.Legs.TWO:
        team_one_total, team_two_total = aggregate_scores(deciding_match)
        if team_one_total == team_two_total:
            raise ValidationError(
                "A completed two-leg tie must have an aggregate or penalty winner."
            )
        winner = tie.team_one if team_one_total > team_two_total else tie.team_two
    else:
        if deciding_match.home_score == deciding_match.away_score:
            raise ValidationError(
                "A completed knockout match must have a decisive winner."
            )
        winner = (
            deciding_match.home_team
            if deciding_match.home_score > deciding_match.away_score
            else deciding_match.away_team
        )

    return {
        "winner": winner,
        "resolution_method": (
            deciding_match.resolution_method
            or (
                "AGGREGATE"
                if rule.number_of_legs == FootballKnockoutRoundRule.Legs.TWO
                else "REGULATION"
            )
        ),
        "deciding_match": deciding_match,
    }


def knockout_is_unresolved(match, home_score=None, away_score=None):
    rule = require_configured_knockout_match(match)
    if not is_deciding_leg(match, rule):
        return False
    if rule.number_of_legs == 1:
        current_home = match.home_score if home_score is None else home_score
        current_away = match.away_score if away_score is None else away_score
        return current_home == current_away
    aggregate_home, aggregate_away = aggregate_scores(match, home_score, away_score)
    return aggregate_home == aggregate_away


def _score_pair(home_score, away_score, label, required=True):
    if home_score is None and away_score is None and not required:
        return None
    if home_score is None or away_score is None:
        raise ValidationError(f"Both {label.lower()} scores are required.")
    if not isinstance(home_score, int) or not isinstance(away_score, int):
        raise ValidationError(f"{label} scores must be whole numbers.")
    if home_score < 0 or away_score < 0:
        raise ValidationError(f"{label} scores cannot be negative.")
    return home_score, away_score


def validate_official_result(
    match,
    *,
    home_score,
    away_score,
    regulation_home_score=None,
    regulation_away_score=None,
    penalty_home_score=None,
    penalty_away_score=None,
):
    """Validate and normalize a result from every official entry path."""
    final_scores = _score_pair(home_score, away_score, "Final")
    regulation_scores = _score_pair(
        regulation_home_score,
        regulation_away_score,
        "Regulation",
        required=False,
    )
    if regulation_scores is None:
        regulation_scores = final_scores
    penalty_scores = _score_pair(
        penalty_home_score,
        penalty_away_score,
        "Penalty",
        required=False,
    )

    if (
        final_scores[0] < regulation_scores[0]
        or final_scores[1] < regulation_scores[1]
    ):
        raise ValidationError(
            "The final football score cannot be lower than the score after 90 minutes."
        )

    if match.stage.stage_type != "KNOCKOUT":
        if final_scores != regulation_scores:
            raise ValidationError(
                "League and group-stage matches cannot include extra time."
            )
        if penalty_scores is not None:
            raise ValidationError(
                "League and group-stage matches cannot use a penalty shootout."
            )
        return {
            "home_score": final_scores[0],
            "away_score": final_scores[1],
            "regulation_home_score": regulation_scores[0],
            "regulation_away_score": regulation_scores[1],
            "penalty_home_score": None,
            "penalty_away_score": None,
            "resolution_method": "REGULATION",
        }

    rule = require_configured_knockout_match(match)
    _validate_previous_legs(match, rule)

    if not is_deciding_leg(match, rule):
        if final_scores != regulation_scores or penalty_scores is not None:
            raise ValidationError(
                "Extra time and penalties are not allowed in the first leg."
            )
        resolution_method = "REGULATION"
    else:
        unresolved_at_regulation = knockout_is_unresolved(
            match,
            regulation_scores[0],
            regulation_scores[1],
        )

        if not unresolved_at_regulation:
            if final_scores != regulation_scores or penalty_scores is not None:
                raise ValidationError("The tie was already decided after regulation.")
            resolution_method = (
                "AGGREGATE" if rule.number_of_legs == 2 else "REGULATION"
            )
        elif (
            rule.decider_policy
            == FootballKnockoutRoundRule.DeciderPolicy.DIRECT_TO_PENALTIES
        ):
            if final_scores != regulation_scores:
                raise ValidationError(
                    "This round goes directly to penalties; extra-time goals cannot be entered."
                )
            if penalty_scores is None:
                raise ValidationError(
                    "Enter the penalty shootout score for this unresolved tie."
                )
            resolution_method = "PENALTIES"
        else:
            unresolved_after_extra_time = knockout_is_unresolved(
                match,
                final_scores[0],
                final_scores[1],
            )
            if unresolved_after_extra_time:
                if penalty_scores is None:
                    raise ValidationError(
                        "The tie is still level. Enter the penalty shootout score."
                    )
                resolution_method = "PENALTIES"
            else:
                if penalty_scores is not None:
                    raise ValidationError(
                        "A penalty shootout is not needed because extra time decided the tie."
                    )
                resolution_method = "EXTRA_TIME"

    if penalty_scores is not None and penalty_scores[0] == penalty_scores[1]:
        raise ValidationError("Penalty shootout scores cannot be tied.")

    return {
        "home_score": final_scores[0],
        "away_score": final_scores[1],
        "regulation_home_score": regulation_scores[0],
        "regulation_away_score": regulation_scores[1],
        "penalty_home_score": penalty_scores[0] if penalty_scores else None,
        "penalty_away_score": penalty_scores[1] if penalty_scores else None,
        "resolution_method": resolution_method,
    }
