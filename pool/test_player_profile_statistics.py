"""Public player-profile presentation coverage for singles and team history."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamMatchScheduledRack,
    PoolTeamRoster,
)
from .services import record_non_played_result, record_rack, start_match


User = get_user_model()


class PlayerProfileStatisticsPresentationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="profile-statistics-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_singles_competition(self, *, race_target=1, name=None):
        self._competition_number += 1
        number = self._competition_number
        competition = PoolCompetition.objects.create(
            name=name or f"Singles Profile Competition {number}",
            slug=f"singles-profile-competition-{number}",
            owner=self.owner,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=race_target,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        return competition, stage

    def make_singles_entry(self, competition, name):
        player = PoolPlayer.objects.create(full_name=name)
        entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=player,
        )
        return player, entry

    def finish_singles_match(self, competition, stage, home_entry, away_entry, winners):
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )
        start_match(pool_match, recorded_by=self.owner)
        for winner, outcome_type in winners:
            record_rack(
                pool_match,
                winner=winner,
                outcome_type=outcome_type,
                recorded_by=self.owner,
            )
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        return pool_match

    def profile_response(self, player, competition):
        response = self.client.get(
            reverse("pool:player_detail", args=[player.public_id]),
            {"competition": competition.slug},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_singles_card_uses_completed_match_and_rack_rates_without_team_terms(self):
        competition, stage = self.make_singles_competition(race_target=11)
        player, home_entry = self.make_singles_entry(competition, "Profile Home")
        _, away_entry = self.make_singles_entry(competition, "Profile Away")
        self.finish_singles_match(
            competition,
            stage,
            home_entry,
            away_entry,
            [
                *[(away_entry, PoolRack.OutcomeType.NORMAL) for _ in range(4)],
                (home_entry, PoolRack.OutcomeType.GREEN_APPLE),
                (home_entry, PoolRack.OutcomeType.RED_APPLE),
                (home_entry, PoolRack.OutcomeType.ONE_VISIT),
                *[(home_entry, PoolRack.OutcomeType.NORMAL) for _ in range(8)],
            ],
        )

        response = self.profile_response(player, competition)
        scoped = response.context["player_competition_stats"]
        self.assertEqual(scoped["matches_played"], 1)
        self.assertEqual(scoped["won"], 1)
        self.assertEqual(scoped["lost"], 0)
        self.assertEqual(scoped["racks_won"], 11)
        self.assertEqual(scoped["racks_lost"], 4)
        self.assertEqual(scoped["normal_wins"], 8)
        self.assertEqual(scoped["match_win_percentage"], 100.0)
        self.assertEqual(scoped["rack_win_percentage"], 73.3)
        self.assertContains(response, "Competition Performance")
        for label in (
            "Matches played",
            "Won",
            "Lost",
            "Match win rate",
            "Racks won",
            "Racks lost",
            "Rack win rate",
            "Normal rack wins",
            "Green apples",
            "Red apples",
            "One visits",
        ):
            self.assertContains(response, label)
        self.assertContains(response, "100%")
        self.assertContains(response, "73.3%")
        self.assertNotContains(response, "Tracked team races")
        self.assertNotContains(response, "Tracked wins")
        self.assertNotContains(response, "Tracked losses")
        self.assertNotContains(response, "Tracked win rate")
        self.assertContains(response, "Singles Career Record")
        self.assertContains(response, "Across published singles competitions recorded on eSportsLive.")

    def test_singles_match_win_rate_is_three_wins_from_four_completed_matches(self):
        competition, stage = self.make_singles_competition()
        player, home_entry = self.make_singles_entry(competition, "Three of Four")
        for number in range(2):
            _, away_entry = self.make_singles_entry(
                competition,
                f"Opponent {number}",
            )
            self.finish_singles_match(
                competition,
                stage,
                home_entry,
                away_entry,
                [(home_entry, PoolRack.OutcomeType.NORMAL)],
            )

        response = self.profile_response(player, competition)
        self.assertEqual(
            response.context["player_competition_stats"]["match_win_percentage"],
            100.0,
        )
        self.assertContains(response, "100%")

        for number, winner in ((2, home_entry), (3, None)):
            _, away_entry = self.make_singles_entry(
                competition,
                f"Opponent {number}",
            )
            self.finish_singles_match(
                competition,
                stage,
                home_entry,
                away_entry,
                [
                    (
                        winner or away_entry,
                        PoolRack.OutcomeType.NORMAL,
                    )
                ],
            )

        response = self.profile_response(player, competition)
        scoped = response.context["player_competition_stats"]
        self.assertEqual(scoped["matches_played"], 4)
        self.assertEqual(scoped["won"], 3)
        self.assertEqual(scoped["lost"], 1)
        self.assertEqual(scoped["match_win_percentage"], 75.0)
        self.assertContains(response, "75%")

    def test_singles_zero_data_renders_no_data_rates(self):
        competition, _ = self.make_singles_competition()
        player, _ = self.make_singles_entry(competition, "No Recorded Matches")

        response = self.profile_response(player, competition)
        scoped = response.context["player_competition_stats"]
        self.assertIsNone(scoped["match_win_percentage"])
        self.assertIsNone(scoped["rack_win_percentage"])
        self.assertContains(response, "<dt>Match win rate</dt><dd>—</dd>", html=True)
        self.assertContains(response, "<dt>Rack win rate</dt><dd>—</dd>", html=True)

    def test_singles_walkover_keeps_the_rack_rate_as_no_data(self):
        competition, stage = self.make_singles_competition()
        player, home_entry = self.make_singles_entry(competition, "Walkover Winner")
        _, away_entry = self.make_singles_entry(competition, "Walkover Opponent")
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )
        record_non_played_result(
            pool_match,
            result_method=PoolMatch.ResultMethod.WALKOVER,
            winner=home_entry,
            recorded_by=self.owner,
        )

        response = self.profile_response(player, competition)
        scoped = response.context["player_competition_stats"]
        self.assertEqual(scoped["matches_played"], 1)
        self.assertEqual(scoped["won"], 1)
        self.assertEqual(scoped["racks_won"], 0)
        self.assertEqual(scoped["racks_lost"], 0)
        self.assertEqual(scoped["match_win_percentage"], 100.0)
        self.assertIsNone(scoped["rack_win_percentage"])
        self.assertContains(response, "<dt>Rack win rate</dt><dd>—</dd>", html=True)

    def test_team_card_keeps_tracked_attribution_without_individual_match_results(self):
        self._competition_number += 1
        number = self._competition_number
        competition = PoolCompetition.objects.create(
            name="Team Profile Competition",
            slug=f"team-profile-competition-{number}",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=1,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        home_team = PoolTeam.objects.create(name="Profile Team Home")
        away_team = PoolTeam.objects.create(name="Profile Team Away")
        home_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=home_team,
        )
        away_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=away_team,
        )
        player = PoolPlayer.objects.create(full_name="Team Profile Player")
        opponent = PoolPlayer.objects.create(full_name="Team Profile Opponent")
        PoolTeamRoster.objects.create(competition_entry=home_entry, player=player)
        PoolTeamRoster.objects.create(competition_entry=away_entry, player=opponent)
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )
        # This legacy-compatible live fixture has a team result, but that
        # result must never turn into an individual player's match W/L record.
        pool_match.status = PoolMatch.Status.LIVE
        pool_match.save()
        record_rack(
            pool_match,
            winning_player=player,
            outcome_type=PoolRack.OutcomeType.NORMAL,
            recorded_by=self.owner,
        )

        response = self.profile_response(player, competition)
        self.assertTrue(response.context["is_team_competition_scope"])
        self.assertFalse(response.context["is_singles_competition_scope"])
        self.assertEqual(response.context["player_stats"]["matches_played"], 0)
        self.assertEqual(response.context["player_stats"]["won"], 0)
        self.assertEqual(response.context["player_stats"]["lost"], 0)
        self.assertContains(response, "Individual Team Performance")
        self.assertContains(response, "Racks won")
        self.assertNotContains(response, "Tracked team races")
        self.assertNotContains(response, "Tracked wins")
        self.assertNotContains(response, "Tracked losses")
        self.assertNotContains(response, "Tracked win rate")
        self.assertNotContains(response, "Competition Performance")

    def test_removed_team_roster_player_with_completed_snapshot_keeps_profile_context(self):
        self._competition_number += 1
        number = self._competition_number
        competition = PoolCompetition.objects.create(
            name="Historical Team Snapshot Competition",
            slug=f"historical-team-snapshot-{number}",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=3,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        home_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=PoolTeam.objects.create(name="Snapshot Home Team"),
        )
        away_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=PoolTeam.objects.create(name="Snapshot Away Team"),
        )
        player = PoolPlayer.objects.create(full_name="Former Snapshot Player")
        opponent = PoolPlayer.objects.create(full_name="Snapshot Opponent")
        PoolTeamRoster.objects.create(competition_entry=home_entry, player=player)
        PoolTeamRoster.objects.create(competition_entry=away_entry, player=opponent)
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )
        pool_match.status = PoolMatch.Status.LIVE
        pool_match.save()
        rack = PoolRack(
            match=pool_match,
            rack_number=1,
            winner=home_entry,
            winning_player=player,
        )
        rack.save(_from_result_service=True)
        PoolTeamMatchScheduledRack.objects.create(
            match=pool_match,
            sequence_number=1,
            session_number=1,
            home_position=1,
            away_position=1,
            status=PoolTeamMatchScheduledRack.Status.COMPLETED,
            home_player=player,
            away_player=opponent,
            result=rack,
        )
        PoolTeamRoster.objects.filter(
            competition_entry=home_entry,
            player=player,
        ).delete()

        response = self.client.get(
            reverse("pool:player_detail", args=[player.public_id]),
            {"competition": competition.slug, "team": home_entry.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_team_competition_scope"])
