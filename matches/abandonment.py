"""Authorised, transaction-safe resolution of abandoned football fixtures."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from leagues.models import Competition
from league_manager.models import ActivityLog
from league_manager.permissions import can_manage_abandonment_decisions
from league_manager.services import record_activity

from .knockout import validate_official_result
from .models import AbandonedMatchDecision, Match
from .services import rebuild_league_standings
from .result_outbox import cancel_result_processing


class AbandonedMatchResolutionError(ValidationError):
    """A user-facing business-rule error for the resolution workflow."""


def _decision_description(decision_type, note="", *, home_score=None, away_score=None):
    """Keep every decision auditable even when the optional note is blank."""

    note = (note or "").strip()
    if note:
        return note

    defaults = {
        AbandonedMatchDecision.DecisionType.REPLAY: (
            "Full replay officially scheduled."
        ),
        AbandonedMatchDecision.DecisionType.RESUME: (
            "Match approved for resumption."
        ),
        AbandonedMatchDecision.DecisionType.CONFIRM: (
            "Recorded score confirmed as official result."
        ),
        AbandonedMatchDecision.DecisionType.VOID: "Match officially voided.",
        "REOPEN": "Official decision reopened for correction.",
    }
    if decision_type == AbandonedMatchDecision.DecisionType.AWARD:
        return f"Official result awarded: {home_score}\N{EN DASH}{away_score}."
    return defaults[decision_type]


def _locked_match(match_id):
    """Lock in the same parent-then-fixture order as fixture deletion."""

    competition_id = Match.objects.filter(pk=match_id).values_list(
        "competition_id", flat=True
    ).first()
    if not competition_id:
        raise AbandonedMatchResolutionError("This match no longer exists.")
    Competition.objects.select_for_update(of=("self",)).get(pk=competition_id)
    match = (
        Match.objects.select_for_update(of=("self",))
        .select_related("competition", "stage", "group", "home_team", "away_team")
        .get(pk=match_id)
    )
    # A committed official decision supersedes pending ordinary work.
    # If the decision fails validation, its transaction also rolls this back.
    cancel_result_processing(match.pk)
    return match


def _require_abandoned_match(match, actor):
    if not can_manage_abandonment_decisions(actor, match.competition):
        raise PermissionDenied("You do not have permission to resolve this abandoned match.")
    if match.status != "ABANDONED":
        raise AbandonedMatchResolutionError(
            "Only an unresolved abandoned match can use this workflow."
        )
    existing = (
        AbandonedMatchDecision.objects.select_for_update()
        .filter(match=match, is_current=True)
        .first()
    )
    if existing:
        raise AbandonedMatchResolutionError(
            "This match already has an official resolution. Reopen that decision "
            "before recording a correction."
        )


def _require_non_knockout(match, operation):
    if match.stage.stage_type == "KNOCKOUT":
        raise AbandonedMatchResolutionError(
            f"{operation} is not available for a knockout fixture. Resume the "
            "existing fixture so the safeguarded bracket can determine its winner."
        )


def _snapshot(match):
    return {
        "status": match.status,
        "score": [match.home_score, match.away_score],
        "abandoned_score": [
            match.abandoned_home_score,
            match.abandoned_away_score,
        ],
        "abandoned_minute": match.abandoned_minute,
        "abandonment_resolution": match.abandonment_resolution,
    }


def _record_decision(*, match, actor, decision_type, description, **kwargs):
    decision = AbandonedMatchDecision(
        match=match,
        decided_by=actor,
        decision_type=decision_type,
        reason=description,
        **kwargs,
    )
    decision.full_clean()
    decision.save()
    return decision


def _log_resolution(match, actor, decision, before):
    record_activity(
        actor=actor,
        competition=match.competition,
        match=match,
        action=ActivityLog.Action.ABANDONMENT_RESOLVED,
        description=(
            f"Abandoned match resolved: {decision.get_decision_type_display()}. "
            f"{decision.reason}"
        ),
        old_values=before,
        new_values={
            **_snapshot(match),
            "decision_id": decision.id,
            "decision_type": decision.decision_type,
            "decision_description": decision.reason,
            "decision_reference": decision.decision_reference,
        },
    )


def _refresh_official_result(match):
    """Commit the decision, standings and prediction points together."""
    from .services import apply_match_to_standings_incrementally
    from predictions.utils import calculate_predictions_for_match

    match = apply_match_to_standings_incrementally(match)
    calculate_predictions_for_match(match)


def _requires_table_rebuild(match):
    return bool(
        match.table_updated
        and match.stage.stage_type in {"LEAGUE", "GROUP"}
    )


@transaction.atomic
def schedule_full_replay(*, match_id, actor, scheduled_for, venue="", note=""):
    """Create one fresh, linked replay while retaining the abandoned fixture."""

    match = _locked_match(match_id)
    _require_abandoned_match(match, actor)
    _require_non_knockout(match, "A full replay")
    if not scheduled_for:
        raise AbandonedMatchResolutionError("Set a date and time for the replay.")
    description = _decision_description(
        AbandonedMatchDecision.DecisionType.REPLAY,
        note,
    )
    before = _snapshot(match)

    replay = Match(
        competition=match.competition,
        stage=match.stage,
        group=match.group,
        home_team=match.home_team,
        away_team=match.away_team,
        replay_of=match,
        match_date=scheduled_for,
        location=(venue or match.location or "").strip() or None,
        gameweek=match.gameweek,
        leg=match.leg,
        status="UPCOMING",
        phase="NONE",
        home_score=0,
        away_score=0,
        minute=0,
    )
    try:
        replay.save(_allow_schedule_locked_replay=True)
    except IntegrityError as error:
        raise AbandonedMatchResolutionError(
            "An active replay fixture already exists for this abandoned match."
        ) from error

    match.abandonment_resolution = Match.AbandonmentResolution.REPLAY
    match.save(update_fields=["abandonment_resolution"])
    decision = _record_decision(
        match=match,
        actor=actor,
        decision_type=AbandonedMatchDecision.DecisionType.REPLAY,
        description=description,
        scheduled_for=scheduled_for,
        venue=replay.location or "",
        replay_match=replay,
    )
    if _requires_table_rebuild(match):
        rebuild_league_standings(match.competition)
    _log_resolution(match, actor, decision, before)
    return match, replay, decision


def _resume_phase(match, minute):
    phase = match.abandoned_phase
    paused_phases = {
        "BREAK": 45,
        "REGULATION_ENDED": 90,
        "EXTRA_BREAK": 105,
        "EXTRA_ENDED": 120,
        "PENALTIES": 90,
    }
    if phase in paused_phases:
        if minute < paused_phases[phase]:
            raise AbandonedMatchResolutionError(
                "The selected resumption minute is before the abandoned match phase."
            )
        return phase, None
    if phase not in {
        "FIRST_HALF",
        "SECOND_HALF",
        "EXTRA_FIRST_HALF",
        "EXTRA_SECOND_HALF",
    }:
        phase = "FIRST_HALF" if minute <= 45 else "SECOND_HALF"

    phase_bases = {
        "FIRST_HALF": 0,
        "SECOND_HALF": 45,
        "EXTRA_FIRST_HALF": 90,
        "EXTRA_SECOND_HALF": 105,
    }
    if minute < phase_bases[phase]:
        raise AbandonedMatchResolutionError(
            "The selected resumption minute is before the abandoned match phase."
        )
    return phase, phase_bases[phase]


@transaction.atomic
def resume_abandoned_match(
    *, match_id, actor, resume_minute=None, scheduled_for=None, venue="", note=""
):
    """Return an abandoned fixture to a valid live-clock state."""

    match = _locked_match(match_id)
    _require_abandoned_match(match, actor)
    description = _decision_description(
        AbandonedMatchDecision.DecisionType.RESUME,
        note,
    )
    before = _snapshot(match)
    if resume_minute is None:
        resume_minute = match.abandoned_minute
    if isinstance(resume_minute, bool) or not isinstance(resume_minute, int):
        raise AbandonedMatchResolutionError("Enter a whole-number resumption minute.")
    if resume_minute < 0:
        raise AbandonedMatchResolutionError("The resumption minute cannot be negative.")

    now = timezone.now()
    if scheduled_for and scheduled_for > now:
        raise AbandonedMatchResolutionError(
            "Resuming starts play immediately. Return at the scheduled time to resume this match."
        )
    phase, phase_base = _resume_phase(match, resume_minute)
    match.status = "LIVE"
    # The frozen on-field score remains authoritative while play is suspended.
    if match.abandoned_home_score is not None:
        match.home_score = match.abandoned_home_score
    if match.abandoned_away_score is not None:
        match.away_score = match.abandoned_away_score
    match.phase = phase
    match.minute = resume_minute
    match.phase_start_time = (
        now - timedelta(minutes=resume_minute - phase_base)
        if phase_base is not None else None
    )
    match.lineups_locked = True
    match.abandonment_resolution = Match.AbandonmentResolution.RESUMED
    if scheduled_for:
        match.match_date = scheduled_for
    if venue:
        match.location = venue.strip()
    match.save(
        update_fields=[
            "status",
            "home_score",
            "away_score",
            "phase",
            "minute",
            "phase_start_time",
            "lineups_locked",
            "abandonment_resolution",
            "match_date",
            "location",
        ],
        _allow_abandoned_resume=True,
    )
    decision = _record_decision(
        match=match,
        actor=actor,
        decision_type=AbandonedMatchDecision.DecisionType.RESUME,
        description=description,
        scheduled_for=scheduled_for,
        venue=match.location or "",
        resume_minute=resume_minute,
    )
    if _requires_table_rebuild(match):
        rebuild_league_standings(match.competition)
    _log_resolution(match, actor, decision, before)
    return match, decision


def _finish_abandoned_match(match, *, home_score, away_score, resolution):
    """Finish a league/group abandonment without erasing its on-field record."""

    normalized = validate_official_result(
        match,
        home_score=home_score,
        away_score=away_score,
    )
    for field_name, value in normalized.items():
        setattr(match, field_name, value)
    match.status = "FINISHED"
    match.phase = "NONE"
    match.phase_start_time = None
    match.abandonment_resolution = resolution
    match.save(update_fields=[
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
        "abandonment_resolution",
    ])


@transaction.atomic
def award_official_result(*, match_id, actor, home_score, away_score, note=""):
    """Apply a committee score without inventing player goal events."""

    match = _locked_match(match_id)
    _require_abandoned_match(match, actor)
    _require_non_knockout(match, "Awarding a result")
    if home_score is None or away_score is None:
        raise AbandonedMatchResolutionError("Enter both official scores.")
    before = _snapshot(match)
    _finish_abandoned_match(
        match,
        home_score=home_score,
        away_score=away_score,
        resolution=Match.AbandonmentResolution.AWARDED,
    )
    description = _decision_description(
        AbandonedMatchDecision.DecisionType.AWARD,
        note,
        home_score=match.home_score,
        away_score=match.away_score,
    )
    decision = _record_decision(
        match=match,
        actor=actor,
        decision_type=AbandonedMatchDecision.DecisionType.AWARD,
        description=description,
        official_home_score=home_score,
        official_away_score=away_score,
    )
    _log_resolution(match, actor, decision, before)
    _refresh_official_result(match)
    return match, decision


@transaction.atomic
def confirm_score_at_abandonment(*, match_id, actor, note=""):
    """Make the frozen on-field score official exactly once."""

    match = _locked_match(match_id)
    _require_abandoned_match(match, actor)
    _require_non_knockout(match, "Confirming a score")
    home_score = (
        match.abandoned_home_score
        if match.abandoned_home_score is not None
        else match.home_score
    )
    away_score = (
        match.abandoned_away_score
        if match.abandoned_away_score is not None
        else match.away_score
    )
    # Confirming a score must never silently bless an event mismatch.
    from .services import validate_result_against_recorded_goals

    validate_result_against_recorded_goals(
        match, home_score=home_score, away_score=away_score
    )
    before = _snapshot(match)
    _finish_abandoned_match(
        match,
        home_score=home_score,
        away_score=away_score,
        resolution=Match.AbandonmentResolution.CONFIRMED,
    )
    description = _decision_description(
        AbandonedMatchDecision.DecisionType.CONFIRM,
        note,
    )
    decision = _record_decision(
        match=match,
        actor=actor,
        decision_type=AbandonedMatchDecision.DecisionType.CONFIRM,
        description=description,
    )
    _log_resolution(match, actor, decision, before)
    _refresh_official_result(match)
    return match, decision


@transaction.atomic
def void_abandoned_match(*, match_id, actor, note=""):
    """Record a no-points outcome while retaining every historical event."""

    match = _locked_match(match_id)
    _require_abandoned_match(match, actor)
    _require_non_knockout(match, "Voiding a match")
    description = _decision_description(
        AbandonedMatchDecision.DecisionType.VOID,
        note,
    )
    before = _snapshot(match)
    match.abandonment_resolution = Match.AbandonmentResolution.VOID
    match.save(update_fields=["abandonment_resolution"])
    decision = _record_decision(
        match=match,
        actor=actor,
        decision_type=AbandonedMatchDecision.DecisionType.VOID,
        description=description,
    )
    if _requires_table_rebuild(match):
        rebuild_league_standings(match.competition)
    _log_resolution(match, actor, decision, before)
    return match, decision


@transaction.atomic
def reopen_abandoned_resolution(*, match_id, actor, note=""):
    """Safely return a non-live league decision to an unresolved state."""

    match = _locked_match(match_id)
    if not can_manage_abandonment_decisions(actor, match.competition):
        raise PermissionDenied("You do not have permission to correct this decision.")
    decision = (
        AbandonedMatchDecision.objects.select_for_update()
        .filter(match=match, is_current=True)
        .first()
    )
    if not decision:
        raise AbandonedMatchResolutionError("There is no official decision to reopen.")
    if decision.decision_type not in {
        AbandonedMatchDecision.DecisionType.AWARD,
        AbandonedMatchDecision.DecisionType.CONFIRM,
        AbandonedMatchDecision.DecisionType.VOID,
    }:
        raise AbandonedMatchResolutionError(
            "A replay or live resumption cannot be reopened here; preserve its "
            "fixture history and use the relevant match controls instead."
        )
    _require_non_knockout(match, "Reopening this decision")
    description = _decision_description("REOPEN", note)
    before = _snapshot(match)
    now = timezone.now()
    decision.is_current = False
    decision.superseded_at = now
    decision.save(update_fields=["is_current", "superseded_at"])

    if match.status == "FINISHED":
        match.home_score = match.abandoned_home_score or 0
        match.away_score = match.abandoned_away_score or 0
        match.regulation_home_score = None
        match.regulation_away_score = None
        match.penalty_home_score = None
        match.penalty_away_score = None
        match.resolution_method = ""
        match.status = "ABANDONED"
    match.phase = "NONE"
    match.phase_start_time = None
    match.abandonment_resolution = Match.AbandonmentResolution.NONE
    match.save(update_fields=[
        "home_score", "away_score", "regulation_home_score", "regulation_away_score",
        "penalty_home_score", "penalty_away_score", "resolution_method", "status",
        "phase", "phase_start_time", "abandonment_resolution",
    ])
    if match.stage.stage_type in {"LEAGUE", "GROUP"}:
        rebuild_league_standings(match.competition)
    # An unresolved match must not retain points from a revoked award.
    from predictions.models import Prediction

    Prediction.objects.filter(prediction_match__match=match).update(points_awarded=0)
    record_activity(
        actor=actor,
        competition=match.competition,
        match=match,
        action=ActivityLog.Action.ABANDONMENT_RESOLUTION_REOPENED,
        description=description,
        old_values={
            **before,
            "decision_id": decision.id,
            "decision_description": decision.reason,
            "decision_reference": decision.decision_reference,
        },
        new_values={**_snapshot(match), "correction_description": description},
    )
    return match, decision
