"""Conservatively repair zero-value standings leaked by legacy team moves."""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from leagues.models import Group, Standing
from leagues.standing_integrity import standing_integrity_issues
from leagues.team_moves import (
    competition_has_team_matches,
    ensure_destination_standing,
    standing_has_activity,
)


class Command(BaseCommand):
    help = (
        "Report stale standings whose team has moved competitions. Use --apply "
        "to remove only zero-value rows with no old-competition fixtures."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply safe repairs. The default is a read-only dry run.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        stale_rows = list(
            Standing.objects.exclude(competition_id=F("team__competition_id"))
            .select_related("team", "competition", "stage", "group", "group__stage")
            .order_by("team__name", "competition__name", "id")
        )
        if not stale_rows:
            self.stdout.write("No team/competition standing inconsistencies found.")
            return

        mode = "APPLY" if apply else "DRY RUN"
        self.stdout.write(f"{mode}: {len(stale_rows)} stale standing(s) found.")
        for standing in stale_rows:
            self._handle_standing(standing, apply=apply)

    def _describe(self, standing):
        stats = "/".join(
            str(getattr(standing, field))
            for field in ("played", "won", "drawn", "lost", "points")
        )
        return (
            f"Team: {standing.team.name}; Team competition: "
            f"{standing.team.competition.name}; Stale standing competition: "
            f"{standing.competition.name}; Standing stats: {stats}"
        )

    def _handle_standing(self, standing, *, apply):
        description = self._describe(standing)
        old_has_matches = competition_has_team_matches(standing.competition, standing.team)
        issues = standing_integrity_issues(standing)
        if standing_has_activity(standing) or old_has_matches:
            self.stdout.write(
                self.style.WARNING(
                    f"REQUIRES MANUAL REVIEW: {description}; "
                    f"fixtures={old_has_matches}; issues={', '.join(issues) or 'team mismatch'}"
                )
            )
            return

        if not apply:
            self.stdout.write(f"SAFE TO REMOVE (dry run): {description}; Action: remove stale standing")
            return

        try:
            with transaction.atomic():
                current = (
                    Standing.objects.select_for_update()
                    .select_related("team__competition", "competition")
                    .get(pk=standing.pk)
                )
                if current.competition_id == current.team.competition_id:
                    self.stdout.write(f"SKIPPED (already repaired): standing={current.pk}")
                    return
                if standing_has_activity(current) or competition_has_team_matches(
                    current.competition, current.team
                ):
                    self.stdout.write(
                        self.style.WARNING(
                            f"REQUIRES MANUAL REVIEW: {self._describe(current)}"
                        )
                    )
                    return

                old_groups = list(
                    Group.objects.select_for_update().filter(
                        stage__competition=current.competition,
                        teams=current.team,
                    )
                )
                for group in old_groups:
                    group.teams.remove(current.team)
                team = current.team
                current.delete()
                ensure_destination_standing(team, team.competition)
        except Exception as error:
            self.stdout.write(
                self.style.WARNING(
                    f"REQUIRES MANUAL REVIEW: {description}; {error}"
                )
            )
            return
        self.stdout.write(self.style.SUCCESS(f"REPAIRED: {description}"))
