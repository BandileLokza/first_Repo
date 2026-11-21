"""League Manager's dedicated finished-match goal editor."""

import logging
import re
from copy import copy
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from matches.bracket import record_blocked_bracket_correction
from matches.finished_scorers import (
    CORRECTION_SALT,
    SCORER_SALT,
    can_manage_finished_scorers,
    goal_capacity,
    save_finished_goal_scorers,
    scorer_snapshot,
)
from matches.forms import FinishedGoalEntryForm, MatchResultForm
from matches.models import Goal, Match, Player
from matches.result_corrections import (
    GOAL_COUNT_MISMATCH,
    RESULT_CONFLICT,
    SCORE_FIELDS,
    is_lock_conflict,
    score_inputs_unchanged,
)
from matches.result_outbox import RESULT_SAVED
from teams.models import Team

from .views import get_managed_league


logger = logging.getLogger(__name__)
SCORER_INVALID = "The goal scorer information could not be saved. Please review the highlighted fields."
SCORER_SAVE_FAILED = "The goal scorer information could not be saved. Please try again."


def _goal_sections(match, snapshot, data, *, edit_result):
    capacity = goal_capacity(snapshot)
    roster = list(
        Player.objects.filter(team_id__in=snapshot["teams"])
        .select_related("team").order_by("team__name", "name", "pk")
    )
    player_names = {player.pk: player.name for player in roster}
    sections = []
    goal_types = dict(Goal.GoalType.choices)
    for side in ("home", "away"):
        team = getattr(match, f"{side}_team")
        existing = [goal for goal in snapshot["goals"] if goal["team_id"] == team.pk]
        slots = [(f"goal-{goal['goal_id']}", goal) for goal in existing]
        if edit_result:
            # Construct only additions actually present in the submitted form.
            prefixes = dict.fromkeys(
                key.rpartition("-")[0] for key in (data or {})
                if re.fullmatch(rf"{side}-(0|[1-9][0-9]*)", key.rpartition("-")[0])
            )
            slots.extend((prefix, None) for prefix in prefixes or (f"{side}-0",))
        else:
            slots.extend((f"{side}-{index}", None) for index in range(max(0, capacity[team.pk])))
        rows = []
        for prefix, goal in slots:
            initial = {
                "player": goal["player_id"] if goal else None,
                "goal_type": goal["goal_type"] if goal else Goal.GoalType.NORMAL,
                "minute": goal["minute"] if goal else None,
            }
            options = {
                "match": match, "benefiting_team": team,
                "prefix": prefix, "initial": initial, "roster": roster,
            }
            fields = ("player", "goal_type", "minute", "player_name", "record_unknown")
            row_data = None
            if data is not None and any(f"{prefix}-{field}" in data for field in fields):
                row_data = {
                    f"{prefix}-{field}": data.get(f"{prefix}-{field}", initial.get(field, ""))
                    for field in fields
                }
            form = FinishedGoalEntryForm(row_data, **options)
            if row_data is not None and not form.has_changed():
                form = FinishedGoalEntryForm(**options)
            saved_name = (goal["player_name"] or player_names.get(goal["player_id"], "")) if goal else ""
            rows.append({
                "goal": goal,
                "goal_type_label": goal_types.get(goal["goal_type"], goal["goal_type"]) if goal else "",
                "form": form,
                "remove": bool(edit_result and goal and data and data.get(f"{prefix}-remove") == "1"),
                "saved_name": saved_name if saved_name != "Unknown Player" else "",
            })
        sections.append({
            "team": team,
            "opposing_team_id": match.away_team_id if side == "home" else match.home_team_id,
            "score": snapshot["result"][f"{side}_score"],
            "over_capacity": capacity[team.pk] < 0,
            "rows": rows,
            "addition_form": FinishedGoalEntryForm(
                match=match, benefiting_team=team, roster=roster, prefix=f"{side}-__index__",
                initial={"goal_type": Goal.GoalType.NORMAL},
            ) if edit_result else None,
        })
    return sections


def _outside_goal_rows(snapshot, data):
    goals = [goal for goal in snapshot["goals"] if goal["team_id"] not in snapshot["teams"]]
    teams = Team.objects.in_bulk({goal["team_id"] for goal in goals})
    goal_types = dict(Goal.GoalType.choices)
    return [
        {
            **goal,
            "team": teams.get(goal["team_id"], "Unknown team"),
            "type_label": goal_types.get(goal["goal_type"], goal["goal_type"]),
            "remove": data is not None and data.get(f"goal-{goal['goal_id']}-remove") == "1",
        }
        for goal in goals
    ]


def _submitted_goal_rows(sections, data, *, edit_result, outside_goals):
    allowed_fields = {"csrfmiddlewaretoken", "version"}
    if edit_result:
        allowed_fields.update(SCORE_FIELDS)
    rows = []
    valid = True
    for section in sections:
        for row in section["rows"]:
            form = row["form"]
            allowed_fields.update(form.add_prefix(field) for field in form.fields)
            if edit_result and row["goal"]:
                allowed_fields.add(form.add_prefix("remove"))
                if row["remove"]:
                    rows.append({
                        "goal_id": row["goal"]["goal_id"],
                        "team_id": section["team"].pk,
                        "remove": True,
                    })
                    continue
            if not form.is_bound:
                continue
            if not form.is_valid():
                valid = False
                continue
            cleaned = form.cleaned_data
            player = cleaned["player"]
            rows.append({
                "goal_id": row["goal"]["goal_id"] if row["goal"] else None,
                "team_id": section["team"].pk,
                "key": form.prefix,
                "player_id": player.pk if player else None,
                "new_player_name": cleaned["player_name"],
                "goal_type": cleaned["goal_type"],
                "minute": cleaned["minute"],
                "record_unknown": cleaned["record_unknown"],
            })
    for goal in outside_goals:
        allowed_fields.add(f"goal-{goal['goal_id']}-remove")
        if goal["remove"]:
            rows.append({"goal_id": goal["goal_id"], "team_id": goal["team_id"], "remove": True})
    if set(data) - allowed_fields or any(len(values) != 1 for _, values in data.lists()):
        raise ValidationError("The submission contains goal fields outside this match's available slots.")
    return rows if valid else None


@login_required
@require_http_methods(["GET", "POST"])
def finished_goal_scorers(request, slug, match_id, *, edit_result=False):
    league = get_managed_league(request, slug)
    view_name = "league_manager:edit_finished_match" if edit_result else "league_manager:finished_goal_scorers"
    salt = CORRECTION_SALT if edit_result else SCORER_SALT
    match = get_object_or_404(
        Match.objects.select_related("home_team", "away_team", "stage", "group", "competition__series"),
        pk=match_id, competition=league,
    )
    if not can_manage_finished_scorers(request.user, match):
        raise PermissionDenied("This match is read-only.")

    data = request.POST if request.method == "POST" else None
    score_form = MatchResultForm(data, instance=copy(match)) if edit_result else None
    save_failed = "The match corrections could not be saved. Please try again." if edit_result else SCORER_SAVE_FAILED

    if data is not None:
        version = request.POST.get("version", "")
        try:
            submission_id, snapshot = signing.loads(version, salt=salt)
        except signing.BadSignature:
            messages.error(request, RESULT_CONFLICT)
            return redirect(view_name, slug=slug, match_id=match_id)
        if (
            snapshot["match_id"] != match.pk or snapshot["competition_id"] != league.pk
            or snapshot["teams"] != [match.home_team_id, match.away_team_id]
        ):
            messages.error(request, RESULT_CONFLICT)
            return redirect(view_name, slug=slug, match_id=match_id)
        sections = _goal_sections(match, snapshot, data, edit_result=edit_result)
        outside_goals = _outside_goal_rows(snapshot, data) if edit_result else []
        try:
            rows = _submitted_goal_rows(sections, data, edit_result=edit_result, outside_goals=outside_goals)
            scores_valid = score_form is None or score_form.is_valid()
            if rows is not None and scores_valid:
                result = None
                if score_form is not None:
                    result = snapshot["result"]
                    if not score_inputs_unchanged(match, score_form.cleaned_data, result):
                        result = {"status": "FINISHED", **score_form.normalized_result}
                result_changed = save_finished_goal_scorers(
                    match, actor=request.user, expected=snapshot,
                    submission_id=submission_id, rows=rows, result=result,
                )
        except ValidationError as error:
            if edit_result:
                record_blocked_bracket_correction(error, actor=request.user)
            messages.error(request, " ".join(error.messages))
            if RESULT_CONFLICT in error.messages:
                return redirect(view_name, slug=slug, match_id=match_id)
        except Match.DoesNotExist:
            messages.error(request, RESULT_CONFLICT)
            return redirect("league_manager:result_update", slug=slug)
        except OperationalError as error:
            logger.exception("Database error saving goal scorers for match %s", match.pk)
            if is_lock_conflict(error):
                messages.error(request, RESULT_CONFLICT)
                if not edit_result:
                    return redirect(view_name, slug=slug, match_id=match_id)
            else:
                messages.error(request, save_failed)
        except PermissionDenied:
            raise
        except Exception:
            logger.exception("Failed to save goal scorers for match %s", match.pk)
            messages.error(request, save_failed)
        else:
            if rows is None or not scores_valid:
                messages.error(
                    request,
                    "Review the highlighted scores and goal details." if edit_result else SCORER_INVALID,
                )
            else:
                if edit_result:
                    messages.success(request, RESULT_SAVED if result_changed else "Match corrections saved.")
                else:
                    messages.success(request, "Goal scorer information updated successfully.")
                return redirect(view_name, slug=slug, match_id=match_id)
    else:
        goals = match.goals.filter(canceled=False).order_by("pk")
        snapshot = scorer_snapshot(match, goals)
        version = signing.dumps((str(uuid4()), snapshot), salt=salt, compress=True)
        sections = _goal_sections(match, snapshot, None, edit_result=edit_result)
        outside_goals = _outside_goal_rows(snapshot, None) if edit_result else []

    return render(request, "league_manager/finished_goal_scorers.html", {
        "league": league,
        "match": match,
        "version": version,
        "edit_result": edit_result,
        "score_form": score_form,
        "sections": sections,
        "goal_count_warning": GOAL_COUNT_MISMATCH if any(s["over_capacity"] for s in sections) else "",
        "invalid_goal_team": not edit_result and any(
            goal["team_id"] not in snapshot["teams"] for goal in snapshot["goals"]
        ),
        "outside_goals": outside_goals,
    })
