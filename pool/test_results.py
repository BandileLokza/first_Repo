"""Transactional result-management coverage for pool fixtures."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolMatch,
    PoolMatchResultRevision,
    PoolPlayer,
    PoolRack,
)
from .services import (
    PoolResultValidationError,
    correct_rack,
    open_result_correction,
    record_non_played_result,
    record_rack,
    start_match,
    transition_match_status,
    undo_rack,
)


User = get_user_model()


class PoolResultServiceTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="pool-result-owner",
            password="test-password",
        )
        self.competition = PoolCompetition.objects.create(
            name="Results Competition",
            slug="results-competition",
            owner=self.owner,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=2,
        )
        self.stage = self.competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        self.home_entry = PoolCompetitionEntry.objects.create(
            competition=self.competition,
            player=PoolPlayer.objects.create(full_name="Results Home"),
        )
        self.away_entry = PoolCompetitionEntry.objects.create(
            competition=self.competition,
            player=PoolPlayer.objects.create(full_name="Results Away"),
        )

    def make_match(self):
        return PoolMatch.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_entry=self.home_entry,
            away_entry=self.away_entry,
            scheduled_at=timezone.now(),
        )

    def finish_played_match(self):
        pool_match = self.make_match()
        start_match(pool_match, recorded_by=self.owner)
        first = record_rack(
            pool_match,
            winner=self.home_entry,
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            breaker=self.home_entry,
            recorded_by=self.owner,
        )
        second = record_rack(
            pool_match,
            winner=self.away_entry,
            outcome_type=PoolRack.OutcomeType.RED_APPLE,
            breaker=self.away_entry,
            recorded_by=self.owner,
        )
        third = record_rack(
            pool_match,
            winner=self.home_entry,
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
            recorded_by=self.owner,
        )
        pool_match.refresh_from_db()
        return pool_match, first, second, third


class PoolRackResultServiceTests(PoolResultServiceTestCase):
    def test_start_and_rack_recording_assigns_sequential_numbers_and_syncs_scores(self):
        pool_match = self.make_match()

        start_match(pool_match, recorded_by=self.owner)
        first = record_rack(
            pool_match,
            winner=self.home_entry,
            outcome_type=PoolRack.OutcomeType.GREEN_APPLE,
            breaker=self.home_entry,
            recorded_by=self.owner,
        )
        second = record_rack(
            pool_match,
            winner=self.away_entry,
            outcome_type=PoolRack.OutcomeType.ONE_VISIT,
            recorded_by=self.owner,
        )
        pool_match.refresh_from_db()

        self.assertEqual([first.rack_number, second.rack_number], [1, 2])
        self.assertEqual(first.recorded_by, self.owner)
        self.assertEqual(pool_match.status, PoolMatch.Status.LIVE)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (1, 1))
        self.assertEqual(
            list(pool_match.racks.values_list("rack_number", flat=True)), [1, 2]
        )

    def test_race_target_finishes_match_and_derives_winner_from_racks(self):
        pool_match, _, _, _ = self.finish_played_match()

        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(pool_match.result_method, PoolMatch.ResultMethod.PLAYED)
        self.assertEqual(pool_match.winner_id, self.home_entry.id)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (2, 1))
        self.assertIsNotNone(pool_match.completed_at)
        self.assertFalse(pool_match.is_in_correction_mode)
        self.assertTrue(
            PoolMatchResultRevision.objects.filter(
                match=pool_match,
                action=PoolMatchResultRevision.Action.RACK_RECORDED,
            ).exists()
        )

    def test_invalid_rack_participant_and_breaker_are_rejected(self):
        pool_match = self.make_match()
        outsider = PoolCompetitionEntry.objects.create(
            competition=self.competition,
            player=PoolPlayer.objects.create(full_name="Not In This Fixture"),
        )
        start_match(pool_match)

        with self.assertRaises(PoolResultValidationError):
            record_rack(pool_match, winner=outsider)
        with self.assertRaises(PoolResultValidationError):
            record_rack(
                pool_match,
                winner=self.home_entry,
                breaker=outsider,
            )

    def test_raw_rack_writes_are_rejected_outside_the_result_service(self):
        pool_match = self.make_match()
        start_match(pool_match)

        with self.assertRaises(ValidationError):
            PoolRack.objects.create(
                match=pool_match,
                rack_number=1,
                winner=self.home_entry,
            )

    def test_completed_match_cannot_be_scored_until_explicit_correction(self):
        pool_match, first, second, third = self.finish_played_match()

        with self.assertRaises(PoolResultValidationError):
            record_rack(pool_match, winner=self.home_entry)
        with self.assertRaises(PoolResultValidationError):
            correct_rack(pool_match, first, winner=self.away_entry)

        open_result_correction(pool_match, recorded_by=self.owner, reason="Fix scorer entry")
        pool_match.refresh_from_db()
        self.assertTrue(pool_match.is_in_correction_mode)
        undo_rack(pool_match, third, recorded_by=self.owner, reason="Undo wrong final rack")
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.LIVE)
        self.assertFalse(pool_match.is_in_correction_mode)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (1, 1))

        record_rack(pool_match, winner=self.away_entry, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(pool_match.winner_id, self.away_entry.id)

    def test_correcting_a_rack_resynchronises_and_can_change_winner(self):
        pool_match, first, _, _ = self.finish_played_match()

        open_result_correction(pool_match, recorded_by=self.owner, reason="Correct rack winner")
        correct_rack(
            pool_match,
            first,
            winner=self.away_entry,
            outcome_type=PoolRack.OutcomeType.NORMAL,
            breaker=self.away_entry,
            recorded_by=self.owner,
            reason="Home rack was awarded incorrectly",
        )
        pool_match.refresh_from_db()

        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(pool_match.winner_id, self.away_entry.id)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (1, 2))
        self.assertFalse(pool_match.is_in_correction_mode)

    def test_walkover_finishes_without_manufacturing_racks(self):
        pool_match = self.make_match()

        record_non_played_result(
            pool_match,
            result_method=PoolMatch.ResultMethod.WALKOVER,
            winner=self.home_entry,
            recorded_by=self.owner,
            reason="Away participant withdrew before play.",
        )
        pool_match.refresh_from_db()

        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(pool_match.result_method, PoolMatch.ResultMethod.WALKOVER)
        self.assertEqual(pool_match.winner_id, self.home_entry.id)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (0, 0))
        self.assertFalse(pool_match.racks.exists())

    def test_postpone_cancel_and_abandon_follow_result_lifecycle_rules(self):
        postponed = self.make_match()
        transition_match_status(postponed, status=PoolMatch.Status.POSTPONED)
        postponed.refresh_from_db()
        self.assertEqual(postponed.status, PoolMatch.Status.POSTPONED)
        transition_match_status(postponed, status=PoolMatch.Status.CANCELLED)
        postponed.refresh_from_db()
        self.assertEqual(postponed.status, PoolMatch.Status.CANCELLED)
        self.assertEqual((postponed.home_score, postponed.away_score), (0, 0))

        abandoned = self.make_match()
        start_match(abandoned)
        record_rack(abandoned, winner=self.home_entry)
        transition_match_status(abandoned, status=PoolMatch.Status.ABANDONED)
        abandoned.refresh_from_db()
        self.assertEqual(abandoned.status, PoolMatch.Status.ABANDONED)
        self.assertEqual((abandoned.home_score, abandoned.away_score), (1, 0))

    def test_manager_post_actions_record_the_authenticated_manager(self):
        pool_match = self.make_match()
        self.client.force_login(self.owner)

        start_response = self.client.post(
            reverse(
                "pool:manage_match_start",
                args=[self.competition.slug, pool_match.pk],
            )
        )
        self.assertRedirects(
            start_response,
            reverse("pool:manage_match", args=[self.competition.slug, pool_match.pk]),
        )
        rack_response = self.client.post(
            reverse(
                "pool:manage_match_record_rack",
                args=[self.competition.slug, pool_match.pk],
            ),
            {
                "winner": self.home_entry.pk,
                "outcome_type": PoolRack.OutcomeType.GREEN_APPLE,
                "breaker": self.home_entry.pk,
                "reason": "",
            },
        )
        self.assertRedirects(
            rack_response,
            reverse("pool:manage_match", args=[self.competition.slug, pool_match.pk]),
        )
        rack = PoolRack.objects.get(match=pool_match, rack_number=1)
        self.assertEqual(rack.recorded_by, self.owner)
