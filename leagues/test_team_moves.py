"""Regression coverage for safe football team competition moves and repair."""

from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from leagues.models import Competition, Group, Stage, Standing
from leagues.standing_integrity import invalid_standings, standing_integrity_issues
from leagues.team_moves import move_team_to_competition
from matches.models import Match, Player
from teams.models import Team


class TeamCompetitionMoveTests(TestCase):
    def make_competition(self, slug, *, format_type="TABLE"):
        competition = Competition.objects.create(
            name=slug.replace("-", " ").title(), slug=slug, season="2026", format_type=format_type
        )
        stage_type = "LEAGUE" if format_type == "TABLE" else "GROUP"
        stage = Stage.objects.create(
            competition=competition, name="Primary Stage", stage_type=stage_type, order=1
        )
        return competition, stage

    def test_new_table_team_gets_a_zero_standing_and_safe_move_reconciles_it(self):
        source, source_stage = self.make_competition("source-table")
        destination, destination_stage = self.make_competition("destination-table")
        team = Team.objects.create(
            name="Movable FC", competition=source, home_ground="Kept Ground", logo="teams/kept.png"
        )
        player = Player.objects.create(name="Kept Player", team=team)
        self.assertTrue(Standing.objects.filter(competition=source, team=team).exists())

        move_team_to_competition(team, destination)
        team.refresh_from_db()
        self.assertEqual(team.competition_id, destination.id)
        self.assertEqual(team.home_ground, "Kept Ground")
        self.assertEqual(team.logo.name, "teams/kept.png")
        self.assertEqual(player.team_id, team.id)
        self.assertFalse(Standing.objects.filter(competition=source, team=team).exists())
        self.assertEqual(
            Standing.objects.filter(
                competition=destination, stage=destination_stage, group=None, team=team
            ).count(),
            1,
        )

    def test_safe_group_move_removes_old_group_membership_and_standing(self):
        source, source_stage = self.make_competition("source-group", format_type="GROUP_KNOCKOUT")
        group = Group.objects.create(stage=source_stage, name="A")
        destination, destination_stage = self.make_competition("destination-table-group")
        team = Team.objects.create(name="Group Mover", competition=source)
        group.teams.add(team)
        self.assertTrue(Standing.objects.filter(competition=source, group=group, team=team).exists())

        move_team_to_competition(team, destination)
        self.assertFalse(group.teams.filter(pk=team.pk).exists())
        self.assertFalse(Standing.objects.filter(competition=source, team=team).exists())
        self.assertTrue(Standing.objects.filter(competition=destination, stage=destination_stage, team=team).exists())

    def test_move_refuses_any_old_fixture_or_meaningful_standing(self):
        source, source_stage = self.make_competition("blocked-source")
        destination, _destination_stage = self.make_competition("blocked-destination")
        team = Team.objects.create(name="Blocked FC", competition=source)
        opponent = Team.objects.create(name="Opponent FC", competition=source)
        Match.objects.create(
            competition=source, stage=source_stage, home_team=team, away_team=opponent
        )
        with self.assertRaisesMessage(ValidationError, "fixtures already exist"):
            move_team_to_competition(team, destination)

        Match.objects.filter(competition=source).delete()
        standing = Standing.objects.get(competition=source, team=team)
        standing.points = 1
        standing.save(update_fields=["points"])
        with self.assertRaisesMessage(ValidationError, "recorded statistics"):
            move_team_to_competition(team, destination)

        standing.points = 0
        standing.baseline_points = 3
        standing.save(update_fields=["points", "baseline_points"])
        with self.assertRaisesMessage(ValidationError, "recorded statistics"):
            move_team_to_competition(team, destination)

    def test_repair_command_is_dry_by_default_then_repairs_only_safe_rows(self):
        source, _source_stage = self.make_competition("repair-source")
        destination, destination_stage = self.make_competition("repair-destination")
        team = Team.objects.create(name="Repair FC", competition=source)
        # Reproduce the legacy unsafe admin update without invoking the new service.
        Team.objects.filter(pk=team.pk).update(competition=destination)
        team.refresh_from_db()
        stale = Standing.objects.get(competition=source, team=team)

        dry_output = StringIO()
        call_command("repair_team_competition_standings", stdout=dry_output)
        self.assertIn("SAFE TO REMOVE (dry run)", dry_output.getvalue())
        self.assertTrue(Standing.objects.filter(pk=stale.pk).exists())

        apply_output = StringIO()
        call_command("repair_team_competition_standings", "--apply", stdout=apply_output)
        self.assertIn("REPAIRED", apply_output.getvalue())
        self.assertFalse(Standing.objects.filter(pk=stale.pk).exists())
        self.assertTrue(Standing.objects.filter(competition=destination, stage=destination_stage, team=team).exists())

        repeat_output = StringIO()
        call_command("repair_team_competition_standings", "--apply", stdout=repeat_output)
        self.assertIn("No team/competition standing inconsistencies", repeat_output.getvalue())

    def test_repair_command_refuses_rows_with_old_matches_or_statistics(self):
        source, source_stage = self.make_competition("unsafe-repair-source")
        destination, _destination_stage = self.make_competition("unsafe-repair-destination")
        team = Team.objects.create(name="Unsafe Repair FC", competition=source)
        opponent = Team.objects.create(name="Unsafe Opponent", competition=source)
        Team.objects.filter(pk=team.pk).update(competition=destination)
        Match.objects.create(
            competition=source, stage=source_stage, home_team=team, away_team=opponent
        )
        output = StringIO()
        call_command("repair_team_competition_standings", "--apply", stdout=output)
        self.assertIn("REQUIRES MANUAL REVIEW", output.getvalue())
        self.assertTrue(Standing.objects.filter(competition=source, team=team).exists())

    def test_standing_diagnostic_identifies_a_team_competition_mismatch(self):
        source, _source_stage = self.make_competition("diagnostic-source")
        destination, _destination_stage = self.make_competition("diagnostic-destination")
        team = Team.objects.create(name="Diagnostic FC", competition=source)
        Team.objects.filter(pk=team.pk).update(competition=destination)
        standing = Standing.objects.select_related("team", "competition", "stage", "group__stage").get(
            competition=source, team=team
        )
        self.assertIn("team belongs to another competition", standing_integrity_issues(standing))
        self.assertIn(standing.pk, list(invalid_standings().values_list("pk", flat=True)))
