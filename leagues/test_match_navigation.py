from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leagues.models import Competition, Stage
from matches.models import Match
from teams.models import Team


class PublicFootballMatchNavigationTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(
            name="Navigation league", slug="navigation-league", season="2026", format_type="TABLE"
        )
        self.stage = Stage.objects.create(
            competition=self.competition, name="League", stage_type="LEAGUE", order=1
        )
        self.home = Team.objects.create(competition=self.competition, name="Home team")
        self.away = Team.objects.create(competition=self.competition, name="Away team")

    def make_finished(self, *, gameweek, offset):
        away = Team.objects.create(
            competition=self.competition, name=f"Away team {offset}"
        )
        return Match.objects.create(
            competition=self.competition, stage=self.stage, home_team=self.home,
            away_team=away, gameweek=gameweek, status="FINISHED",
            home_score=offset, away_score=0, match_date=timezone.now() + timedelta(days=offset),
        )

    def test_cards_expose_match_navigation_and_independent_team_links(self):
        match = self.make_finished(gameweek=1, offset=1)
        response = self.client.get(reverse("league_detail", args=[self.competition.slug]))
        self.assertContains(response, 'data-match-url="%s"' % reverse("match_detail", args=[match.id]))
        self.assertContains(response, reverse("team_profile", args=[self.competition.slug, self.home.id]))
        self.assertContains(response, "football-match-card")
        self.assertContains(response, 'id="leagueTabs"')
        detail = self.client.get(reverse("match_detail", args=[match.pk]))
        league_url = reverse("league_detail", args=[self.competition.slug])
        self.assertContains(detail, f'href="{league_url}" class="history-back" data-history-back')

    def test_results_group_gameweeks_and_keep_null_gameweeks_visible(self):
        self.make_finished(gameweek=2, offset=2)
        first = self.make_finished(gameweek=1, offset=1)
        legacy = self.make_finished(gameweek=None, offset=3)
        response = self.client.get(reverse("league_results", args=[self.competition.slug]))
        content = response.content.decode()
        self.assertLess(content.index("Gameweek 2"), content.index("Gameweek 1"))
        self.assertIn("Other Results", content)
        self.assertLess(content.index(str(first.home_score)), content.index(str(legacy.home_score)))
