"""Transactional goal and score corrections for an ordinary finished fixture."""

import hashlib
import json
from uuid import UUID, uuid5

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from league_manager.models import ActivityLog
from league_manager.permissions import can_correct_finished_result
from league_manager.services import record_activity

from .models import Goal, Match, Player
from .result_corrections import (
    GOAL_COUNT_MISMATCH,
    RESULT_CONFLICT,
    apply_finished_result_correction,
    result_snapshot,
)
from .result_outbox import lock_result_competition
from .services import resolve_goal_scorer


SCORER_SALT = "matches.finished-scorers"
CORRECTION_SALT = "matches.finished-match-correction"


def finished_scorer_eligible(match):
    return match.status == "FINISHED" and match.abandonment_resolution in {
        Match.AbandonmentResolution.NONE, Match.AbandonmentResolution.RESUMED,
    }


def can_manage_finished_scorers(user, match):
    return finished_scorer_eligible(match) and can_correct_finished_result(user, match)


def goal_snapshot(goal):
    return {
        "goal_id": goal.pk,
        "team_id": goal.team_id,
        "player_id": goal.player_id,
        "player_name": goal.player_name,
        "goal_type": goal.goal_type,
        "minute": goal.minute,
        "period": goal.period,
    }


def scorer_snapshot(match, goals):
    return {
        "match_id": match.pk,
        "competition_id": match.competition_id,
        "teams": [match.home_team_id, match.away_team_id],
        "result": result_snapshot(match),
        "goals": [goal_snapshot(goal) for goal in goals],
    }


def goal_capacity(snapshot, result=None):
    home, away = snapshot["teams"]
    scores = snapshot["result"] if result is None else result
    capacity = {
        home: scores["home_score"],
        away: scores["away_score"],
    }
    for goal in snapshot["goals"]:
        if goal["team_id"] in capacity:
            capacity[goal["team_id"]] -= 1
    return capacity


def _validate_goal_capacity(match, result, capacity):
    errors = []
    for team, field in ((match.home_team, "home_score"), (match.away_team, "away_score")):
        excess = -capacity[team.pk]
        if excess > 0:
            score = result[field]
            recorded = score + excess
            recorded_noun = "goal" if recorded == 1 else "goals"
            excess_noun = "goal" if excess == 1 else "goals"
            errors.append(
                f"{team} has {recorded} recorded {recorded_noun}, but the proposed final score is {score}. "
                f"Remove or correct {excess} {excess_noun} before saving."
            )
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def save_finished_goal_scorers(match, *, actor, expected, submission_id, rows, result=None):
    # Share the competition -> match -> active goals order with result writers.
    competition = lock_result_competition(match.competition_id, nowait=True)
    match = Match.objects.select_for_update(of=("self",), nowait=True).select_related(
        "home_team", "away_team", "stage", "knockout_tie",
    ).get(pk=match.pk, competition=competition)
    match.competition = competition
    if not can_manage_finished_scorers(actor, match):
        raise PermissionDenied("This match is read-only.")

    payload = rows if result is None else {"rows": rows, "result": result}
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    submission = {"submission_id": submission_id, "submission_hash": fingerprint}
    previous_submission = match.activity_logs.filter(
        actor=actor, new_values__submission_id=submission_id,
        action__in=[
            ActivityLog.Action.GOAL_ADDED, ActivityLog.Action.GOAL_EDITED,
            ActivityLog.Action.GOAL_CANCELLED, ActivityLog.Action.RESULT_CORRECTED,
        ],
    ).values_list("new_values", flat=True).first()
    if previous_submission is not None:
        if previous_submission.get("submission_hash") == fingerprint:
            return False
        raise ValidationError(RESULT_CONFLICT)

    goals = {
        goal.pk: goal for goal in match.goals.select_for_update()
        .filter(canceled=False).order_by("pk")
    }
    current = scorer_snapshot(match, goals.values())
    if current != expected:
        raise ValidationError(RESULT_CONFLICT)
    capacity = goal_capacity(current, result)

    submitted_goals = set()
    for row in rows:
        team_id = row["team_id"]
        goal_id = row["goal_id"]
        if team_id not in capacity and not row.get("remove"):
            raise ValidationError("Invalid goal details for this match.")
        if goal_id is not None:
            if goal_id not in goals or goals[goal_id].team_id != team_id or goal_id in submitted_goals:
                raise ValidationError("Invalid goal details for this match.")
            submitted_goals.add(goal_id)
        if row.get("remove"):
            if result is None or goal_id is None:
                raise ValidationError("Only an existing goal can be removed in a match correction.")
            if team_id in capacity:
                capacity[team_id] += 1
        else:
            if row["goal_type"] not in Goal.GoalType.values:
                raise ValidationError("Invalid goal details for this match.")
            if goal_id is None:
                capacity[team_id] -= 1
                if result is None and capacity[team_id] < 0:
                    raise ValidationError(GOAL_COUNT_MISMATCH)
    if result is not None:
        if any(goal.team_id not in capacity and goal.pk not in submitted_goals for goal in goals.values()):
            raise ValidationError("Remove goals credited to teams outside this fixture before saving.")
        _validate_goal_capacity(match, result, capacity)

    player_ids = {row["player_id"] for row in rows if row.get("player_id")}
    selected_players = {
        player.pk: player for player in Player.objects.select_for_update()
        .filter(pk__in=player_ids).order_by("pk")
    }
    for row in rows:
        goal = goals.get(row["goal_id"])
        old_values = goal_snapshot(goal) if goal else {}
        if row.get("remove"):
            # Preserve the recorded details even if historical attribution is invalid.
            match.goals.filter(pk=goal.pk).update(canceled=True)
            record_activity(
                actor=actor, competition=competition, match=match,
                action=ActivityLog.Action.GOAL_CANCELLED,
                description=f"Recorded goal cancelled for {goal.team}: {goal.player_name or 'Unknown Scorer'}.",
                old_values={**old_values, "canceled": False},
                new_values={**old_values, "canceled": True, **submission},
            )
            continue
        player = selected_players.get(row["player_id"])
        if row["player_id"] and player is None:
            raise ValidationError("The selected player is no longer on the roster.")
        player = resolve_goal_scorer(
            match=match,
            benefiting_team_id=row["team_id"],
            goal_type=row["goal_type"],
            player=player,
            new_player_name=row["new_player_name"],
            allow_unattributed_own_goal=True,
        )
        if goal is None:
            goal = Goal(
                match=match, team_id=row["team_id"],
                idempotency_key=uuid5(UUID(submission_id), row["key"]),
            )
        goal.player = player
        if player and (player.pk != old_values.get("player_id") or not goal.player_name):
            goal.player_name = player.name
        elif not player and (old_values.get("player_id") or row["record_unknown"]):
            goal.player_name = ""
        goal.goal_type = row["goal_type"]
        goal.minute = row["minute"]
        if old_values:
            if goal_snapshot(goal) == old_values:
                continue
            goal.save(update_fields=["player", "player_name", "goal_type", "minute"])
        else:
            goal.save()
        record_activity(
            actor=actor, competition=competition, match=match,
            action=ActivityLog.Action.GOAL_EDITED if old_values else ActivityLog.Action.GOAL_ADDED,
            description=f"Goal scorer details updated for {goal.team}: {goal.player_name or 'Scorer not recorded'}.",
            old_values=old_values,
            new_values={**goal_snapshot(goal), **submission},
        )

    result_changed = result is not None and result != current["result"]
    if result_changed:
        apply_finished_result_correction(
            match, result, actor=actor, previous=current["result"], submission=submission,
        )
    return result_changed
