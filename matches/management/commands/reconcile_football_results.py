"""Report or repair stale football result projections in one competition."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F, Q

from leagues.models import Competition
from matches.models import Match, MatchResultProcessingOutbox
from matches.result_outbox import request_result_processing
from matches.services import rebuild_league_standings
from matches.tasks import refresh_completed_matches


class Command(BaseCommand):
    help = (
        "Report finished matches with unapplied standings scores in one competition. "
        "Use --apply to reconcile them, --match-id to refresh specific matches "
        "including predictions, or --rebuild when standings snapshots cannot be trusted."
    )

    def add_arguments(self, parser):
        parser.add_argument("--competition", required=True, help="Competition slug.")
        parser.add_argument("--match-id", type=int, action="append", default=[])
        parser.add_argument("--apply", action="store_true", help="Write repairs. Defaults to dry run.")
        parser.add_argument("--rebuild", action="store_true", help="Rebuild this competition's tables.")
        parser.add_argument("--enqueue", action="store_true", help="Queue ordinary results for background processing.")

    def handle(self, *args, **options):
        apply = options["apply"]
        rebuild = options["rebuild"]
        enqueue = options["enqueue"]
        if rebuild and enqueue:
            raise CommandError("Use either --rebuild or --enqueue.")
        requested_ids = set(options["match_id"])
        if rebuild and requested_ids:
            raise CommandError("Use either --rebuild or --match-id.")

        with transaction.atomic():
            competitions = Competition.objects.all()
            if apply:
                competitions = competitions.select_for_update(of=("self",))
            try:
                competition = competitions.get(slug=options["competition"])
            except Competition.DoesNotExist as error:
                raise CommandError("Competition not found.") from error

            matches = Match.objects.filter(competition=competition, status="FINISHED")
            if requested_ids:
                matches = matches.filter(pk__in=requested_ids)
            else:
                matches = matches.filter(stage__stage_type__in=["LEAGUE", "GROUP"])
                if not rebuild:
                    matches = matches.filter(
                        Q(table_updated=False)
                        | Q(table_home_score__isnull=True)
                        | Q(table_away_score__isnull=True)
                        | ~Q(table_home_score=F("home_score"), table_away_score=F("away_score"))
                    )

            if enqueue:
                matches = matches.filter(
                    abandonment_resolution__in=[
                        Match.AbandonmentResolution.NONE, Match.AbandonmentResolution.RESUMED,
                    ],
                )
                if not requested_ids:
                    # Unsafe historical reversals remain an explicit repair operation.
                    matches = matches.exclude(
                        Q(table_updated=True) & (
                            Q(table_home_score__isnull=True) | Q(table_away_score__isnull=True)
                        ),
                    )

            rows = list(matches.order_by("pk").values(
                "id", "stage_id", "group_id", "table_updated",
                "home_score", "away_score", "table_home_score", "table_away_score",
            ))
            match_ids = [row["id"] for row in rows]
            if requested_ids - set(match_ids):
                raise CommandError("Every requested match must be finished and belong to this competition.")

            mode = "APPLY" if apply else "DRY RUN"
            self.stdout.write(f"{mode} {competition.slug}: {len(rows)} finished match(es) selected.")
            for row in rows:
                applied = (
                    f"{row['table_home_score']}-{row['table_away_score']}"
                    if row["table_updated"] else "unapplied"
                )
                self.stdout.write(
                    f"Match {row['id']} (stage {row['stage_id']}, group {row['group_id']}): "
                    f"applied {applied}, current {row['home_score']}-{row['away_score']}."
                )
            if not apply:
                return

            try:
                if enqueue:
                    fixtures = (
                        Match.objects.select_for_update(of=("self",))
                        .filter(pk__in=match_ids, competition=competition)
                        .exclude(result_processing__state=MatchResultProcessingOutbox.State.PENDING)
                        .order_by("pk")
                    )
                    enqueued = 0
                    for fixture in fixtures:
                        request_result_processing(fixture)
                        enqueued += 1
                elif rebuild:
                    rebuild_league_standings(competition)
                else:
                    refresh_completed_matches(match_ids)
            except ValidationError as error:
                raise CommandError(" ".join(error.messages)) from error

        if enqueue:
            self.stdout.write(self.style.SUCCESS(f"Queued {enqueued} match(es) for background processing."))
        elif rebuild:
            self.stdout.write(self.style.SUCCESS(f"Rebuilt standings for {competition.slug}."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Refreshed {len(match_ids)} match(es)."))
