"""Focused regression coverage for advanced pool tournament operations.

These tests deliberately stay at the model/service boundary.  They verify the
authoritative state that the manager and public interfaces consume, without
coupling the new operations to a particular template implementation.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from unittest.mock import patch
from django.utils import timezone

from .brackets import (
    SUPPORTED_BRACKET_SIZES,
    create_draw_preview,
    generate_bracket_from_preview,
)
from .forms import PoolMatchScorerForm, PoolPlayerRegistrationForm
from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionStage,
    PoolBracketDrawPreview,
    PoolKnockoutTie,
    PoolMatch,
    PoolMatchScorerAssignment,
    PoolPlayer,
)
from .permissions import (
    user_can_manage_competition,
    user_can_score_pool_match,
)
from .services import (
    correct_rack,
    expire_match_if_due,
    open_result_correction,
    record_rack,
    start_match,
    transition_match_status,
    undo_rack,
)


User = get_user_model()


class AdvancedTournamentOperationsTestCase(TestCase):
    """Small factory set for new timer, scorer, and life-aware tests."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="advanced-pool-owner",
            password="test-password",
        )
        self._competition_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        defaults = {
            "name": f"Advanced Pool Competition {self._competition_number}",
            "slug": f"advanced-pool-{self._competition_number}",
            "owner": self.owner,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.KNOCKOUT,
            "visibility": PoolCompetition.Visibility.DRAFT,
            "default_race_target": 2,
        }
        defaults.update(overrides)
        return PoolCompetition.objects.create(**defaults)

    @staticmethod
    def knockout_stage(competition):
        return competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        )

    def make_entry(self, competition, name):
        return PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name=name),
        )

    def make_match(self, competition, home_entry, away_entry):
        return PoolMatch.objects.create(
            competition=competition,
            stage=self.knockout_stage(competition),
            home_entry=home_entry,
            away_entry=away_entry,
            scheduled_at=timezone.now(),
        )


class AdvancedCompetitionConfigurationTests(AdvancedTournamentOperationsTestCase):
    def test_timer_and_two_life_options_are_limited_to_their_supported_formats(self):
        league = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE,
        )
        league.match_time_limit_minutes = 10
        with self.assertRaises(ValidationError):
            league.full_clean()

        team_knockout = PoolCompetition(
            name="Advanced Team Knockout",
            slug="advanced-team-knockout",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            allow_two_lives=True,
        )
        with self.assertRaises(ValidationError):
            team_knockout.full_clean()

    def test_enabled_two_life_mode_creates_life_one_for_each_real_entry(self):
        competition = self.make_competition(allow_two_lives=True)
        entry = self.make_entry(competition, "Two Life Entrant")

        self.assertEqual(entry.tournament_life_count, 1)
        life = entry.lives.get(life_number=1)
        self.assertEqual(life.status, PoolCompetitionEntryLife.Status.ACTIVE)

    def test_deleting_an_unreferenced_entry_cascades_its_life_identity(self):
        competition = self.make_competition(allow_two_lives=True)
        entry = self.make_entry(competition, "Disposable Entrant")
        life_id = entry.lives.get(life_number=1).pk

        entry.delete()

        self.assertFalse(PoolCompetitionEntryLife.objects.filter(pk=life_id).exists())

    def test_bulk_entry_import_creates_life_one_and_life_identity_is_immutable(self):
        competition = self.make_competition(allow_two_lives=True)
        imported_player = PoolPlayer.objects.create(full_name="Bulk Imported Entrant")
        imported_entry = PoolCompetitionEntry(
            competition=competition,
            player=imported_player,
        )

        PoolCompetitionEntry.objects.bulk_create([imported_entry])
        imported_entry.refresh_from_db()
        life = imported_entry.lives.get(life_number=1)

        self.assertEqual(life.life_number, 1)
        with self.assertRaises(ValidationError):
            PoolCompetitionEntryLife.objects.filter(pk=life.pk).update(
                life_number=2
            )
        life.refresh_from_db()
        self.assertEqual(life.life_number, 1)

    def test_competition_state_save_does_not_repair_every_entry_life(self):
        competition = self.make_competition(allow_two_lives=True)
        entries = [
            self.make_entry(competition, f"State entrant {number}")
            for number in range(8)
        ]
        before_life_ids = set(
            PoolCompetitionEntryLife.objects.filter(
                competition_entry__in=entries
            ).values_list("pk", flat=True)
        )

        with patch(
            "pool.models.PoolCompetitionEntryLife.objects.get_or_create"
        ) as create_life:
            competition.operational_state = PoolCompetition.OperationalState.ACTIVE
            competition.save()
            competition.operational_state = PoolCompetition.OperationalState.COMPLETED
            competition.save()

        competition.refresh_from_db()
        self.assertEqual(
            competition.operational_state,
            PoolCompetition.OperationalState.COMPLETED,
        )
        self.assertEqual(competition.entries.count(), len(entries))
        self.assertEqual(
            set(
                PoolCompetitionEntryLife.objects.filter(
                    competition_entry__in=entries
                ).values_list("pk", flat=True)
            ),
            before_life_ids,
        )
        create_life.assert_not_called()

    def test_preview_blocks_later_entry_registration_before_draw_can_go_stale(self):
        competition = self.make_competition()
        first_entry = self.make_entry(competition, "Preview Entrant One")
        second_entry = self.make_entry(competition, "Preview Entrant Two")
        stage = self.knockout_stage(competition)
        create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=2,
            manual_entry_ids=[first_entry.pk, second_entry.pk],
            actor=self.owner,
        )

        with self.assertRaises(ValidationError):
            PoolCompetitionEntry.objects.create(
                competition=competition,
                player=PoolPlayer.objects.create(full_name="Late Preview Entrant"),
            )

    def test_saved_entry_cannot_be_reassigned_to_a_different_real_player(self):
        competition = self.make_competition()
        entry = self.make_entry(competition, "Permanent Entry Player")
        entry.player = PoolPlayer.objects.create(full_name="Replacement Player")

        with self.assertRaises(ValidationError):
            entry.save()

    def test_saved_entry_cannot_be_moved_to_a_different_competition(self):
        competition = self.make_competition()
        entry = self.make_entry(competition, "Permanent Competition Entry")
        entry.competition = self.make_competition()

        with self.assertRaises(ValidationError):
            entry.save()

    def test_saved_life_cannot_be_moved_to_a_different_entry(self):
        competition = self.make_competition(allow_two_lives=True)
        first_entry = self.make_entry(competition, "First Life Owner")
        second_entry = self.make_entry(competition, "Second Life Owner")
        life_two = PoolCompetitionEntryLife.objects.create(
            competition_entry=first_entry,
            life_number=2,
        )
        life_two.competition_entry = second_entry

        with self.assertRaises(ValidationError):
            life_two.save()


class AdvancedMatchTimerTests(AdvancedTournamentOperationsTestCase):
    def setUp(self):
        super().setUp()
        self.competition = self.make_competition(match_time_limit_minutes=3)
        self.home_entry = self.make_entry(self.competition, "Timer Home")
        self.away_entry = self.make_entry(self.competition, "Timer Away")

    def test_start_snapshots_timer_and_deadline_finishes_leader_at_actual_score(self):
        pool_match = self.make_match(
            self.competition,
            self.home_entry,
            self.away_entry,
        )
        start_match(pool_match, recorded_by=self.owner)
        pool_match.refresh_from_db()

        self.assertEqual(pool_match.applied_time_limit_minutes, 3)
        self.assertIsNotNone(pool_match.timer_started_at)
        self.assertIsNotNone(pool_match.timer_deadline)

        # Subsequent competition edits affect only fixtures that start later.
        self.competition.match_time_limit_minutes = 9
        self.competition.save()
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.applied_time_limit_minutes, 3)

        record_rack(pool_match, winner=self.home_entry, recorded_by=self.owner)
        expired_started_at = timezone.now() - timedelta(minutes=4)
        PoolMatch.objects.filter(pk=pool_match.pk).update(
            timer_started_at=expired_started_at,
            timer_deadline=expired_started_at + timedelta(minutes=3),
        )

        expire_match_if_due(pool_match, recorded_by=self.owner)
        pool_match.refresh_from_db()

        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(pool_match.result_method, PoolMatch.ResultMethod.PLAYED)
        self.assertEqual(pool_match.winner_id, self.home_entry.pk)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (1, 0))
        self.assertEqual(
            pool_match.completion_reason,
            PoolMatch.CompletionReason.TIME_LIMIT,
        )

    def test_tied_expiry_persists_deciding_rack_state_and_next_rack_finishes(self):
        pool_match = self.make_match(
            self.competition,
            self.home_entry,
            self.away_entry,
        )
        start_match(pool_match, recorded_by=self.owner)
        # Bring the race level to one rack each before its deadline. The next
        # rack will also hit the race target, so this specifically guards the
        # required TIME_LIMIT reason for an expiry-created deciding rack.
        record_rack(pool_match, winner=self.home_entry, recorded_by=self.owner)
        record_rack(pool_match, winner=self.away_entry, recorded_by=self.owner)
        expired_started_at = timezone.now() - timedelta(minutes=4)
        PoolMatch.objects.filter(pk=pool_match.pk).update(
            timer_started_at=expired_started_at,
            timer_deadline=expired_started_at + timedelta(minutes=3),
        )

        expire_match_if_due(pool_match, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.LIVE)
        self.assertTrue(pool_match.time_expired_tied)
        self.assertIsNone(pool_match.winner_id)

        record_rack(pool_match, winner=self.home_entry, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertFalse(pool_match.time_expired_tied)
        self.assertEqual(pool_match.winner_id, self.home_entry.pk)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (2, 1))
        self.assertEqual(
            pool_match.completion_reason,
            PoolMatch.CompletionReason.TIME_LIMIT,
        )

    def test_first_unlimited_start_is_a_durable_timer_policy_snapshot(self):
        self.competition.match_time_limit_minutes = None
        self.competition.save()
        pool_match = self.make_match(
            self.competition,
            self.home_entry,
            self.away_entry,
        )

        start_match(pool_match, recorded_by=self.owner)
        pool_match.refresh_from_db()
        first_snapshot_at = pool_match.timer_snapshot_at
        self.assertIsNotNone(first_snapshot_at)
        self.assertIsNone(pool_match.applied_time_limit_minutes)
        self.assertIsNone(pool_match.timer_deadline)

        transition_match_status(
            pool_match,
            status=PoolMatch.Status.POSTPONED,
            recorded_by=self.owner,
        )
        self.competition.match_time_limit_minutes = 7
        self.competition.save()

        start_match(pool_match, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.timer_snapshot_at, first_snapshot_at)
        self.assertIsNone(pool_match.applied_time_limit_minutes)
        self.assertIsNone(pool_match.timer_started_at)
        self.assertIsNone(pool_match.timer_deadline)

    def test_expired_race_target_correction_reopens_as_a_deciding_tie(self):
        """A late correction must retain the original timer, not restart play."""

        pool_match = self.make_match(
            self.competition,
            self.home_entry,
            self.away_entry,
        )
        start_match(pool_match, recorded_by=self.owner)
        first_rack = record_rack(
            pool_match,
            winner=self.home_entry,
            recorded_by=self.owner,
        )
        record_rack(
            pool_match,
            winner=self.home_entry,
            recorded_by=self.owner,
        )
        expired_started_at = timezone.now() - timedelta(minutes=4)
        PoolMatch.objects.filter(pk=pool_match.pk).update(
            timer_started_at=expired_started_at,
            timer_deadline=expired_started_at + timedelta(minutes=3),
        )

        open_result_correction(
            pool_match,
            recorded_by=self.owner,
            reason="Correct a post-match rack outcome.",
        )
        correct_rack(
            pool_match,
            first_rack,
            winner=self.away_entry,
            recorded_by=self.owner,
            reason="Away player won the first rack.",
        )
        pool_match.refresh_from_db()

        self.assertEqual(pool_match.status, PoolMatch.Status.LIVE)
        self.assertTrue(pool_match.time_expired_tied)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (1, 1))
        self.assertEqual(pool_match.completion_reason, "")

        record_rack(
            pool_match,
            winner=self.home_entry,
            recorded_by=self.owner,
        )
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(pool_match.winner_id, self.home_entry.pk)
        self.assertEqual(
            pool_match.completion_reason,
            PoolMatch.CompletionReason.TIME_LIMIT,
        )

    def test_expired_race_target_undo_finishes_the_actual_leader_by_time_limit(self):
        pool_match = self.make_match(
            self.competition,
            self.home_entry,
            self.away_entry,
        )
        start_match(pool_match, recorded_by=self.owner)
        record_rack(
            pool_match,
            winner=self.home_entry,
            recorded_by=self.owner,
        )
        latest_rack = record_rack(
            pool_match,
            winner=self.home_entry,
            recorded_by=self.owner,
        )
        expired_started_at = timezone.now() - timedelta(minutes=4)
        PoolMatch.objects.filter(pk=pool_match.pk).update(
            timer_started_at=expired_started_at,
            timer_deadline=expired_started_at + timedelta(minutes=3),
        )

        open_result_correction(
            pool_match,
            recorded_by=self.owner,
            reason="Undo the accidentally recorded final rack.",
        )
        undo_rack(
            pool_match,
            latest_rack,
            recorded_by=self.owner,
            reason="Remove accidental final rack.",
        )
        pool_match.refresh_from_db()

        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        self.assertFalse(pool_match.time_expired_tied)
        self.assertEqual((pool_match.home_score, pool_match.away_score), (1, 0))
        self.assertEqual(pool_match.winner_id, self.home_entry.pk)
        self.assertEqual(
            pool_match.completion_reason,
            PoolMatch.CompletionReason.TIME_LIMIT,
        )


class AdvancedGenericScorerTests(AdvancedTournamentOperationsTestCase):
    def setUp(self):
        super().setUp()
        self.competition = self.make_competition()
        self.home_entry = self.make_entry(self.competition, "Scorer Home")
        self.away_entry = self.make_entry(self.competition, "Scorer Away")
        self.pool_match = self.make_match(
            self.competition,
            self.home_entry,
            self.away_entry,
        )

    def test_exact_active_account_assignment_is_fixture_scoped_not_management_access(self):
        scorer = User.objects.create_user(
            username="fixture-scorer",
            email="fixture-scorer@example.test",
            password="test-password",
        )
        inactive = User.objects.create_user(
            username="inactive-scorer",
            email="inactive-scorer@example.test",
            password="test-password",
            is_active=False,
        )

        partial_form = PoolMatchScorerForm(
            {"identifier": "fixture"},
            match=self.pool_match,
        )
        self.assertFalse(partial_form.is_valid())

        inactive_form = PoolMatchScorerForm(
            {"identifier": inactive.username},
            match=self.pool_match,
        )
        self.assertFalse(inactive_form.is_valid())

        form = PoolMatchScorerForm(
            {"identifier": scorer.email},
            match=self.pool_match,
        )
        self.assertTrue(form.is_valid(), form.errors)
        assignment = form.save(assigned_by=self.owner)

        self.assertIsInstance(assignment, PoolMatchScorerAssignment)
        self.assertTrue(user_can_score_pool_match(scorer, self.pool_match))
        self.assertFalse(user_can_manage_competition(scorer, self.competition))

        other_match = self.make_match(
            self.competition,
            self.away_entry,
            self.home_entry,
        )
        self.assertFalse(user_can_score_pool_match(scorer, other_match))

    def test_inactive_account_assignment_can_be_deactivated_but_not_reactivated(self):
        scorer = User.objects.create_user(
            username="later-inactive-scorer",
            email="later-inactive@example.test",
            password="test-password",
        )
        assignment = PoolMatchScorerAssignment.objects.create(
            match=self.pool_match,
            user=scorer,
            assigned_by=self.owner,
            is_active=True,
        )

        scorer.is_active = False
        scorer.save(update_fields=["is_active"])
        assignment.is_active = False
        assignment.save(update_fields=["is_active", "updated_at"])
        assignment.refresh_from_db()
        self.assertFalse(assignment.is_active)

        assignment.is_active = True
        with self.assertRaises(ValidationError):
            assignment.save(update_fields=["is_active", "updated_at"])


class AdvancedPlayerRegistrationTests(AdvancedTournamentOperationsTestCase):
    def test_duplicate_suggestions_prefer_the_normalised_exact_region(self):
        competition = self.make_competition()
        local_player = PoolPlayer.objects.create(
            full_name="Regional Duplicate",
            region="Mbabane",
        )
        other_region_player = PoolPlayer.objects.create(
            full_name="Regional Duplicate",
            region="Manzini",
        )

        form = PoolPlayerRegistrationForm(
            {
                "full_name": "  Regional   Duplicate  ",
                "nickname": "",
                "region": "  mbabane ",
                "country": "",
                "gender": "",
                "contact_email": "",
                "contact_phone": "",
            },
            competition=competition,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["region"], "mbabane")
        self.assertEqual(
            {player.pk for player in form.potential_duplicate_players()},
            {local_player.pk},
        )
        self.assertNotIn(
            other_region_player.pk,
            {player.pk for player in form.potential_duplicate_players()},
        )


class AdvancedTwoLifeBracketTests(AdvancedTournamentOperationsTestCase):
    def setUp(self):
        super().setUp()
        self.competition = self.make_competition(
            allow_two_lives=True,
            default_race_target=1,
        )
        self.stage = self.knockout_stage(self.competition)
        self.first_entry = self.make_entry(self.competition, "Converging Entrant")
        self.second_entry = self.make_entry(self.competition, "Opponent Entrant")
        self.first_lives = [
            self.first_entry.lives.get(life_number=1),
            PoolCompetitionEntryLife.objects.create(
                competition_entry=self.first_entry,
                life_number=2,
            ),
        ]
        self.second_lives = [
            self.second_entry.lives.get(life_number=1),
            PoolCompetitionEntryLife.objects.create(
                competition_entry=self.second_entry,
                life_number=2,
            ),
        ]

    def test_life_draw_uses_256_capable_topology_and_resolves_same_entry_final(self):
        self.assertTrue({64, 128, 256}.issubset(SUPPORTED_BRACKET_SIZES))
        manual_life_ids = [
            self.first_lives[0].pk,
            self.second_lives[0].pk,
            self.first_lives[1].pk,
            self.second_lives[1].pk,
        ]
        preview = create_draw_preview(
            self.competition,
            stage=self.stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
            manual_life_ids=manual_life_ids,
            actor=self.owner,
        )
        self.assertEqual(preview.life_draw_order, manual_life_ids)
        self.assertEqual(
            preview.draw_order,
            [
                self.first_entry.pk,
                self.second_entry.pk,
                self.first_entry.pk,
                self.second_entry.pk,
            ],
        )

        bracket = generate_bracket_from_preview(preview, actor=self.owner)
        semi_ids = list(
            bracket.ties.filter(round_number=1, is_third_place=False)
            .order_by("position")
            .values_list("pk", flat=True)
        )
        self.assertEqual(len(semi_ids), 2)

        # Each life advances through its own semi-final.  The final must not
        # fabricate a self-fixture when both paths converge on one real entry.
        for tie_id in semi_ids:
            semi = PoolKnockoutTie.objects.get(pk=tie_id)
            start_match(semi.match, recorded_by=self.owner)
            record_rack(
                semi.match,
                winner=self.first_entry,
                recorded_by=self.owner,
            )

        final = bracket.ties.get(round_number=2, is_third_place=False)
        final.refresh_from_db()
        self.assertIsNone(final.match_id)
        self.assertEqual(final.resolved_winner_id, self.first_entry.pk)
        self.assertEqual(
            final.resolution_method,
            PoolKnockoutTie.ResolutionMethod.SAME_ENTRY_FINAL,
        )

        for life in self.first_lives:
            life.refresh_from_db()
            self.assertEqual(life.status, PoolCompetitionEntryLife.Status.CHAMPION)
        for life in self.second_lives:
            life.refresh_from_db()
            self.assertEqual(life.status, PoolCompetitionEntryLife.Status.ELIMINATED)

    def test_same_entry_third_place_convergence_creates_no_self_fixture(self):
        manual_life_ids = [
            self.first_lives[0].pk,
            self.second_lives[0].pk,
            self.first_lives[1].pk,
            self.second_lives[1].pk,
        ]
        preview = create_draw_preview(
            self.competition,
            stage=self.stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
            manual_life_ids=manual_life_ids,
            third_place_enabled=True,
            actor=self.owner,
        )
        bracket = generate_bracket_from_preview(preview, actor=self.owner)

        # The opponent wins both semi-finals. Its two lives converge in the
        # final and this entrant's two lives converge in third place.
        for semi in bracket.ties.filter(round_number=1, is_third_place=False):
            start_match(semi.match, recorded_by=self.owner)
            record_rack(
                semi.match,
                winner=self.second_entry,
                recorded_by=self.owner,
            )

        third_place = bracket.ties.get(is_third_place=True)
        third_place.refresh_from_db()
        self.assertIsNone(third_place.match_id)
        self.assertEqual(third_place.resolved_winner_id, self.first_entry.pk)
        self.assertEqual(
            third_place.resolution_method,
            PoolKnockoutTie.ResolutionMethod.SAME_ENTRY_THIRD,
        )
        for life in self.first_lives:
            life.refresh_from_db()
            self.assertEqual(life.status, PoolCompetitionEntryLife.Status.ELIMINATED)

    def test_entry_cannot_be_deleted_while_a_life_draw_preview_depends_on_it(self):
        preview = create_draw_preview(
            self.competition,
            stage=self.stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
            manual_life_ids=[
                self.first_lives[0].pk,
                self.second_lives[0].pk,
                self.first_lives[1].pk,
                self.second_lives[1].pk,
            ],
            actor=self.owner,
        )
        self.assertTrue(preview.pk)
        with self.assertRaises(ValidationError):
            self.first_entry.delete()
        with self.assertRaises(ValidationError):
            PoolCompetitionEntry.objects.filter(pk=self.second_entry.pk).delete()

    def test_life_one_is_not_independently_deletable(self):
        with self.assertRaises(ValidationError):
            self.first_lives[0].delete()
        with self.assertRaises(ValidationError):
            PoolCompetitionEntryLife.objects.filter(
                pk=self.first_lives[0].pk
            ).delete()
        self.assertTrue(
            self.first_entry.lives.filter(life_number=1).exists()
        )
