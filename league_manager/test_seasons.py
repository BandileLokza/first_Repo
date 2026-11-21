"""Regression tests for archived football season HTTP permissions."""

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from allauth.socialaccount.models import SocialApp

from league_manager.models import LeagueMembership
from league_manager.permissions import can_control_match, can_manage_team_lineup
from leagues.models import Competition, Stage
from leagues.seasons import archive_competition_season
from matches.models import Match, MatchAdminAssignment, Player
from teams.models import Team, TeamCoachAssignment


User = get_user_model()


class ArchivedFootballSeasonReadOnlyTests(TestCase):
    def setUp(self):
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test-client",
            secret="test-secret",
        )
        social_app.sites.add(Site.objects.get_current())

        self.superuser = User.objects.create_superuser(
            username="archive-superuser",
            email="archive-superuser@example.com",
            password="pass",
        )
        self.owner = User.objects.create_user(username="archive-owner", password="pass")
        self.agent = User.objects.create_user(username="archive-agent", password="pass")
        self.coach = User.objects.create_user(username="archive-coach", password="pass")
        self.staff_user = User.objects.create_user(
            username="archive-staff",
            password="pass",
            is_staff=True,
        )
        self.league = Competition.objects.create(
            name="Read-only League",
            slug="read-only-league",
            season="2026",
            format_type="TABLE",
        )
        self.stage = Stage.objects.create(
            competition=self.league,
            name="League Stage",
            stage_type="LEAGUE",
            order=1,
        )
        self.home_team = Team.objects.create(name="Home", competition=self.league)
        self.away_team = Team.objects.create(name="Away", competition=self.league)
        self.player = Player.objects.create(name="Home Player", team=self.home_team)
        self.match = Match.objects.create(
            competition=self.league,
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            match_date=timezone.now(),
            status="FINISHED",
        )
        LeagueMembership.objects.create(
            user=self.owner,
            competition=self.league,
            role=LeagueMembership.Role.OWNER,
        )
        LeagueMembership.objects.create(
            user=self.agent,
            competition=self.league,
            role=LeagueMembership.Role.AGENT,
        )
        MatchAdminAssignment.objects.create(match=self.match, admin=self.agent)
        TeamCoachAssignment.objects.create(team=self.home_team, user=self.coach)
        archive_competition_season(self.league, actor=self.superuser)

    def test_normal_manager_and_legacy_staff_posts_are_rejected(self):
        self.client.force_login(self.owner)
        # Historical manager pages remain readable, but mutation is centrally
        # blocked by get_managed_league for every archived-season POST.
        self.assertEqual(
            self.client.get(
                reverse("league_manager:league_dashboard", args=[self.league.slug])
            ).status_code,
            200,
        )
        protected_manager_urls = [
            (
                reverse("league_manager:team_create", args=[self.league.slug]),
                {"name": "Blocked Team"},
            ),
            (
                reverse("league_manager:player_create", args=[self.league.slug]),
                {
                    "name": "Blocked Player",
                    "team": self.home_team.id,
                    "starting_goals": 0,
                },
            ),
            (
                reverse("league_manager:fixture_create", args=[self.league.slug]),
                {},
            ),
            (
                reverse("league_manager:result_update", args=[self.league.slug]),
                {},
            ),
        ]
        for url, data in protected_manager_urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, data).status_code, 403)

        self.client.force_login(self.staff_user)
        legacy_player_url = reverse("add_player", args=[self.home_team.id])
        self.assertEqual(
            self.client.post(
                legacy_player_url,
                {"name": "Blocked Legacy Player", "starting_goals": 0},
            ).status_code,
            403,
        )
        self.assertFalse(Player.objects.filter(name="Blocked Legacy Player").exists())

    def test_agent_and_coach_mutation_paths_are_rejected_when_archived(self):
        # Manufacture an otherwise mutable state after archival only to prove
        # the archive guard itself—not ordinary terminal-match rules—rejects it.
        self.match.status = "LIVE"
        self.match.phase = "SECOND_HALF"
        self.match.phase_start_time = timezone.now()
        self.match.save(update_fields=["status", "phase", "phase_start_time"])
        # HTTP handlers fetch a fresh match/competition pair.  Do the same for
        # the direct permission assertion instead of retaining the pre-archive
        # relation cached by this test fixture.
        self.match = Match.objects.select_related("competition").get(pk=self.match.pk)

        self.client.force_login(self.agent)
        self.assertFalse(can_control_match(self.agent, self.match))
        self.assertEqual(
            self.client.post(
                reverse("add_goal", args=[self.match.id, self.home_team.id]),
                {},
            ).status_code,
            403,
        )

        self.match.status = "UPCOMING"
        self.match.phase = "NONE"
        self.match.phase_start_time = None
        self.match.lineups_locked = False
        self.match.save(
            update_fields=["status", "phase", "phase_start_time", "lineups_locked"]
        )
        self.client.force_login(self.coach)
        self.assertFalse(can_manage_team_lineup(self.coach, self.match, self.home_team))
        self.assertEqual(
            self.client.post(
                reverse("add_lineup", args=[self.match.id, self.home_team.id]),
                {},
            ).status_code,
            403,
        )

        self.assertTrue(can_control_match(self.superuser, self.match))

    def test_only_superuser_can_use_archive_route_and_public_history_remains_available(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(
                reverse("league_manager:archive_season", args=[self.league.slug])
            ).status_code,
            403,
        )

        public_response = self.client.get(reverse("league_detail", args=[self.league.slug]))
        self.assertEqual(public_response.status_code, 200)
        self.assertContains(public_response, "Archived season — historical data is read-only.")
