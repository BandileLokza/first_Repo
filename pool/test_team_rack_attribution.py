"""Focused service coverage for authoritative team rack-player attribution."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolMatch,
    PoolMatchResultRevision,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamRoster,
)
from .services import (
    PoolResultValidationError,
    correct_rack,
    record_rack,
    start_match,
)


User = get_user_model()


class PoolTeamRackAttributionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="team-rack-owner",
            password="test-password",
        )
        self.competition = PoolCompetition.objects.create(
            name="Team rack attribution",
            slug="team-rack-attribution",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=3,
        )
        self.stage = self.competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        self.home_entry, self.home_players = self.make_team_entry(
            "Home Sharks",
            ("Home One", "Home Two"),
        )
        self.away_entry, self.away_players = self.make_team_entry(
            "Away Eagles",
            ("Away One", "Away Two"),
        )
        self.pool_match = PoolMatch.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_entry=self.home_entry,
            away_entry=self.away_entry,
        )

    def make_team_entry(self, team_name, player_names, *, competition=None):
        competition = competition or self.competition
        team = PoolTeam.objects.create(name=team_name)
        entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=team,
        )
        players = []
        for sort_order, player_name in enumerate(player_names, start=1):
            player = PoolPlayer.objects.create(full_name=player_name)
            PoolTeamRoster.objects.create(
                competition_entry=entry,
                player=player,
                sort_order=sort_order,
            )
            players.append(player)
        return entry, players

    def start_team_match(self):
        # These assertions exercise the retained Phase Two scorer path for a
        # persisted team fixture that predates the Phase Four 25-rack score
        # sheet.  New team fixtures must start through ``start_match`` with
        # submitted five-player lineups; an already-live legacy fixture must
        # remain scoreable without inventing a schedule retroactively.
        self.pool_match.status = PoolMatch.Status.LIVE
        self.pool_match.save()
        self.pool_match.refresh_from_db()

    def test_team_rack_infers_winning_entry_from_active_roster_player(self):
        self.start_team_match()

        rack = record_rack(
            self.pool_match,
            winning_player=self.home_players[0],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )
        self.pool_match.refresh_from_db()

        self.assertEqual(rack.winner_id, self.home_entry.id)
        self.assertEqual(rack.winning_player_id, self.home_players[0].id)
        self.assertEqual(rack.outcome_type, PoolRack.OutcomeType.GREEN_APPLE)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (1, 0))

    def test_team_rack_requires_a_winning_player_for_new_played_racks(self):
        self.start_team_match()

        with self.assertRaises(PoolResultValidationError):
            record_rack(self.pool_match, winner=self.home_entry)

        self.assertFalse(self.pool_match.racks.exists())

    def test_team_rack_rejects_mismatched_winner_and_player(self):
        self.start_team_match()

        with self.assertRaises(PoolResultValidationError):
            record_rack(
                self.pool_match,
                winner=self.home_entry,
                winning_player=self.away_players[0],
            )

        self.assertFalse(self.pool_match.racks.exists())

    def test_team_rack_rejects_third_team_other_competition_inactive_and_forged_players(self):
        self.start_team_match()
        _, third_team_players = self.make_team_entry("Third Tigers", ("Third One",))
        inactive_player = PoolPlayer.objects.create(full_name="Inactive Home")
        PoolTeamRoster.objects.create(
            competition_entry=self.home_entry,
            player=inactive_player,
            is_active=False,
        )

        other_competition = PoolCompetition.objects.create(
            name="Other team attribution",
            slug="other-team-attribution",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        _, other_competition_players = self.make_team_entry(
            "Other Competition Team",
            ("Other Competition Player",),
            competition=other_competition,
        )

        for invalid_player in (
            third_team_players[0],
            inactive_player,
            other_competition_players[0],
            999999,
        ):
            with self.subTest(invalid_player=invalid_player):
                with self.assertRaises(PoolResultValidationError):
                    record_rack(self.pool_match, winning_player=invalid_player)

        self.assertFalse(self.pool_match.racks.exists())

    def test_ambiguous_player_active_for_both_match_teams_is_rejected(self):
        self.start_team_match()
        shared_player = PoolPlayer.objects.create(full_name="Shared Player")
        PoolTeamRoster.objects.create(
            competition_entry=self.home_entry,
            player=shared_player,
        )

        # New valid writes enforce the competition-wide one-team rule.
        with self.assertRaises(ValidationError):
            PoolTeamRoster.objects.create(
                competition_entry=self.away_entry,
                player=shared_player,
            )

        # Retain a defensive scorer check for an historical/corrupt record
        # that predates the domain guard. Normal roster managers deliberately
        # cannot use this private base-manager import path.
        PoolTeamRoster._base_manager.bulk_create(
            [
                PoolTeamRoster(
                    competition_entry=self.away_entry,
                    player=shared_player,
                )
            ]
        )

        with self.assertRaises(PoolResultValidationError):
            record_rack(self.pool_match, winning_player=shared_player)

    def test_singles_rack_keeps_entry_first_scoring_and_auto_attributes_player(self):
        singles = PoolCompetition.objects.create(
            name="Singles attribution",
            slug="singles-attribution",
            owner=self.owner,
            default_race_target=2,
        )
        stage = singles.stages.get(stage_type=PoolCompetitionStage.StageType.LEAGUE)
        home_player = PoolPlayer.objects.create(full_name="Singles Home")
        away_player = PoolPlayer.objects.create(full_name="Singles Away")
        home_entry = PoolCompetitionEntry.objects.create(
            competition=singles,
            player=home_player,
        )
        away_entry = PoolCompetitionEntry.objects.create(
            competition=singles,
            player=away_player,
        )
        pool_match = PoolMatch.objects.create(
            competition=singles,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
        )
        start_match(pool_match)

        rack = record_rack(pool_match, winner=home_entry)

        self.assertEqual(rack.winner_id, home_entry.id)
        self.assertEqual(rack.winning_player_id, home_player.id)

    def test_team_player_correction_updates_attribution_without_changing_team_score(self):
        self.start_team_match()
        rack = record_rack(
            self.pool_match,
            winning_player=self.home_players[0],
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )

        corrected = correct_rack(
            self.pool_match,
            rack,
            winning_player=self.home_players[1],
            outcome_type=PoolRack.OutcomeType.NORMAL,
            recorded_by=self.owner,
            reason="Correct player attribution",
        )
        self.pool_match.refresh_from_db()
        revision = PoolMatchResultRevision.objects.filter(
            match=self.pool_match,
            action=PoolMatchResultRevision.Action.RACK_CORRECTED,
        ).latest("id")

        self.assertEqual(corrected.winner_id, self.home_entry.id)
        self.assertEqual(corrected.winning_player_id, self.home_players[1].id)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (1, 0))
        self.assertEqual(
            revision.previous_result["racks"][0]["winning_player_id"],
            self.home_players[0].id,
        )
        self.assertEqual(
            revision.revised_result["racks"][0]["winning_player_id"],
            self.home_players[1].id,
        )

    def test_player_only_team_correction_can_move_the_rack_to_the_other_team(self):
        self.start_team_match()
        rack = record_rack(
            self.pool_match,
            winning_player=self.home_players[0],
        )

        corrected = correct_rack(
            self.pool_match,
            rack,
            winning_player=self.away_players[0],
        )
        self.pool_match.refresh_from_db()

        self.assertEqual(corrected.winner_id, self.away_entry.id)
        self.assertEqual(corrected.winning_player_id, self.away_players[0].id)
        self.assertEqual((self.pool_match.home_score, self.pool_match.away_score), (0, 1))

    def test_legacy_unattributed_team_rack_remains_valid_and_can_be_corrected(self):
        self.start_team_match()
        # ``bulk_create`` simulates a pre-migration historical row.  It does
        # not invoke the service/model save guard, exactly as an already
        # persisted legacy rack would not be rewritten by the migration.
        PoolRack.objects.bulk_create(
            [
                PoolRack(
                    match=self.pool_match,
                    rack_number=1,
                    winner=self.home_entry,
                    outcome_type=PoolRack.OutcomeType.NORMAL,
                )
            ]
        )
        legacy_rack = PoolRack.objects.get(match=self.pool_match, rack_number=1)

        legacy_rack.full_clean()
        corrected = correct_rack(
            self.pool_match,
            legacy_rack,
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
            recorded_by=self.owner,
        )

        self.assertIsNone(corrected.winning_player_id)
        self.assertEqual(corrected.outcome_type, PoolRack.OutcomeType.ONE_VISIT)
