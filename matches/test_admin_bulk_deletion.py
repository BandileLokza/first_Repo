"""Regression coverage for the safe football Match admin bulk-delete action."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import ActivityLog
from leagues.models import Competition, CompetitionSeries, Region, Stage, Standing
from matches.models import (
    FootballKnockoutTie,
    Goal,
    Match,
    MatchLineup,
    MatchLineupEntry,
    Player,
    YellowCard,
)
from matches.services import rebuild_league_standings
from predictions.models import Prediction, PredictionMatch
from teams.models import Team


User = get_user_model()


class MatchAdminBulkDeletionTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="match-admin-delete-super",
            email="match-admin-delete-super@example.test",
            password="test-password",
        )
        self.owner = User.objects.create_user(
            username="match-admin-delete-owner",
            password="test-password",
        )
        self.region = Region.objects.create(
            name="Admin Delete Region",
            slug="admin-delete-region",
        )
        self.series = CompetitionSeries.objects.create(
            name="Admin Delete Series",
            slug="admin-delete-series",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=self.region,
        )
        self.competition = Competition.objects.create(
            name="Admin Delete Competition",
            slug="admin-delete-competition",
            season="2026/27",
            series=self.series,
            format_type="TABLE",
        )
        self.stage = Stage.objects.create(
            competition=self.competition,
            name="League stage",
            stage_type="LEAGUE",
            order=1,
        )
        self.teams = [
            Team.objects.create(
                competition=self.competition,
                name=f"Admin Delete Team {number}",
            )
            for number in range(1, 5)
        ]
        self.changelist_url = reverse("admin:matches_match_changelist")

    def make_match(
        self,
        *,
        home_index=0,
        away_index=1,
        stage=None,
        leg=1,
        status="UPCOMING",
        gameweek=1,
        knockout_tie=None,
    ):
        return Match.objects.create(
            competition=self.competition,
            stage=stage or self.stage,
            home_team=self.teams[home_index],
            away_team=self.teams[away_index],
            knockout_tie=knockout_tie,
            leg=leg,
            gameweek=gameweek,
            status=status,
            match_date=timezone.now() + timedelta(days=1),
        )

    @staticmethod
    def delete_selection(*fixture_ids, select_across="0", confirmed=False):
        payload = {
            "action": "delete_selected_matches",
            "select_across": select_across,
            "_selected_action": [str(fixture_id) for fixture_id in fixture_ids],
        }
        if confirmed:
            payload["post"] = "yes"
        return payload

    def test_authorized_admin_sees_safe_delete_and_existing_match_actions(self):
        self.make_match()
        self.client.force_login(self.superuser)

        response = self.client.get(self.changelist_url)

        self.assertContains(response, "Delete selected matches")
        self.assertContains(response, "Start First Half")
        self.assertContains(response, "End First Half")
        self.assertContains(response, "Start Second Half")
        self.assertContains(response, "End Match")
        self.assertContains(response, "Unlock selected match lineups")

    def test_non_superuser_with_model_delete_permission_cannot_use_bulk_action(self):
        fixture = self.make_match()
        staff_user = User.objects.create_user(
            username="match-admin-delete-staff",
            password="test-password",
            is_staff=True,
        )
        staff_user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="matches",
                content_type__model="match",
                codename="view_match",
            ),
            Permission.objects.get(
                content_type__app_label="matches",
                content_type__model="match",
                codename="delete_match",
            ),
        )
        self.client.force_login(staff_user)

        response = self.client.get(self.changelist_url)
        self.assertNotContains(response, "Delete selected matches")
        response = self.client.post(self.changelist_url, self.delete_selection(fixture.pk))

        self.assertIn(response.status_code, {200, 302})
        self.assertTrue(Match.objects.filter(pk=fixture.pk).exists())

    def test_confirmation_deletes_owned_records_and_rebuilds_finished_standings(self):
        finished = self.make_match(status="FINISHED")
        finished.home_score = 2
        finished.away_score = 0
        finished.save(update_fields=["home_score", "away_score"])
        survivor = self.make_match(
            home_index=2,
            away_index=3,
            leg=2,
            gameweek=2,
            status="FINISHED",
        )
        survivor.home_score = 1
        survivor.away_score = 0
        survivor.save(update_fields=["home_score", "away_score"])
        player = Player.objects.create(name="Deleted fixture player", team=self.teams[0])
        goal = Goal.objects.create(match=finished, team=self.teams[0], player=player)
        card = YellowCard.objects.create(
            match=finished,
            team=self.teams[0],
            player=player,
        )
        lineup = MatchLineup.objects.create(match=finished, team=self.teams[0])
        appearance = MatchLineupEntry.objects.create(
            lineup=lineup,
            player=player,
            role=MatchLineupEntry.Role.STARTER,
        )
        prediction_match, _ = PredictionMatch.objects.get_or_create(
            match=finished,
            defaults={"competition": self.competition},
        )
        prediction = Prediction.objects.create(
            user=self.owner,
            prediction_match=prediction_match,
            predicted_result="H",
            predicted_home_goals=2,
            predicted_away_goals=0,
        )
        rebuild_league_standings(self.competition)
        self.client.force_login(self.superuser)
        selection = self.delete_selection(finished.pk)

        confirmation = self.client.post(self.changelist_url, selection)

        self.assertEqual(confirmation.status_code, 200)
        self.assertEqual(confirmation.context["selected_count"], 1)
        self.assertContains(confirmation, "This action cannot be undone")
        self.assertContains(confirmation, self.competition.name)
        self.assertTrue(Match.objects.filter(pk=finished.pk).exists())

        response = self.client.post(
            self.changelist_url,
            self.delete_selection(finished.pk, confirmed=True),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Match.objects.filter(pk=finished.pk).exists())
        self.assertTrue(Match.objects.filter(pk=survivor.pk).exists())
        self.assertFalse(Goal.objects.filter(pk=goal.pk).exists())
        self.assertFalse(YellowCard.objects.filter(pk=card.pk).exists())
        self.assertFalse(MatchLineup.objects.filter(pk=lineup.pk).exists())
        self.assertFalse(MatchLineupEntry.objects.filter(pk=appearance.pk).exists())
        self.assertFalse(PredictionMatch.objects.filter(pk=prediction_match.pk).exists())
        self.assertFalse(Prediction.objects.filter(pk=prediction.pk).exists())
        self.assertTrue(Player.objects.filter(pk=player.pk).exists())
        self.assertEqual(
            Standing.objects.get(
                competition=self.competition,
                stage=self.stage,
                team=self.teams[0],
            ).played,
            0,
        )
        self.assertEqual(
            Standing.objects.get(
                competition=self.competition,
                stage=self.stage,
                team=self.teams[2],
            ).played,
            1,
        )
        audit = ActivityLog.objects.get(
            action=ActivityLog.Action.FIXTURE_DELETED,
            old_values__match_id=finished.pk,
        )
        self.assertIsNone(audit.match_id)
        self.assertEqual(audit.actor_id, self.superuser.id)

    def test_protected_mixed_selection_is_rejected_without_partial_deletion(self):
        knockout_stage = Stage.objects.create(
            competition=self.competition,
            name="Protected knockout stage",
            stage_type="KNOCKOUT",
            order=2,
        )
        tie = FootballKnockoutTie.objects.create(
            stage=knockout_stage,
            position=1,
            team_one=self.teams[0],
            team_two=self.teams[1],
        )
        protected = self.make_match(
            stage=knockout_stage,
            knockout_tie=tie,
            status="UPCOMING",
        )
        ordinary = self.make_match(home_index=2, away_index=3, leg=2)
        self.client.force_login(self.superuser)
        selection = self.delete_selection(protected.pk, ordinary.pk)

        response = self.client.post(self.changelist_url, selection, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connected knockout-bracket fixtures cannot be deleted")
        self.assertTrue(Match.objects.filter(pk=protected.pk).exists())
        self.assertTrue(Match.objects.filter(pk=ordinary.pk).exists())

        response = self.client.post(
            self.changelist_url,
            self.delete_selection(protected.pk, ordinary.pk, confirmed=True),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Match.objects.filter(pk=protected.pk).exists())
        self.assertTrue(Match.objects.filter(pk=ordinary.pk).exists())

    def test_select_across_deletes_every_filtered_fixture_after_one_confirmation(self):
        first = self.make_match()
        second = self.make_match(home_index=2, away_index=3, leg=2)
        self.client.force_login(self.superuser)

        confirmation = self.client.post(
            self.changelist_url,
            self.delete_selection(first.pk, select_across="1"),
        )

        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(
            confirmation,
            "You are about to permanently delete 2 selected",
        )
        response = self.client.post(
            self.changelist_url,
            self.delete_selection(first.pk, select_across="1", confirmed=True),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Match.objects.filter(pk__in=[first.pk, second.pk]).exists())

    def test_bulk_delete_locks_regional_and_unregional_competitions_safely(self):
        """Exercise the nullable series-region join used by PostgreSQL locks."""

        regional = self.make_match(status="FINISHED")
        regional.home_score = 2
        regional.away_score = 0
        regional.save(update_fields=["home_score", "away_score"])
        rebuild_league_standings(self.competition)

        unregional_series = CompetitionSeries.objects.create(
            name="Admin Delete Unregional Series",
            slug="admin-delete-unregional-series",
            league_type=CompetitionSeries.LeagueType.PLE,
        )
        unregional_competition = Competition.objects.create(
            name="Admin Delete Unregional Competition",
            slug="admin-delete-unregional-competition",
            season="2026/27",
            series=unregional_series,
            format_type="TABLE",
        )
        unregional_stage = Stage.objects.create(
            competition=unregional_competition,
            name="League stage",
            stage_type="LEAGUE",
            order=1,
        )
        unregional_teams = [
            Team.objects.create(
                competition=unregional_competition,
                name=f"Admin Delete Unregional Team {number}",
            )
            for number in range(1, 3)
        ]
        unregional = Match.objects.create(
            competition=unregional_competition,
            stage=unregional_stage,
            home_team=unregional_teams[0],
            away_team=unregional_teams[1],
            status="FINISHED",
            home_score=1,
            away_score=0,
            gameweek=1,
            match_date=timezone.now() + timedelta(days=1),
        )
        rebuild_league_standings(unregional_competition)
        self.client.force_login(self.superuser)

        confirmation = self.client.post(
            self.changelist_url,
            self.delete_selection(regional.pk, unregional.pk),
        )
        self.assertEqual(confirmation.status_code, 200)
        self.assertEqual(confirmation.context["selected_count"], 2)
        self.assertContains(confirmation, self.competition.name)
        self.assertContains(confirmation, unregional_competition.name)

        response = self.client.post(
            self.changelist_url,
            self.delete_selection(regional.pk, unregional.pk, confirmed=True),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Match.objects.filter(pk=regional.pk).exists())
        self.assertFalse(Match.objects.filter(pk=unregional.pk).exists())
        self.assertEqual(
            Standing.objects.get(
                competition=self.competition,
                stage=self.stage,
                team=self.teams[0],
            ).played,
            0,
        )
        self.assertEqual(
            Standing.objects.get(
                competition=unregional_competition,
                stage=unregional_stage,
                team=unregional_teams[0],
            ).played,
            0,
        )
