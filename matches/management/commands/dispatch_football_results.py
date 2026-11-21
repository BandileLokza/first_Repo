import logging
from time import sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from matches.models import MatchResultProcessingOutbox as Outbox
from matches.result_dispatcher import dispatch_due_result_work


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Publish due football result work from PostgreSQL. Run as a background process."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--once", action="store_true", help="Run one bounded dispatch pass.")
        mode.add_argument("--status", action="store_true", help="Report queue health without publishing.")
        parser.add_argument("--interval", type=float, default=3, help="Seconds between dispatch passes.")
        parser.add_argument("--batch-size", type=int, default=20)

    def handle(self, *args, **options):
        if not 1 <= options["batch_size"] <= 100 or not 1 <= options["interval"] <= 300:
            raise CommandError("Use a batch size of 1–100 and an interval of 1–300 seconds.")
        if options["status"]:
            self.report_status()
            return
        if options["once"]:
            count = dispatch_due_result_work(options["batch_size"])
            self.stdout.write(f"Claimed {count} due football result job(s).")
            return

        self.stdout.write("Football result dispatcher started.")
        while True:
            try:
                close_old_connections()
                dispatch_due_result_work(options["batch_size"])
            except Exception:
                logger.exception("Football result dispatcher pass failed")
            finally:
                close_old_connections()
            sleep(options["interval"])

    def report_status(self):
        now = timezone.now()
        pending = Q(state=Outbox.State.PENDING)
        repeated_failure = Q(processing_failures__gte=3) | (
            Q(dispatch_attempts__gte=3) & ~Q(last_error="")
        )
        totals = Outbox.objects.aggregate(
            pending=Count("pk", filter=pending),
            due=Count("pk", filter=pending & Q(next_attempt_at__lte=now)),
            repeatedly_failing=Count("pk", filter=pending & repeated_failure),
            oldest=Min("pending_since", filter=pending),
            latest_completion=Max("last_completed_at"),
        )
        age = int((now - totals["oldest"]).total_seconds()) if totals["oldest"] else 0
        self.stdout.write(
            f"Pending {totals['pending']}, due {totals['due']}, "
            f"repeatedly failing {totals['repeatedly_failing']}, oldest age {age}s. "
            f"Last completion {totals['latest_completion'] or 'none'}."
        )
        for job in Outbox.objects.filter(pending & repeated_failure).order_by("pending_since", "pk")[:10]:
            self.stdout.write(
                f"Match {job.match_id} revision {job.requested_revision}, "
                f"{job.dispatch_attempts} dispatch attempts, {job.processing_failures} "
                f"processing failures. {job.last_error}"
            )
