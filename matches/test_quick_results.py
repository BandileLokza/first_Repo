from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from league_manager.models import LeagueMembership
from leagues.models import Competition, Stage, Standing
from matches.models import Goal, Match, MatchAdminAssignment, Player
from matches import views as match_views
from matches.models import MatchResultProcessingOutbox as Outbox
from matches.tasks import process_football_result_task
from league_manager import views as manager_views
from teams.models import Team


class QuickResultTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("quick-agent", password="pass")
        self.competition = Competition.objects.create(
            name="Quick results", slug="quick-results", season="2026", format_type="TABLE"
        )
        self.stage = Stage.objects.create(
            competition=self.competition, name="League", stage_type="LEAGUE", order=1
        )
        self.home = Team.objects.create(competition=self.competition, name="Home")
        self.away = Team.objects.create(competition=self.competition, name="Away")
        self.match = Match.objects.create(
            competition=self.competition, stage=self.stage, home_team=self.home,
            away_team=self.away, match_date=timezone.now(), gameweek=2,
        )
        LeagueMembership.objects.create(
            user=self.user, competition=self.competition, role=LeagueMembership.Role.AGENT
        )
        MatchAdminAssignment.objects.create(match=self.match, admin=self.user)
        self.home_player = Player.objects.create(team=self.home, name="Home scorer")
        self.away_player = Player.objects.create(team=self.away, name="Away defender")
        self.client.force_login(self.user)

    def test_quick_result_uses_the_same_manager_finalizer(self):
        self.assertIs(
            match_views.finalize_match_result_fast,
            manager_views.finalize_match_result_fast,
        )

    def test_agent_finalizes_scheduled_match_and_queues_normal_background_refresh(self):
        response = self.client.post(reverse("save_quick_result", args=[self.match.id]), {
            "home_score": 3, "away_score": 1,
        })
        self.assertEqual(Outbox.objects.get(match=self.match).requested_revision, 1)
        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]) + "?quick-goals=1")
        self.match.refresh_from_db()
        self.assertEqual((self.match.status, self.match.home_score, self.match.away_score), ("FINISHED", 3, 1))
        # TABLE teams receive zero-value standings when they are created.
        # Finalisation leaves those rows untouched until the worker runs.
        self.assertEqual(
            list(
                Standing.objects.filter(competition=self.competition)
                .order_by("team_id")
                .values_list(
                    "played", "won", "drawn", "lost", "goals_for",
                    "goals_against", "points",
                )
            ),
            [(0, 0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0, 0)],
        )
        process_football_result_task.run(self.match.pk)
        home = Standing.objects.get(competition=self.competition, team=self.home)
        away = Standing.objects.get(competition=self.competition, team=self.away)
        self.assertEqual((home.played, home.won, home.goals_for, home.goals_against, home.points), (1, 1, 3, 1, 3))
        self.assertEqual((away.played, away.lost, away.goals_for, away.goals_against), (1, 1, 1, 3))

        # The deployed worker can safely retry or later re-run this task.
        process_football_result_task.run(self.match.pk)
        home.refresh_from_db()
        self.assertEqual((home.played, home.won, home.goals_for, home.goals_against, home.points), (1, 1, 3, 1, 3))

    def test_quick_result_locks_only_the_match_row(self):
        with patch.object(Match.objects, "select_for_update", wraps=Match.objects.select_for_update) as lock:
            response = self.client.post(
                reverse("save_quick_result", args=[self.match.id]),
                {"home_score": 1, "away_score": 0},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(lock.called)
        self.assertTrue(all(call.kwargs["of"] == ("self",) for call in lock.call_args_list))


    def test_goal_details_allow_repeated_and_unknown_scorers_without_minutes(self):
        self.client.post(reverse("save_quick_result", args=[self.match.id]), {
            "home_score": 2, "away_score": 1,
        })
        response = self.client.post(reverse("save_quick_goal_details", args=[self.match.id]), {
            "home-player-0": self.home_player.id, "home-goal-type-0": "NORMAL",
            "home-player-1": self.home_player.id, "home-goal-type-1": "NORMAL",
            "away-player-0": self.home_player.id, "away-goal-type-0": "OWN_GOAL",
        })
        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        goals = Goal.objects.filter(match=self.match, canceled=False)
        self.assertEqual(goals.count(), 3)
        self.assertEqual(
            goals.filter(player=self.home_player, goal_type="NORMAL").count(),
            2,
        )
        self.assertTrue(goals.filter(player=self.home_player, minute__isnull=True, goal_type="OWN_GOAL").exists())
        self.assertEqual(self.home_player.recorded_goals, 2)

    def test_unknown_own_goal_is_saved_without_a_fake_player(self):
        self.client.post(reverse("save_quick_result", args=[self.match.id]), {
            "home_score": 1, "away_score": 0,
        })
        self.client.post(reverse("save_quick_goal_details", args=[self.match.id]), {
            "home-player-0": "", "home-goal-type-0": "OWN_GOAL",
        })
        goal = Goal.objects.get(match=self.match)
        self.assertIsNone(goal.player)
        self.assertEqual(goal.player_name, "")
        self.assertIsNone(goal.minute)

    def test_goal_details_create_and_reuse_a_new_team_player(self):
        self.client.post(reverse("save_quick_result", args=[self.match.id]), {
            "home_score": 2, "away_score": 0,
        })
        response = self.client.post(reverse("save_quick_goal_details", args=[self.match.id]), {
            "home-player-0": "__new__", "home-new-player-0": "  Sabelo  Nkambule ",
            "home-goal-type-0": "NORMAL",
            "home-player-1": "__new__", "home-new-player-1": "sabelo nkambule",
            "home-goal-type-1": "NORMAL",
        })
        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        player = Player.objects.get(team=self.home, name="Sabelo Nkambule")
        self.assertEqual(Goal.objects.filter(match=self.match, player=player).count(), 2)
        self.assertEqual(player.recorded_goals, 2)
        self.assertEqual(Player.objects.filter(team=self.home, name__iexact="sabelo nkambule").count(), 1)

    def test_goal_details_reject_empty_new_player_name(self):
        self.client.post(reverse("save_quick_result", args=[self.match.id]), {
            "home_score": 1, "away_score": 0,
        })
        response = self.client.post(reverse("save_quick_goal_details", args=[self.match.id]), {
            "home-player-0": "__new__", "home-new-player-0": "",
            "home-goal-type-0": "NORMAL",
        })
        self.assertRedirects(
            response, reverse("match_detail", args=[self.match.id]) + "?quick-goals=1"
        )
        self.assertFalse(Goal.objects.filter(match=self.match).exists())

    def test_existing_goal_conflict_is_rejected(self):
        self.match.start_first_half()
        Goal.objects.create(match=self.match, team=self.home, player=self.home_player)
        self.match.home_score = 1
        self.match.save(update_fields=["home_score"])
        response = self.client.post(reverse("save_quick_result", args=[self.match.id]), {
            "home_score": 2, "away_score": 0,
        })
        self.match.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.match.status, "LIVE")
        self.assertEqual(Goal.objects.filter(match=self.match).count(), 1)
        self.assertFalse(Outbox.objects.filter(match=self.match).exists())

    def test_unauthorised_and_abandoned_matches_cannot_be_quick_finalised(self):
        other = get_user_model().objects.create_user("quick-other", password="pass")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("save_quick_result", args=[self.match.id]), {
            "home_score": 1, "away_score": 0,
        }).status_code, 403)

        self.client.force_login(self.user)
        self.match.abandon_match("Weather")
        self.assertEqual(self.client.post(reverse("save_quick_result", args=[self.match.id]), {
            "home_score": 1, "away_score": 0,
        }).status_code, 403)
