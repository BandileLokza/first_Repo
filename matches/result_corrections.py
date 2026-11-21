"""Score snapshots and finalization for result corrections."""

from sqlite3 import SQLITE_BUSY, SQLITE_LOCKED

from league_manager.models import ActivityLog
from league_manager.services import record_activity

from .services import finalize_match_result_fast


SCORE_FIELDS = (
    "home_score", "away_score",
    "regulation_home_score", "regulation_away_score",
    "penalty_home_score", "penalty_away_score",
)
RESULT_CONFLICT = (
    "This match was updated by another administrator. "
    "Refresh the page and review the latest information before saving."
)
RESULT_SAVE_FAILED = "The result could not be saved. Please try again."
GOAL_COUNT_MISMATCH = (
    "This match has more recorded goals than the final score allows. "
    "Please reconcile the goal records before continuing."
)


def is_lock_conflict(error):
    cause = error.__cause__
    postgres_state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    sqlite_code = getattr(cause, "sqlite_errorcode", 0) & 0xFF
    return (
        postgres_state in {"55P03", "40P01", "40001"}
        or sqlite_code in {SQLITE_BUSY, SQLITE_LOCKED}
    )


def result_snapshot(match):
    return {
        **{field: getattr(match, field) for field in SCORE_FIELDS},
        "status": match.status,
        "resolution_method": match.resolution_method,
    }


def score_inputs_unchanged(match, submitted, baseline):
    fields = SCORE_FIELDS if match.stage.stage_type == "KNOCKOUT" else SCORE_FIELDS[:2]
    return all(submitted[field] == baseline[field] for field in fields)


def apply_finished_result_correction(match, result, *, actor, previous, submission):
    """Finalize a correction after its goal changes have been validated under lock."""
    finalize_match_result_fast(
        match, actor=actor, **{field: result[field] for field in SCORE_FIELDS},
    )
    record_activity(
        actor=actor,
        competition=match.competition,
        match=match,
        action=ActivityLog.Action.RESULT_CORRECTED,
        description=f"Result corrected: {match.home_team} {match.home_score}-{match.away_score} {match.away_team}",
        old_values=previous,
        new_values={**result_snapshot(match), **submission},
    )
