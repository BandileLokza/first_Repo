"""Durable work for projecting an official football result."""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from leagues.models import Competition
from predictions.utils import calculate_predictions_for_match

from .models import Match, MatchResultProcessingOutbox as Outbox


logger = logging.getLogger(__name__)
RESULT_SAVED = "Result saved successfully. Standings and prediction points are updating."
RESULT_BUSY = "A result update is in progress. Please try again."


def lock_result_competition(competition_id, *, nowait=False):
    # Writers exclude one another, while unrelated FK checks can still proceed.
    return Competition.objects.select_for_update(
        of=("self",), no_key=True, nowait=nowait,
    ).get(pk=competition_id)


def lock_result_match(match_id, *, nowait=False):
    competition_id = Match.objects.values_list("competition_id", flat=True).get(pk=match_id)
    try:
        lock_result_competition(competition_id, nowait=nowait)
    except Competition.DoesNotExist as error:
        raise Match.DoesNotExist("The fixture's competition was removed.") from error
    return Match.objects.select_for_update(of=("self",), nowait=nowait).select_related(
        "competition", "stage", "home_team", "away_team", "knockout_tie",
    ).get(pk=match_id, competition_id=competition_id)


def ordinary_finished_result(match):
    return match.status == "FINISHED" and match.abandonment_resolution in {
        Match.AbandonmentResolution.NONE, Match.AbandonmentResolution.RESUMED,
    }


def request_result_processing(match):
    """Record a revision while the caller holds the fixture lock."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Result processing must be recorded in the result transaction.")
    now = timezone.now()
    job, created = Outbox.objects.select_for_update().get_or_create(
        match=match, defaults={"pending_since": now, "next_attempt_at": now},
    )
    if not created:
        job.requested_revision += 1
        if job.state != Outbox.State.PENDING:
            job.pending_since = now
        job.state = Outbox.State.PENDING
        job.next_attempt_at = now
        job.dispatch_token = None
        job.dispatch_attempts = 0
        job.processing_failures = 0
        job.last_error = ""
        job.save(update_fields=[
            "requested_revision", "state", "pending_since", "next_attempt_at",
            "dispatch_token", "dispatch_attempts", "processing_failures", "last_error",
            "updated_at",
        ])
    return job


def cancel_result_processing(match_id):
    """Retire ordinary work inside a locked official-decision transaction."""
    Outbox.objects.filter(match_id=match_id, state=Outbox.State.PENDING).update(
        state=Outbox.State.CANCELLED, pending_since=None, next_attempt_at=None,
        dispatch_token=None, last_error="", updated_at=timezone.now(),
    )


def retry_delay(attempts):
    return timedelta(seconds=min(300, 2 ** min(attempts, 9)))


def process_result_work(match_id, *, allow_untracked=False, repair=False):
    """Project the latest result and record completion in the same transaction."""
    from .services import apply_match_to_standings_incrementally

    revision = None
    try:
        with transaction.atomic():
            match = lock_result_match(match_id)
            job = Outbox.objects.select_for_update().filter(match_id=match_id).first()
            if not ordinary_finished_result(match):
                if job and job.state == Outbox.State.PENDING:
                    cancel_result_processing(match_id)
                if not (repair and match.status == "FINISHED"):
                    return False
                job = None
            if job is None:
                if not (allow_untracked or repair):
                    return False
            elif job.state == Outbox.State.PENDING:
                revision = job.requested_revision
            elif not repair:
                return False

            refreshed = apply_match_to_standings_incrementally(match)
            calculate_predictions_for_match(refreshed)
            if revision is not None:
                now = timezone.now()
                job.completed_revision = revision
                job.state = Outbox.State.COMPLETED
                job.pending_since = None
                job.next_attempt_at = None
                job.dispatch_token = None
                job.last_processing_at = now
                job.last_completed_at = now
                job.last_error = ""
                job.save(update_fields=[
                    "completed_revision", "state", "pending_since", "next_attempt_at",
                    "dispatch_token", "last_processing_at", "last_completed_at",
                    "last_error", "updated_at",
                ])
            return True
    except Match.DoesNotExist:
        # Deletion cascades to the outbox. A message may outlive either row.
        return False
    except Exception as error:
        if revision is not None:
            try:
                _record_processing_failure(match_id, revision, error)
            except Exception:
                logger.exception("Could not record result processing failure for match %s", match_id)
        raise


@transaction.atomic
def _record_processing_failure(match_id, revision, error):
    job = Outbox.objects.select_for_update().filter(
        match_id=match_id, requested_revision=revision, state=Outbox.State.PENDING,
    ).first()
    if job is None:
        return
    now = timezone.now()
    job.processing_failures += 1
    job.last_processing_at = now
    # Store a bounded diagnostic without database or broker credentials.
    job.last_error = f"Processing failed ({type(error).__name__})."[:160]
    fields = ["processing_failures", "last_processing_at", "last_error", "updated_at"]
    if job.dispatch_token is None:
        job.next_attempt_at = now + retry_delay(job.processing_failures)
        fields.append("next_attempt_at")
    job.save(update_fields=fields)
