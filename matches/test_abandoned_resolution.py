"""Regression coverage for the controlled abandoned-match workflow."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.fixture_deletion import delete_fixtures
from league_manager.forms import AbandonedMatchResolutionForm
from league_manager.models import ActivityLog
from leagues.models import Competition, CompetitionSeries, Region, Stage, Standing
from matches.abandonment import (
    AbandonedMatchResolutionError,
    award_official_result,
    confirm_score_at_abandonment,
    reopen_abandoned_resolution,
    resume_abandoned_match,
    schedule_full_replay,
    void_abandoned_match,
)
from matches.models import AbandonedMatchDecision, Goal, Match, Player
from matches.services import apply_match_to_standings_incrementally, rebuild_league_standings
from teams.models import Team


User = get_user_model()


class AbandonedMatchResolutionTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_superuser(
            username="abandonment-super",
            email="abandonment-super@example.test",
            password="test-password",
        )
        region = Region.objects.create(name="Resolution Region", slug="resolution-region")
        series = CompetitionSeries.objects.create(
            name="Resolution Series",
            slug="resolution-series",
            league_type=CompetitionSeries.LeagueType.REGIONAL,
            region=region,
        )
        self.competition = Competition.objects.create(
            name="Resolution League",
            slug="resolution-league",
            season="2026/27",
            series=series,
            format_type="TABLE",
        )
        self.stage = Stage.objects.create(
            competition=self.competition,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        self.home = Team.objects.create(name="Resolution Home", competition=self.competition)
        self.away = Team.objects.create(name="Resolution Away", competition=self.competition)

    def abandoned_match(self, *, score=(2, 1), minute=63, phase="SECOND_HALF", leg=1):
        match = Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            home_score=score[0],
            away_score=score[1],
            leg=leg,
            status="LIVE",
            phase=phase,
            minute=minute,
            match_date=timezone.now() - timedelta(days=1),
        )
        match.abandon_match("Rain made the field unsafe")
        match.refresh_from_db()
        return match

    def test_public_notice_follows_both_teams_and_scorers_with_action_permissions(self):
        match = self.abandoned_match()
        Goal.objects.bulk_create([
            Goal(match=match, team=self.home, player_name="Home Scorer", minute=12),
            Goal(match=match, team=self.away, player_name="Away Scorer", minute=23),
        ])
        self.client.force_login(self.actor)
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        notice = html.index('class="match-abandonment-notice')
        self.assertLess(html.index("Home Scorer"), notice)
        self.assertLess(html.index("Away Scorer"), notice)
        self.assertLess(notice, html.index('id="matchTabs"'))
        self.assertContains(response, "Resolve Abandoned Match")
        self.assertContains(response, "This score does not affect league standings.")
        ordinary = User.objects.create_user(username="notice-viewer")
        self.client.force_login(ordinary)
        response = self.client.get(reverse("match_detail", args=[match.pk]))
        self.assertContains(response, "Match Abandoned")
        self.assertNotContains(response, "Resolve Abandoned Match")

    def test_replay_preserves_original_and_prevents_duplicate_active_replay(self):
        match = self.abandoned_match()
        scorer = Player.objects.create(name="Recorded scorer", team=self.home)
        match.status = "LIVE"
        match.phase = "SECOND_HALF"
        match.save(update_fields=["status", "phase"], _allow_abandoned_resume=True)
        Goal.objects.create(match=match, team=self.home, player=scorer, minute=20)
        match.abandon_match("Rain made the field unsafe")

        source, replay, decision = schedule_full_replay(
            match_id=match.id,
            actor=self.actor,
            scheduled_for=timezone.now() + timedelta(days=7),
            venue="Replay Ground",
        )

        self.assertEqual(source.status, "ABANDONED")
        self.assertEqual(source.abandonment_resolution, "REPLAY")
        self.assertEqual(replay.replay_of_id, source.id)
        self.assertEqual((replay.home_score, replay.away_score, replay.minute), (0, 0, 0))
        self.assertEqual(replay.status, "UPCOMING")
        self.assertEqual(source.goals.count(), 1)
        self.assertEqual(decision.decision_type, AbandonedMatchDecision.DecisionType.REPLAY)
        self.assertEqual(decision.reason, "Full replay officially scheduled.")
        self.assertEqual(decision.decision_reference, "")
        with self.assertRaises(AbandonedMatchResolutionError):
            schedule_full_replay(
                match_id=match.id,
                actor=self.actor,
                scheduled_for=timezone.now() + timedelta(days=8),
            )

    def test_resume_uses_saved_minute_without_counting_downtime(self):
        match = self.abandoned_match(minute=63)

        resumed, decision = resume_abandoned_match(
            match_id=match.id,
            actor=self.actor,
            resume_minute=63,
            scheduled_for=timezone.now() - timedelta(minutes=1),
            venue="Continuation Ground",
        )

        self.assertEqual(resumed.status, "LIVE")
        self.assertEqual(resumed.phase, "SECOND_HALF")
        self.assertEqual(resumed.minute, 63)
        self.assertEqual(resumed.abandonment_resolution, "RESUMED")
        self.assertEqual(decision.resume_minute, 63)
        self.assertEqual(decision.reason, "Match approved for resumption.")

    def test_generic_result_and_scorer_screens_exclude_official_decisions(self):
        from django.urls import reverse
        match = self.abandoned_match(score=(0, 0))
        self.client.force_login(self.actor)
        results_url = reverse("update_results_for_league", args=[self.competition.slug]) + "?filter=finished"
        response = self.client.get(results_url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'name="home_score_{match.pk}"')
        award_official_result(match_id=match.id, actor=self.actor, home_score=3, away_score=0)
        response = self.client.get(results_url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'name="home_score_{match.pk}"')
        response = self.client.get(reverse("match_detail", args=[match.pk]) + "?quick-goals=1")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_quick_goal_details"])
        self.assertFalse(response.context["show_quick_goal_details"])
        self.assertNotContains(response, 'id="quick-goal-details-heading"')

    def test_resumption_restores_the_frozen_on_field_score(self):
        match = self.abandoned_match(score=(0, 1))
        # A stale importer or an older administrative client must not change
        # the score that is restored when the suspended match resumes.
        Match.objects.filter(pk=match.id).update(home_score=8, away_score=0)
        resumed, _ = resume_abandoned_match(match_id=match.id, actor=self.actor)
        self.assertEqual((resumed.home_score, resumed.away_score), (0, 1))
        resumed.refresh_from_db()
        self.assertEqual((resumed.home_score, resumed.away_score), (0, 1))

    def test_resumption_preserves_paused_and_shootout_phases(self):
        match = self.abandoned_match(score=(1, 1), phase="BREAK", minute=45)
        for phase, minute in (
            ("BREAK", 45), ("REGULATION_ENDED", 90),
            ("EXTRA_BREAK", 105), ("EXTRA_ENDED", 120), ("PENALTIES", 120),
        ):
            with self.subTest(phase=phase):
                if match.status == "LIVE":
                    match.phase = phase
                    match.minute = minute
                    match.phase_start_time = None
                    match.save(update_fields=["phase", "minute", "phase_start_time"])
                    match.abandon_match("Interruption")
                resumed, _ = resume_abandoned_match(
                    match_id=match.id, actor=self.actor, resume_minute=minute,
                )
                self.assertEqual(resumed.phase, phase)
                self.assertEqual(resumed.status, "LIVE")
                self.assertEqual(resumed.minute, minute)
                self.assertIsNone(resumed.phase_start_time)
                match = resumed

    def test_replay_is_allowed_in_a_locked_schedule(self):
        match = self.abandoned_match()
        self.competition.schedule_locked_at = timezone.now()
        self.competition.save(update_fields=["schedule_locked_at"])
        source, replay, decision = schedule_full_replay(
            match_id=match.id, actor=self.actor,
            scheduled_for=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(replay.replay_of_id, source.id)
        self.assertEqual(replay.status, "UPCOMING")
        self.assertEqual(decision.replay_match_id, replay.id)
        with self.assertRaises(ValidationError):
            Match.objects.create(
                competition=self.competition, stage=self.stage,
                home_team=self.away, away_team=self.home,
                match_date=timezone.now(),
            )

    def test_future_resumption_is_rejected_without_starting_the_clock(self):
        match = self.abandoned_match()
        future = timezone.now() + timedelta(days=2)
        with self.assertRaisesMessage(AbandonedMatchResolutionError, "immediately"):
            resume_abandoned_match(
                match_id=match.id, actor=self.actor, resume_minute=63,
                scheduled_for=future,
            )
        match.refresh_from_db()
        self.assertEqual(match.status, "ABANDONED")
        self.assertIsNone(match.phase_start_time)
        self.assertFalse(match.abandonment_decisions.exists())
        form = AbandonedMatchResolutionForm({
            "decision_type": "RESUME", "resume_minute": 63,
            "scheduled_for": future.strftime("%Y-%m-%dT%H:%M"),
        }, match=match)
        self.assertFalse(form.is_valid())
        self.assertIn("scheduled_for", form.errors)

    def test_knockout_only_offers_resumption_and_cannot_be_voided(self):
        match = self.abandoned_match()
        self.stage.stage_type = "KNOCKOUT"
        self.stage.save(update_fields=["stage_type"])
        match.refresh_from_db()
        form = AbandonedMatchResolutionForm(match=match)
        self.assertEqual([value for value, _ in form.fields["decision_type"].choices], ["RESUME"])
        with self.assertRaisesMessage(AbandonedMatchResolutionError, "knockout"):
            void_abandoned_match(match_id=match.id, actor=self.actor)
        match.refresh_from_db()
        self.assertEqual(match.abandonment_resolution, "")
        self.assertFalse(match.abandonment_decisions.exists())

    def test_unresolved_abandonment_blocks_season_archive_until_voided(self):
        from leagues.seasons import archive_competition_season
        match = self.abandoned_match()
        with self.assertRaisesMessage(ValidationError, "unresolved abandoned"):
            archive_competition_season(self.competition, actor=self.actor)
        void_abandoned_match(match_id=match.id, actor=self.actor)
        archived = archive_competition_season(self.competition, actor=self.actor)
        self.assertTrue(archived.is_archived)

    def test_official_award_updates_table_and_predictions_immediately(self):
        from predictions.models import Prediction, PredictionMatch
        match = self.abandoned_match(score=(0, 0))
        prediction_match, _ = PredictionMatch.objects.get_or_create(
            match=match, defaults={"competition": self.competition},
        )
        prediction = Prediction.objects.create(
            user=self.actor, prediction_match=prediction_match,
            predicted_result="H", predicted_home_goals=3, predicted_away_goals=0,
        )
        awarded, _ = award_official_result(
            match_id=match.id, actor=self.actor, home_score=3, away_score=0,
        )
        prediction.refresh_from_db()
        standing = Standing.objects.get(
            competition=self.competition, stage=self.stage, team=self.home,
        )
        self.assertEqual((standing.played, standing.points), (1, 3))
        self.assertEqual(prediction.points_awarded, 20)
        awarded.refresh_from_db()
        self.assertTrue(awarded.table_updated)

    def test_failed_official_result_refresh_rolls_back_the_entire_decision(self):
        match = self.abandoned_match(score=(0, 0))
        with patch("predictions.utils.calculate_predictions_for_match", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                award_official_result(
                    match_id=match.id, actor=self.actor, home_score=3, away_score=0,
                )
        match.refresh_from_db()
        self.assertEqual((match.status, match.home_score, match.away_score), ("ABANDONED", 0, 0))
        self.assertFalse(match.table_updated)
        self.assertFalse(match.abandonment_decisions.exists())
        self.assertEqual(Standing.objects.get(
            competition=self.competition, stage=self.stage, team=self.home,
        ).points, 0)

    def test_legacy_abandonment_migration_preserves_the_recorded_snapshot(self):
        from importlib import import_module
        from django.apps import apps
        match = self.abandoned_match(score=(2, 1), minute=63)
        Match.objects.filter(pk=match.id).update(
            abandoned_home_score=None, abandoned_away_score=None,
            abandoned_minute=None, abandoned_phase="", abandoned_at=None,
            abandoned_scheduled_for=None,
        )
        migration = import_module("matches.migrations.0018_abandoned_match_resolution")
        migration.backfill_historical_abandonments(apps, None)
        match.refresh_from_db()
        self.assertEqual((match.abandoned_home_score, match.abandoned_away_score), (2, 1))
        self.assertEqual(match.abandoned_minute, 63)
        self.assertEqual(match.abandoned_scheduled_for, match.match_date)
        self.assertIsNone(match.abandoned_at)

    def test_award_counts_for_table_but_not_player_statistics(self):
        match = self.abandoned_match(score=(2, 1))
        scorer = Player.objects.create(name="On-field scorer", team=self.home)
        # Persist a pre-abandonment event through the normal live lifecycle.
        match.status = "LIVE"
        match.phase = "SECOND_HALF"
        match.save(update_fields=["status", "phase"], _allow_abandoned_resume=True)
        Goal.objects.create(match=match, team=self.home, player=scorer, minute=30)
        match.abandon_match("Rain made the field unsafe")

        awarded, decision = award_official_result(
            match_id=match.id,
            actor=self.actor,
            home_score=4,
            away_score=2,
        )
        apply_match_to_standings_incrementally(awarded)
        home_standing = Standing.objects.get(
            competition=self.competition, stage=self.stage, team=self.home
        )

        scorer.refresh_from_db()
        self.assertEqual(awarded.status, "FINISHED")
        self.assertEqual(awarded.abandonment_resolution, "AWARDED")
        self.assertEqual((home_standing.played, home_standing.won, home_standing.points), (1, 1, 3))
        self.assertEqual(scorer.recorded_goals, 0)
        self.assertEqual((decision.official_home_score, decision.official_away_score), (4, 2))
        self.assertEqual(decision.reason, "Official result awarded: 4–2.")
        self.assertEqual(decision.decision_reference, "")

    def test_confirmed_score_keeps_recorded_goals_and_void_has_no_points(self):
        confirmed_match = self.abandoned_match(score=(1, 0))
        scorer = Player.objects.create(name="Confirmed scorer", team=self.home)
        confirmed_match.status = "LIVE"
        confirmed_match.phase = "SECOND_HALF"
        confirmed_match.save(update_fields=["status", "phase"], _allow_abandoned_resume=True)
        Goal.objects.create(match=confirmed_match, team=self.home, player=scorer, minute=34)
        confirmed_match.abandon_match("Rain made the field unsafe")

        confirmed, _ = confirm_score_at_abandonment(
            match_id=confirmed_match.id,
            actor=self.actor,
        )
        apply_match_to_standings_incrementally(confirmed)
        scorer.refresh_from_db()
        self.assertEqual(confirmed.abandonment_resolution, "CONFIRMED")
        self.assertEqual(scorer.recorded_goals, 1)
        self.assertEqual(
            confirmed.abandonment_decisions.get().reason,
            "Recorded score confirmed as official result.",
        )

        voided_match = self.abandoned_match(score=(4, 0), minute=30, leg=2)
        voided, _ = void_abandoned_match(
            match_id=voided_match.id,
            actor=self.actor,
            note="  \t  ",
        )
        self.assertEqual(voided.status, "ABANDONED")
        self.assertEqual(voided.abandonment_resolution, "VOID")
        self.assertEqual(
            voided.abandonment_decisions.get().reason,
            "Match officially voided.",
        )
        self.assertEqual(
            Standing.objects.get(competition=self.competition, stage=self.stage, team=self.home).played,
            1,
        )

    def test_direct_abandoned_to_live_transition_is_rejected(self):
        match = self.abandoned_match()
        match.status = "LIVE"
        with self.assertRaises(ValidationError):
            match.save(update_fields=["status"])

    def test_awarded_decision_can_be_reopened_without_erasing_history(self):
        match = self.abandoned_match(score=(1, 0))
        awarded, decision = award_official_result(
            match_id=match.id,
            actor=self.actor,
            home_score=3,
            away_score=0,
        )
        apply_match_to_standings_incrementally(awarded)

        reopened, prior_decision = reopen_abandoned_resolution(
            match_id=match.id,
            actor=self.actor,
        )

        prior_decision.refresh_from_db()
        home_standing = Standing.objects.get(
            competition=self.competition, stage=self.stage, team=self.home
        )
        self.assertEqual(reopened.status, "ABANDONED")
        self.assertEqual(reopened.abandonment_resolution, "")
        self.assertFalse(prior_decision.is_current)
        self.assertEqual(prior_decision.reason, "Official result awarded: 3–0.")
        self.assertEqual(home_standing.played, 0)
        self.assertEqual(
            AbandonedMatchDecision.objects.filter(match=match).count(),
            1,
        )
        self.assertEqual(
            ActivityLog.objects.get(
                action=ActivityLog.Action.ABANDONMENT_RESOLUTION_REOPENED,
                match=match,
            ).description,
            "Official decision reopened for correction.",
        )

    def test_optional_note_form_and_manual_note_are_preserved(self):
        match = self.abandoned_match()
        form = AbandonedMatchResolutionForm(
            {
                "decision_type": AbandonedMatchDecision.DecisionType.AWARD,
                "official_home_score": 2,
                "official_away_score": 0,
                "additional_note": "   ",
            },
            match=match,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("decision_reference", form.fields)

        awarded, decision = award_official_result(
            match_id=match.id,
            actor=self.actor,
            home_score=2,
            away_score=0,
            note="Committee decision recorded after review.",
        )

        self.assertEqual(awarded.status, "FINISHED")
        self.assertEqual(decision.reason, "Committee decision recorded after review.")

    def test_deleting_unresolved_abandonment_does_not_rebuild_standings(self):
        match = self.abandoned_match()
        with patch("league_manager.fixture_deletion.rebuild_league_standings") as rebuild:
            delete_fixtures(actor=self.actor, fixture_ids=[match.id])
        self.assertFalse(Match.objects.filter(pk=match.id).exists())
        rebuild.assert_not_called()

    def test_bulk_rebuild_matches_finished_source_results(self):
        finished = Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            home_score=2,
            away_score=1,
            status="FINISHED",
            match_date=timezone.now(),
        )
        rebuild_league_standings(self.competition)
        finished.refresh_from_db()
        standing = Standing.objects.get(
            competition=self.competition, stage=self.stage, team=self.home
        )
        self.assertTrue(finished.table_updated)
        self.assertEqual((standing.played, standing.won, standing.points), (1, 1, 3))
