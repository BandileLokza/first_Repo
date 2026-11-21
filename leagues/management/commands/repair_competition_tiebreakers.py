"""Safely report or explicitly repair leaked tie-breaker positions."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from leagues.models import Competition, CompetitionTieBreaker
from leagues.services import replace_competition_tie_breakers


LEAKED_POSITION_FLOOR = 32000


class Command(BaseCommand):
    help = (
        "Report leaked high tie-breaker positions, or explicitly rebuild one "
        "local SQLite competition with a supplied configuration."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--competition",
            type=int,
            help="Competition primary key to report or repair.",
        )
        parser.add_argument(
            "--criteria",
            help=(
                "Comma-separated ordered criterion values for an explicit "
                "repair, for example POINTS,OVERALL_GOAL_DIFFERENCE."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the explicit local repair. Without this flag the command is read-only.",
        )

    def handle(self, *args, **options):
        competition_id = options["competition"]

        if not options["apply"]:
            self._report_leaked_positions(competition_id)
            return

        if not settings.DEBUG or connection.vendor != "sqlite":
            raise CommandError(
                "Refusing to modify data unless DEBUG=True and the active database is SQLite."
            )
        if competition_id is None:
            raise CommandError("--competition is required with --apply.")
        if not options["criteria"]:
            raise CommandError("--criteria is required with --apply.")

        competition = Competition.objects.filter(pk=competition_id).first()
        if competition is None:
            raise CommandError(f"Competition {competition_id} does not exist.")

        criteria = self._parse_criteria(options["criteria"])
        replace_competition_tie_breakers(
            competition,
            list(enumerate(criteria, start=1)),
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt tie-breakers for local competition {competition.pk}."
            )
        )

    def _report_leaked_positions(self, competition_id):
        rows = CompetitionTieBreaker.objects.filter(order__gte=LEAKED_POSITION_FLOOR)
        if competition_id is not None:
            rows = rows.filter(competition_id=competition_id)

        rows = rows.select_related("competition").order_by(
            "competition_id", "order", "id"
        )
        if not rows.exists():
            self.stdout.write("No tie-breaker positions at or above 32000 were found.")
            return

        self.stdout.write("Leaked high tie-breaker positions (read-only report):")
        for row in rows:
            self.stdout.write(
                "  competition={competition_id} ({competition_name}), row={row_id}, "
                "position={position}, criterion={criterion}".format(
                    competition_id=row.competition_id,
                    competition_name=row.competition.name,
                    row_id=row.pk,
                    position=row.order,
                    criterion=row.criterion,
                )
            )

    def _parse_criteria(self, value):
        criteria = []
        for raw_criterion in value.split(","):
            criterion = raw_criterion.strip()
            if not criterion:
                raise CommandError("--criteria must not contain an empty value.")

            try:
                criteria.append(CompetitionTieBreaker.Criterion[criterion].value)
            except KeyError as error:
                raise CommandError(
                    f"Unknown tie-breaker criterion: {criterion}."
                ) from error

        return criteria
