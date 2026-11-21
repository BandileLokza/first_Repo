from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import ActivityLog, LeagueMembership
from leagues.models import Competition, Stage, Standing
from matches.models import Goal, Match, MatchResultProcessingOutbox, Player
from matches.services import apply_match_to_standings_incrementally
from teams.models import Team


class FinishedGoalScorerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.admin = users.create_user(username="scorer-manager")
        cls.staff = users.create_user(username="scorer-staff", is_staff=True)
        cls.league = Competition.objects.create(name="Goal League", slug="goal-league", season="2026")
        LeagueMembership.objects.create(
            competition=cls.league, user=cls.admin, role=LeagueMembership.Role.ADMIN,
        )
        cls.stage = Stage.objects.create(
            competition=cls.league, name="League", stage_type="LEAGUE", order=1,
        )
        cls.home = Team.objects.create(competition=cls.league, name="Home")
        cls.away = Team.objects.create(competition=cls.league, name="Away")
        cls.player_a = Player.objects.create(team=cls.home, name="Original Scorer", starting_goals=3)
        cls.player_b = Player.objects.create(team=cls.home, name="Correct Scorer")
        cls.away_player = Player.objects.create(team=cls.away, name="Away Scorer")
        cls.match = Match.objects.create(
            competition=cls.league, stage=cls.stage, home_team=cls.home, away_team=cls.away,
            status="FINISHED", match_date=timezone.now(), home_score=3, away_score=1,
            regulation_home_score=3, regulation_away_score=1, resolution_method="REGULATION",
        )
        apply_match_to_standings_incrementally(cls.match)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("league_manager:finished_goal_scorers", args=[self.league.slug, self.match.pk])
        self.result_url = reverse("league_manager:result_update", args=[self.league.slug]) + "?tab=finished"

    def editor(self):
        return self.client.get(self.url)

    def goal(self, *, team=None, player=None, **details):
        return Goal.objects.create(match=self.match, team=team or self.home, player=player, **details)

    def rows(self, response):
        return [[row["form"].prefix for row in section["rows"]] for section in response.context["sections"]]

    def state(self):
        return (
            Match.objects.filter(pk=self.match.pk).values().get(),
            list(Standing.objects.filter(competition=self.league).order_by("pk").values()),
        )

    @staticmethod
    def form_row(prefix, *, player="", goal_type="NORMAL", minute="", name="", record_unknown=False):
        fields = {"player": player, "goal_type": goal_type, "minute": minute, "player_name": name}
        if record_unknown:
            fields["record_unknown"] = "on"
        return {f"{prefix}-{field}": value for field, value in fields.items()}

    def post(self, *rows, version=None, follow=False):
        payload = {"version": version if version is not None else self.editor().context["version"]}
        for row in rows:
            payload.update(row)
        return self.client.post(self.url, payload, follow=follow)

    def test_score_only_editor_shows_slots_and_saves_only_explicit_changes(self):
        before = self.state()
        page = self.editor()
        self.assertContains(self.client.get(self.result_url), "Manage Scorers")
        self.assertContains(page, "Manage goal scorers without changing the final result.")
        self.assertEqual(self.rows(page), [["home-0", "home-1", "home-2"], ["away-0"]])
        self.assertFalse(self.match.goals.exists())
        response = self.post(
            self.form_row("home-0", player=self.player_a.pk, minute="0"),
            self.form_row("home-1"),
            self.form_row("home-2"),
            self.form_row("away-0"),
            version=page.context["version"], follow=True,
        )
        self.assertContains(response, "Goal scorer information updated successfully.")
        goal = self.match.goals.get()
        self.assertEqual((goal.player_id, goal.minute), (self.player_a.pk, 0))
        self.assertEqual(self.state(), before)
        self.assertFalse(MatchResultProcessingOutbox.objects.filter(match=self.match).exists())

    def test_partial_history_counts_existing_and_unknown_rows_by_credited_side(self):
        known = self.goal(player=self.player_a, minute=24)
        unknown = self.goal(team=self.away, player_name="Unknown Player", minute=88)
        canceled = self.goal(player=self.player_b, canceled=True, minute=41)
        page = self.editor()
        self.assertEqual(self.rows(page), [[f"goal-{known.pk}", "home-0", "home-1"], [f"goal-{unknown.pk}"]])
        self.assertContains(page, "Recorded scorer")
        self.assertContains(page, "Recorded goal")
        self.assertEqual(self.post(
            self.form_row(f"goal-{known.pk}", player=self.player_a.pk, minute=24),
            self.form_row(f"goal-{unknown.pk}", player=self.away_player.pk, minute=88),
            self.form_row("home-0", player=self.player_a.pk),
            version=page.context["version"],
        ).status_code, 302)
        unknown.refresh_from_db()
        canceled.refresh_from_db()
        self.assertEqual((unknown.player_id, unknown.player_name, unknown.minute), (self.away_player.pk, self.away_player.name, 88))
        self.assertTrue(canceled.canceled)
        self.assertEqual(self.match.goals.filter(canceled=False, player=self.player_a).count(), 2)
        self.assertEqual(self.match.goals.count(), 4)

    def test_correction_changes_public_match_top_scorers_and_squad_totals(self):
        before = self.state()
        goal = self.goal(player=self.player_a, goal_type=Goal.GoalType.PENALTY, minute=95)
        other_league = Competition.objects.create(name="Other", slug="other-scorers", season="2026")
        other_team = Team.objects.create(competition=other_league, name="Home")
        other_player = Player.objects.create(team=other_team, name=self.player_a.name, starting_goals=5)
        self.assertEqual(self.player_a.total_goals, 4)
        response = self.post({f"goal-{goal.pk}-player": self.player_b.pk})
        self.assertEqual(response.status_code, 302)
        goal.refresh_from_db()
        self.assertEqual((goal.player_id, goal.player_name, goal.minute), (self.player_b.pk, self.player_b.name, 95))
        self.player_a.refresh_from_db()
        self.assertEqual((self.player_a.total_goals, self.player_a.starting_goals, self.player_b.total_goals), (3, 3, 1))
        self.assertEqual(other_player.total_goals, 5)
        detail = self.client.get(reverse("match_detail", args=[self.match.pk]))
        self.assertContains(detail, "Correct Scorer 95'")
        self.assertContains(detail, "(P)")
        self.assertNotContains(detail, "Original Scorer")
        for url_name in ("league_detail", "league_stats"):
            response = self.client.get(reverse(url_name, args=[self.league.slug]))
            self.assertEqual(
                {player.pk: player.competition_total_goals for player in response.context["top_scorers"]},
                {self.player_a.pk: 3, self.player_b.pk: 1},
            )
        response = self.client.get(reverse("team_profile", args=[self.league.slug, self.home.pk]))
        self.assertEqual(
            {player.pk: player.total_goals for player in response.context["squad"]},
            {self.player_a.pk: 3, self.player_b.pk: 1},
        )
        audit = self.match.activity_logs.get(action=ActivityLog.Action.GOAL_EDITED)
        self.assertEqual((audit.old_values["player_id"], audit.new_values["player_id"]), (self.player_a.pk, self.player_b.pk))
        self.assertEqual((audit.new_values["goal_id"], audit.old_values["minute"]), (goal.pk, 95))
        self.assertEqual(self.state(), before)

    def test_add_new_player_normalizes_name_and_own_goals_use_opposition(self):
        response = self.post(
            self.form_row("home-0", player="__new__", name="  New   Player "),
            self.form_row("home-1", player="__new__", name="new player", goal_type="PENALTY"),
            self.form_row("home-2", goal_type="OWN_GOAL", player=self.away_player.pk, minute=16),
            self.form_row("away-0", goal_type="OWN_GOAL"),
        )
        self.assertEqual(response.status_code, 302)
        player = Player.objects.get(team=self.home, name="New Player")
        self.assertEqual(player.recorded_goals, 2)
        self.assertEqual(self.match.goals.filter(player=player, minute__isnull=True).count(), 2)
        own_goal = self.match.goals.get(team=self.home, goal_type="OWN_GOAL")
        self.assertEqual(own_goal.player_id, self.away_player.pk)
        self.assertEqual(self.away_player.recorded_goals, 0)
        self.assertIsNone(self.match.goals.get(team=self.away, goal_type="OWN_GOAL").player_id)
        detail = self.client.get(reverse("match_detail", args=[self.match.pk]))
        self.assertContains(detail, "(OG)", count=2)
        self.assertContains(detail, "Scorer not recorded")

    def test_invalid_roster_minute_or_unlisted_goal_rolls_back_entire_form(self):
        for row in (
            self.form_row("home-1", player=self.away_player.pk),
            self.form_row("home-1", player=self.player_a.pk, goal_type="OWN_GOAL"),
            self.form_row("home-1", minute=-1),
            self.form_row("home-1", player="__new__"),
            self.form_row("goal-999999", player=self.player_a.pk),
            {"home_score": 4},
        ):
            with self.subTest(row=row):
                response = self.post(self.form_row("home-0", player=self.player_a.pk), row)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(self.match.goals.exists())

    def test_named_legacy_snapshot_and_explicit_unknown_goal_remain_distinct(self):
        existing = self.goal(player_name="Historical Name", minute=12)
        response = self.post(
            {f"goal-{existing.pk}-minute": "13"},
            self.form_row("home-0", record_unknown=True),
        )
        self.assertEqual(response.status_code, 302)
        existing.refresh_from_db()
        self.assertEqual((existing.player_id, existing.player_name, existing.minute), (None, "Historical Name", 13))
        unknown = self.match.goals.exclude(pk=existing.pk).get()
        self.assertEqual((unknown.player_id, unknown.player_name, unknown.minute), (None, "", None))

    def test_repeat_submission_is_idempotent_and_stale_editor_cannot_overwrite(self):
        first = self.editor().context["version"]
        second = self.editor().context["version"]
        request = self.form_row("home-0", player=self.player_a.pk)
        for _ in range(2):
            self.assertEqual(self.post(request, version=first).status_code, 302)
        response = self.post(self.form_row("home-0", player=self.player_b.pk), version=second, follow=True)
        self.assertContains(response, "This match was updated by another administrator.")
        self.assertEqual(self.match.goals.get().player_id, self.player_a.pk)
        self.assertEqual(self.match.activity_logs.filter(action=ActivityLog.Action.GOAL_ADDED).count(), 1)

    def test_new_player_and_goals_roll_back_if_audit_fails(self):
        with self.assertLogs("league_manager.finished_scorer_views", "ERROR"):
            with patch("matches.finished_scorers.record_activity", side_effect=RuntimeError("audit failed")):
                response = self.post(self.form_row("home-0", player="__new__", name="Rollback Scorer"))
        self.assertContains(response, "Please try again.")
        self.assertFalse(Player.objects.filter(name="Rollback Scorer").exists())
        self.assertFalse(self.match.goals.exists())

    def test_over_recorded_side_can_edit_but_cannot_add_and_preserves_history(self):
        goal = self.goal(player=self.player_a, minute=17)
        Match.objects.filter(pk=self.match.pk).update(home_score=0, regulation_home_score=0)
        page = self.editor()
        self.assertContains(page, "This match has more recorded goals than the final score allows.")
        self.assertEqual(self.rows(page), [[f"goal-{goal.pk}"], ["away-0"]])
        response = self.post(self.form_row("home-0", player=self.player_b.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post(
            self.form_row(f"goal-{goal.pk}", player=self.player_b.pk, minute=17),
        ).status_code, 302)
        goal.refresh_from_db()
        self.assertEqual((goal.player_id, self.match.goals.count()), (self.player_b.pk, 1))

    def test_permission_scope_and_protected_fixtures(self):
        self.client.logout()
        self.assertEqual(self.editor().status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.editor().status_code, 403)
        LeagueMembership.objects.create(
            competition=self.league, user=self.staff, role=LeagueMembership.Role.AGENT,
        )
        self.assertEqual(self.editor().status_code, 403)
        self.client.force_login(self.admin)
        unrelated = Competition.objects.create(name="Unrelated", slug="unrelated-scorers", season="2026")
        LeagueMembership.objects.create(
            competition=unrelated, user=self.admin, role=LeagueMembership.Role.OWNER,
        )
        self.assertEqual(self.client.get(reverse(
            "league_manager:finished_goal_scorers", args=[unrelated.slug, self.match.pk],
        )).status_code, 404)
        version = self.editor().context["version"]
        for status, resolution in (("ABANDONED", ""), ("FINISHED", "AWARDED"), ("FINISHED", "CONFIRMED")):
            with self.subTest(status=status, resolution=resolution):
                Match.objects.filter(pk=self.match.pk).update(status=status, abandonment_resolution=resolution)
                self.assertEqual(self.editor().status_code, 403)
                self.assertEqual(self.post(self.form_row("home-0", player=self.player_a.pk), version=version).status_code, 403)
                self.assertNotContains(self.client.get(self.result_url), "Manage Scorers")
        Match.objects.filter(pk=self.match.pk).update(status="FINISHED", abandonment_resolution="")
        Competition.objects.filter(pk=self.league.pk).update(is_archived=True)
        self.assertEqual(self.editor().status_code, 403)
        self.assertNotContains(self.client.get(self.result_url), "Manage Scorers")
        self.assertFalse(self.match.goals.exists())

    def test_knockout_scorers_use_football_score_and_leave_result_fields_alone(self):
        stage = Stage.objects.create(
            competition=self.league, name="Knockout", stage_type="KNOCKOUT", order=2,
        )
        Match.objects.filter(pk=self.match.pk).update(
            stage=stage, home_score=1, away_score=1, regulation_home_score=0,
            regulation_away_score=0, penalty_home_score=5, penalty_away_score=4,
            resolution_method="PENALTIES",
        )
        before = self.state()
        page = self.editor()
        self.assertEqual(self.rows(page), [["home-0"], ["away-0"]])
        with patch("matches.tasks.recalculate_league_after_bulk_update_task.delay") as publish:
            self.assertEqual(self.post(
                self.form_row("home-0", player=self.player_a.pk, minute=105),
                version=page.context["version"],
            ).status_code, 302)
        publish.assert_not_called()
        self.assertEqual(self.state(), before)
