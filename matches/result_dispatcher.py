"""Lease and publish due football result work from a background process."""

import logging
from datetime import timedelta
from uuid import uuid4

from celery import current_app
from django.db import connection, transaction
from django.utils import timezone

from .models import MatchResultProcessingOutbox as Outbox
from .result_outbox import retry_delay
from .tasks import process_football_result_task


logger = logging.getLogger(__name__)
DISPATCH_LEASE = timedelta(seconds=120)


@transaction.atomic
def claim_due_result_work(batch_size):
    now = timezone.now()
    query = Outbox.objects.filter(
        state=Outbox.State.PENDING, next_attempt_at__lte=now,
    ).order_by("next_attempt_at", "pk")
    if connection.features.has_select_for_update_skip_locked:
        query = query.select_for_update(skip_locked=True)
    else:
        query = query.select_for_update()

    jobs = list(query[:batch_size])
    token = uuid4()
    for job in jobs:
        job.dispatch_token = token
        job.dispatch_attempts += 1
        job.last_dispatch_at = now
        job.next_attempt_at = now + DISPATCH_LEASE
        job.updated_at = now
    if jobs:
        Outbox.objects.bulk_update(jobs, [
            "dispatch_token", "dispatch_attempts", "last_dispatch_at",
            "next_attempt_at", "updated_at",
        ])
    return jobs


def publish_result_work(match_id):
    # Bound the transport attempt without changing broker options for other tasks.
    options = {
        **(current_app.conf.broker_transport_options or {}),
        "socket_connect_timeout": 3,
        "socket_timeout": 3,
        "retry_on_timeout": False,
    }
    with current_app.connection_for_write(transport_options=options) as broker:
        broker.ensure_connection(max_retries=0, timeout=3)
        process_football_result_task.apply_async(
            args=[match_id], connection=broker, retry=False,
        )


@transaction.atomic
def settle_dispatch(job, *, error=None):
    now = timezone.now()
    if error is None:
        # Publication is not completion. Revisit uncompleted deliveries.
        delay = max(timedelta(seconds=60), retry_delay(job.dispatch_attempts))
        changes = {}
    else:
        delay = retry_delay(job.dispatch_attempts)
        changes = {"last_error": f"Publication failed ({type(error).__name__})."[:160]}
    claimed = Outbox.objects.filter(
        pk=job.pk, state=Outbox.State.PENDING,
        requested_revision=job.requested_revision, dispatch_token=job.dispatch_token,
    )
    if connection.features.has_select_for_update_skip_locked:
        # A worker can already hold the row. Leave its lease to expire rather
        # than delaying dispatch of unrelated matches while the worker runs.
        if not claimed.select_for_update(skip_locked=True).exists():
            return
    claimed.update(
        next_attempt_at=now + delay, dispatch_token=None, updated_at=now, **changes,
    )


def dispatch_due_result_work(batch_size):
    if connection.in_atomic_block:
        raise RuntimeError("Result work must be published outside a database transaction.")
    jobs = claim_due_result_work(batch_size)
    for job in jobs:
        try:
            publish_result_work(job.match_id)
        except Exception as error:
            logger.warning(
                "Could not publish result work for match %s revision %s (%s)",
                job.match_id, job.requested_revision, type(error).__name__,
            )
            settle_dispatch(job, error=error)
        else:
            settle_dispatch(job)
    return len(jobs)
