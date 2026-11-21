"""Central state machine for match-control visibility and backend checks."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from .knockout import (
    is_deciding_leg,
    knockout_is_unresolved,
    require_configured_knockout_match,
)
from .models import FootballKnockoutRoundRule


@dataclass(frozen=True)
class MatchControlState:
    can_start_first_half: bool = False
    can_end_first_half: bool = False
    can_start_second_half: bool = False
    can_end_regulation: bool = False
    can_start_extra_time: bool = False
    can_end_extra_time_first_half: bool = False
    can_start_extra_time_second_half: bool = False
    can_end_extra_time: bool = False
    can_start_penalties: bool = False
    can_finish_match: bool = False
    can_finish_penalties: bool = False
    can_add_goal: bool = False
    can_abandon: bool = False
    unavailable_reason: str = ""


def get_match_control_state(match):
    if match.status in {"FINISHED", "ABANDONED"}:
        return MatchControlState()

    # These are the only controls shared by every football match.  Extra-time
    # and shootout controls are added below only after the knockout rule and
    # tie state prove that they are legal.
    base = {
        "can_start_first_half": (
            match.status == "UPCOMING" and match.phase == "NONE"
        ),
        "can_end_first_half": (
            match.status == "LIVE" and match.phase == "FIRST_HALF"
        ),
        "can_start_second_half": (
            match.status == "LIVE" and match.phase == "BREAK"
        ),
        "can_end_regulation": (
            match.status == "LIVE" and match.phase == "SECOND_HALF"
        ),
        "can_add_goal": (
            match.status == "LIVE"
            and match.phase in {
                "FIRST_HALF",
                "SECOND_HALF",
            }
        ),
        "can_abandon": match.status in {"UPCOMING", "LIVE"},
    }

    if match.stage.stage_type != "KNOCKOUT":
        if (
            match.status == "LIVE"
            and match.phase == "REGULATION_ENDED"
        ):
            base["can_finish_match"] = True
        return MatchControlState(**base)

    try:
        rule = require_configured_knockout_match(match)
        deciding_leg = is_deciding_leg(match, rule)
    except ValidationError as error:
        return MatchControlState(
            can_abandon=base["can_abandon"],
            unavailable_reason=" ".join(error.messages),
        )

    def unavailable(reason):
        if isinstance(reason, ValidationError):
            reason = " ".join(reason.messages)
        return MatchControlState(
            can_abandon=base["can_abandon"],
            unavailable_reason=reason,
        )

    # A deciding second leg cannot complete out of order.  This also prevents
    # a manually selected extra-time phase from bypassing an unfinished first
    # leg before the normal end-regulation transition runs.
    if base["can_end_regulation"] and deciding_leg:
        try:
            knockout_is_unresolved(match, match.home_score, match.away_score)
        except ValidationError as error:
            return unavailable(error)

    if match.phase == "REGULATION_ENDED":
        if match.status != "LIVE":
            return unavailable("Regulation can only continue while the match is live.")
        if (
            match.regulation_home_score is None
            or match.regulation_away_score is None
        ):
            return MatchControlState(
                can_abandon=base["can_abandon"],
                unavailable_reason="Record the regulation score before continuing this knockout match.",
            )
        try:
            unresolved = knockout_is_unresolved(
                match,
                match.regulation_home_score,
                match.regulation_away_score,
            )
        except ValidationError as error:
            return unavailable(error)
        if not deciding_leg or not unresolved:
            base["can_finish_match"] = True
        elif (
            rule.decider_policy
            == FootballKnockoutRoundRule.DeciderPolicy.DIRECT_TO_PENALTIES
        ):
            base["can_start_penalties"] = True
        else:
            base["can_start_extra_time"] = True

    extra_time_phases = {
        "EXTRA_FIRST_HALF",
        "EXTRA_BREAK",
        "EXTRA_SECOND_HALF",
        "EXTRA_ENDED",
    }
    if match.phase in extra_time_phases:
        if match.status != "LIVE":
            return unavailable("Extra time can only run while the match is live.")
        if not deciding_leg:
            return unavailable("Extra time is only available in a deciding knockout leg.")
        if (
            rule.decider_policy
            != FootballKnockoutRoundRule.DeciderPolicy.EXTRA_TIME_THEN_PENALTIES
        ):
            return unavailable("This round goes directly to penalties when level.")
        if (
            match.regulation_home_score is None
            or match.regulation_away_score is None
        ):
            return unavailable("Record the regulation score before starting extra time.")
        try:
            unresolved_after_regulation = knockout_is_unresolved(
                match,
                match.regulation_home_score,
                match.regulation_away_score,
            )
        except ValidationError as error:
            return unavailable(error)
        if not unresolved_after_regulation:
            return unavailable("The tie was already decided after regulation.")

        if match.phase == "EXTRA_FIRST_HALF":
            base["can_end_extra_time_first_half"] = True
            base["can_add_goal"] = True
        elif match.phase == "EXTRA_BREAK":
            base["can_start_extra_time_second_half"] = True
        elif match.phase == "EXTRA_SECOND_HALF":
            base["can_end_extra_time"] = True
            base["can_add_goal"] = True
        else:
            try:
                unresolved_after_extra_time = knockout_is_unresolved(match)
            except ValidationError as error:
                return unavailable(error)
            if unresolved_after_extra_time:
                base["can_start_penalties"] = True
            else:
                base["can_finish_match"] = True

    if match.phase == "PENALTIES":
        if match.status != "LIVE":
            return unavailable("A penalty shootout can only run while the match is live.")
        if not deciding_leg:
            return unavailable("Penalty shootouts are only available in a deciding knockout leg.")
        if (
            match.regulation_home_score is None
            or match.regulation_away_score is None
        ):
            return unavailable("Record the regulation score before a penalty shootout.")
        try:
            unresolved_after_regulation = knockout_is_unresolved(
                match,
                match.regulation_home_score,
                match.regulation_away_score,
            )
        except ValidationError as error:
            return unavailable(error)
        if not unresolved_after_regulation:
            return unavailable("The tie was already decided before penalties.")

        if (
            rule.decider_policy
            == FootballKnockoutRoundRule.DeciderPolicy.DIRECT_TO_PENALTIES
        ):
            if (
                match.home_score != match.regulation_home_score
                or match.away_score != match.regulation_away_score
            ):
                return unavailable(
                    "Direct-to-penalties rounds cannot contain extra-time goals."
                )
        else:
            try:
                unresolved_after_extra_time = knockout_is_unresolved(match)
            except ValidationError as error:
                return unavailable(error)
            if not unresolved_after_extra_time:
                return unavailable("The tie was decided in extra time.")

        base["can_finish_penalties"] = True

    return MatchControlState(**base)
