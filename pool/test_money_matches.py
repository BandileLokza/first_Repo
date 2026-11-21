"""Focused coverage for the isolated Pool Money Match format."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamMatchScheduledRack,
)
from .services import (
    PoolResultValidationError,
    _locked_money_match_entries,
    _locked_money_match_fixture,
    configure_money_match,
    create_money_match,
    record_rack,
    result_processing_plan,
    start_match,
)
User = get_user_model()


class PoolMoneyMatchTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="money-match-owner",
            password="test-password",
        )

    def create_singles_money_match(
        self,
        *,
        title="",
        race_target=1,
        visibility=None,
        money_match_prize_pot=None,
    ):
        home = PoolPlayer.objects.create(full_name=f"Money Home {PoolPlayer.objects.count()}")
        away = PoolPlayer.objects.create(full_name=f"Money Away {PoolPlayer.objects.count()}")
        return create_money_match(
            opponent_one=home,
            opponent_two=away,
            title=title,
            race_target=race_target,
            owner=self.owner,
            visibility=visibility or PoolCompetition.Visibility.PUBLISHED,
            scheduled_at=timezone.now(),
            money_match_prize_pot=money_match_prize_pot,
        )

    def create_incomplete_money_match(self, *, slug="incomplete-money-match"):
        return PoolCompetition.objects.create(
            name="Incomplete Money Match",
            slug=slug,
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.SINGLES,
            format_type=PoolCompetition.FormatType.MONEY_MATCH,
        )

    def test_manager_creation_makes_one_singles_fixture_and_blank_title_falls_back(self):
        # An assigned manager can use the new workflow; an existing ordinary
        # competition establishes the same manager access the app uses today.
        PoolCompetition.objects.create(
            name="Existing manager competition",
            slug="existing-manager-competition",
            owner=self.owner,
        )
        home = PoolPlayer.objects.create(full_name="Bandiswa")
        away = PoolPlayer.objects.create(full_name="Nathi")
        scorer = User.objects.create_user(
            username="money-match-scorer",
            email="scorer@example.com",
            password="test-password",
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("pool:manage_money_match_create"),
            {
                "title": "",
                "player_one": home.pk,
                "player_two": away.pk,
                "race_target": 50,
                "money_match_prize_pot": "2000",
                "scheduled_at": "2026-10-01T18:30",
                "venue": "Manzini",
                "scorer_identifier": scorer.username,
                "visibility": PoolCompetition.Visibility.PUBLISHED,
            },
        )

        self.assertEqual(response.status_code, 302)
        competition = PoolCompetition.objects.get(format_type=PoolCompetition.FormatType.MONEY_MATCH)
        pool_match = competition.matches.get()
        self.assertEqual(competition.name, "Bandiswa vs Nathi")
        self.assertEqual(competition.entries.count(), 2)
        self.assertEqual(pool_match.race_target, 50)
        self.assertEqual(pool_match.venue, "Manzini")
        self.assertEqual(competition.money_match_prize_pot, Decimal("2000"))
        self.assertTrue(pool_match.is_money_match_fixture)
        self.assertTrue(
            pool_match.scorer_assignments.filter(user=scorer, is_active=True).exists()
        )
        self.assertEqual(
            pool_match.stage.stage_type,
            PoolCompetitionStage.StageType.MONEY,
        )
        self.assertFalse(competition.has_league_or_group_stage)
        self.assertFalse(competition.has_knockout_stage)
        self.assertRedirects(
            response,
            reverse("pool:manage_competition_dashboard", args=[competition.slug]),
            fetch_redirect_response=False,
        )

        create_page = self.client.get(reverse("pool:manage_money_match_create"))
        self.assertNotContains(create_page, "Participant type")
        self.assertNotContains(create_page, "Team 1")
        self.assertNotContains(create_page, "Team 2")

    def test_large_singles_money_match_targets_need_no_team_schedule(self):
        for race_target in (50, 100):
            competition, pool_match = self.create_singles_money_match(
                race_target=race_target
            )

            self.assertEqual(
                competition.participant_mode,
                PoolCompetition.ParticipantMode.SINGLES,
            )
            self.assertEqual(pool_match.race_target, race_target)
            self.assertTrue(pool_match.home_entry.player_id)
            self.assertTrue(pool_match.away_entry.player_id)
            self.assertFalse(
                PoolTeamMatchScheduledRack.objects.filter(match=pool_match).exists()
            )

    def test_configuration_locks_only_money_match_entry_rows(self):
        competition, pool_match = self.create_singles_money_match(race_target=7)
        original_lock = PoolCompetitionEntry.objects.select_for_update

        with patch.object(
            PoolCompetitionEntry.objects,
            "select_for_update",
            wraps=original_lock,
        ) as select_for_update:
            with transaction.atomic():
                entries = _locked_money_match_entries(competition)

        self.assertEqual([entry.pk for entry in entries], [
            pool_match.home_entry_id,
            pool_match.away_entry_id,
        ])
        select_for_update.assert_called_once_with(of=("self",))

    def test_configuration_locks_only_money_match_fixture_rows(self):
        competition, pool_match = self.create_singles_money_match(race_target=7)
        original_lock = PoolMatch.objects.select_for_update

        with patch.object(
            PoolMatch.objects,
            "select_for_update",
            wraps=original_lock,
        ) as select_for_update:
            with transaction.atomic():
                fixture = _locked_money_match_fixture(competition)

        self.assertEqual(fixture.pk, pool_match.pk)
        select_for_update.assert_called_once_with(of=("self",))

    def test_money_match_configuration_updates_opponents_without_global_duplicates(self):
        competition, pool_match = self.create_singles_money_match(race_target=7)
        original_away = pool_match.away_entry.player
        replacement = PoolPlayer.objects.create(full_name="Replacement through service")

        configured_competition, configured_match = configure_money_match(
            competition,
            opponent_one=replacement,
            opponent_two=original_away,
            race_target=50,
            title="Service-configured Money Match",
            assigned_by=self.owner,
        )

        self.assertEqual(configured_competition.pk, competition.pk)
        self.assertEqual(configured_match.race_target, 50)
        self.assertEqual(configured_match.home_entry.player_id, replacement.pk)
        self.assertEqual(configured_match.away_entry.player_id, original_away.pk)
        self.assertEqual(competition.entries.count(), 2)
        self.assertEqual(PoolPlayer.objects.filter(pk=replacement.pk).count(), 1)
        self.assertFalse(
            PoolTeamMatchScheduledRack.objects.filter(match=configured_match).exists()
        )

    def test_replacing_opponents_creates_the_locked_fixture_without_generic_save(self):
        competition, pool_match = self.create_singles_money_match(race_target=7)
        replacement = PoolPlayer.objects.create(full_name="Fast replacement opponent")

        with patch.object(
            PoolMatch,
            "save",
            side_effect=AssertionError("Money Match lifecycle must not call PoolMatch.save()."),
        ):
            _competition, configured_match = configure_money_match(
                competition,
                opponent_one=replacement,
                opponent_two=pool_match.away_entry.player,
                race_target=11,
                assigned_by=self.owner,
            )

        self.assertEqual(configured_match.race_target, 11)
        self.assertEqual(configured_match.home_entry.player, replacement)
        self.assertTrue(configured_match.is_money_match_fixture)
        self.assertEqual(competition.matches.count(), 1)

    def test_money_match_prize_pot_is_stored_preserved_updated_and_cleared(self):
        competition, pool_match = self.create_singles_money_match(
            race_target=7,
            money_match_prize_pot=Decimal("2000"),
        )
        home = pool_match.home_entry.player
        away = pool_match.away_entry.player

        configure_money_match(
            competition,
            opponent_one=home,
            opponent_two=away,
            race_target=7,
            assigned_by=self.owner,
        )
        competition.refresh_from_db()
        self.assertEqual(competition.money_match_prize_pot, Decimal("2000"))

        configure_money_match(
            competition,
            opponent_one=home,
            opponent_two=away,
            race_target=7,
            assigned_by=self.owner,
            money_match_prize_pot=Decimal("2500.50"),
        )
        competition.refresh_from_db()
        self.assertEqual(competition.money_match_prize_pot, Decimal("2500.50"))

        configure_money_match(
            competition,
            opponent_one=home,
            opponent_two=away,
            race_target=7,
            assigned_by=self.owner,
            money_match_prize_pot=None,
        )
        competition.refresh_from_db()
        self.assertIsNone(competition.money_match_prize_pot)

    def test_money_match_configuration_can_replace_each_opponent_without_duplicates(self):
        competition, pool_match = self.create_singles_money_match(
            race_target=7,
            money_match_prize_pot=Decimal("2000"),
        )
        replacement_home = PoolPlayer.objects.create(full_name="Replacement home")
        replacement_away = PoolPlayer.objects.create(full_name="Replacement away")

        _competition, pool_match = configure_money_match(
            competition,
            opponent_one=replacement_home,
            opponent_two=pool_match.away_entry.player,
            race_target=7,
            assigned_by=self.owner,
        )
        _competition, pool_match = configure_money_match(
            competition,
            opponent_one=replacement_home,
            opponent_two=replacement_away,
            race_target=7,
            assigned_by=self.owner,
        )

        self.assertEqual(competition.entries.count(), 2)
        self.assertEqual(pool_match.home_entry.player, replacement_home)
        self.assertEqual(pool_match.away_entry.player, replacement_away)
        self.assertEqual(competition.matches.count(), 1)

    def test_money_match_edit_form_errors_do_not_become_server_errors(self):
        competition, pool_match = self.create_singles_money_match(race_target=7)
        self.client.force_login(self.owner)
        url = reverse("pool:manage_money_match_edit", args=[competition.slug])

        same_player = pool_match.home_entry.player
        response = self.client.post(
            url,
            {
                "title": "",
                "player_one": same_player.pk,
                "player_two": same_player.pk,
                "race_target": 7,
                "scheduled_at": "",
                "venue": "",
                "scorer_identifier": "",
                "visibility": PoolCompetition.Visibility.DRAFT,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A player cannot face themselves.")

        response = self.client.post(
            url,
            {
                "title": "",
                "player_one": "",
                "player_two": pool_match.away_entry.player_id,
                "race_target": 7,
                "scheduled_at": "",
                "venue": "",
                "scorer_identifier": "",
                "visibility": PoolCompetition.Visibility.DRAFT,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select Player 1.")

    def test_money_match_edit_rejects_a_negative_prize_pot_as_a_form_error(self):
        competition, pool_match = self.create_singles_money_match(race_target=7)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("pool:manage_money_match_edit", args=[competition.slug]),
            {
                "title": "",
                "player_one": pool_match.home_entry.player_id,
                "player_two": pool_match.away_entry.player_id,
                "race_target": 7,
                "money_match_prize_pot": "-1",
                "scheduled_at": "",
                "venue": "",
                "scorer_identifier": "",
                "visibility": PoolCompetition.Visibility.PUBLISHED,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("money_match_prize_pot", response.context["form"].errors)
        competition.refresh_from_db()
        self.assertIsNone(competition.money_match_prize_pot)

    def test_validation_rejects_self_match_invalid_target_and_second_fixture(self):
        player = PoolPlayer.objects.create(full_name="Cannot play self")
        with self.assertRaises(PoolResultValidationError):
            create_money_match(
                opponent_one=player,
                opponent_two=player,
                race_target=7,
                owner=self.owner,
            )
        opponent = PoolPlayer.objects.create(full_name="Valid opponent")
        with self.assertRaises(PoolResultValidationError):
            create_money_match(
                opponent_one=player,
                opponent_two=opponent,
                race_target=0,
                owner=self.owner,
            )

        competition, pool_match = self.create_singles_money_match(race_target=7)
        duplicate = PoolMatch(
            competition=competition,
            stage=pool_match.stage,
            is_money_match_fixture=True,
            home_entry=pool_match.home_entry,
            away_entry=pool_match.away_entry,
            race_target=7,
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()
        third_player = PoolPlayer.objects.create(full_name="Third player")
        with self.assertRaises(ValidationError):
            PoolCompetitionEntry.objects.create(
                competition=competition,
                player=third_player,
            )

        self.client.force_login(self.owner)
        dashboard = self.client.get(
            reverse("pool:manage_competition_dashboard", args=[competition.slug])
        )
        self.assertNotContains(dashboard, ">Stages<")
        self.assertNotContains(dashboard, ">Standings<")
        self.assertNotContains(dashboard, "Fixture generator")
        self.assertEqual(
            self.client.get(
                reverse("pool:manage_competition_fixture_create", args=[competition.slug])
            ).status_code,
            404,
        )

    def test_money_matches_reject_team_participant_mode(self):
        with self.assertRaisesMessage(
            ValidationError,
            "Money Matches are available for singles players only.",
        ):
            PoolCompetition.objects.create(
                name="Invalid Team Money Match",
                slug="invalid-team-money-match",
                owner=self.owner,
                participant_mode=PoolCompetition.ParticipantMode.TEAMS,
                format_type=PoolCompetition.FormatType.MONEY_MATCH,
            )

    def test_normal_team_schedule_database_ranges_remain_protected(self):
        competition = PoolCompetition.objects.create(
            name="Normal Team Constraint Check",
            slug="normal-team-constraint-check",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        home_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=PoolTeam.objects.create(name="Constraint Home"),
        )
        away_entry = PoolCompetitionEntry.objects.create(
            competition=competition,
            team=PoolTeam.objects.create(name="Constraint Away"),
        )
        too_high = PoolMatch(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            race_target=14,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "A team match race target cannot exceed 13",
        ):
            too_high.full_clean()
        pool_match = PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home_entry,
            away_entry=away_entry,
            race_target=7,
        )

        valid = PoolTeamMatchScheduledRack.objects.create(
            match=pool_match,
            sequence_number=25,
            session_number=5,
            home_position=5,
            away_position=5,
        )
        self.assertEqual((valid.sequence_number, valid.session_number), (25, 5))

        for sequence_number, session_number in ((26, 5), (24, 6)):
            with transaction.atomic():
                with self.assertRaises(IntegrityError):
                    PoolTeamMatchScheduledRack.objects.bulk_create(
                        [
                            PoolTeamMatchScheduledRack(
                                match=pool_match,
                                sequence_number=sequence_number,
                                session_number=session_number,
                                home_position=1,
                                away_position=1,
                            )
                        ]
                    )

    def test_money_match_edit_restores_a_missing_fixture_for_two_existing_entries(self):
        competition = self.create_incomplete_money_match()
        home = PoolPlayer.objects.create(full_name="Recover Home")
        away = PoolPlayer.objects.create(full_name="Recover Away")
        PoolCompetitionEntry.objects.create(competition=competition, player=home)
        PoolCompetitionEntry.objects.create(competition=competition, player=away)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_money_match_edit", args=[competition.slug])
        )

        self.assertEqual(response.status_code, 200)
        pool_match = competition.matches.get()
        self.assertTrue(pool_match.is_money_match_fixture)
        self.assertEqual(pool_match.home_entry.player, home)
        self.assertEqual(pool_match.away_entry.player, away)
        self.assertEqual(response.context["form"].initial["race_target"], 7)

    def test_money_match_edit_shows_configuration_for_zero_entries_and_creates_fixture(self):
        competition = self.create_incomplete_money_match(slug="empty-money-match")
        home = PoolPlayer.objects.create(full_name="Configure Home")
        away = PoolPlayer.objects.create(full_name="Configure Away")
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_money_match_edit", args=[competition.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configure opponents")
        self.assertEqual(competition.matches.count(), 0)

        response = self.client.post(
            reverse("pool:manage_money_match_edit", args=[competition.slug]),
            {
                "title": "Configured later",
                "player_one": home.pk,
                "player_two": away.pk,
                "race_target": 15,
                "scheduled_at": "2026-10-03T19:00",
                "venue": "Mbabane",
                "scorer_identifier": "",
                "visibility": PoolCompetition.Visibility.PUBLISHED,
            },
        )

        self.assertRedirects(
            response,
            reverse("pool:manage_competition_dashboard", args=[competition.slug]),
            fetch_redirect_response=False,
        )
        competition.refresh_from_db()
        pool_match = competition.matches.get()
        self.assertEqual(competition.entries.count(), 2)
        self.assertEqual(competition.name, "Configured later")
        self.assertEqual(pool_match.home_entry.player, home)
        self.assertEqual(pool_match.away_entry.player, away)
        self.assertEqual(pool_match.race_target, 15)

    def test_money_match_edit_completes_a_single_existing_entry(self):
        competition = self.create_incomplete_money_match(slug="one-entry-money-match")
        existing_player = PoolPlayer.objects.create(full_name="Existing entry")
        new_player = PoolPlayer.objects.create(full_name="Missing entry")
        PoolCompetitionEntry.objects.create(
            competition=competition,
            player=existing_player,
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_money_match_edit", args=[competition.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["player_one"], existing_player)
        self.assertIsNone(response.context["pool_match"])

        response = self.client.post(
            reverse("pool:manage_money_match_edit", args=[competition.slug]),
            {
                "title": "Completed from one entry",
                "player_one": existing_player.pk,
                "player_two": new_player.pk,
                "race_target": 7,
                "scheduled_at": "",
                "venue": "",
                "scorer_identifier": "",
                "visibility": PoolCompetition.Visibility.DRAFT,
            },
        )

        self.assertEqual(response.status_code, 302)
        competition.refresh_from_db()
        pool_match = competition.matches.get()
        self.assertEqual(competition.entries.count(), 2)
        self.assertEqual(pool_match.home_entry.player, existing_player)
        self.assertEqual(pool_match.away_entry.player, new_player)

    def test_money_match_edit_prepopulates_and_updates_unstarted_opponents_and_scorer(self):
        competition, pool_match = self.create_singles_money_match(
            race_target=7,
            money_match_prize_pot=Decimal("2000"),
        )
        original_home = pool_match.home_entry.player
        away = pool_match.away_entry.player
        replacement = PoolPlayer.objects.create(full_name="Replacement opponent")
        scorer = User.objects.create_user(
            username="replacement-scorer", password="test-password"
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_money_match_edit", args=[competition.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["player_one"], original_home)
        self.assertEqual(response.context["form"].initial["player_two"], away)
        self.assertEqual(response.context["form"].initial["race_target"], 7)
        self.assertEqual(
            response.context["form"].initial["money_match_prize_pot"],
            Decimal("2000"),
        )

        response = self.client.post(
            reverse("pool:manage_money_match_edit", args=[competition.slug]),
            {
                "title": "Changed before play",
                "player_one": replacement.pk,
                "player_two": away.pk,
                "race_target": 9,
                "money_match_prize_pot": "2500.50",
                "scheduled_at": "2026-10-05T20:00",
                "venue": "Siteki",
                "scorer_identifier": scorer.username,
                "visibility": PoolCompetition.Visibility.PUBLISHED,
            },
        )

        self.assertEqual(response.status_code, 302)
        competition.refresh_from_db()
        pool_match = competition.matches.get()
        self.assertEqual(competition.entries.count(), 2)
        self.assertEqual(pool_match.home_entry.player, replacement)
        self.assertEqual(pool_match.away_entry.player, away)
        self.assertEqual(pool_match.race_target, 9)
        self.assertEqual(competition.money_match_prize_pot, Decimal("2500.50"))
        self.assertTrue(
            pool_match.scorer_assignments.filter(user=scorer, is_active=True).exists()
        )

        response = self.client.post(
            reverse("pool:manage_money_match_edit", args=[competition.slug]),
            {
                "title": "Changed before play",
                "player_one": replacement.pk,
                "player_two": away.pk,
                "race_target": 9,
                "money_match_prize_pot": "",
                "scheduled_at": "2026-10-05T20:00",
                "venue": "Siteki",
                "scorer_identifier": scorer.username,
                "visibility": PoolCompetition.Visibility.PUBLISHED,
            },
        )
        self.assertEqual(response.status_code, 302)
        competition.refresh_from_db()
        self.assertIsNone(competition.money_match_prize_pot)

    def test_money_match_edit_reports_more_than_two_entries_without_creating_fixture(self):
        competition = self.create_incomplete_money_match(slug="invalid-money-match")
        players = [
            PoolPlayer.objects.create(full_name=f"Invalid entry {number}")
            for number in range(1, 4)
        ]
        PoolCompetitionEntry._base_manager.bulk_create(
            [
                PoolCompetitionEntry(competition=competition, player=player)
                for player in players
            ]
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pool:manage_money_match_edit", args=[competition.slug])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "more than two entries")
        self.assertEqual(competition.matches.count(), 0)

    def test_money_match_participants_cannot_change_after_play_starts(self):
        competition, pool_match = self.create_singles_money_match(race_target=2)
        original_home = pool_match.home_entry.player
        original_away = pool_match.away_entry.player
        replacement = PoolPlayer.objects.create(full_name="Too late replacement")
        start_match(pool_match, recorded_by=self.owner)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("pool:manage_money_match_edit", args=[competition.slug]),
            {
                "title": "Attempted replacement",
                "player_one": replacement.pk,
                "player_two": original_away.pk,
                "race_target": 2,
                "scheduled_at": "2026-10-05T20:00",
                "venue": "Siteki",
                "scorer_identifier": "",
                "visibility": PoolCompetition.Visibility.PUBLISHED,
            },
        )

        self.assertRedirects(
            response,
            reverse("pool:manage_competition_dashboard", args=[competition.slug]),
            fetch_redirect_response=False,
        )
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.home_entry.player, original_home)
        self.assertEqual(pool_match.away_entry.player, original_away)

    def test_singles_scoring_completes_money_match_and_feeds_profile_statistics(self):
        competition, pool_match = self.create_singles_money_match(race_target=2)
        home_player = pool_match.home_entry.player
        away_player = pool_match.away_entry.player

        start_match(pool_match, recorded_by=self.owner)
        green_apple = record_rack(
            pool_match,
            winner=pool_match.home_entry,
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            recorded_by=self.owner,
        )
        red_apple = record_rack(
            pool_match,
            winner=pool_match.away_entry,
            outcome_type=PoolRack.OutcomeType.RED_APPLE,
            recorded_by=self.owner,
        )
        one_visit = record_rack(
            pool_match,
            winner=pool_match.home_entry,
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
            recorded_by=self.owner,
        )

        pool_match.refresh_from_db()
        competition.refresh_from_db()
        self.assertEqual(green_apple.outcome_type, PoolRack.OutcomeType.GREEN_APPLE)
        self.assertEqual(red_apple.outcome_type, PoolRack.OutcomeType.RED_APPLE)
        self.assertEqual(one_visit.outcome_type, PoolRack.OutcomeType.ONE_VISIT)
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(pool_match.winner_id, pool_match.home_entry_id)
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.COMPLETED,
        )
        self.assertEqual(result_processing_plan(pool_match), ("player_statistics",))

        response = self.client.get(reverse("pool:player_detail", args=[home_player.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["player_money_match_stats"]["money_matches_played"], 1)
        self.assertEqual(response.context["player_money_match_stats"]["money_match_wins"], 1)
        self.assertEqual(response.context["player_money_match_stats"]["money_match_losses"], 0)
        self.assertEqual(response.context["player_stats"]["green_apples"], 1)
        self.assertEqual(response.context["player_stats"]["one_visits"], 1)
        self.assertContains(response, "Money Match Record")
        self.assertContains(response, "Money Match &middot; Race to 2", html=True)

        response = self.client.get(reverse("pool:player_detail", args=[away_player.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["player_money_match_stats"]["money_matches_played"], 1)
        self.assertEqual(response.context["player_money_match_stats"]["money_match_wins"], 0)
        self.assertEqual(response.context["player_money_match_stats"]["money_match_losses"], 1)
        self.assertEqual(response.context["player_stats"]["red_apples"], 1)

    def test_public_filters_keep_finished_money_matches_out_of_generic_finished(self):
        active_competition, active_match = self.create_singles_money_match(race_target=2)
        start_match(active_match, recorded_by=self.owner)
        upcoming_competition, _upcoming_match = self.create_singles_money_match(race_target=2)
        finished_competition, finished_match = self.create_singles_money_match(race_target=1)
        start_match(finished_match, recorded_by=self.owner)
        record_rack(
            finished_match,
            winner=finished_match.home_entry,
            recorded_by=self.owner,
        )

        response = self.client.get(reverse("pool:home"), {"status": "active"})
        cards = {card.pk: card for card in response.context["competitions"]}
        self.assertTrue(cards[active_competition.pk].public_filter_initially_visible)
        self.assertFalse(cards[upcoming_competition.pk].public_filter_initially_visible)

        response = self.client.get(
            reverse("pool:home"),
            {"status": "upcoming", "category": "money_matches"},
        )
        cards = {card.pk: card for card in response.context["competitions"]}
        self.assertTrue(cards[upcoming_competition.pk].public_filter_initially_visible)

        response = self.client.get(reverse("pool:home"), {"status": "finished"})
        cards = {card.pk: card for card in response.context["competitions"]}
        self.assertFalse(cards[finished_competition.pk].public_filter_initially_visible)

        response = self.client.get(
            reverse("pool:home"),
            {"status": "finished", "category": "money_matches"},
        )
        cards = {card.pk: card for card in response.context["competitions"]}
        self.assertTrue(cards[finished_competition.pk].public_filter_initially_visible)

    def test_public_money_match_cards_show_the_optional_prize_pot(self):
        competition, pool_match = self.create_singles_money_match(
            race_target=1,
            money_match_prize_pot=Decimal("2500.50"),
        )
        competition_url = reverse("pool:competition_detail", args=[competition.slug])

        fixtures = self.client.get(competition_url, {"tab": "fixtures"})
        overview = self.client.get(competition_url)
        home = self.client.get(reverse("pool:home"), {"category": "money_matches"})

        for response in (fixtures, overview, home):
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Prize Pot: E2,500.50")

        start_match(pool_match, recorded_by=self.owner)
        record_rack(pool_match, winner=pool_match.home_entry, recorded_by=self.owner)
        results = self.client.get(competition_url, {"tab": "results"})
        self.assertEqual(results.status_code, 200)
        self.assertContains(results, "Prize Pot: E2,500.50")

        normal_competition = PoolCompetition.objects.create(
            name="No Prize Pot League",
            slug="no-prize-pot-league",
            owner=self.owner,
            visibility=PoolCompetition.Visibility.PUBLISHED,
        )
        normal_overview = self.client.get(
            reverse("pool:competition_detail", args=[normal_competition.slug])
        )
        self.assertNotContains(normal_overview, "pool-money-match-prize-pot")
