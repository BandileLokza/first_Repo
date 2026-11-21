"""Public Pool home coverage for competition-centred live-score cards."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .homepage import (
    HOMEPAGE_MATCH_LIMIT,
    homepage_match_prefetches,
    homepage_match_rows,
    homepage_matches_for_competition,
)
from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolTeam,
)
from .services import record_manual_final_result, start_match
from . import views


User = get_user_model()


class PoolHomeLiveScoreTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="pool-home-live-owner", password="test-password"
        )
        self.now = timezone.now().replace(second=0, microsecond=0)
        self.today_midday = timezone.localtime(self.now).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        self._competition_number = 0

    def make_competition(self, **overrides):
        self._competition_number += 1
        values = {
            "name": f"Live score competition {self._competition_number}",
            "slug": f"live-score-competition-{self._competition_number}",
            "owner": self.owner,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "operational_state": PoolCompetition.OperationalState.ACTIVE,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.LEAGUE,
            "default_race_target": 5,
        }
        values.update(overrides)
        competition = PoolCompetition.objects.create(**values)
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        return competition, stage

    @staticmethod
    def make_entries(competition, *, prefix="Cueist"):
        if competition.participant_mode == PoolCompetition.ParticipantMode.TEAMS:
            home = PoolCompetitionEntry.objects.create(
                competition=competition,
                team=PoolTeam.objects.create(name=f"{prefix} Home Team"),
            )
            away = PoolCompetitionEntry.objects.create(
                competition=competition,
                team=PoolTeam.objects.create(name=f"{prefix} Away Team"),
            )
        else:
            home = PoolCompetitionEntry.objects.create(
                competition=competition,
                player=PoolPlayer.objects.create(full_name=f"{prefix} Home"),
            )
            away = PoolCompetitionEntry.objects.create(
                competition=competition,
                player=PoolPlayer.objects.create(full_name=f"{prefix} Away"),
            )
        return home, away

    def make_match(self, competition, stage, home, away, *, scheduled_at=None, **values):
        return PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home,
            away_entry=away,
            race_target=competition.default_race_target,
            scheduled_at=scheduled_at,
            **values,
        )

    def preloaded_competition(self, competition):
        return (
            PoolCompetition.objects.filter(pk=competition.pk)
            .prefetch_related(*homepage_match_prefetches())
            .get()
        )

    def finish_match(self, pool_match):
        return record_manual_final_result(
            pool_match,
            home_score=pool_match.race_target,
            away_score=1,
            recorded_by=self.owner,
        )

    def test_public_sport_navigation_links_both_homepages_without_authentication(self):
        football_response = self.client.get(reverse("league_list"))
        pool_response = self.client.get(reverse("pool:home"))

        self.assertEqual(football_response.status_code, 200)
        self.assertEqual(pool_response.status_code, 200)
        self.assertContains(football_response, reverse("pool:home"))
        self.assertContains(pool_response, reverse("league_list"))
        self.assertContains(pool_response, 'aria-current="page"')

    def test_active_sport_tab_has_a_persistent_surface_and_full_width_underline(self):
        stylesheet = Path(finders.find("css/styles.css")).read_text(encoding="utf-8")
        active_rule = stylesheet.split(".sport-selector-link.is-active {")[1].split("}")[0]
        underline_rule = stylesheet.split(".sport-selector-link.is-active::after {")[1].split("}")[0]

        self.assertIn("background:", active_rule)
        self.assertIn("box-shadow:", active_rule)
        self.assertIn("left: 0", underline_rule)
        self.assertIn("right: 0", underline_rule)

    def test_association_logo_lookup_prefers_svg_then_png_and_avoids_broken_images(self):
        def static_url(path):
            return f"/static/{path}"

        views._pool_association_logo_url.cache_clear()
        try:
            with patch("pool.views.finders.find", side_effect=lambda path: path), patch(
                "pool.views.static", side_effect=static_url
            ):
                self.assertEqual(
                    views._pool_association_logo_url(),
                    "/static/pool/images/cue-sports-association-logo.svg",
                )

            views._pool_association_logo_url.cache_clear()
            with patch(
                "pool.views.finders.find",
                side_effect=lambda path: path if path.endswith(".png") else None,
            ), patch("pool.views.static", side_effect=static_url):
                self.assertEqual(
                    views._pool_association_logo_url(),
                    "/static/pool/images/cue-sports-association-logo.png",
                )

            views._pool_association_logo_url.cache_clear()
            with patch("pool.views.finders.find", return_value=None):
                self.assertEqual(views._pool_association_logo_url(), "")
        finally:
            views._pool_association_logo_url.cache_clear()

    def test_live_matches_rank_first_and_card_selection_never_exceeds_five(self):
        competition, stage = self.make_competition()
        home, away = self.make_entries(competition)
        for offset in (1, 2):
            live_match = self.make_match(
                competition,
                stage,
                home,
                away,
                scheduled_at=self.today_midday + timedelta(minutes=offset),
            )
            start_match(live_match, recorded_by=self.owner)
        self.make_match(
            competition,
            stage,
            home,
            away,
            scheduled_at=self.today_midday + timedelta(hours=2),
        )
        for offset in (3, 4, 5):
            self.finish_match(
                self.make_match(
                    competition,
                    stage,
                    home,
                    away,
                    scheduled_at=self.today_midday - timedelta(minutes=offset),
                )
            )

        rows = homepage_match_rows(self.preloaded_competition(competition))

        self.assertEqual(len(rows), HOMEPAGE_MATCH_LIMIT)
        self.assertEqual([row["state_kind"] for row in rows[:2]], ["live", "live"])
        self.assertEqual(rows[2]["state_kind"], "upcoming")

    def test_today_results_rank_before_tomorrow_fixtures_after_matchday_finishes(self):
        competition, stage = self.make_competition()
        home, away = self.make_entries(competition)
        todays_result = self.finish_match(
            self.make_match(
                competition,
                stage,
                home,
                away,
                scheduled_at=self.today_midday - timedelta(minutes=10),
            )
        )
        tomorrow = self.make_match(
            competition,
            stage,
            home,
            away,
            scheduled_at=self.today_midday + timedelta(days=1),
        )

        selected = homepage_matches_for_competition(
            self.preloaded_competition(competition)
        )

        self.assertEqual([match.pk for match in selected[:2]], [todays_result.pk, tomorrow.pk])

    def test_non_matchday_prefers_nearest_future_fixture_before_recent_result(self):
        competition, stage = self.make_competition()
        home, away = self.make_entries(competition)
        older_result = self.finish_match(
            self.make_match(
                competition,
                stage,
                home,
                away,
                scheduled_at=self.now - timedelta(days=1),
            )
        )
        future = self.make_match(
            competition,
            stage,
            home,
            away,
            scheduled_at=self.now + timedelta(days=2),
        )

        selected = homepage_matches_for_competition(
            self.preloaded_competition(competition)
        )

        self.assertEqual([match.pk for match in selected[:2]], [future.pk, older_result.pk])

    def test_homepage_selection_is_competition_scoped_and_supports_singles_and_teams(self):
        singles, singles_stage = self.make_competition(name="Singles live card")
        singles_home, singles_away = self.make_entries(singles, prefix="Singles")
        singles_match = self.make_match(
            singles,
            singles_stage,
            singles_home,
            singles_away,
            scheduled_at=self.now + timedelta(hours=1),
        )
        teams, teams_stage = self.make_competition(
            name="Teams live card",
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        teams_home, teams_away = self.make_entries(teams, prefix="Teams")
        teams_match = self.make_match(
            teams,
            teams_stage,
            teams_home,
            teams_away,
            scheduled_at=self.now + timedelta(hours=2),
        )

        selected_singles = homepage_matches_for_competition(
            self.preloaded_competition(singles)
        )
        team_rows = homepage_match_rows(self.preloaded_competition(teams))

        self.assertEqual([match.pk for match in selected_singles], [singles_match.pk])
        self.assertNotIn(teams_match.pk, [match.pk for match in selected_singles])
        self.assertEqual(team_rows[0]["match"].home_participant_display_name, "Teams Home Team")
        self.assertEqual(team_rows[0]["match"].away_participant_display_name, "Teams Away Team")

    def test_home_with_no_matches_has_no_empty_card_match_section(self):
        competition, _stage = self.make_competition(name="No fixture competition")

        response = self.client.get(reverse("pool:home"))

        self.assertContains(response, competition.name)
        self.assertNotContains(response, 'class="pool-home-match-list"')
        self.assertContains(response, "View competition")

    def test_stream_badge_markup_and_truncation_rules_remain_available(self):
        competition, stage = self.make_competition()
        home, away = self.make_entries(competition)
        self.make_match(
            competition,
            stage,
            home,
            away,
            scheduled_at=self.now + timedelta(hours=1),
            stream_url="https://example.test/live-stream",
        )

        response = self.client.get(reverse("pool:home"))
        stylesheet = Path(finders.find("pool/css/pool.css")).read_text(encoding="utf-8")

        self.assertContains(response, 'class="pool-stream-badge"')
        self.assertIn("text-overflow: ellipsis", stylesheet)
        self.assertIn(".pool-stream-badge", stylesheet)
        self.assertIn("min-width: 0", stylesheet)
