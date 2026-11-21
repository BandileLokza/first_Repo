"""Result-refresh and table-rebuild consistency after abandonment decisions."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from leagues.models import Competition, CompetitionSeries, Region, Stage, Standing
from matches.abandonment import (
    award_official_result, confirm_score_at_abandonment,
    reopen_abandoned_resolution, resume_abandoned_match,
)
from matches.models import Goal, Match, Player
from matches.services import (
    finalize_match_result_fast, rebuild_league_standings, save_quick_goal_details,
)
from matches.tasks import (
    recalculate_league_after_bulk_update_task,
    recalculate_match_after_result_task,
)
from predictions.models import Prediction, PredictionMatch
from teams.models import Team


class AbandonmentConsistencyTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(
            username="consistency-manager",
            email="consistency@example.test",
            password="test-password",
        )
        region = Region.objects.create(name="Consistency region", slug="consistency-region")
        series = CompetitionSeries.objects.create(
            name="Consistency series",
            slug="consistency-series",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=region,
        )
        self.competition = Competition.objects.create(
            name="Consistency league", slug="consistency-league", season="2026/27",
            series=series, format_type="TABLE",
        )
        self.stage = Stage.objects.create(
            competition=self.competition, name="League", stage_type="LEAGUE", order=1,
        )
        self.home = Team.objects.create(competition=self.competition, name="Home")
        self.away = Team.objects.create(competition=self.competition, name="Away")

    def make_match(self, *, leg=1, status="FINISHED", score=(2, 1)):
        return Match.objects.create(
            competition=self.competition, stage=self.stage,
            home_team=self.home, away_team=self.away, leg=leg,
            status=status, home_score=score[0], away_score=score[1],
            phase="FIRST_HALF" if status == "LIVE" else "NONE",
            minute=30 if status == "LIVE" else 0, match_date=timezone.now(),
        )

    def make_prediction(self, match, *, score):
        prediction_match, _ = PredictionMatch.objects.get_or_create(
            match=match, defaults={"competition": self.competition},
        )
        return Prediction.objects.create(
            user=self.actor, prediction_match=prediction_match,
            predicted_result="H", predicted_home_goals=score[0],
            predicted_away_goals=score[1],
        )

    def assert_home_table(self, *, played, points, goals_for, goals_against):
        standing = Standing.objects.get(
            competition=self.competition, stage=self.stage, team=self.home,
        )
        self.assertEqual(
            (standing.played, standing.points, standing.goals_for, standing.goals_against),
            (played, points, goals_for, goals_against),
        )

    def test_delayed_single_and_bulk_jobs_do_not_rescore_reopened_awards(self):
        for leg, bulk in enumerate((False, True), start=1):
            with self.subTest(bulk=bulk):
                match = self.make_match(leg=leg, status="LIVE", score=(1, 0))
                match.abandon_match("Unsafe pitch")
                prediction = self.make_prediction(match, score=(3, 0))
                award_official_result(
                    match_id=match.pk, actor=self.actor, home_score=3, away_score=0,
                )
                if bulk:
                    refresh = lambda: recalculate_league_after_bulk_update_task.run(
                        self.competition.pk, [match.pk]
                    )
                else:
                    refresh = lambda: recalculate_match_after_result_task.run(match.pk)
                refresh()
                refresh()
                prediction.refresh_from_db()
                self.assertEqual(prediction.points_awarded, 20)
                self.assert_home_table(played=1, points=3, goals_for=3, goals_against=0)

                reopen_abandoned_resolution(match_id=match.pk, actor=self.actor)
                prediction.refresh_from_db()
                self.assertEqual(prediction.points_awarded, 0)
                # This models an already queued award job arriving after the
                # committee has revoked the official result.
                refresh()
                match.refresh_from_db()
                prediction.refresh_from_db()
                self.assertEqual(match.status, "ABANDONED")
                self.assertFalse(match.table_updated)
                self.assertEqual(prediction.points_awarded, 0)
                self.assert_home_table(played=0, points=0, goals_for=0, goals_against=0)

    def test_bulk_refresh_uses_latest_corrected_result_and_remains_idempotent(self):
        match = self.make_match()
        prediction = self.make_prediction(match, score=(2, 1))
        recalculate_match_after_result_task.run(match.pk)
        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 20)
        Match.objects.filter(pk=match.pk).update(
            home_score=1, away_score=2, regulation_home_score=1, regulation_away_score=2,
        )

        for _ in range(2):
            recalculate_league_after_bulk_update_task.run(self.competition.pk, [match.pk])

        prediction.refresh_from_db()
        self.assertEqual(prediction.points_awarded, 0)
        self.assert_home_table(played=1, points=0, goals_for=1, goals_against=2)

    def test_rebuild_does_not_mark_an_intervening_finish_as_already_counted(self):
        self.make_match()
        later_match = self.make_match(leg=2, status="LIVE", score=(1, 0))
        original_bulk_update = Standing.objects.bulk_update

        def finish_after_source_snapshot(*args, **kwargs):
            # Simulate a result becoming final after totals were read. This
            # deterministic interleaving also runs on SQLite, without relying
            # on thread timing or its lack of SELECT FOR UPDATE support.
            Match.objects.filter(pk=later_match.pk).update(status="FINISHED", phase="NONE")
            return original_bulk_update(*args, **kwargs)

        with patch.object(Standing.objects, "bulk_update", side_effect=finish_after_source_snapshot):
            rebuild_league_standings(self.competition)

        later_match.refresh_from_db()
        self.assertFalse(later_match.table_updated)
        self.assert_home_table(played=1, points=3, goals_for=2, goals_against=1)
        recalculate_match_after_result_task.run(later_match.pk)
        self.assert_home_table(played=2, points=6, goals_for=3, goals_against=1)
        rebuild_league_standings(self.competition)
        self.assert_home_table(played=2, points=6, goals_for=3, goals_against=1)

    def test_rebuild_leaves_new_finished_fixture_for_its_own_refresh(self):
        self.make_match()
        original_bulk_update = Standing.objects.bulk_update
        inserted = []

        def insert_after_source_snapshot(*args, **kwargs):
            inserted.append(self.make_match(leg=2, score=(1, 0)))
            return original_bulk_update(*args, **kwargs)

        with patch.object(Standing.objects, "bulk_update", side_effect=insert_after_source_snapshot):
            rebuild_league_standings(self.competition)

        later_match = Match.objects.get(pk=inserted[0].pk)
        self.assertFalse(later_match.table_updated)
        recalculate_match_after_result_task.run(later_match.pk)
        self.assert_home_table(played=2, points=6, goals_for=3, goals_against=1)

    def test_deleted_fixture_refreshes_are_harmless(self):
        match = self.make_match()
        match_id = match.pk
        match.delete()
        recalculate_match_after_result_task.run(match_id)
        recalculate_league_after_bulk_update_task.run(self.competition.pk, [match_id])
        self.assertFalse(
            Standing.objects.filter(competition=self.competition).exclude(played=0).exists()
        )

    def test_generic_finalization_rejects_current_abandonment_from_a_stale_form(self):
        match = self.make_match(status="LIVE", score=(1, 0))
        stale_form_match = Match.objects.get(pk=match.pk)
        match.abandon_match("Unsafe pitch")

        with self.assertRaisesMessage(ValidationError, "abandoned-match workflow"):
            finalize_match_result_fast(stale_form_match, home_score=9, away_score=0)

        match.refresh_from_db()
        self.assertEqual((match.status, match.home_score, match.away_score), ("ABANDONED", 1, 0))
        self.assertFalse(match.abandonment_decisions.exists())

    def test_generic_correction_cannot_overwrite_a_current_committee_decision(self):
        for leg, decision_type in enumerate(("AWARD", "CONFIRM"), start=1):
            with self.subTest(decision_type=decision_type):
                match = self.make_match(leg=leg, status="LIVE", score=(1, 0))
                # This is the ordinary form's copy, without a decision marker.
                stale_form_match = Match.objects.get(pk=match.pk)
                match.abandon_match("Unsafe pitch")
                if decision_type == "AWARD":
                    _, decision = award_official_result(
                        match_id=match.pk, actor=self.actor, home_score=3, away_score=0,
                    )
                    expected_score = (3, 0)
                else:
                    _, decision = confirm_score_at_abandonment(
                        match_id=match.pk, actor=self.actor,
                    )
                    expected_score = (1, 0)
                reason = decision.reason

                with self.assertRaisesMessage(ValidationError, "Reopen the official abandonment decision"):
                    finalize_match_result_fast(stale_form_match, home_score=9, away_score=0)

                match.refresh_from_db()
                decision.refresh_from_db()
                self.assertEqual(match.status, "FINISHED")
                self.assertEqual((match.home_score, match.away_score), expected_score)
                self.assertTrue(decision.is_current)
                self.assertEqual(decision.reason, reason)

    def test_official_result_marker_is_protected_even_without_a_decision_record(self):
        for leg, resolution in enumerate((
            Match.AbandonmentResolution.AWARDED,
            Match.AbandonmentResolution.CONFIRMED,
        ), start=1):
            with self.subTest(resolution=resolution):
                match = self.make_match(leg=leg, score=(2, 1))
                Match.objects.filter(pk=match.pk).update(abandonment_resolution=resolution)
                self.assertFalse(match.abandonment_decisions.exists())

                with self.assertRaisesMessage(ValidationError, "Reopen the official abandonment decision"):
                    finalize_match_result_fast(match, home_score=3, away_score=1)

                match.refresh_from_db()
                self.assertEqual((match.status, match.home_score, match.away_score), ("FINISHED", 2, 1))
                self.assertEqual(match.abandonment_resolution, resolution)

    def test_normal_results_and_resumed_matches_still_use_generic_finalization(self):
        for leg, status in enumerate(("UPCOMING", "LIVE", "FINISHED", "RESUMED"), start=1):
            with self.subTest(status=status):
                match = self.make_match(
                    leg=leg, status="LIVE" if status == "RESUMED" else status, score=(1, 0),
                )
                if status == "RESUMED":
                    match.abandon_match("Unsafe pitch")
                    match, _ = resume_abandoned_match(
                        match_id=match.pk, actor=self.actor, resume_minute=30,
                    )
                finalized = finalize_match_result_fast(match, home_score=2, away_score=1)
                finalized.refresh_from_db()
                self.assertEqual(
                    (finalized.status, finalized.home_score, finalized.away_score),
                    ("FINISHED", 2, 1),
                )
                if status == "RESUMED":
                    self.assertEqual(finalized.abandonment_resolution, "RESUMED")

    def test_official_score_decisions_reject_goal_creation_from_stale_forms(self):
        for leg, decision_type in enumerate(("AWARD", "CONFIRM"), start=1):
            with self.subTest(decision_type=decision_type):
                match = self.make_match(leg=leg, status="LIVE", score=(1, 0))
                stale_form_match = Match.objects.get(pk=match.pk)
                match.abandon_match("Unsafe pitch")
                if decision_type == "AWARD":
                    award_official_result(
                        match_id=match.pk, actor=self.actor, home_score=3, away_score=0,
                    )
                    goal_count = 3
                else:
                    confirm_score_at_abandonment(match_id=match.pk, actor=self.actor)
                    goal_count = 1
                rows = [
                    {
                        "team_id": self.home.pk,
                        "goal_type": Goal.GoalType.NORMAL,
                        "new_player_name": "Invented scorer",
                    }
                    for _ in range(goal_count)
                ]

                with self.assertRaisesMessage(ValidationError, "official abandonment decision"):
                    save_quick_goal_details(stale_form_match, rows, actor=self.actor)

                self.assertFalse(match.goals.exists())
                self.assertFalse(Player.objects.filter(name="Invented scorer").exists())
                self.assertTrue(match.abandonment_decisions.get().is_current)

    def test_reopened_award_can_resume_and_finish_without_losing_reason_or_inventing_goals(self):
        match = self.make_match(status="LIVE", score=(1, 0))
        scorer = Player.objects.create(team=self.home, name="Recorded scorer")
        original_goal = Goal.objects.create(
            match=match, team=self.home, player=scorer, minute=12,
        )
        match.abandon_match("Lightning evacuation")
        award_official_result(
            match_id=match.pk, actor=self.actor, home_score=3, away_score=0,
        )
        self.assertEqual(match.goals.count(), 1)
        reopen_abandoned_resolution(match_id=match.pk, actor=self.actor)
        resumed, _ = resume_abandoned_match(
            match_id=match.pk, actor=self.actor, resume_minute=30,
        )
        # Submitted/stale objects must not erase the locked historical value.
        resumed.abandon_reason = None
        finished = finalize_match_result_fast(resumed, home_score=1, away_score=0)
        finished.refresh_from_db()

        self.assertEqual(finished.status, "FINISHED")
        self.assertEqual(finished.abandonment_resolution, "RESUMED")
        self.assertEqual(finished.abandon_reason, "Lightning evacuation")
        self.assertEqual(
            (finished.abandoned_home_score, finished.abandoned_away_score), (1, 0)
        )
        self.assertEqual(list(finished.goals.values_list("pk", flat=True)), [original_goal.pk])
        self.assertEqual(finished.abandonment_decisions.count(), 2)
        self.assertEqual(scorer.recorded_goals, 1)
