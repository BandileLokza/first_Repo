"""Regression coverage for abandonment decision pages and protected fixture history."""

from datetime import timedelta
from unittest.mock import patch
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.models import model_to_dict
from django.contrib.messages import get_messages
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.fixture_deletion import (
    FixtureDeletionBlocked,
    delete_fixtures,
    fixture_deletion_block_reason,
    validate_fixture_deletion,
)
from league_manager.models import ActivityLog
from leagues.models import Competition, CompetitionSeries, Stage
from matches.abandonment import (
    AbandonedMatchResolutionError,
    award_official_result,
    confirm_score_at_abandonment,
    reopen_abandoned_resolution,
    schedule_full_replay,
    void_abandoned_match,
)
from matches.admin import GoalAdmin, MatchAdmin
from matches.forms import GoalAdminForm, MatchAdminForm
from matches.models import AbandonedMatchDecision, Goal, Match
from teams.models import Team


class AbandonmentManagementRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_superuser(
            username="abandonment-regression-admin",
            email="abandonment-regression@example.test",
            password="test-password",
        )
        series = CompetitionSeries.objects.create(
            name="Abandonment Regression Series",
            slug="abandonment-regression-series",
            league_type=CompetitionSeries.LeagueType.ZONAL,
        )
        cls.competition = Competition.objects.create(
            name="Abandonment Regression League",
            slug="abandonment-regression-league",
            season="2026/27",
            series=series,
            format_type="TABLE",
        )
        cls.stage = Stage.objects.create(
            competition=cls.competition,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        cls.home = Team.objects.create(name="Regression Home", competition=cls.competition)
        cls.away = Team.objects.create(name="Regression Away", competition=cls.competition)

    def setUp(self):
        self.client.force_login(self.actor)

    def abandoned_match(self, *, score=(0, 0), leg=None):
        if leg is None:
            leg = (Match.objects.filter(
                competition=self.competition, stage=self.stage,
                home_team=self.home, away_team=self.away,
            ).order_by("-leg").values_list("leg", flat=True).first() or 0) + 1
        match = Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            home_score=score[0],
            away_score=score[1],
            leg=leg,
            status="LIVE",
            phase="SECOND_HALF",
            minute=63,
            match_date=timezone.now() - timedelta(days=1),
        )
        match.abandon_match("Unsafe playing conditions")
        return match

    def resolution_url(self, match):
        return reverse(
            "league_manager:resolve_abandoned_match",
            args=[self.competition.slug, match.pk],
        )

    def deletion_url(self, match):
        return reverse(
            "league_manager:fixture_delete",
            args=[self.competition.slug, match.pk],
        )

    def test_current_void_shows_correction_instead_of_another_resolution(self):
        match, _ = void_abandoned_match(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        response = self.client.get(self.resolution_url(match))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_resolve"])
        self.assertTrue(response.context["can_reopen"])
        self.assertContains(response, "Reopen for Correction")
        self.assertNotContains(response, "Record Official Decision")

    def test_voided_match_detail_keeps_correction_link(self):
        match, _ = void_abandoned_match(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "Review / Correct Decision")
        self.assertContains(response, self.resolution_url(match))

    def test_voided_decision_can_be_reopened_from_management_page(self):
        match, decision = void_abandoned_match(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        response = self.client.post(self.resolution_url(match), {"action": "reopen"})
        self.assertRedirects(response, self.resolution_url(match), fetch_redirect_response=False)
        match.refresh_from_db()
        decision.refresh_from_db()
        self.assertEqual(match.status, "ABANDONED")
        self.assertEqual(match.abandonment_resolution, "")
        self.assertFalse(decision.is_current)

    def test_invalid_reopen_renders_validation_error_without_crashing(self):
        match, decision = void_abandoned_match(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        with patch(
            "league_manager.views.reopen_abandoned_resolution",
            side_effect=AbandonedMatchResolutionError("The decision cannot be reopened now."),
        ):
            response = self.client.post(self.resolution_url(match), {"action": "reopen"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["form"])
        self.assertContains(response, "The decision cannot be reopened now.")
        decision.refresh_from_db()
        self.assertTrue(decision.is_current)

    def test_replay_history_is_readable_without_offering_new_decision(self):
        match, replay, _ = schedule_full_replay(
            match_id=self.abandoned_match().pk,
            actor=self.actor,
            scheduled_for=timezone.now() + timedelta(days=2),
        )
        response = self.client.get(self.resolution_url(match))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_resolve"])
        self.assertFalse(response.context["can_reopen"])
        self.assertContains(response, "This match already has a controlled resolution.")
        self.assertContains(response, reverse("match_detail", args=[replay.pk]))

    def test_awarded_result_review_preserves_frozen_zero_score(self):
        match, _ = award_official_result(
            match_id=self.abandoned_match(score=(0, 0)).pk,
            actor=self.actor,
            home_score=3,
            away_score=1,
        )
        response = self.client.get(self.resolution_url(match))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Regression Home 0 – 0 Regression Away")
        self.assertNotContains(response, "Regression Home 3 – 1 Regression Away")

    def test_deletion_preview_explains_protected_decision_history(self):
        match, _ = void_abandoned_match(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        response = self.client.get(self.deletion_url(match))
        self.assertEqual(response.status_code, 200)
        self.assertIn("official abandonment decision history", response.context["deletion_block_reason"])
        self.assertContains(response, "official abandonment decision history")

    def test_deletion_post_preserves_fixture_and_official_decision(self):
        match, decision = void_abandoned_match(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        response = self.client.post(self.deletion_url(match))
        self.assertRedirects(
            response,
            reverse("league_manager:fixture_list", args=[self.competition.slug]),
            fetch_redirect_response=False,
        )
        self.assertTrue(Match.objects.filter(pk=match.pk).exists())
        self.assertTrue(AbandonedMatchDecision.objects.filter(pk=decision.pk).exists())
        self.assertIn(
            "official abandonment decision history",
            " ".join(str(message) for message in get_messages(response.wsgi_request)),
        )
        self.assertFalse(ActivityLog.objects.filter(action=ActivityLog.Action.FIXTURE_DELETED).exists())

    def test_bulk_deletion_of_protected_selection_is_all_or_nothing(self):
        protected, _ = void_abandoned_match(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        other = self.abandoned_match(leg=2)
        with self.assertRaises(FixtureDeletionBlocked):
            delete_fixtures(actor=self.actor, fixture_ids=[other.pk, protected.pk])
        self.assertEqual(Match.objects.filter(pk__in=[protected.pk, other.pk]).count(), 2)
        self.assertFalse(ActivityLog.objects.filter(action=ActivityLog.Action.FIXTURE_DELETED).exists())

    def test_superseded_decision_history_also_prevents_deletion(self):
        match, _ = void_abandoned_match(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        reopen_abandoned_resolution(match_id=match.pk, actor=self.actor)
        match.refresh_from_db()
        self.assertIn("official abandonment decision history", fixture_deletion_block_reason(match))
        with self.assertRaises(FixtureDeletionBlocked):
            validate_fixture_deletion(actor=self.actor, fixture_ids=[match.pk])

    def test_replay_decision_reference_protects_replay_fixture(self):
        _, replay, _ = schedule_full_replay(
            match_id=self.abandoned_match().pk,
            actor=self.actor,
            scheduled_for=timezone.now() + timedelta(days=2),
        )
        self.assertIn("official abandonment decision history", fixture_deletion_block_reason(replay))
        with self.assertRaises(FixtureDeletionBlocked):
            delete_fixtures(actor=self.actor, fixture_ids=[replay.pk])
        self.assertTrue(Match.objects.filter(pk=replay.pk).exists())

    def test_late_protected_relation_returns_business_error_and_rolls_back_audit(self):
        match = self.abandoned_match()
        with patch(
            "matches.models.MatchQuerySet.delete",
            side_effect=ProtectedError("Protected relation", {match}),
        ):
            with self.assertRaisesMessage(FixtureDeletionBlocked, "protected match history"):
                delete_fixtures(actor=self.actor, fixture_ids=[match.pk])
        self.assertTrue(Match.objects.filter(pk=match.pk).exists())
        self.assertFalse(ActivityLog.objects.filter(action=ActivityLog.Action.FIXTURE_DELETED).exists())


    def active_match(self, *, score=(0, 0), status="LIVE", resolution=""):
        leg = (Match.objects.filter(
            competition=self.competition, stage=self.stage,
            home_team=self.home, away_team=self.away,
        ).order_by("-leg").values_list("leg", flat=True).first() or 0) + 1
        return Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            home_score=score[0],
            away_score=score[1],
            status=status,
            phase="FIRST_HALF" if status == "LIVE" else "NONE",
            minute=12,
            match_date=timezone.now(),
            abandonment_resolution=resolution,
            leg=leg,
        )

    def admin_request(self):
        request = RequestFactory().post("/admin/")
        request.user = self.actor
        return request

    def goal_data(self, match):
        return {
            "match": match.pk,
            "team": self.home.pk,
            "player": "",
            "player_name": "Recorded scorer",
            "goal_type": Goal.GoalType.NORMAL,
            "minute": 12,
            "period": Goal.Period.FIRST_HALF,
        }

    def test_abandoned_admin_form_rejects_sporting_and_history_edits(self):
        match = self.abandoned_match(score=(1, 0))
        third_team = Team.objects.create(name="Unrelated team", competition=self.competition)
        other_stage = Stage.objects.create(
            competition=self.competition, name="Other Stage", stage_type="LEAGUE", order=2
        )
        changes = {
            "home_score": 8,
            "away_score": 6,
            "status": "LIVE",
            "phase": "SECOND_HALF",
            "phase_start_time": timezone.now(),
            "minute": 90,
            "abandon_reason": "A replacement reason",
            "home_team": third_team.pk,
            "stage": other_stage.pk,
            "leg": 2,
            "gameweek": 4,
            "abandoned_home_score": 9,
            "abandonment_resolution": "RESUMED",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                match.refresh_from_db()
                data = model_to_dict(match)
                data[field] = value
                form = MatchAdminForm(data=data, instance=match)
                self.assertFalse(form.is_valid())
                self.assertIn("locked", str(form.non_field_errors()))
        match.refresh_from_db()
        self.assertEqual((match.home_score, match.away_score), (1, 0))
        self.assertEqual(match.abandon_reason, "Unsafe playing conditions")
        self.assertEqual(match.abandonment_resolution, "")

    def test_abandoned_admin_allows_kickoff_and_location_metadata(self):
        match = self.abandoned_match(score=(1, 0))
        data = model_to_dict(match)
        kickoff = timezone.now() + timedelta(days=2)
        data.update(match_date=kickoff, location="Replacement stadium")
        form = MatchAdminForm(data=data, instance=match)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        match.refresh_from_db()
        self.assertEqual(match.match_date, kickoff)
        self.assertEqual(match.location, "Replacement stadium")
        self.assertEqual(match.abandoned_home_score, 1)
        self.assertNotEqual(match.abandoned_scheduled_for, kickoff)
        readonly = MatchAdmin(Match, admin.site).get_readonly_fields(self.admin_request(), match)
        for field in ("home_score", "away_score", "phase", "minute", "home_team", "abandon_reason"):
            self.assertIn(field, readonly)
        self.assertNotIn("match_date", readonly)
        self.assertNotIn("location", readonly)

    def test_abandoned_match_admin_backend_rejects_tampering_after_validation(self):
        match = self.abandoned_match(score=(1, 0))
        form = MatchAdminForm(data=model_to_dict(match), instance=match)
        self.assertTrue(form.is_valid(), form.errors)
        form.instance.home_score = 8
        with self.assertRaisesMessage(ValidationError, "locked"):
            form.save()
        with self.assertRaisesMessage(ValidationError, "locked"):
            MatchAdmin(Match, admin.site).save_model(
                self.admin_request(), form.instance,
                SimpleNamespace(changed_data=["home_score"]), True,
            )
        match.refresh_from_db()
        self.assertEqual(match.home_score, 1)

    def test_normal_and_resumed_match_admin_edits_remain_available(self):
        for status, resolution in (("LIVE", ""), ("FINISHED", ""), ("LIVE", "RESUMED")):
            with self.subTest(status=status, resolution=resolution):
                match = self.active_match(status=status, resolution=resolution)
                data = model_to_dict(match)
                data["home_score"] = 2
                form = MatchAdminForm(data=data, instance=match)
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                match.refresh_from_db()
                self.assertEqual(match.home_score, 2)

    def test_goal_admin_rejects_additions_to_protected_fixtures(self):
        pending = self.abandoned_match()
        awarded, _ = award_official_result(
            match_id=self.abandoned_match().pk, actor=self.actor, home_score=3, away_score=0
        )
        confirmed, _ = confirm_score_at_abandonment(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        for match in (pending, awarded, confirmed):
            with self.subTest(resolution=match.abandonment_resolution):
                form = GoalAdminForm(data=self.goal_data(match))
                self.assertFalse(form.is_valid())
                self.assertIn("read-only", str(form.non_field_errors()))
                response = self.client.post(reverse("admin:matches_goal_add"), self.goal_data(match))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(Goal.objects.filter(match=match).exists())

    def test_goal_admin_rejects_moves_from_or_into_protected_fixtures(self):
        source = self.active_match(score=(1, 0))
        goal = Goal.objects.create(match=source, team=self.home, player_name="Original scorer", minute=12)
        source.abandon_match("Unsafe playing conditions")
        destination = self.active_match(score=(1, 0))
        data = model_to_dict(goal)
        data["match"] = destination.pk
        form = GoalAdminForm(data=data, instance=goal)
        self.assertFalse(form.is_valid())
        self.assertIn("read-only", str(form.non_field_errors()))
        goal.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "read-only"):
            goal.match = destination
            GoalAdmin(Goal, admin.site).save_model(self.admin_request(), goal, None, True)
        goal.refresh_from_db()
        self.assertEqual(goal.match_id, source.pk)

        ordinary_goal = Goal.objects.create(
            match=destination, team=self.home, player_name="Other scorer", minute=12
        )
        data = model_to_dict(ordinary_goal)
        data["match"] = source.pk
        form = GoalAdminForm(data=data, instance=ordinary_goal)
        self.assertFalse(form.is_valid())
        self.assertIn("read-only", str(form.non_field_errors()))
        ordinary_goal.refresh_from_db()
        ordinary_goal.match = source
        with self.assertRaisesMessage(ValidationError, "read-only"):
            GoalAdmin(Goal, admin.site).save_model(self.admin_request(), ordinary_goal, None, True)
        ordinary_goal.refresh_from_db()
        self.assertEqual(ordinary_goal.match_id, destination.pk)

    def test_goal_admin_form_save_rechecks_fixture_after_validation(self):
        active = self.active_match()
        form = GoalAdminForm(data=self.goal_data(active))
        self.assertTrue(form.is_valid(), form.errors)
        active.abandon_match("Unsafe playing conditions")
        with self.assertRaisesMessage(ValidationError, "read-only"):
            form.save()
        self.assertFalse(Goal.objects.filter(match=active).exists())

    def test_protected_goal_admin_is_read_only_and_bulk_delete_is_atomic(self):
        source = self.active_match(score=(1, 0))
        protected_goal = Goal.objects.create(
            match=source, team=self.home, player_name="Protected scorer", minute=12
        )
        source.abandon_match("Unsafe playing conditions")
        ordinary = self.active_match(score=(1, 0))
        ordinary_goal = Goal.objects.create(
            match=ordinary, team=self.home, player_name="Ordinary scorer", minute=12
        )
        modeladmin = GoalAdmin(Goal, admin.site)
        protected_goal.refresh_from_db()
        request = self.admin_request()
        self.assertFalse(modeladmin.has_change_permission(request, protected_goal))
        self.assertFalse(modeladmin.has_delete_permission(request, protected_goal))
        self.assertTrue(modeladmin.has_view_permission(request, protected_goal))
        with self.assertRaises(PermissionDenied):
            modeladmin.delete_model(request, protected_goal)
        with self.assertRaises(PermissionDenied):
            modeladmin.delete_queryset(request, Goal.objects.filter(pk__in=[ordinary_goal.pk, protected_goal.pk]))
        self.assertEqual(Goal.objects.filter(pk__in=[ordinary_goal.pk, protected_goal.pk]).count(), 2)
        response = self.client.get(reverse("admin:matches_goal_change", args=[protected_goal.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')

    def test_goal_admin_still_accepts_normal_finished_and_resumed_fixtures(self):
        for status, resolution in (("LIVE", ""), ("FINISHED", ""), ("LIVE", "RESUMED")):
            with self.subTest(status=status, resolution=resolution):
                match = self.active_match(score=(1, 0), status=status, resolution=resolution)
                form = GoalAdminForm(data=self.goal_data(match))
                self.assertTrue(form.is_valid(), form.errors)
                goal = form.save()
                modeladmin = GoalAdmin(Goal, admin.site)
                self.assertTrue(modeladmin.has_change_permission(self.admin_request(), goal))
                self.assertTrue(modeladmin.has_delete_permission(self.admin_request(), goal))
                self.assertEqual(goal.match_id, match.pk)


    def test_official_abandonment_admin_freezes_identity_and_allows_metadata(self):
        third_team = Team.objects.create(name="Changed result team", competition=self.competition)
        awarded, _ = award_official_result(
            match_id=self.abandoned_match().pk, actor=self.actor, home_score=3, away_score=0
        )
        confirmed, _ = confirm_score_at_abandonment(
            match_id=self.abandoned_match().pk, actor=self.actor
        )
        for match in (awarded, confirmed):
            with self.subTest(resolution=match.abandonment_resolution):
                data = model_to_dict(match)
                data["home_team"] = third_team.pk
                form = MatchAdminForm(data=data, instance=match)
                self.assertFalse(form.is_valid())
                self.assertIn("locked", str(form.non_field_errors()))
                match.refresh_from_db()
                data = model_to_dict(match)
                data["location"] = "Corrected result venue"
                form = MatchAdminForm(data=data, instance=match)
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                match.refresh_from_db()
                self.assertEqual(match.home_team_id, self.home.pk)
                self.assertEqual(match.location, "Corrected result venue")
                readonly = MatchAdmin(Match, admin.site).get_readonly_fields(self.admin_request(), match)
                self.assertIn("home_team", readonly)
                self.assertIn("phase", readonly)
