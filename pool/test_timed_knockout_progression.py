"""Regression coverage for timed knockout completion and live advancement.

This targets the production-shaped path where an authoritative timer finishes
an already-live knockout tie below its race target.  It intentionally builds a
persisted bracket first: initial-draw confirmation has a different optimisation
path from materialising one downstream tie during live play.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection, reset_queries
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .brackets import create_draw_preview, generate_bracket_from_preview
from .models import (
    PoolBracketDrawPreview,
    PoolBracketAuditEvent,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionStage,
    PoolKnockoutTie,
    PoolKnockoutTieSlot,
    PoolMatch,
    PoolMatchResultRevision,
    PoolPlayer,
)
from .services import PoolResultValidationError, expire_match_if_due, record_rack, start_match


User = get_user_model()


class TimedKnockoutProgressionTests(TestCase):
    """Exercise a real live QF -> SF materialisation transaction."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="timed-knockout-owner",
            password="test-password",
        )
        self._competition_number = 0

    def _competition(self, *, entrant_count, allow_two_lives=False):
        self._competition_number += 1
        competition = PoolCompetition.objects.create(
            name=f"Timed Knockout {self._competition_number}",
            slug=f"timed-knockout-{self._competition_number}",
            owner=self.owner,
            participant_mode=PoolCompetition.ParticipantMode.SINGLES,
            format_type=PoolCompetition.FormatType.KNOCKOUT,
            visibility=PoolCompetition.Visibility.PUBLISHED,
            default_race_target=2,
            match_time_limit_minutes=1,
            allow_two_lives=allow_two_lives,
        )
        entries = [
            PoolCompetitionEntry.objects.create(
                competition=competition,
                player=PoolPlayer.objects.create(
                    full_name=f"Timed entrant {self._competition_number}-{number}"
                ),
            )
            for number in range(1, entrant_count + 1)
        ]
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT
        )
        return competition, stage, entries

    def _generate_manual_bracket(self, *, entrant_count):
        competition, stage, entries = self._competition(entrant_count=entrant_count)
        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=entrant_count,
            manual_entry_ids=[entry.pk for entry in entries],
            actor=self.owner,
        )
        return generate_bracket_from_preview(preview, actor=self.owner), stage

    def _finish_at_race_target(self, pool_match, winner):
        start_match(pool_match, recorded_by=self.owner)
        for _number in range(pool_match.race_target):
            record_rack(pool_match, winner=winner, recorded_by=self.owner)
        pool_match.refresh_from_db()
        self.assertEqual(pool_match.status, PoolMatch.Status.FINISHED)
        return pool_match

    def _prepare_expired_quarterfinal(self, *, entrant_count=8):
        bracket, stage = self._generate_manual_bracket(entrant_count=entrant_count)
        opening_ties = list(
            bracket.ties.filter(round_number=1, is_third_place=False).order_by(
                "position"
            )
        )
        source_tie, sibling_tie = opening_ties[:2]
        self._finish_at_race_target(sibling_tie.match, sibling_tie.match.home_entry)

        source_match = source_tie.match
        start_match(source_match, recorded_by=self.owner)
        record_rack(
            source_match,
            winner=source_match.home_entry,
            recorded_by=self.owner,
        )
        expired_started_at = timezone.now() - timedelta(minutes=2)
        PoolMatch.objects.filter(pk=source_match.pk).update(
            timer_started_at=expired_started_at,
            timer_deadline=expired_started_at + timedelta(minutes=1),
        )
        source_match.refresh_from_db()
        target_tie = bracket.ties.get(
            round_number=2,
            position=1,
            is_third_place=False,
        )
        target_tie.refresh_from_db()
        self.assertIsNone(target_tie.match_id)
        self.assertEqual((source_match.home_score, source_match.away_score), (1, 0))
        self.assertEqual(source_match.status, PoolMatch.Status.LIVE)
        return bracket, stage, source_tie, source_match, sibling_tie, target_tie

    def _assert_expired_quarterfinal_progressed(
        self,
        *,
        stage,
        source_tie,
        source_match,
        sibling_tie,
        target_tie,
    ):
        source_match.refresh_from_db()
        source_tie.refresh_from_db()
        sibling_tie.refresh_from_db()
        target_tie.refresh_from_db()
        self.assertEqual(source_match.status, PoolMatch.Status.FINISHED)
        self.assertEqual(source_match.result_method, PoolMatch.ResultMethod.PLAYED)
        self.assertEqual(source_match.completion_reason, PoolMatch.CompletionReason.TIME_LIMIT)
        self.assertEqual(source_match.winner_id, source_match.home_entry_id)
        self.assertEqual((source_match.home_score, source_match.away_score), (1, 0))
        self.assertEqual(source_tie.resolution_method, PoolKnockoutTie.ResolutionMethod.MATCH)
        self.assertEqual(source_tie.resolved_winner_id, source_match.home_entry_id)
        self.assertIsNotNone(target_tie.match_id)
        self.assertEqual(target_tie.match.status, PoolMatch.Status.UPCOMING)
        self.assertEqual(target_tie.match.race_target, target_tie.race_target)
        self.assertEqual(
            {slot.entry_id for slot in target_tie.slots.all()},
            {source_tie.resolved_winner_id, sibling_tie.resolved_winner_id},
        )
        self.assertEqual(PoolMatch.objects.filter(stage=stage).count(), 5)

    def test_expired_leading_quarterfinal_finishes_and_materialises_semifinal(self):
        (
            _bracket,
            stage,
            source_tie,
            source_match,
            sibling_tie,
            target_tie,
        ) = self._prepare_expired_quarterfinal()

        expire_match_if_due(source_match, recorded_by=self.owner)

        self._assert_expired_quarterfinal_progressed(
            stage=stage,
            source_tie=source_tie,
            source_match=source_match,
            sibling_tie=sibling_tie,
            target_tie=target_tie,
        )

    def test_expiry_http_endpoint_finishes_and_progresses_the_quarterfinal(self):
        (
            _bracket,
            stage,
            source_tie,
            source_match,
            sibling_tie,
            target_tie,
        ) = self._prepare_expired_quarterfinal()
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("pool:match_expire", args=[source_match.pk]),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "status": PoolMatch.Status.FINISHED,
                "home_score": 1,
                "away_score": 0,
                "time_expired_tied": False,
                "completion_reason": PoolMatch.CompletionReason.TIME_LIMIT,
                "timer_deadline": source_match.timer_deadline.isoformat(),
            },
        )
        self._assert_expired_quarterfinal_progressed(
            stage=stage,
            source_tie=source_tie,
            source_match=source_match,
            sibling_tie=sibling_tie,
            target_tie=target_tie,
        )

    def test_repeated_expiry_is_idempotent_without_duplicate_semifinal(self):
        (
            bracket,
            stage,
            source_tie,
            source_match,
            sibling_tie,
            target_tie,
        ) = self._prepare_expired_quarterfinal()

        expire_match_if_due(source_match, recorded_by=self.owner)
        target_tie.refresh_from_db()
        first_match_id = target_tie.match_id
        first_revision_count = PoolMatchResultRevision.objects.filter(
            match=source_match,
            action=PoolMatchResultRevision.Action.MATCH_COMPLETED,
        ).count()
        first_slot_audit_count = PoolBracketAuditEvent.objects.filter(
            bracket=bracket,
            action=PoolBracketAuditEvent.Action.SLOT_UPDATED,
        ).count()

        # The HTTP endpoint reads the current row for every POST.  A fresh
        # already-finished instance must therefore avoid contending for the
        # bracket lock or replaying progression altogether.
        source_match.refresh_from_db()
        reset_queries()
        with CaptureQueriesContext(connection) as retry_queries:
            expire_match_if_due(source_match, recorded_by=self.owner)

        target_tie.refresh_from_db()
        self.assertEqual(target_tie.match_id, first_match_id)
        # A nested ``transaction.atomic`` can emit a SAVEPOINT/RELEASE pair
        # under Django's TestCase wrapper. The only real read is the current
        # match row; no bracket/progression SQL should run.
        self.assertLessEqual(len(retry_queries), 3, len(retry_queries))
        self.assertEqual(PoolMatch.objects.filter(stage=stage).count(), 5)
        self.assertEqual(
            PoolMatchResultRevision.objects.filter(
                match=source_match,
                action=PoolMatchResultRevision.Action.MATCH_COMPLETED,
            ).count(),
            first_revision_count,
        )
        self.assertEqual(
            PoolBracketAuditEvent.objects.filter(
                bracket=bracket,
                action=PoolBracketAuditEvent.Action.SLOT_UPDATED,
            ).count(),
            first_slot_audit_count,
        )
        self._assert_expired_quarterfinal_progressed(
            stage=stage,
            source_tie=source_tie,
            source_match=source_match,
            sibling_tie=sibling_tie,
            target_tie=target_tie,
        )

    def test_rack_submission_at_expired_leading_quarterfinal_records_no_extra_rack(self):
        (
            _bracket,
            stage,
            source_tie,
            source_match,
            sibling_tie,
            target_tie,
        ) = self._prepare_expired_quarterfinal()

        with self.assertRaises(PoolResultValidationError):
            record_rack(
                source_match,
                winner=source_match.home_entry,
                recorded_by=self.owner,
            )

        source_match.refresh_from_db()
        self.assertEqual(source_match.racks.count(), 1)
        self._assert_expired_quarterfinal_progressed(
            stage=stage,
            source_tie=source_tie,
            source_match=source_match,
            sibling_tie=sibling_tie,
            target_tie=target_tie,
        )

    def test_expired_two_life_tie_advances_the_exact_winning_life(self):
        competition, stage, entries = self._competition(
            entrant_count=2,
            allow_two_lives=True,
        )
        first_entry, second_entry = entries
        first_life_one = first_entry.lives.get(life_number=1)
        first_life_two = PoolCompetitionEntryLife.objects.create(
            competition_entry=first_entry,
            life_number=2,
        )
        second_life_one = second_entry.lives.get(life_number=1)
        second_life_two = PoolCompetitionEntryLife.objects.create(
            competition_entry=second_entry,
            life_number=2,
        )
        preview = create_draw_preview(
            competition,
            stage=stage,
            draw_method=PoolBracketDrawPreview.DrawMethod.MANUAL,
            bracket_size=4,
            manual_life_ids=[
                first_life_one.pk,
                second_life_one.pk,
                second_life_two.pk,
                first_life_two.pk,
            ],
            actor=self.owner,
        )
        bracket = generate_bracket_from_preview(preview, actor=self.owner)
        source_tie, sibling_tie = list(
            bracket.ties.filter(round_number=1, is_third_place=False).order_by(
                "position"
            )
        )
        self._finish_at_race_target(sibling_tie.match, second_entry)
        source_match = source_tie.match
        start_match(source_match, recorded_by=self.owner)
        record_rack(source_match, winner=first_entry, recorded_by=self.owner)
        expired_started_at = timezone.now() - timedelta(minutes=2)
        PoolMatch.objects.filter(pk=source_match.pk).update(
            timer_started_at=expired_started_at,
            timer_deadline=expired_started_at + timedelta(minutes=1),
        )

        expire_match_if_due(source_match, recorded_by=self.owner)

        source_match.refresh_from_db()
        source_tie.refresh_from_db()
        final = bracket.ties.get(round_number=2, position=1, is_third_place=False)
        final.refresh_from_db()
        self.assertEqual(source_match.winner_entry_life_id, first_life_one.pk)
        self.assertEqual(source_tie.resolved_winner_life_id, first_life_one.pk)
        self.assertIsNotNone(final.match_id)
        self.assertIn(
            first_life_one.pk,
            {final.match.home_entry_life_id, final.match.away_entry_life_id},
        )
        self.assertIn(
            second_life_two.pk,
            {final.match.home_entry_life_id, final.match.away_entry_life_id},
        )

    def test_live_progression_query_growth_is_bounded(self):
        """One resolved tie must not re-scan/lock the full bracket topology."""

        (
            _small_bracket,
            _small_stage,
            _small_source_tie,
            small_match,
            _small_sibling_tie,
            _small_target_tie,
        ) = self._prepare_expired_quarterfinal(entrant_count=8)
        (
            _large_bracket,
            _large_stage,
            _large_source_tie,
            large_match,
            _large_sibling_tie,
            _large_target_tie,
        ) = self._prepare_expired_quarterfinal(entrant_count=64)

        reset_queries()
        with CaptureQueriesContext(connection) as small_queries:
            expire_match_if_due(small_match, recorded_by=self.owner)
        reset_queries()
        with CaptureQueriesContext(connection) as large_queries:
            expire_match_if_due(large_match, recorded_by=self.owner)

        # SQLite can split otherwise constant-size statements, so leave
        # backend headroom while catching a return to per-tie revalidation.
        self.assertLessEqual(
            len(large_queries),
            len(small_queries) + 16,
            (len(small_queries), len(large_queries)),
        )
        # The representative 64-slot case currently takes 60 SQLite queries,
        # including the authoritative source-result save and result-revision
        # audit.  Keep enough backend headroom while preventing a return to
        # the repeated tie/slot full-clean and lock/query amplification.
        self.assertLessEqual(len(large_queries), 70, len(large_queries))

    def test_target_slots_keep_the_expected_source_ties(self):
        """Guard the representative QF -> SF topology used by the tests."""

        (
            _bracket,
            _stage,
            source_tie,
            _source_match,
            sibling_tie,
            target_tie,
        ) = self._prepare_expired_quarterfinal()
        self.assertEqual(
            set(
                PoolKnockoutTieSlot.objects.filter(tie=target_tie).values_list(
                    "source_tie_id", flat=True
                )
            ),
            {source_tie.pk, sibling_tie.pk},
        )
