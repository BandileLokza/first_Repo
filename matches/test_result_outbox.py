from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from itertools import product
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import OperationalError, connection, connections, transaction
from django.test import RequestFactory, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from league_manager.models import ActivityLog
from leagues.models import Competition, CompetitionTieBreaker, Stage, Standing
from leagues.seasons import archive_competition_season
from leagues.standings import rank_standings
from matches.abandonment import award_official_result, reopen_abandoned_resolution
from matches.admin import MatchAdmin
from matches.models import Match, MatchResultProcessingOutbox as Outbox
from matches.result_dispatcher import claim_due_result_work, dispatch_due_result_work, settle_dispatch
from matches.result_outbox import RESULT_BUSY, lock_result_competition
from matches.services import finalize_match_result_fast
from matches.tasks import process_football_result_task, recalculate_match_after_result_task
from predictions.models import Prediction, PredictionHistory
from predictions.utils import calculate_predictions_for_match
from teams.models import Team


class ResultOutboxTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="outbox-admin", email="outbox@example.test",
        )
        self.match = self.make_match("outbox-league")

    def make_match(self, slug):
        league = Competition.objects.create(name=slug, slug=slug, season="2026", format_type="TABLE")
        stage = Stage.objects.create(competition=league, name="League", stage_type="LEAGUE", order=1)
        home = Team.objects.create(competition=league, name="Home")
        away = Team.objects.create(competition=league, name="Away")
        return Match.objects.create(
            competition=league, stage=stage, home_team=home, away_team=away,
            match_date=timezone.now() - timedelta(hours=2),
        )

    def finish(self, home=2, away=1):
        return finalize_match_result_fast(
            self.match, home_score=home, away_score=away,
            regulation_home_score=home, regulation_away_score=away,
        )

    def job(self):
        return Outbox.objects.get(match=self.match)

    def predict(self, home=2, away=1, outcome="H"):
        return Prediction.objects.create(
            user=self.user, prediction_match=self.match.prediction_match,
            predicted_result=outcome, predicted_home_goals=home, predicted_away_goals=away,
        )

    def points(self):
        return tuple(
            Standing.objects.get(competition=self.match.competition, team=team).points
            for team in (self.match.home_team, self.match.away_team)
        )

    def force_due(self):
        Outbox.objects.filter(match=self.match).update(next_attempt_at=timezone.now())

    def test_commit_persists_work_and_duplicate_delivery_does_not_repeat_writes(self):
        prediction = self.predict()
        with patch("matches.result_dispatcher.publish_result_work", side_effect=ConnectionError) as publish:
            self.finish()
        publish.assert_not_called()
        self.assertFalse(connection.in_atomic_block)
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "FINISHED")
        self.assertEqual((self.job().requested_revision, self.job().completed_revision), (1, 0))
        self.assertEqual(self.points(), (0, 0))
        self.assertEqual(prediction.points_awarded, 0)

        self.assertTrue(process_football_result_task.run(self.match.pk))
        job = self.job()
        prediction.refresh_from_db()
        self.assertEqual(self.points(), (3, 0))
        self.assertEqual(prediction.points_awarded, 20)
        self.assertEqual((job.state, job.completed_revision), (Outbox.State.COMPLETED, 1))
        self.assertIsNotNone(job.last_completed_at)
        # Delivery again simulates a worker completing before its acknowledgement was received.
        with patch("matches.result_outbox.calculate_predictions_for_match") as score:
            self.assertFalse(process_football_result_task.run(self.match.pk))
        score.assert_not_called()
        self.assertEqual(self.points(), (3, 0))

    def test_result_and_job_roll_back_together(self):
        with self.assertRaisesMessage(RuntimeError, "after finalization"):
            with transaction.atomic():
                self.finish()
                self.assertEqual(self.job().state, Outbox.State.PENDING)
                raise RuntimeError("after finalization")
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "UPCOMING")
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())

    def test_worker_reads_latest_revision_after_multiple_corrections(self):
        prediction = self.predict(0, 2, "A")
        self.finish()
        self.finish(1, 1)
        self.finish(0, 2)
        self.finish(0, 2)
        job = self.job()
        self.assertEqual(Outbox.objects.filter(match=self.match).count(), 1)
        self.assertEqual((job.requested_revision, job.completed_revision), (3, 0))
        self.assertTrue(process_football_result_task.run(self.match.pk))
        self.assertFalse(process_football_result_task.run(self.match.pk))
        self.assertEqual(self.points(), (0, 3))
        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 20)
        self.assertEqual(self.job().completed_revision, 3)

    def test_publication_error_and_published_but_unfinished_job_are_recoverable(self):
        self.finish()
        with self.assertLogs("matches.result_dispatcher", "WARNING"):
            with patch("matches.result_dispatcher.publish_result_work", side_effect=ConnectionError("private broker details")):
                self.assertEqual(dispatch_due_result_work(20), 1)
        job = self.job()
        self.assertIn("ConnectionError", job.last_error)
        self.assertNotIn("private broker details", job.last_error)
        self.assertGreater(job.next_attempt_at, timezone.now())
        self.assertEqual(job.completed_revision, 0)

        self.force_due()
        with patch("matches.result_dispatcher.publish_result_work") as publish:
            self.assertEqual(dispatch_due_result_work(20), 1)
            publish.assert_called_once_with(self.match.pk)
            self.assertEqual(dispatch_due_result_work(20), 0)
        self.assertEqual(self.job().state, Outbox.State.PENDING)
        self.assertIsNone(self.job().dispatch_token)
        self.assertEqual(self.points(), (0, 0))

        self.force_due()

        def deliver(match_id):
            self.assertFalse(connection.in_atomic_block)
            process_football_result_task.run(match_id)

        with patch("matches.result_dispatcher.publish_result_work", side_effect=deliver):
            self.assertEqual(dispatch_due_result_work(20), 1)
        self.assertEqual(self.job().state, Outbox.State.COMPLETED)
        self.assertIsNone(self.job().next_attempt_at)
        self.assertEqual(self.points(), (3, 0))

    def test_ambiguous_publish_error_cannot_reopen_completed_work(self):
        self.finish()

        def delivered_before_error(match_id):
            self.assertTrue(process_football_result_task.run(match_id))
            raise ConnectionError("connection lost after publication")

        with self.assertLogs("matches.result_dispatcher", "WARNING"):
            with patch("matches.result_dispatcher.publish_result_work", side_effect=delivered_before_error):
                self.assertEqual(dispatch_due_result_work(20), 1)
        job = self.job()
        self.assertEqual((job.state, job.completed_revision), (Outbox.State.COMPLETED, 1))
        self.assertEqual(job.last_error, "")
        self.assertIsNone(job.next_attempt_at)

    def test_claim_expires_and_old_publisher_cannot_settle_new_lease_or_revision(self):
        self.finish()
        first, = claim_due_result_work(20)
        self.assertEqual(claim_due_result_work(20), [])
        self.force_due()
        second, = claim_due_result_work(20)
        self.assertNotEqual(first.dispatch_token, second.dispatch_token)
        settle_dispatch(first, error=ConnectionError())
        self.assertEqual(self.job().dispatch_token, second.dispatch_token)
        self.finish(0, 1)
        settle_dispatch(second, error=ConnectionError())
        job = self.job()
        self.assertEqual((job.requested_revision, job.dispatch_attempts), (2, 0))
        self.assertEqual(job.last_error, "")
        self.assertIsNone(job.dispatch_token)
        self.assertTrue(process_football_result_task.run(self.match.pk))
        self.assertEqual(self.points(), (0, 3))

    def test_worker_failure_rolls_back_points_and_standings_and_retries_remain_due(self):
        prediction = self.predict()
        self.finish()

        def fail_after_write(match):
            Prediction.objects.filter(pk=prediction.pk).update(points_awarded=20)
            raise RuntimeError("crash after scoring")

        with patch("matches.result_outbox.calculate_predictions_for_match", side_effect=fail_after_write):
            for attempt in range(1, 5):
                with self.assertRaisesMessage(RuntimeError, "crash after scoring"):
                    process_football_result_task.run(self.match.pk)
                self.assertEqual(self.job().processing_failures, attempt)
                self.assertEqual(self.job().completed_revision, 0)
        self.assertEqual(self.points(), (0, 0))
        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 0)
        self.assertEqual(self.job().state, Outbox.State.PENDING)
        self.assertGreater(self.job().next_attempt_at, timezone.now())
        self.force_due()
        with patch("matches.result_dispatcher.publish_result_work", side_effect=process_football_result_task.run):
            dispatch_due_result_work(20)
        self.assertEqual(self.job().completed_revision, 1)
        self.assertEqual(self.points(), (3, 0))

    def test_prediction_update_preserves_each_scoring_component_in_one_statement(self):
        cases = list(product(("H", "D", "A"), range(3), range(3)))
        user_model = get_user_model()
        users = user_model.objects.bulk_create([
            user_model(username=f"predictor-{i}") for i in range(len(cases))
        ])
        Prediction.objects.bulk_create([
            Prediction(
                user=user, prediction_match=self.match.prediction_match,
                predicted_result=outcome, predicted_home_goals=home, predicted_away_goals=away,
            )
            for user, (outcome, home, away) in zip(users, cases)
        ])
        self.match.home_score, self.match.away_score = 4, 3
        for actual_home, actual_away, actual_outcome in ((2, 1, "H"), (1, 1, "D"), (1, 2, "A")):
            self.match.regulation_home_score = actual_home
            self.match.regulation_away_score = actual_away
            with self.assertNumQueries(1):
                calculate_predictions_for_match(self.match)
            actual = dict(Prediction.objects.values_list("user_id", "points_awarded"))
            for user, (outcome, home, away) in zip(users, cases):
                expected = 5 * sum((
                    outcome == actual_outcome,
                    home == actual_home,
                    away == actual_away,
                    (home, away) == (actual_home, actual_away),
                ))
                self.assertEqual(actual[user.pk], expected)

    def test_official_decisions_retire_old_work_and_reopening_removes_projection(self):
        prediction = self.predict(0, 3, "A")
        self.finish()
        # A historic pending row can coexist with an interrupted fixture.
        Match.objects.filter(pk=self.match.pk).update(status="ABANDONED")
        award_official_result(match_id=self.match.pk, actor=self.user, home_score=0, away_score=3)
        self.assertEqual(self.job().state, Outbox.State.CANCELLED)
        self.assertEqual(self.job().completed_revision, 0)
        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 20)
        self.assertFalse(process_football_result_task.run(self.match.pk))
        self.assertEqual(self.points(), (0, 3))

        reopen_abandoned_resolution(match_id=self.match.pk, actor=self.user)
        recalculate_match_after_result_task.run(self.match.pk)
        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 0)
        self.assertEqual(self.points(), (0, 0))

    def test_archive_and_prediction_history_wait_until_latest_work_completes(self):
        self.predict()
        self.finish()
        self.client.force_login(self.user)
        reset = reverse("reset_predictions", args=[self.match.competition.slug])
        with self.assertRaisesMessage(ValidationError, "Wait for pending standings"):
            archive_competition_season(self.match.competition, actor=self.user)
        self.assertEqual(self.client.post(reset).status_code, 302)
        self.assertFalse(PredictionHistory.objects.exists())
        self.assertTrue(Prediction.objects.exists())

        process_football_result_task.run(self.match.pk)
        self.assertEqual(self.client.post(reset).status_code, 302)
        self.assertEqual(PredictionHistory.objects.get().points_awarded, 20)
        self.assertTrue(archive_competition_season(self.match.competition, actor=self.user).is_archived)

    def test_head_to_head_uses_applied_score_while_public_match_uses_new_score(self):
        league = self.match.competition
        self.finish()
        process_football_result_task.run(self.match.pk)
        Standing.objects.filter(competition=league, team=self.match.away_team).update(baseline_points=3)
        CompetitionTieBreaker.objects.filter(competition=league).delete()
        CompetitionTieBreaker.objects.create(
            competition=league, order=1, criterion=CompetitionTieBreaker.Criterion.POINTS,
        )
        CompetitionTieBreaker.objects.create(
            competition=league, order=2, criterion=CompetitionTieBreaker.Criterion.HEAD_TO_HEAD_POINTS,
        )
        table = Standing.objects.filter(
            competition=league, stage=self.match.stage, group__isnull=True,
        ).select_related("team")

        self.finish(0, 2)
        self.assertContains(self.client.get(reverse("match_detail", args=[self.match.pk])), "0 - 2")
        with self.assertNumQueries(2):
            ranked = rank_standings(league, table)
        self.assertEqual([row.team_id for row in ranked], [self.match.home_team_id, self.match.away_team_id])
        process_football_result_task.run(self.match.pk)
        self.assertEqual(
            [row.team_id for row in rank_standings(league, table)],
            [self.match.away_team_id, self.match.home_team_id],
        )

    def test_historical_enqueue_is_scoped_dry_run_and_coalesces(self):
        other = self.make_match("unrelated-outbox-league")
        Match.objects.filter(pk__in=[self.match.pk, other.pk]).update(status="FINISHED", home_score=2)
        args = {"competition": self.match.competition.slug, "enqueue": True, "stdout": StringIO()}
        call_command("reconcile_football_results", **args)
        self.assertFalse(Outbox.objects.exists())
        call_command("reconcile_football_results", apply=True, **args)
        call_command("reconcile_football_results", apply=True, **args)
        self.assertEqual(Outbox.objects.count(), 1)
        self.assertEqual(self.job().requested_revision, 1)
        process_football_result_task.run(self.match.pk)
        self.assertEqual(self.points(), (3, 0))
        other.refresh_from_db()
        self.assertFalse(other.table_updated)

    def test_quick_result_returns_retry_feedback_on_a_busy_competition(self):
        class LockUnavailable(Exception):
            sqlstate = "55P03"

        def busy(*args, **kwargs):
            self.assertTrue(kwargs["nowait"])
            raise OperationalError("locked") from LockUnavailable()

        self.client.force_login(self.user)
        with patch("matches.views.lock_result_match", side_effect=busy):
            response = self.client.post(
                reverse("save_quick_result", args=[self.match.pk]),
                {"home_score": 1, "away_score": 0}, follow=True,
            )
        self.assertContains(response, RESULT_BUSY)
        self.assertFalse(Outbox.objects.exists())

    def test_live_completion_and_admin_correction_both_record_work_and_audit(self):
        Match.objects.filter(pk=self.match.pk).update(
            status="LIVE", phase="SECOND_HALF", home_score=2, away_score=1,
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse("end_match", args=[self.match.pk])).status_code, 302)
        self.assertEqual(self.job().requested_revision, 1)
        self.assertEqual(self.points(), (0, 0))
        self.match.refresh_from_db()
        self.match.home_score = 1
        self.match.regulation_home_score = 1
        request = RequestFactory().post("/")
        request.user = self.user
        model_admin = MatchAdmin(Match, admin.site)
        with patch.object(model_admin, "message_user"):
            model_admin.save_model(
                request, self.match, SimpleNamespace(changed_data=["home_score", "regulation_home_score"]), True,
            )
        self.assertEqual(self.job().requested_revision, 2)
        self.assertEqual(
            set(self.match.activity_logs.values_list("action", flat=True)),
            {ActivityLog.Action.MATCH_FINISHED, ActivityLog.Action.RESULT_CORRECTED},
        )
        process_football_result_task.run(self.match.pk)
        self.assertEqual(self.points(), (1, 1))

    def test_deleted_match_has_no_outstanding_job(self):
        self.finish()
        match_id = self.match.pk
        self.match.delete()
        self.assertFalse(Outbox.objects.filter(match_id=match_id).exists())
        self.assertFalse(process_football_result_task.run(match_id))

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_dispatcher_skips_claimed_rows_and_processes_other_competition(self):
        other = self.make_match("other-claim")
        self.finish()
        finalize_match_result_fast(other, home_score=1, away_score=0)

        def claim():
            try:
                return claim_due_result_work(20)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                Outbox.objects.select_for_update().get(match=self.match)
                jobs = executor.submit(claim).result(timeout=10)
                self.assertEqual([job.match_id for job in jobs], [other.pk])
            jobs = executor.submit(claim).result(timeout=10)
            self.assertEqual([job.match_id for job in jobs], [self.match.pk])

    @skipUnlessDBFeature("has_select_for_update")
    def test_worker_reloads_after_concurrent_writer_and_repair(self):
        self.finish()
        entered = Event()

        def work():
            try:
                entered.set()
                return process_football_result_task.run(self.match.pk)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                lock_result_competition(self.match.competition_id)
                self.finish(0, 2)
                task = executor.submit(work)
                self.assertTrue(entered.wait(5))
                call_command(
                    "reconcile_football_results", competition=self.match.competition.slug,
                    match_id=[self.match.pk], apply=True, stdout=StringIO(),
                )
            self.assertFalse(task.result(timeout=10))
        self.assertEqual(self.job().completed_revision, 2)
        self.assertEqual(self.points(), (0, 3))


    @skipUnlessDBFeature("has_select_for_update_nowait")
    def test_busy_competition_rejects_web_save_while_other_competition_processes(self):
        other = self.make_match("independent-competition")
        finalize_match_result_fast(other, home_score=1, away_score=0)
        self.client.force_login(self.user)

        def submit():
            try:
                return self.client.post(
                    reverse("save_quick_result", args=[self.match.pk]),
                    {"home_score": 2, "away_score": 1}, follow=True,
                )
            finally:
                connections.close_all()

        def process_other():
            try:
                return process_football_result_task.run(other.pk)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                lock_result_competition(self.match.competition_id)
                self.assertContains(executor.submit(submit).result(timeout=10), RESULT_BUSY)
                self.assertTrue(executor.submit(process_other).result(timeout=10))
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())
