"""Public league-statistics identity regression coverage."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leagues.models import Competition, Stage
from matches.models import Goal, Match, Player
from teams.models import Team


class LeagueStatsIdentityTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(
            name="Manzini Super League",
            slug="manzini-super-league-stats",
            season="2026/2027",
        )
        stage = Stage.objects.create(
            competition=self.competition,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        home = Team.objects.create(name="Stats Home", competition=self.competition)
        away = Team.objects.create(name="Stats Away", competition=self.competition)
        player = Player.objects.create(name="Top Scorer", team=home)
        match = Match.objects.create(
            competition=self.competition,
            stage=stage,
            home_team=home,
            away_team=away,
            status="LIVE",
            phase="FIRST_HALF",
            phase_start_time=timezone.now(),
            match_date=timezone.now(),
        )
        Goal.objects.create(match=match, team=home, player=player, minute=12)

    def test_stats_tab_shows_league_name_and_season_without_changing_scorers(self):
        response = self.client.get(reverse("league_stats", args=[self.competition.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manzini Super League")
        self.assertContains(response, "Season 2026/2027")
        self.assertContains(response, "Top Scorers")
        self.assertContains(response, "Top Scorer")
        self.assertContains(response, ">1<", html=False)
