"""Focused regression coverage for normal, penalty, and own match goals."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from allauth.socialaccount.models import SocialApp

from league_manager.models import ActivityLog, LeagueMembership
from leagues.models import Competition, Stage
from matches.models import Goal, Match, Player
from teams.models import Team


User = get_user_model()


class GoalTypeTests(TestCase):
    """Goal-type behavior must remain distinct from penalty shootout kicks."""

    def setUp(self):
        social_app = SocialApp.objects.create(
            provider="google",
            name="Goal type test Google",
            client_id="goal-type-test-client",
            secret="goal-type-test-secret",
        )
        social_app.sites.add(Site.objects.get_current())

        self.manager = User.objects.create_user(
            username="goal-type-manager",
            password="pass",
        )
        self.competition = Competition.objects.create(
            name="Goal Type Test League",
            slug="goal-type-test-league",
            season="2026",
        )
        LeagueMembership.objects.create(
            user=self.manager,
            competition=self.competition,
            role=LeagueMembership.Role.ADMIN,
        )
        self.stage = Stage.objects.create(
            competition=self.competition,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        self.home_team = Team.objects.create(
            name="Goal Type Home",
            competition=self.competition,
        )
        self.away_team = Team.objects.create(
            name="Goal Type Away",
            competition=self.competition,
        )
        self.match = Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            match_date=timezone.now(),
        )
        self.home_player = Player.objects.create(
            name="Home Scorer",
            team=self.home_team,
        )
        self.away_player = Player.objects.create(
            name="Away Defender",
            team=self.away_team,
        )
        self.client.force_login(self.manager)

    def make_match_live(self, *, phase="FIRST_HALF"):
        self.match.status = "LIVE"
        self.match.phase = phase
        self.match.phase_start_time = (
            None if phase == "PENALTIES" else timezone.now()
        )
        self.match.save(
            update_fields=["status", "phase", "phase_start_time"],
        )
        self.match.refresh_from_db()
        return self.match

    def add_goal(self, benefiting_team, **data):
        return self.client.post(
            reverse("add_goal", args=[self.match.id, benefiting_team.id]),
            data,
        )

    def test_goal_type_defaults_to_normal_and_normal_and_penalty_count_for_scorer(self):
        self.make_match_live()

        normal_goal = Goal.objects.create(
            match=self.match,
            team=self.home_team,
            player=self.home_player,
        )
        penalty_goal = Goal.objects.create(
            match=self.match,
            team=self.home_team,
            player=self.home_player,
            goal_type=Goal.GoalType.PENALTY,
        )

        self.assertEqual(normal_goal.goal_type, Goal.GoalType.NORMAL)
        self.assertEqual(penalty_goal.goal_type, Goal.GoalType.PENALTY)
        self.assertEqual(self.home_player.recorded_goals, 2)

    def test_normal_and_penalty_posts_credit_the_benefiting_team_and_log_the_type(self):
        self.make_match_live()

        normal_response = self.add_goal(
            self.home_team,
            player=self.home_player.id,
            goal_type=Goal.GoalType.NORMAL,
        )
        penalty_response = self.add_goal(
            self.home_team,
            player=self.home_player.id,
            goal_type=Goal.GoalType.PENALTY,
        )

        self.assertRedirects(
            normal_response,
            reverse("match_detail", args=[self.match.id]),
        )
        self.assertRedirects(
            penalty_response,
            reverse("match_detail", args=[self.match.id]),
        )
        self.match.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (2, 0))
        self.assertEqual(self.home_player.recorded_goals, 2)

        activities = list(
            ActivityLog.objects.filter(
                match=self.match,
                action=ActivityLog.Action.GOAL_ADDED,
            ).order_by("created_at", "id")
        )
        self.assertEqual(
            [activity.new_values["goal_type"] for activity in activities],
            [Goal.GoalType.NORMAL, Goal.GoalType.PENALTY],
        )
        self.assertIn("Penalty Goal", activities[-1].description)

    def test_own_goal_credits_benefiting_team_but_excludes_opposing_player_scorer_stats(self):
        self.make_match_live()

        response = self.add_goal(
            self.home_team,
            player=self.away_player.id,
            goal_type=Goal.GoalType.OWN_GOAL,
        )

        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        self.match.refresh_from_db()
        goal = Goal.objects.get(match=self.match)
        self.assertEqual(goal.team_id, self.home_team.id)
        self.assertEqual(goal.player_id, self.away_player.id)
        self.assertEqual(goal.goal_type, Goal.GoalType.OWN_GOAL)
        self.assertEqual((self.match.home_score, self.match.away_score), (1, 0))
        self.assertEqual(self.away_player.recorded_goals, 0)

        activity = ActivityLog.objects.get(
            match=self.match,
            action=ActivityLog.Action.GOAL_ADDED,
        )
        self.assertEqual(activity.new_values["goal_type"], Goal.GoalType.OWN_GOAL)
        self.assertIn("Own Goal", activity.description)

    def test_own_goal_rejects_a_player_from_the_benefiting_team(self):
        self.make_match_live()

        with self.assertRaises(ValidationError):
            Goal.objects.create(
                match=self.match,
                team=self.home_team,
                player=self.home_player,
                goal_type=Goal.GoalType.OWN_GOAL,
            )

        response = self.add_goal(
            self.home_team,
            player=self.home_player.id,
            goal_type=Goal.GoalType.OWN_GOAL,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "For an own goal, choose a player from the opposing team.",
        )
        self.assertFalse(Goal.objects.exists())

    def test_normal_and_penalty_reject_a_player_from_the_opposing_team(self):
        self.make_match_live()

        for goal_type in (Goal.GoalType.NORMAL, Goal.GoalType.PENALTY):
            with self.subTest(goal_type=goal_type):
                with self.assertRaises(ValidationError):
                    Goal.objects.create(
                        match=self.match,
                        team=self.home_team,
                        player=self.away_player,
                        goal_type=goal_type,
                    )

    def test_typed_own_goal_player_is_created_for_the_opposing_team(self):
        self.make_match_live()

        response = self.add_goal(
            self.home_team,
            goal_type=Goal.GoalType.OWN_GOAL,
            player_name="Typed Away Defender",
        )

        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        goal = Goal.objects.get(match=self.match)
        self.assertEqual(goal.team_id, self.home_team.id)
        self.assertEqual(goal.player.team_id, self.away_team.id)
        self.assertEqual(goal.player.name, "Typed Away Defender")
        self.assertEqual(goal.goal_type, Goal.GoalType.OWN_GOAL)
        self.match.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (1, 0))
        self.assertEqual(goal.player.recorded_goals, 0)

    def test_typed_home_player_for_an_away_own_goal_keeps_home_registration(self):
        self.make_match_live()

        response = self.add_goal(
            self.away_team,
            goal_type=Goal.GoalType.OWN_GOAL,
            player_name="Typed Home Defender",
        )

        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        goal = Goal.objects.get(match=self.match)
        self.assertEqual(goal.team_id, self.away_team.id)
        self.assertEqual(goal.player.team_id, self.home_team.id)
        self.match.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (0, 1))
        self.assertEqual(goal.player.recorded_goals, 0)

    def test_new_live_scorer_is_registered_once_and_credited_normally(self):
        self.make_match_live()

        for name in ("  Sabelo   Nkambule ", "sabelo nkambule"):
            response = self.add_goal(
                self.home_team,
                goal_type=Goal.GoalType.NORMAL,
                player_name=name,
            )
            self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))

        player = Player.objects.get(team=self.home_team, name="Sabelo Nkambule")
        self.assertEqual(Goal.objects.filter(match=self.match, player=player).count(), 2)
        self.assertEqual(player.recorded_goals, 2)
        self.assertEqual(Player.objects.filter(team=self.home_team, name__iexact="sabelo nkambule").count(), 1)

    def test_live_goals_allow_a_manual_minute_without_rewinding_the_clock(self):
        recorded_at = timezone.now().replace(second=0, microsecond=0)
        self.match.status = "LIVE"
        self.match.phase = "FIRST_HALF"
        self.match.phase_start_time = recorded_at - timedelta(minutes=20)
        self.match.save(update_fields=["status", "phase", "phase_start_time"])

        with patch("matches.models.timezone.now", return_value=recorded_at):
            late_goal = self.add_goal(
                self.home_team,
                player=self.home_player.id,
                goal_type=Goal.GoalType.NORMAL,
                minute="20",
            )
            earlier_goal = self.add_goal(
                self.home_team,
                player=self.home_player.id,
                goal_type=Goal.GoalType.NORMAL,
                minute="8",
            )
            automatic_goal = self.add_goal(
                self.home_team,
                player=self.home_player.id,
                goal_type=Goal.GoalType.NORMAL,
            )

            self.assertEqual(self.match.get_current_minute(), 20)

        self.assertEqual(late_goal.status_code, 302)
        self.assertEqual(earlier_goal.status_code, 302)
        self.assertEqual(automatic_goal.status_code, 302)
        self.assertEqual(
            list(Goal.objects.filter(match=self.match).order_by("minute", "id").values_list("minute", flat=True)),
            [8, 20, 20],
        )
        self.match.refresh_from_db()
        self.assertEqual((self.match.home_score, self.match.away_score), (3, 0))

        response = self.client.get(reverse("match_detail", args=[self.match.id]))
        self.assertLess(
            response.content.find(b" 8'"),
            response.content.find(b" 20'"),
        )

    def test_goal_minute_is_optional_but_validated_and_zero_is_preserved(self):
        self.make_match_live()

        response = self.add_goal(
            self.home_team,
            player=self.home_player.id,
            goal_type=Goal.GoalType.NORMAL,
            minute="0",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Goal.objects.get(match=self.match).minute, 0)

        for minute in ("-1", "not-a-minute", "1"):
            with self.subTest(minute=minute):
                response = self.add_goal(
                    self.home_team,
                    player=self.home_player.id,
                    goal_type=Goal.GoalType.NORMAL,
                    minute=minute,
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(Goal.objects.filter(match=self.match).count(), 1)

    def test_goal_correction_can_update_the_recorded_minute(self):
        self.make_match_live()
        self.match.phase_start_time = timezone.now() - timedelta(minutes=20)
        self.match.save(update_fields=["phase_start_time"])
        self.add_goal(
            self.home_team,
            player=self.home_player.id,
            goal_type=Goal.GoalType.NORMAL,
            minute="20",
        )
        goal = Goal.objects.get(match=self.match)

        response = self.client.post(
            reverse("edit_goal", args=[goal.id]),
            {
                "player": self.home_player.id,
                "goal_type": Goal.GoalType.NORMAL,
                "minute": "8",
            },
        )

        self.assertEqual(response.status_code, 302)
        goal.refresh_from_db()
        self.assertEqual(goal.minute, 8)

    def test_live_goal_rejects_forged_opposing_player(self):
        self.make_match_live()
        response = self.add_goal(
            self.home_team,
            goal_type=Goal.GoalType.NORMAL,
            player=self.away_player.id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a player from the team receiving the goal.")
        self.assertFalse(Goal.objects.exists())

    def test_failed_live_goal_does_not_keep_new_player(self):
        self.make_match_live()
        with patch("matches.views.Goal.objects.create", side_effect=ValidationError("No goal")):
            with self.assertRaises(ValidationError):
                self.add_goal(
                    self.home_team,
                    goal_type=Goal.GoalType.NORMAL,
                    player_name="Atomic scorer",
                )
        self.assertFalse(Player.objects.filter(name="Atomic scorer").exists())

    def test_editing_a_goal_to_an_own_goal_preserves_the_score_and_updates_the_audit_log(self):
        self.make_match_live()
        self.add_goal(
            self.home_team,
            player=self.home_player.id,
            goal_type=Goal.GoalType.NORMAL,
        )
        goal = Goal.objects.get(match=self.match)

        response = self.client.post(
            reverse("edit_goal", args=[goal.id]),
            {
                "player": self.away_player.id,
                "goal_type": Goal.GoalType.OWN_GOAL,
            },
        )

        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        goal.refresh_from_db()
        self.match.refresh_from_db()
        self.assertEqual(goal.goal_type, Goal.GoalType.OWN_GOAL)
        self.assertEqual(goal.player_id, self.away_player.id)
        self.assertEqual((self.match.home_score, self.match.away_score), (1, 0))
        self.assertEqual(self.home_player.recorded_goals, 0)
        self.assertEqual(self.away_player.recorded_goals, 0)

        activity = ActivityLog.objects.get(
            match=self.match,
            action=ActivityLog.Action.GOAL_EDITED,
        )
        self.assertEqual(activity.old_values["goal_type"], Goal.GoalType.NORMAL)
        self.assertEqual(activity.new_values["goal_type"], Goal.GoalType.OWN_GOAL)

    def test_canceling_an_own_goal_reverses_the_benefiting_team_score(self):
        self.make_match_live()
        self.add_goal(
            self.home_team,
            player=self.away_player.id,
            goal_type=Goal.GoalType.OWN_GOAL,
        )
        goal = Goal.objects.get(match=self.match)

        response = self.client.post(reverse("cancel_goal", args=[goal.id]))

        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        goal.refresh_from_db()
        self.match.refresh_from_db()
        self.assertTrue(goal.canceled)
        self.assertEqual((self.match.home_score, self.match.away_score), (0, 0))
        self.assertEqual(self.away_player.recorded_goals, 0)

        activity = ActivityLog.objects.get(
            match=self.match,
            action=ActivityLog.Action.GOAL_CANCELLED,
        )
        self.assertEqual(activity.new_values["goal_type"], Goal.GoalType.OWN_GOAL)

    def test_match_detail_marks_penalty_and_own_goals_without_marking_normal_goals(self):
        self.make_match_live()
        Goal.objects.create(
            match=self.match,
            team=self.home_team,
            player=self.home_player,
            goal_type=Goal.GoalType.NORMAL,
        )
        Goal.objects.create(
            match=self.match,
            team=self.home_team,
            player=self.home_player,
            goal_type=Goal.GoalType.PENALTY,
        )
        Goal.objects.create(
            match=self.match,
            team=self.home_team,
            player=self.away_player,
            goal_type=Goal.GoalType.OWN_GOAL,
        )

        response = self.client.get(reverse("match_detail", args=[self.match.id]))

        self.assertContains(response, "(P)")
        self.assertContains(response, "(OG)")
        self.assertEqual(response.content.count(b"goal-type-marker"), 2)

    def test_shootout_state_rejects_goal_mutations_without_touching_penalty_scores(self):
        self.make_match_live()
        self.add_goal(
            self.home_team,
            player=self.home_player.id,
            goal_type=Goal.GoalType.NORMAL,
        )
        goal = Goal.objects.get(match=self.match)

        self.match.phase = "PENALTIES"
        self.match.phase_start_time = None
        self.match.penalty_home_score = 5
        self.match.penalty_away_score = 4
        self.match.save(
            update_fields=[
                "phase",
                "phase_start_time",
                "penalty_home_score",
                "penalty_away_score",
            ],
        )

        add_response = self.add_goal(
            self.home_team,
            player=self.home_player.id,
            goal_type=Goal.GoalType.PENALTY,
        )
        edit_response = self.client.post(
            reverse("edit_goal", args=[goal.id]),
            {
                "player": self.home_player.id,
                "goal_type": Goal.GoalType.PENALTY,
            },
        )
        cancel_response = self.client.post(reverse("cancel_goal", args=[goal.id]))

        self.assertEqual(add_response.status_code, 400)
        self.assertEqual(edit_response.status_code, 400)
        self.assertEqual(cancel_response.status_code, 400)
        goal.refresh_from_db()
        self.match.refresh_from_db()
        self.assertFalse(goal.canceled)
        self.assertEqual(goal.goal_type, Goal.GoalType.NORMAL)
        self.assertEqual(
            (self.match.penalty_home_score, self.match.penalty_away_score),
            (5, 4),
        )
        self.assertEqual(Goal.objects.filter(match=self.match).count(), 1)
