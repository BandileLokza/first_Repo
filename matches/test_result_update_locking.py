from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leagues.models import Competition, Stage, Standing
from matches.models import Match, MatchResultProcessingOutbox as Outbox
from matches.tasks import recalculate_league_after_bulk_update_task
from matches.views import _submitted_result_match_lock_queryset
from teams.models import Team


class LeagueManagerResultUpdateLockingTests(TestCase):
    """Regression coverage for the legacy League Manager result endpoint."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="result-manager",
            email="result-manager@example.test",
            password="pass",
        )
        self.competition = Competition.objects.create(
            name="Result update locking",
            slug="result-update-locking",
            season="2026",
            format_type="TABLE",
        )
        self.stage = Stage.objects.create(
            competition=self.competition,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        self.home = Team.objects.create(competition=self.competition, name="Home")
        self.away = Team.objects.create(competition=self.competition, name="Away")
        self.match = Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.home,
            away_team=self.away,
            match_date=timezone.now(),
        )
        self.client.force_login(self.user)

    def result_post_data(self, match):
        return {
            f"home_score_{match.id}": "2",
            f"away_score_{match.id}": "1",
            f"regulation_home_score_{match.id}": "2",
            f"regulation_away_score_{match.id}": "1",
            f"penalty_home_score_{match.id}": "",
            f"penalty_away_score_{match.id}": "",
        }

    def test_legacy_update_finalizes_one_match_and_queues_standings_refresh(self):
        url = (
            reverse("update_results_for_league", args=[self.competition.slug])
            + "?filter=pending"
        )

        response = self.client.post(url, self.result_post_data(self.match))

        self.assertRedirects(response, url)
        self.match.refresh_from_db()
        self.assertEqual(
            (self.match.status, self.match.home_score, self.match.away_score),
            ("FINISHED", 2, 1),
        )
        self.assertEqual(Outbox.objects.get(match=self.match).state, Outbox.State.PENDING)

        recalculate_league_after_bulk_update_task.run(
            self.competition.id, [self.match.id]
        )
        home = Standing.objects.get(
            competition=self.competition, stage=self.stage, team=self.home
        )
        away = Standing.objects.get(
            competition=self.competition, stage=self.stage, team=self.away
        )
        self.assertEqual(
            (home.played, home.won, home.goals_for, home.goals_against, home.points),
            (1, 1, 2, 1, 3),
        )
        self.assertEqual(
            (away.played, away.lost, away.goals_for, away.goals_against, away.points),
            (1, 1, 1, 2, 0),
        )

    def test_submitted_result_lock_is_self_only_nowait_and_id_ordered(self):
        other_match = Match.objects.create(
            competition=self.competition,
            stage=self.stage,
            home_team=self.away,
            away_team=self.home,
            match_date=timezone.now(),
        )

        queryset = _submitted_result_match_lock_queryset([other_match.id, self.match.id])
        query = queryset.query
        self.assertTrue(query.select_for_update)
        self.assertTrue(query.select_for_update_nowait)
        self.assertEqual(query.select_for_update_of, ("self",))
        self.assertEqual(tuple(query.order_by), ("id",))

        with transaction.atomic():
            locked_matches = list(queryset)
        self.assertEqual(
            [match.id for match in locked_matches],
            sorted((self.match.id, other_match.id)),
        )

    def test_conflicting_result_lock_returns_a_retry_response_without_writing(self):
        url = (
            reverse("update_results_for_league", args=[self.competition.slug])
            + "?filter=pending"
        )

        with patch(
            "matches.views._lock_submitted_result_matches",
            side_effect=OperationalError("would block"),
        ):
            response = self.client.post(url, self.result_post_data(self.match))

        self.assertRedirects(response, url)
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "UPCOMING")

    def test_submitted_lock_query_keeps_nullable_relations_out_of_lock_targets(self):
        # ``group`` and ``knockout_tie`` are nullable Match relations. The
        # queryset is intentionally safe for PostgreSQL's FOR UPDATE rules.
        query = _submitted_result_match_lock_queryset([self.match.id]).query
        self.assertEqual(query.select_for_update_of, ("self",))
        self.assertIsNone(self.match.group_id)
        self.assertIsNone(self.match.knockout_tie_id)
