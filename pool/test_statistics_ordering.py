"""Regression coverage for public competition-statistics ordering."""

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
    PoolTeamRoster,
)
from .queries import (
    competition_player_rack_statistics,
    competition_team_rack_statistics,
    order_public_competition_stat_rows,
    team_entry_player_rack_statistics,
)
from .services import record_rack, start_match


User = get_user_model()


class PublicCompetitionStatisticsOrderingTests(TestCase):
    """The rendered public tables share one special-outcome ordering rule."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="public-statistics-order-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_competition(self, *, participant_mode):
        self._competition_number += 1
        competition = PoolCompetition.objects.create(
            name=f"Statistics ordering {self._competition_number}",
            slug=f"statistics-ordering-{self._competition_number}",
            owner=self.owner,
            participant_mode=participant_mode,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=1,
        )
        return competition, competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )

    @staticmethod
    def statistic_names(rows, *, key):
        return [
            row[key].full_name if key == "player" else row[key].name
            for row in rows
        ]

    def make_singles_entry(self, competition, name):
        player = PoolPlayer.objects.create(full_name=name)
        return PoolCompetitionEntry.objects.create(
            competition=competition,
            player=player,
        )

    def record_singles_outcomes(self, competition, stage, winner, opponent, outcomes):
        match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=winner,
            away_entry=opponent,
            scheduled_at=timezone.now(),
            race_target=len(outcomes),
        )
        start_match(match, recorded_by=self.owner)
        for outcome in outcomes:
            record_rack(
                match,
                winner=winner,
                outcome_type=outcome,
                recorded_by=self.owner,
            )

    def record_live_singles_outcomes(
        self,
        competition,
        stage,
        winner,
        opponent,
        outcomes,
    ):
        """Record rack contribution without reaching the fixture race target."""
        match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=winner,
            away_entry=opponent,
            scheduled_at=timezone.now(),
            race_target=len(outcomes) + 1,
        )
        start_match(match, recorded_by=self.owner)
        for outcome in outcomes:
            record_rack(
                match,
                winner=winner,
                outcome_type=outcome,
                recorded_by=self.owner,
            )
        match.refresh_from_db()
        self.assertEqual(match.status, PoolMatch.Status.LIVE)

    def make_team_entry(self, competition, name, player_names):
        team = PoolTeam.objects.create(name=name)
        entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=team,
        )
        players = []
        for sort_order, player_name in enumerate(player_names):
            player = PoolPlayer.objects.create(full_name=player_name)
            PoolTeamRoster.objects.create(
                competition_entry=entry,
                player=player,
                sort_order=sort_order,
            )
            players.append(player)
        return entry, players

    def record_team_outcomes(
        self,
        competition,
        stage,
        winner,
        winning_player,
        opponent,
        outcomes,
    ):
        # These intentionally model a supported legacy team fixture without a
        # generated score sheet.  The public read model must continue to sort
        # its attributed rack history exactly like a current fixture.
        match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=winner,
            away_entry=opponent,
            scheduled_at=timezone.now(),
            race_target=len(outcomes),
        )
        match.status = PoolMatch.Status.LIVE
        match.save()
        for outcome in outcomes:
            record_rack(
                match,
                winning_player=winning_player,
                outcome_type=outcome,
                recorded_by=self.owner,
            )

    def test_shared_helper_prioritises_special_outcomes_and_casefolded_names(self):
        rows = [
            {
                "display_name": "Zero Participant",
                "green_apples": 0,
                "red_apples": 0,
                "one_visits": 0,
            },
            {
                "display_name": "One low",
                "green_apples": 6,
                "red_apples": 9,
                "one_visits": 1,
            },
            {
                "display_name": "Bravo",
                "green_apples": 0,
                "red_apples": 0,
                "one_visits": 0,
            },
            {
                "display_name": "Red priority",
                "green_apples": 6,
                "red_apples": 10,
                "one_visits": 0,
            },
            {
                "display_name": "alpha",
                "green_apples": 0,
                "red_apples": 0,
                "one_visits": 0,
            },
            {
                "display_name": "Green priority",
                "green_apples": 7,
                "red_apples": 0,
                "one_visits": 0,
            },
            {
                "display_name": "One priority",
                "green_apples": 6,
                "red_apples": 9,
                "one_visits": 2,
            },
        ]

        ordered = order_public_competition_stat_rows(rows)

        self.assertEqual(
            [row["display_name"] for row in ordered],
            [
                "Green priority",
                "Red priority",
                "One priority",
                "One low",
                "alpha",
                "Bravo",
                "Zero Participant",
            ],
        )

    def test_public_singles_stats_use_displayed_rack_outcomes(self):
        competition, stage = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.SINGLES
        )
        opponent = self.make_singles_entry(competition, "Omega opponent")
        green = self.make_singles_entry(competition, "Green leader")
        live_green = self.make_singles_entry(competition, "Live green leader")
        red = self.make_singles_entry(competition, "Red leader")
        one_high = self.make_singles_entry(competition, "One leader")
        one_low = self.make_singles_entry(competition, "One low")
        self.make_singles_entry(competition, "alpha")
        self.make_singles_entry(competition, "Bravo")
        self.make_singles_entry(competition, "Zero participant")

        self.record_singles_outcomes(
            competition,
            stage,
            green,
            opponent,
            [PoolRack.OutcomeType.GREEN_APPLE, PoolRack.OutcomeType.GREEN_APPLE],
        )
        self.record_live_singles_outcomes(
            competition,
            stage,
            live_green,
            opponent,
            [PoolRack.OutcomeType.GREEN_APPLE] * 3,
        )
        self.record_singles_outcomes(
            competition,
            stage,
            red,
            opponent,
            [
                PoolRack.OutcomeType.GREEN_APPLE,
                PoolRack.OutcomeType.RED_APPLE,
                PoolRack.OutcomeType.RED_APPLE,
            ],
        )
        self.record_singles_outcomes(
            competition,
            stage,
            one_high,
            opponent,
            [
                PoolRack.OutcomeType.GREEN_APPLE,
                PoolRack.OutcomeType.RED_APPLE,
                PoolRack.OutcomeType.ONE_VISIT,
                PoolRack.OutcomeType.ONE_VISIT,
            ],
        )
        self.record_singles_outcomes(
            competition,
            stage,
            one_low,
            opponent,
            [
                PoolRack.OutcomeType.GREEN_APPLE,
                PoolRack.OutcomeType.RED_APPLE,
                PoolRack.OutcomeType.ONE_VISIT,
            ],
        )

        # Give equal zero-stat rows intentionally opposite casing.  Neither
        # receives a fixture, so this also proves deterministic zero rows.
        expected_prefix = [
            "Live green leader",
            "Green leader",
            "Red leader",
            "One leader",
            "One low",
            "alpha",
            "Bravo",
        ]
        rack_rows = competition_player_rack_statistics(competition)
        self.assertEqual(
            self.statistic_names(rack_rows, key="player")[:7],
            expected_prefix,
        )

        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]),
            {"tab": "stats"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["participant"].full_name for row in response.context["stats"]][:7],
            expected_prefix,
        )

    def test_public_team_and_team_player_statistics_share_the_order(self):
        competition, stage = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS
        )
        opponent, _ = self.make_team_entry(
            competition,
            "Omega opponent",
            ["Omega player"],
        )
        green, green_players = self.make_team_entry(
            competition,
            "Green team",
            ["Green player"],
        )
        red, red_players = self.make_team_entry(
            competition,
            "Red team",
            ["Red player"],
        )
        one_high, one_high_players = self.make_team_entry(
            competition,
            "One team",
            ["One player"],
        )
        one_low, one_low_players = self.make_team_entry(
            competition,
            "One low team",
            ["One low player"],
        )
        self.make_team_entry(
            competition,
            "alpha team",
            ["alpha player"],
        )
        self.make_team_entry(
            competition,
            "Bravo team",
            ["Bravo player"],
        )
        self.make_team_entry(competition, "Zero team", ["Zero player"])

        series = (
            (green, green_players[0], [PoolRack.OutcomeType.GREEN_APPLE] * 2),
            (
                red,
                red_players[0],
                [
                    PoolRack.OutcomeType.GREEN_APPLE,
                    PoolRack.OutcomeType.RED_APPLE,
                    PoolRack.OutcomeType.RED_APPLE,
                ],
            ),
            (
                one_high,
                one_high_players[0],
                [
                    PoolRack.OutcomeType.GREEN_APPLE,
                    PoolRack.OutcomeType.RED_APPLE,
                    PoolRack.OutcomeType.ONE_VISIT,
                    PoolRack.OutcomeType.ONE_VISIT,
                ],
            ),
            (
                one_low,
                one_low_players[0],
                [
                    PoolRack.OutcomeType.GREEN_APPLE,
                    PoolRack.OutcomeType.RED_APPLE,
                    PoolRack.OutcomeType.ONE_VISIT,
                ],
            ),
        )
        for entry, player, outcomes in series:
            self.record_team_outcomes(
                competition,
                stage,
                entry,
                player,
                opponent,
                outcomes,
            )

        expected_team_prefix = [
            "Green team",
            "Red team",
            "One team",
            "One low team",
            "alpha team",
            "Bravo team",
        ]
        team_rows = competition_team_rack_statistics(competition)
        self.assertEqual(
            self.statistic_names(team_rows, key="team")[:6],
            expected_team_prefix,
        )
        response = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]),
            {"tab": "stats"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.statistic_names(response.context["team_rack_stats"], key="team")[:6],
            expected_team_prefix,
        )

        player_board, board_players = self.make_team_entry(
            competition,
            "Player board",
            [
                "Board green",
                "Board red",
                "Board one high",
                "Board one low",
                "board alpha",
                "Board Bravo",
                "Board zero",
            ],
        )
        player_series = (
            (board_players[0], [PoolRack.OutcomeType.GREEN_APPLE] * 2),
            (
                board_players[1],
                [
                    PoolRack.OutcomeType.GREEN_APPLE,
                    PoolRack.OutcomeType.RED_APPLE,
                    PoolRack.OutcomeType.RED_APPLE,
                ],
            ),
            (
                board_players[2],
                [
                    PoolRack.OutcomeType.GREEN_APPLE,
                    PoolRack.OutcomeType.RED_APPLE,
                    PoolRack.OutcomeType.ONE_VISIT,
                    PoolRack.OutcomeType.ONE_VISIT,
                ],
            ),
            (
                board_players[3],
                [
                    PoolRack.OutcomeType.GREEN_APPLE,
                    PoolRack.OutcomeType.RED_APPLE,
                    PoolRack.OutcomeType.ONE_VISIT,
                ],
            ),
        )
        for player, outcomes in player_series:
            self.record_team_outcomes(
                competition,
                stage,
                player_board,
                player,
                opponent,
                outcomes,
            )

        expected_player_prefix = [
            "Board green",
            "Board red",
            "Board one high",
            "Board one low",
            "board alpha",
            "Board Bravo",
        ]
        player_rows = team_entry_player_rack_statistics(player_board)
        self.assertEqual(
            self.statistic_names(player_rows, key="player")[:6],
            expected_player_prefix,
        )
        team_response = self.client.get(
            reverse("pool:team_detail", args=[player_board.team.pk]),
            {"competition": competition.slug},
        )
        self.assertEqual(team_response.status_code, 200)
        self.assertEqual(
            self.statistic_names(
                team_response.context["team_rack_stats"][0]["player_rows"],
                key="player",
            )[:6],
            expected_player_prefix,
        )
