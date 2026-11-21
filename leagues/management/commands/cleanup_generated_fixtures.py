"""Auditable cleanup for a specifically approved, incorrectly generated league."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league_manager.models import ActivityLog
from leagues.models import Competition
from matches.models import Goal, Match, MatchAdminAssignment, MatchLineup, YellowCard
from predictions.models import PredictionMatch


def protection_reasons(match):
    """Return every reason a fixture must never be deleted by this command."""

    reasons = []
    if match.status != "UPCOMING":
        reasons.append(f"status={match.status}")
    if any(
        value not in (None, 0)
        for value in (
            match.home_score,
            match.away_score,
            match.regulation_home_score,
            match.regulation_away_score,
            match.penalty_home_score,
            match.penalty_away_score,
            match.table_home_score,
            match.table_away_score,
        )
    ):
        reasons.append("recorded score")
    if match.phase != "NONE" or match.minute:
        reasons.append("match lifecycle activity")
    if match.lineups_locked:
        reasons.append("locked lineups")
    if match.knockout_tie_id:
        reasons.append("knockout tie")
    if Goal.objects.filter(match=match).exists():
        reasons.append("goals")
    if YellowCard.objects.filter(match=match).exists():
        reasons.append("cards")
    if MatchAdminAssignment.objects.filter(match=match).exists():
        reasons.append("agent assignments")
    if MatchLineup.objects.filter(match=match).exists():
        reasons.append("lineups")
    if ActivityLog.objects.filter(match=match).exists():
        reasons.append("activity log")
    if PredictionMatch.objects.filter(match=match).exists():
        reasons.append("prediction record")
    return reasons


def fixture_snapshot(match):
    return {
        "id": match.id,
        "competition_id": match.competition_id,
        "stage_id": match.stage_id,
        "home_team_id": match.home_team_id,
        "away_team_id": match.away_team_id,
        "gameweek": match.gameweek,
        "leg": match.leg,
        "status": match.status,
        "match_date": match.match_date.isoformat() if match.match_date else None,
        "location": match.location,
    }


class Command(BaseCommand):
    help = (
        "Dry-run cleanup of unplayed, unlinked generated fixtures for one exact "
        "competition. Gameweeks 1 and 2 are preserved by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("competition_id", type=int)
        parser.add_argument(
            "--from-gameweek",
            type=int,
            default=3,
            help="First candidate gameweek (default: 3; preserves Gameweeks 1 and 2).",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Perform deletion after all protections and confirmation checks pass.",
        )
        parser.add_argument(
            "--confirm-competition-id",
            type=int,
            help="Must repeat the exact competition ID when --execute is supplied.",
        )
        parser.add_argument(
            "--backup-file",
            help="New JSON backup path required with --execute; existing files are never overwritten.",
        )
        parser.add_argument(
            "--match-ids",
            type=int,
            nargs="+",
            help=(
                "Exact reviewed candidate IDs to delete. Required with --execute so "
                "Gameweek alone never selects records for removal."
            ),
        )

    def handle(self, *args, **options):
        competition_id = options["competition_id"]
        from_gameweek = options["from_gameweek"]
        if from_gameweek < 3:
            raise CommandError("--from-gameweek must be 3 or later; Gameweeks 1 and 2 are protected.")
        try:
            competition = Competition.objects.get(pk=competition_id)
        except Competition.DoesNotExist as error:
            raise CommandError(f"Competition {competition_id} does not exist.") from error
        if competition.format_type != "TABLE":
            raise CommandError("This cleanup command is limited to Table Only competitions.")

        scope_candidates = list(
            Match.objects.filter(
                competition=competition,
                gameweek__gte=from_gameweek,
                stage__stage_type="LEAGUE",
                group__isnull=True,
                leg=1,
            )
            .select_related("home_team", "away_team", "stage")
            .order_by("gameweek", "id")
        )
        requested_ids = options["match_ids"] or []
        if len(requested_ids) != len(set(requested_ids)):
            raise CommandError("--match-ids must not contain duplicate IDs.")
        scope_by_id = {match.id: match for match in scope_candidates}
        unknown_ids = sorted(set(requested_ids) - set(scope_by_id))
        if unknown_ids:
            raise CommandError(
                "The requested match IDs are outside this competition's eligible "
                f"Table/leg-1 cleanup scope: {unknown_ids}."
            )
        candidates = (
            [scope_by_id[match_id] for match_id in requested_ids]
            if requested_ids
            else scope_candidates
        )
        protected = {
            match.id: protection_reasons(match)
            for match in candidates
            if protection_reasons(match)
        }
        report = {
            "competition_id": competition.id,
            "competition": competition.name,
            "from_gameweek": from_gameweek,
            "scope_candidate_count": len(scope_candidates),
            "scope_candidate_ids": [match.id for match in scope_candidates],
            "candidate_count": len(candidates),
            "candidate_ids": [match.id for match in candidates],
            "protected": protected,
        }
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))

        if not options["execute"]:
            self.stdout.write(self.style.WARNING("DRY RUN: no fixtures were deleted."))
            return
        if options["confirm_competition_id"] != competition_id:
            raise CommandError("--execute requires --confirm-competition-id matching the positional ID.")
        if not options["backup_file"]:
            raise CommandError("--execute requires --backup-file for an auditable pre-delete backup.")
        if not requested_ids:
            raise CommandError(
                "--execute requires explicit --match-ids selected from a reviewed dry-run."
            )
        backup_path = Path(options["backup_file"])
        if backup_path.exists():
            raise CommandError("Refusing to overwrite an existing backup file.")
        if protected:
            raise CommandError("Protected candidate fixtures exist; no deletion was performed.")
        if not candidates:
            self.stdout.write(self.style.WARNING("No candidate fixtures found; nothing was deleted."))
            return

        with transaction.atomic():
            Competition.objects.select_for_update().get(pk=competition_id)
            locked = list(
                Match.objects.select_for_update()
                .filter(id__in=[match.id for match in candidates], competition_id=competition_id)
                .order_by("id")
            )
            if len(locked) != len(candidates):
                raise CommandError("Candidate fixtures changed during review; run a new dry-run.")
            protected_after_lock = {
                match.id: protection_reasons(match)
                for match in locked
                if protection_reasons(match)
            }
            if protected_after_lock:
                raise CommandError("A candidate gained protected data; no deletion was performed.")
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(
                json.dumps(
                    {
                        "competition_id": competition_id,
                        "fixtures": [fixture_snapshot(match) for match in locked],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            deleted_ids = [match.id for match in locked]
            Match.objects.filter(id__in=deleted_ids, competition_id=competition_id).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {len(deleted_ids)} protected-checked fixture(s): {deleted_ids}. "
                f"Backup: {backup_path}"
            )
        )
