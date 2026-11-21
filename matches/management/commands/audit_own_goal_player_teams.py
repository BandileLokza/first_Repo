"""Report historical own goals whose current player team is the benefiting side."""

from django.core.management.base import BaseCommand
from django.db.models import F

from matches.models import Goal


class Command(BaseCommand):
    help = (
        "List possible historical own-goal player-team mismatches for manual "
        "review. This command never changes data."
    )

    def handle(self, *args, **options):
        candidates = (
            Goal.objects.filter(
                goal_type=Goal.GoalType.OWN_GOAL,
                player__isnull=False,
                player__team_id=F("team_id"),
            )
            .select_related("match", "team", "player", "player__team")
            .order_by("match_id", "minute", "id")
        )

        count = candidates.count()
        if not count:
            self.stdout.write(self.style.SUCCESS(
                "No potential own-goal player-team mismatches found."
            ))
            return

        self.stdout.write(
            "Potential mismatches (review before making any correction):"
        )
        for goal in candidates.iterator():
            self.stdout.write(
                "goal={goal_id} match={match_id} minute={minute} player={player} "
                "current_player_team={player_team} benefiting_team={benefiting_team}".format(
                    goal_id=goal.id,
                    match_id=goal.match_id,
                    minute=goal.minute if goal.minute is not None else "unknown",
                    player=goal.player.name,
                    player_team=goal.player.team.name,
                    benefiting_team=goal.team.name,
                )
            )
        self.stdout.write(self.style.WARNING(
            f"{count} potential mismatch(es) reported; no records were changed."
        ))
