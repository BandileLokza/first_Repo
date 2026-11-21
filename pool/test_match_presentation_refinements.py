"""Regression coverage for public fixture presentation refinements."""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionEntryLife,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolTeam,
)


User = get_user_model()


class PoolMatchPresentationRefinementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="presentation-owner",
            password="test-password",
        )
        self._number = 0

    def make_competition(self, **overrides):
        self._number += 1
        values = {
            "name": f"Presentation event {self._number}",
            "slug": f"presentation-event-{self._number}",
            "owner": self.owner,
            "visibility": PoolCompetition.Visibility.PUBLISHED,
            "participant_mode": PoolCompetition.ParticipantMode.SINGLES,
            "format_type": PoolCompetition.FormatType.KNOCKOUT,
            "operational_state": PoolCompetition.OperationalState.UPCOMING,
            "default_race_target": 5,
        }
        values.update(overrides)
        competition = PoolCompetition.objects.create(**values)
        stage = competition.stages.get(
            stage_type=(
                PoolCompetitionStage.StageType.KNOCKOUT
                if competition.format_type == PoolCompetition.FormatType.KNOCKOUT
                else PoolCompetitionStage.StageType.LEAGUE
            )
        )
        return competition, stage

    @staticmethod
    def make_player_entry(competition, name):
        return PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name=name),
        )

    def make_life_match(self, *, status=PoolMatch.Status.UPCOMING, stream_url=""):
        competition, stage = self.make_competition(allow_two_lives=True)
        home = self.make_player_entry(competition, "Life Home")
        away = self.make_player_entry(competition, "Life Away")
        home_life = PoolCompetitionEntryLife.objects.get(
            competition_entry=home,
            life_number=1,
        )
        away_life = PoolCompetitionEntryLife.objects.create(
            competition_entry=away,
            life_number=2,
        )
        values = {
            "competition": competition,
            "stage": stage,
            "home_entry": home,
            "away_entry": away,
            "home_entry_life": home_life,
            "away_entry_life": away_life,
            "race_target": 5,
            "scheduled_at": timezone.now(),
            "status": status,
            "stream_url": stream_url,
        }
        if status == PoolMatch.Status.FINISHED:
            values.update(
                {
                    "winner": home,
                    "winner_entry_life": home_life,
                    "result_method": PoolMatch.ResultMethod.WALKOVER,
                }
            )
        return competition, PoolMatch.objects.create(**values)

    def test_match_participant_labels_show_independent_lives_only_when_persisted(self):
        competition, pool_match = self.make_life_match()

        self.assertEqual(pool_match.home_participant_display_name, "Life Home — Life 1")
        self.assertEqual(pool_match.away_participant_display_name, "Life Away — Life 2")

        fixtures = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]),
            {"tab": "fixtures"},
        )
        self.assertContains(fixtures, "Life Home — Life 1")
        self.assertContains(fixtures, "Life Away — Life 2")

        detail = self.client.get(reverse("pool:match_detail", args=[pool_match.pk]))
        self.assertContains(detail, "Life Home — Life 1")
        self.assertContains(detail, "Life Away — Life 2")

    def test_results_and_home_recent_results_keep_life_identity(self):
        competition, pool_match = self.make_life_match(status=PoolMatch.Status.FINISHED)

        results = self.client.get(
            reverse("pool:competition_detail", args=[competition.slug]),
            {"tab": "results"},
        )
        self.assertContains(results, "Life Home — Life 1")
        self.assertContains(results, "Life Away — Life 2")
        self.assertContains(results, "Winner: Life Home — Life 1")

        home = self.client.get(reverse("pool:home"))
        self.assertContains(home, "Life Home — Life 1")
        self.assertContains(home, "Life Away — Life 2")
        detail = self.client.get(reverse("pool:match_detail", args=[pool_match.pk]))
        self.assertContains(detail, "Winner:")
        self.assertContains(detail, "Life Home — Life 1")

    def test_ordinary_and_team_matches_keep_their_existing_names(self):
        singles, singles_stage = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE,
        )
        singles_home = self.make_player_entry(singles, "Ordinary Home")
        singles_away = self.make_player_entry(singles, "Ordinary Away")
        ordinary_match = PoolMatch.objects.create(
            competition=singles,
            stage=singles_stage,
            home_entry=singles_home,
            away_entry=singles_away,
            race_target=5,
        )
        self.assertEqual(ordinary_match.home_participant_display_name, "Ordinary Home")
        self.assertEqual(ordinary_match.away_participant_display_name, "Ordinary Away")

        teams, team_stage = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
            format_type=PoolCompetition.FormatType.LEAGUE,
        )
        home_team = PoolCompetitionEntry.objects.create(
            competition=teams,
            team=PoolTeam.objects.create(name="Team Home"),
        )
        away_team = PoolCompetitionEntry.objects.create(
            competition=teams,
            team=PoolTeam.objects.create(name="Team Away"),
        )
        team_match = PoolMatch.objects.create(
            competition=teams,
            stage=team_stage,
            home_entry=home_team,
            away_entry=away_team,
            race_target=5,
        )
        self.assertEqual(team_match.home_participant_display_name, "Team Home")
        self.assertEqual(team_match.away_participant_display_name, "Team Away")

    def test_stream_badges_render_only_for_configured_public_matches(self):
        competition, streamed_match = self.make_life_match(
            stream_url="https://example.test/live",
        )
        unstreamed_competition, unstreamed_match = self.make_life_match()
        live_competition, live_match = self.make_life_match(
            status=PoolMatch.Status.LIVE,
            stream_url="https://example.test/live-now",
        )
        completed_competition, completed_match = self.make_life_match(
            status=PoolMatch.Status.FINISHED,
            stream_url="https://example.test/replay",
        )

        self.assertTrue(streamed_match.has_stream)
        self.assertFalse(unstreamed_match.has_stream)
        self.assertTrue(live_match.has_stream)
        self.assertTrue(completed_match.has_stream)

        home = self.client.get(reverse("pool:home"))
        self.assertContains(home, "Stream available")
        self.assertContains(
            self.client.get(
                reverse("pool:competition_detail", args=[competition.slug]),
                {"tab": "fixtures"},
            ),
            "Stream available",
        )
        self.assertContains(
            self.client.get(
                reverse("pool:competition_detail", args=[live_competition.slug]),
                {"tab": "fixtures"},
            ),
            "Stream available",
        )
        self.assertContains(
            self.client.get(
                reverse("pool:competition_detail", args=[completed_competition.slug]),
                {"tab": "results"},
            ),
            "Stream available",
        )
        detail = self.client.get(reverse("pool:match_detail", args=[streamed_match.pk]))
        self.assertContains(detail, "Watch stream")
        self.assertNotContains(
            self.client.get(reverse("pool:match_detail", args=[unstreamed_match.pk])),
            "Watch stream",
        )
        self.assertNotContains(
            self.client.get(
                reverse("pool:competition_detail", args=[unstreamed_competition.slug]),
                {"tab": "fixtures"},
            ),
            "Stream available",
        )

    def test_competition_overview_is_a_closed_semantic_disclosure(self):
        competition, _stage = self.make_competition(
            description="The overview information stays available.",
        )

        response = self.client.get(reverse("pool:competition_detail", args=[competition.slug]))
        self.assertContains(response, "data-pool-competition-overview")
        self.assertContains(response, "Competition overview")
        self.assertContains(response, "View competition details")
        self.assertContains(response, "The overview information stays available.")
        self.assertNotIn("data-pool-competition-overview open", response.content.decode())

    def test_manager_fixture_filters_have_progressive_enhancement_contract(self):
        competition, stage = self.make_competition(
            format_type=PoolCompetition.FormatType.LEAGUE,
        )
        home = self.make_player_entry(competition, "Filter Home")
        away = self.make_player_entry(competition, "Filter Away")
        PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home,
            away_entry=away,
            race_target=5,
        )
        PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=away,
            away_entry=home,
            race_target=5,
            status=PoolMatch.Status.LIVE,
        )
        PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home,
            away_entry=away,
            race_target=5,
            status=PoolMatch.Status.FINISHED,
            result_method=PoolMatch.ResultMethod.WALKOVER,
            winner=home,
        )
        self.client.force_login(self.owner)
        url = reverse("pool:manage_competition_fixtures", args=[competition.slug])

        response = self.client.get(url, {"status": "live"})
        html = response.content.decode()
        self.assertContains(response, 'data-pool-match-filters')
        self.assertContains(response, 'data-pool-match-filter="live"')
        self.assertContains(response, 'data-match-status="live"')
        self.assertIn('data-match-status="upcoming" hidden', html)
        self.assertIn('pool/js/fixture_filters.js', html)
        self.assertEqual(self.client.get(url, {"status": "all"}).status_code, 200)

        source = Path("pool/static/pool/js/fixture_filters.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("history.pushState", source)
        self.assertIn("popstate", source)
        self.assertIn("dataset.matchStatus", source)
