from celery import shared_task
from .result_outbox import process_result_work


@shared_task
def process_football_result_task(match_id):
    # The outbox schedules retries even if a worker dies or Redis loses a message.
    return process_result_work(match_id)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def recalculate_match_after_result_task(self, match_id: int):
    """Consume messages already queued by a previous application release."""
    process_result_work(match_id, allow_untracked=True)


@shared_task
def debug_celery_task():
    print("CELERY IS WORKING")
    return "ok"


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def recalculate_league_after_bulk_update_task(self, league_id: int, match_ids: list[int]):
    """Keep the previous task name and arguments during deployment."""
    for match_id in sorted(set(match_ids)):
        process_result_work(match_id, allow_untracked=True)


def refresh_completed_matches(match_ids):
    """Explicit repair, including official results that bypass the outbox."""
    for match_id in sorted(set(match_ids)):
        process_result_work(match_id, repair=True)
