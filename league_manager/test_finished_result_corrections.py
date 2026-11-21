from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, transaction
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from league_manager.models import ActivityLog, LeagueMembership
from leagues.models import Competition, Group, Stage, Standing
from matches.abandonment import award_official_result, confirm_score_at_abandonment
from matches.models import FootballKnockoutRoundRule, FootballKnockoutTie, Goal, Match, Player
from matches.models import MatchResultProcessingOutbox as Outbox
from matches.result_outbox import RESULT_SAVED
from matches.finished_scorers import CORRECTION_SALT, scorer_snapshot
from matches.services import apply_match_to_standings_incrementally, finalize_match_result_fast
from matches.tasks import process_football_result_task, recalculate_league_after_bulk_update_task
from predictions.models import Prediction
from teams.models import Team


def finished_fixture(slug, *, format_type="TABLE"):
    league = Competition.objects.create(name=slug, slug=slug, season="2026", format_type=format_type)
    stage_type = {"TABLE": "LEAGUE", "GROUP_KNOCKOUT": "GROUP", "KNOCKOUT": "KNOCKOUT"}[format_type]
    stage = Stage.objects.create(competition=league, name=stage_type, stage_type=stage_type, order=1)
    home = Team.objects.create(competition=league, name="Home")
    away = Team.objects.create(competition=league, name="Away")
    group = None
    if stage_type == "GROUP":
        group = Group.objects.create(stage=stage, name="Group A")
        group.teams.add(home, away)
    match = Match.objects.create(
        competition=league, stage=stage, group=group, home_team=home, away_team=away,
        status="FINISHED", match_date=timezone.now(),
        home_score=2, away_score=1, regulation_home_score=2, regulation_away_score=1,
        resolution_method="REGULATION",
    )
    apply_match_to_standings_incrementally(match)
    match.refresh_from_db()
    return match


def score_post(match, home, away, **other_scores):
    snapshot = scorer_snapshot(match, match.goals.filter(canceled=False).order_by("pk"))
    return {
        "version": signing.dumps((str(uuid4()), snapshot), salt=CORRECTION_SALT),
        "home_score": home,
        "away_score": away,
        **other_scores,
    }


def finished_url(match):
    return reverse("league_manager:edit_finished_match", args=[match.competition.slug, match.pk])


def finished_list_url(match):
    return reverse("league_manager:result_update", args=[match.competition.slug]) + "?tab=finished"


def table_totals(match):
    rows = Standing.objects.filter(competition=match.competition, stage=match.stage, group=match.group)
    return {
        standing.team_id: (
            standing.played, standing.won, standing.drawn, standing.lost,
            standing.goals_for, standing.goals_against, standing.goal_difference(), standing.points,
        )
        for standing in rows
    }


class FinishedResultCorrectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(username="result-superuser", email="result@example.test")
        cls.match = finished_fixture("finished-corrections")

    def setUp(self):
        self.client.force_login(self.user)
        self.url = finished_url(self.match)

    def audit_count(self, match=None):
        return (match or self.match).activity_logs.filter(action=ActivityLog.Action.RESULT_CORRECTED).count()

    def test_two_corrections_queue_table_snapshots_and_predictions_without_redis(self):
        prediction = Prediction.objects.create(
            user=self.user, prediction_match=self.match.prediction_match,
            predicted_result="D", predicted_home_goals=1, predicted_away_goals=1,
        )
        first = score_post(self.match, 1, 1)
        with patch("matches.tasks.recalculate_league_after_bulk_update_task.delay", side_effect=ConnectionError("Redis unavailable")) as publish:
            response = self.client.post(self.url, first, follow=True)
            self.assertContains(response, RESULT_SAVED)
            publish.assert_not_called()
        self.match.refresh_from_db()
        prediction.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (1, 1))
        self.assertEqual((self.match.table_home_score, self.match.table_away_score), (2, 1))
        self.assertEqual(prediction.points_awarded, 0)
        job = Outbox.objects.get(match=self.match)
        self.assertEqual((job.requested_revision, job.completed_revision), (1, 0))
        self.assertTrue(process_football_result_task.run(self.match.pk))
        self.match.refresh_from_db()
        prediction.refresh_from_db()
        self.assertEqual((self.match.table_home_score, self.match.table_away_score), (1, 1))
        self.assertEqual(prediction.points_awarded, 20)
        self.assertEqual(table_totals(self.match), {
            self.match.home_team_id: (1, 0, 1, 0, 1, 1, 0, 1),
            self.match.away_team_id: (1, 0, 1, 0, 1, 1, 0, 1),
        })
        self.client.post(self.url, first)
        self.assertEqual(self.audit_count(), 1)
        # A worker published by an earlier request reads the latest committed score.
        recalculate_league_after_bulk_update_task.run(self.match.competition_id, [self.match.pk, self.match.pk])
        self.assertEqual(table_totals(self.match)[self.match.home_team_id], (1, 0, 1, 0, 1, 1, 0, 1))

        self.client.post(self.url, score_post(self.match, 0, 2))
        job.refresh_from_db()
        self.assertEqual((job.requested_revision, job.completed_revision), (2, 1))
        self.assertTrue(process_football_result_task.run(self.match.pk))
        self.match.refresh_from_db()
        prediction.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (0, 2))
        self.assertEqual((self.match.table_home_score, self.match.table_away_score), (0, 2))
        self.assertEqual(table_totals(self.match)[self.match.away_team_id], (1, 1, 0, 0, 2, 0, 2, 3))
        self.assertEqual(prediction.points_awarded, 0)
        self.assertEqual(self.audit_count(), 2)
        self.assertContains(self.client.get(reverse("match_detail", args=[self.match.pk])), "0 - 2")

    def test_group_stage_same_winner_preserves_other_match_and_competition_contributions(self):
        match = finished_fixture("group-corrections", format_type="GROUP_KNOCKOUT")
        unrelated = finished_fixture("unrelated-corrections")
        third = Team.objects.create(competition=match.competition, name="Third")
        match.group.teams.add(third)
        historical = Match.objects.create(
            competition=match.competition, stage=match.stage, group=match.group,
            home_team=third, away_team=match.home_team, status="FINISHED", home_score=1, away_score=1,
        )
        apply_match_to_standings_incrementally(historical)
        old_history = Match.objects.filter(pk=historical.pk).values().get()
        unrelated_table = table_totals(unrelated)
        response = self.client.post(finished_url(match), score_post(match, 4, 2), follow=True)
        self.assertContains(response, RESULT_SAVED)
        self.assertTrue(process_football_result_task.run(match.pk))
        self.assertEqual(table_totals(match), {
            match.home_team_id: (2, 1, 1, 0, 5, 3, 2, 4),
            match.away_team_id: (1, 0, 0, 1, 2, 4, -2, 0),
            third.pk: (1, 0, 1, 0, 1, 1, 0, 1),
        })
        self.assertEqual(Match.objects.filter(pk=historical.pk).values().get(), old_history)
        self.assertEqual(table_totals(unrelated), unrelated_table)

    def test_stale_editor_cannot_overwrite_a_newer_goal_edit(self):
        goal = Goal.objects.create(match=self.match, team=self.match.home_team, minute=30)
        stale = score_post(self.match, 1, 1)
        stale[f"goal-{goal.pk}-remove"] = "1"
        newer = score_post(self.match, 2, 1)
        newer[f"goal-{goal.pk}-minute"] = "35"
        self.assertRedirects(self.client.post(self.url, newer), self.url, fetch_redirect_response=False)
        self.assertContains(
            self.client.post(self.url, stale, follow=True),
            "This match was updated by another administrator.",
        )
        goal.refresh_from_db()
        self.match.refresh_from_db()
        self.assertEqual(goal.minute, 35)
        self.assertFalse(goal.canceled)
        self.assertEqual((self.match.home_score, self.match.away_score), (2, 1))
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())

    def official_result(self, *, leg, award=False):
        match = Match.objects.create(
            competition=self.match.competition, stage=self.match.stage,
            home_team=self.match.away_team, away_team=self.match.home_team,
            leg=leg, status="LIVE", phase="FIRST_HALF", minute=30,
            home_score=1, away_score=0,
        )
        match.abandon_match("Unsafe pitch")
        if award:
            return award_official_result(
                match_id=match.pk, actor=self.user, home_score=3, away_score=0,
            )
        return confirm_score_at_abandonment(match_id=match.pk, actor=self.user)

    def test_awards_and_confirmations_are_read_only_and_link_to_the_decision(self):
        awarded, award = self.official_result(leg=1, award=True)
        confirmed, confirmation = self.official_result(leg=2)
        page = self.client.get(finished_list_url(self.match))
        self.assertContains(page, "Official Abandonment Decision", count=2)
        self.assertContains(page, "Review Decision", count=2)
        for protected, decision in ((awarded, award), (confirmed, confirmation)):
            self.assertNotContains(page, finished_url(protected))
            self.assertEqual(self.client.get(finished_url(protected)).status_code, 403)
            self.assertEqual(self.client.post(finished_url(protected), score_post(protected, 5, 0)).status_code, 403)
            decision.refresh_from_db()
            self.assertTrue(decision.is_current)
            self.assertFalse(Outbox.objects.filter(match=protected).exists())

    def test_finished_list_has_read_only_mobile_cards_and_dedicated_actions(self):
        self.match.gameweek = 7
        self.match.location = "Community Field"
        self.match.save(update_fields=["gameweek", "location"])
        self.match.home_team.name = "Arsenal Community Football Club"
        self.match.home_team.save(update_fields=["name"])
        page = self.client.get(finished_list_url(self.match))
        self.assertContains(page, "<article")
        self.assertContains(page, "Arsenal Community Football Club")
        self.assertContains(page, "2 &ndash; 1")
        self.assertContains(page, "Gameweek 7")
        self.assertContains(page, self.match.match_date.strftime("%Y"))
        self.assertContains(page, "Edit Result")
        self.assertContains(page, "Manage Scorers")
        self.assertNotContains(page, 'name="home_score')
        self.assertNotContains(page, "table-responsive")
        editor = self.client.get(self.url)
        self.assertContains(editor, "Edit Finished Match")
        self.assertContains(editor, "Community Field")
        self.assertContains(editor, "Save Match Corrections")

    def test_result_editor_requires_match_scoped_manager_permission(self):
        manager = get_user_model().objects.create_user(username="scoped-editor")
        membership = LeagueMembership.objects.create(
            competition=self.match.competition, user=manager, role=LeagueMembership.Role.AGENT,
        )
        self.client.force_login(manager)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, score_post(self.match, 3, 1)).status_code, 403)
        membership.role = LeagueMembership.Role.ADMIN
        membership.save(update_fields=["role"])
        self.assertEqual(self.client.get(self.url).status_code, 200)
        unrelated = finished_fixture("other-scope")
        self.assertEqual(self.client.get(finished_url(unrelated)).status_code, 403)
        wrong_fixture = reverse(
            "league_manager:edit_finished_match", args=[self.match.competition.slug, unrelated.pk],
        )
        self.assertEqual(self.client.get(wrong_fixture).status_code, 404)

    def test_outbox_failure_rolls_back_the_result_and_audit(self):
        before = table_totals(self.match)
        goal = Goal.objects.create(match=self.match, team=self.match.home_team)
        payload = score_post(self.match, 1, 1)
        payload[f"goal-{goal.pk}-remove"] = "1"
        payload["home-0-record_unknown"] = "on"
        with self.assertLogs("league_manager.finished_scorer_views", "ERROR"):
            with patch("matches.services.request_result_processing", side_effect=RuntimeError("outbox failure")):
                response = self.client.post(self.url, payload)
        self.assertContains(response, "The match corrections could not be saved.")
        goal.refresh_from_db()
        self.assertFalse(goal.canceled)
        self.assertEqual(self.match.goals.count(), 1)
        self.assertFalse(self.match.activity_logs.filter(action=ActivityLog.Action.GOAL_CANCELLED).exists())
        self.assertFalse(self.match.activity_logs.filter(action=ActivityLog.Action.GOAL_ADDED).exists())
        self.assertNotContains(response, RESULT_SAVED)
        self.match.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (2, 1))
        self.assertEqual(table_totals(self.match), before)
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())
        self.assertEqual(self.audit_count(), 0)

    def test_score_cannot_fall_below_active_goal_count_but_partial_history_is_valid(self):
        Goal.objects.create(match=self.match, team=self.match.home_team)
        Goal.objects.create(match=self.match, team=self.match.home_team)
        Goal.objects.create(match=self.match, team=self.match.away_team, canceled=True)
        response = self.client.post(self.url, score_post(self.match, 1, 1))
        self.assertContains(response, "Home has 2 recorded goals, but the proposed final score is 1.")
        self.assertContains(response, "Remove or correct 1 goal before saving.")
        self.assertEqual(self.match.goals.count(), 3)
        self.assertEqual(self.audit_count(), 0)
        self.client.post(self.url, score_post(self.match, 2, 0))
        self.match.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (2, 0))
        self.assertEqual(self.match.goals.count(), 3)

    def test_old_bulk_forms_cannot_save_finished_scores(self):
        legacy_url = reverse("update_results_for_league", args=[self.match.competition.slug]) + "?filter=finished"
        list_url = finished_list_url(self.match)
        self.assertRedirects(self.client.get(legacy_url), list_url)
        payload = {f"home_score_{self.match.pk}": 1, f"away_score_{self.match.pk}": 1}
        for url in (legacy_url, list_url):
            self.assertRedirects(self.client.post(url, payload), list_url, fetch_redirect_response=False)
        pending_url = list_url.partition("?")[0]
        self.assertEqual(self.client.post(pending_url, payload).status_code, 200)
        self.match.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (2, 1))
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())

    def test_score_and_explicit_cancellation_commit_together_and_replay_once(self):
        finalize_match_result_fast(
            self.match, home_score=3, away_score=0, regulation_home_score=3, regulation_away_score=0,
        )
        process_football_result_task.run(self.match.pk)
        scorer = Player.objects.create(team=self.match.home_team, name="Original Scorer")
        teammate = Player.objects.create(team=self.match.home_team, name="Correct Scorer")
        opponent = Player.objects.create(team=self.match.away_team, name="Opposing Player")
        remove = Goal.objects.create(match=self.match, team=self.match.home_team, player=scorer, minute=15)
        edit = Goal.objects.create(match=self.match, team=self.match.home_team, player=scorer, minute=30)
        own = Goal.objects.create(
            match=self.match, team=self.match.home_team, player=opponent, goal_type=Goal.GoalType.OWN_GOAL,
        )
        payload = score_post(self.match, 2, 0)
        payload.update({
            "version": self.client.get(self.url).context["version"],
            f"goal-{remove.pk}-remove": "1",
            f"goal-{edit.pk}-player": teammate.pk,
            f"goal-{edit.pk}-goal_type": Goal.GoalType.PENALTY,
            f"goal-{edit.pk}-minute": "31",
        })
        for _ in range(2):
            self.assertRedirects(self.client.post(self.url, payload), self.url, fetch_redirect_response=False)
        self.match.refresh_from_db()
        remove.refresh_from_db()
        edit.refresh_from_db()
        own.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score, self.match.table_home_score), (2, 0, 3))
        self.assertTrue(remove.canceled)
        self.assertEqual((remove.player_id, remove.minute), (scorer.pk, 15))
        self.assertEqual((edit.player_id, edit.goal_type, edit.minute), (teammate.pk, Goal.GoalType.PENALTY, 31))
        self.assertEqual((own.team_id, own.player_id), (self.match.home_team_id, opponent.pk))
        audit = self.match.activity_logs.get(action=ActivityLog.Action.GOAL_CANCELLED)
        self.assertEqual((audit.actor_id, audit.new_values["goal_id"]), (self.user.pk, remove.pk))
        self.assertTrue(audit.new_values["canceled"])
        self.assertEqual(self.audit_count(), 1)
        self.assertEqual(Outbox.objects.get(match=self.match).requested_revision, 2)
        detail = self.client.get(reverse("match_detail", args=[self.match.pk]))
        self.assertContains(detail, "2 - 0")
        self.assertNotContains(detail, "Original Scorer")
        rankings = self.client.get(reverse("league_stats", args=[self.match.competition.slug]))
        self.assertEqual(
            {player.pk: player.competition_total_goals for player in rankings.context["top_scorers"]},
            {teammate.pk: 1},
        )
        self.assertTrue(process_football_result_task.run(self.match.pk))

    def test_invalid_goal_edits_leave_score_and_confirmed_removal_untouched(self):
        goal = Goal.objects.create(match=self.match, team=self.match.home_team)
        opponent = Player.objects.create(team=self.match.away_team, name="Opposing Player")
        payload = score_post(self.match, 1, 0)
        payload.update({f"goal-{goal.pk}-remove": "1", "home-0-player": opponent.pk})
        self.assertContains(self.client.post(self.url, payload), "Choose a player from the team receiving the goal.")
        goal.refresh_from_db()
        self.match.refresh_from_db()
        self.assertFalse(goal.canceled)
        self.assertEqual((self.match.home_score, self.match.away_score), (2, 1))
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())

    def test_historical_goal_credited_outside_fixture_can_be_cancelled(self):
        outside = Team.objects.create(competition=self.match.competition, name="Third Club")
        goal = Goal(match=self.match, team=outside, player_name="Historical scorer", minute=8)
        Goal.objects.bulk_create([goal])
        self.assertContains(self.client.get(self.url), "Remove Third Club goal")
        payload = score_post(self.match, 2, 1)
        response = self.client.post(self.url, payload)
        self.assertContains(response, "Remove goals credited to teams outside this fixture before saving.")
        payload[f"goal-{goal.pk}-remove"] = "1"
        self.assertRedirects(self.client.post(self.url, payload), self.url, fetch_redirect_response=False)
        goal.refresh_from_db()
        self.assertTrue(goal.canceled)
        self.assertEqual(goal.player_name, "Historical scorer")
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())

    def test_score_increase_can_add_multiple_goals_or_leave_them_unrecorded(self):
        opponent = Player.objects.create(team=self.match.away_team, name="Opposing Player")
        payload = score_post(self.match, 5, 1)
        payload.update({
            "home-0-record_unknown": "on",
            "home-1-goal_type": Goal.GoalType.OWN_GOAL,
            "home-1-player": opponent.pk,
        })
        self.assertRedirects(self.client.post(self.url, payload), self.url, fetch_redirect_response=False)
        self.match.refresh_from_db()
        self.assertEqual(self.match.home_score, 5)
        self.assertEqual(self.match.goals.filter(team=self.match.home_team).count(), 2)
        own = self.match.goals.get(goal_type=Goal.GoalType.OWN_GOAL)
        self.assertEqual((own.team_id, own.player_id, opponent.recorded_goals), (self.match.home_team_id, opponent.pk, 0))

    def test_scorer_only_edit_preserves_unset_result_metadata_and_creates_no_work(self):
        Match.objects.filter(pk=self.match.pk).update(
            regulation_home_score=None, regulation_away_score=None, resolution_method="",
        )
        self.match.refresh_from_db()
        goal = Goal.objects.create(match=self.match, team=self.match.home_team)
        payload = score_post(self.match, 2, 1)
        payload[f"goal-{goal.pk}-minute"] = "12"
        self.assertRedirects(self.client.post(self.url, payload), self.url, fetch_redirect_response=False)
        goal.refresh_from_db()
        self.match.refresh_from_db()
        self.assertEqual(goal.minute, 12)
        self.assertIsNone(self.match.regulation_home_score)
        self.assertEqual(self.audit_count(), 0)
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())

    def test_knockout_correction_preserves_football_regulation_and_shootout_semantics(self):
        match = finished_fixture("knockout-corrections", format_type="KNOCKOUT")
        FootballKnockoutRoundRule.objects.create(
            stage=match.stage, number_of_legs=1,
            decider_policy=FootballKnockoutRoundRule.DeciderPolicy.EXTRA_TIME_THEN_PENALTIES,
        )
        tie = FootballKnockoutTie.objects.create(
            stage=match.stage, team_one=match.home_team, team_two=match.away_team,
        )
        match.knockout_tie = tie
        match.save(update_fields=["knockout_tie"])
        finalize_match_result_fast(
            match, home_score=3, away_score=2, regulation_home_score=2, regulation_away_score=2,
        )
        prediction = Prediction.objects.create(
            user=self.user, prediction_match=match.prediction_match,
            predicted_result="D", predicted_home_goals=2, predicted_away_goals=2,
        )
        data = score_post(
            match, 2, 2, regulation_home_score=2, regulation_away_score=2,
            penalty_home_score=5, penalty_away_score=4,
        )
        response = self.client.post(finished_url(match), data, follow=True)
        self.assertContains(response, RESULT_SAVED)
        self.assertTrue(process_football_result_task.run(match.pk))
        match.refresh_from_db()
        prediction.refresh_from_db()
        self.assertEqual((match.home_score, match.away_score, match.penalty_home_score, match.penalty_away_score), (2, 2, 5, 4))
        self.assertEqual((match.resolution_method, prediction.points_awarded), ("PENALTIES", 20))
        self.assertFalse(match.table_updated)
        self.assertFalse(Standing.objects.filter(competition=match.competition).exists())

    def test_pending_result_saves_without_inline_refresh_or_publication(self):
        Match.objects.filter(pk=self.match.pk).update(status="UPCOMING")
        pending_url = reverse("league_manager:result_update", args=[self.match.competition.slug])
        with patch("matches.result_dispatcher.publish_result_work", side_effect=ConnectionError) as publish:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(pending_url, {
                    f"home_score_{self.match.pk}": 1,
                    f"away_score_{self.match.pk}": 1,
                })
        self.assertEqual(response.status_code, 302)
        publish.assert_not_called()
        self.assertEqual(Outbox.objects.get(match=self.match).state, Outbox.State.PENDING)
        self.assertEqual(table_totals(self.match)[self.match.home_team_id][-1], 3)

    def test_repair_dry_run_apply_repeat_and_explicit_rebuild(self):
        other = finished_fixture("unrelated-repair")
        other_table = table_totals(other)
        Match.objects.filter(pk=self.match.pk).update(
            home_score=1, away_score=1, regulation_home_score=1, regulation_away_score=1,
        )
        opts = {"competition": self.match.competition.slug}
        output = StringIO()
        call_command("reconcile_football_results", stdout=output, **opts)
        self.assertIn(f"Match {self.match.pk}", output.getvalue())
        self.assertEqual(table_totals(self.match)[self.match.home_team_id][-1], 3)
        for _ in range(2):
            call_command("reconcile_football_results", apply=True, stdout=StringIO(), **opts)
            self.assertEqual(table_totals(self.match)[self.match.home_team_id], (1, 0, 1, 0, 1, 1, 0, 1))

        Match.objects.filter(pk=self.match.pk).update(table_home_score=None)
        with self.assertRaisesMessage(CommandError, "missing its applied score"):
            call_command("reconcile_football_results", apply=True, stdout=StringIO(), **opts)
        call_command("reconcile_football_results", rebuild=True, apply=True, stdout=StringIO(), **opts)
        self.match.refresh_from_db()
        self.assertEqual((self.match.table_home_score, self.match.table_away_score), (1, 1))
        self.assertEqual(table_totals(other), other_table)


class FinishedResultContentionTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update_nowait")
    def test_competing_admin_requests_receive_retry_feedback_under_real_lock(self):
        match = finished_fixture("correction-contention")
        admin = get_user_model().objects.create_user(username="concurrent-manager")
        LeagueMembership.objects.create(competition=match.competition, user=admin, role=LeagueMembership.Role.ADMIN)
        client = Client()
        client.force_login(admin)
        url = finished_url(match)
        goal = Goal.objects.create(match=match, team=match.home_team)
        payload = score_post(match, 1, 1)
        payload[f"goal-{goal.pk}-remove"] = "1"

        def submit():
            close_old_connections()
            try:
                return client.post(url, payload)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                Competition.objects.select_for_update().get(pk=match.competition_id)
                with self.assertLogs("league_manager.finished_scorer_views", "ERROR"):
                    response = executor.submit(submit).result(timeout=10)
            self.assertContains(response, "This match was updated by another administrator.")
            self.assertContains(response, f'name="goal-{goal.pk}-remove" value="1" data-remove-goal checked')
        match.refresh_from_db()
        self.assertEqual((match.home_score, match.away_score), (2, 1))
        goal.refresh_from_db()
        self.assertFalse(goal.canceled)
        self.assertEqual(client.post(url, payload).status_code, 302)
        goal.refresh_from_db()
        self.assertTrue(goal.canceled)
        process_football_result_task.run(match.pk)
        self.assertEqual(table_totals(match)[match.home_team_id], (1, 0, 1, 0, 1, 1, 0, 1))


    @skipUnlessDBFeature("has_select_for_update_nowait")
    def test_concurrent_scorer_edits_cannot_claim_the_same_last_slot(self):
        match = finished_fixture("scorer-contention")
        Goal.objects.create(match=match, team=match.home_team)
        first_player = Player.objects.create(team=match.home_team, name="First")
        second_player = Player.objects.create(team=match.home_team, name="Second")
        url = reverse("league_manager:finished_goal_scorers", args=[match.competition.slug, match.pk])
        users = get_user_model().objects
        first = users.create_superuser(username="first-scorer-admin", email="first-scorer@example.test")
        second = users.create_superuser(username="second-scorer-admin", email="second-scorer@example.test")
        self.client.force_login(first)
        rival_client = Client()
        rival_client.force_login(second)
        first_version = self.client.get(url).context["version"]
        second_version = rival_client.get(url).context["version"]
        first_data = {"version": first_version, "home-0-player": first_player.pk}
        rival_data = {"version": second_version, "home-0-player": second_player.pk}

        def submit_rival():
            close_old_connections()
            try:
                return rival_client.post(url, rival_data, follow=True)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                self.assertEqual(self.client.post(url, first_data).status_code, 302)
                with self.assertLogs("league_manager.finished_scorer_views", "ERROR"):
                    rival_response = executor.submit(submit_rival).result(timeout=10)
                self.assertContains(rival_response, "This match was updated by another administrator.")
        self.assertContains(
            rival_client.post(url, rival_data, follow=True),
            "This match was updated by another administrator.",
        )
        self.assertEqual(match.goals.filter(team=match.home_team).count(), 2)
        self.assertEqual(match.goals.get(player__isnull=False).player_id, first_player.pk)
        self.assertEqual(match.activity_logs.filter(action=ActivityLog.Action.GOAL_ADDED).count(), 1)
