from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import LeagueMembership
from league_manager.permissions import can_control_match
from leagues.models import Competition, Stage
from matches.models import Match, MatchAdminAssignment, MatchLineup, MatchLineupEntry, Player
from teams.models import Team, TeamCoachAssignment
from allauth.socialaccount.models import SocialApp


User = get_user_model()


class TeamCoachLineupTests(TestCase):
    def setUp(self):
        social_app = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="test-client",
            secret="test-secret",
        )
        social_app.sites.add(Site.objects.get_current())
        self.league = Competition.objects.create(
            name="Premier Test League",
            slug="premier-test-league",
            season="2026",
        )
        self.stage = Stage.objects.create(
            competition=self.league,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        self.home_team = Team.objects.create(name="Home FC", competition=self.league)
        self.away_team = Team.objects.create(name="Away FC", competition=self.league)
        self.other_team = Team.objects.create(name="Other FC", competition=self.league)
        self.match = Match.objects.create(
            competition=self.league,
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            match_date=timezone.now(),
        )

        self.home_players = [
            Player.objects.create(name=f"Home {index}", team=self.home_team)
            for index in range(1, 26)
        ]
        self.away_players = [
            Player.objects.create(name=f"Away {index}", team=self.away_team)
            for index in range(1, 12)
        ]
        self.other_player = Player.objects.create(name="Other 1", team=self.other_team)

        self.coach = User.objects.create_user(username="home-coach", password="pass")
        self.other_coach = User.objects.create_user(username="away-coach", password="pass")
        self.agent = User.objects.create_user(username="agent", password="pass")
        self.admin = User.objects.create_user(username="league-admin", password="pass")
        TeamCoachAssignment.objects.create(team=self.home_team, user=self.coach)
        TeamCoachAssignment.objects.create(team=self.away_team, user=self.other_coach)
        LeagueMembership.objects.create(
            user=self.agent,
            competition=self.league,
            role=LeagueMembership.Role.AGENT,
        )
        MatchAdminAssignment.objects.create(match=self.match, admin=self.agent)
        LeagueMembership.objects.create(
            user=self.admin,
            competition=self.league,
            role=LeagueMembership.Role.ADMIN,
        )

    def lineup_data(self, *, starters=None, substitutes=None):
        return {
            "starting_players": [
                player.id for player in (starters or self.home_players[:11])
            ],
            "substitute_players": [
                player.id for player in (substitutes or self.home_players[11:13])
            ],
        }

    def test_only_assigned_coach_can_submit_their_team_lineup(self):
        url = reverse("add_lineup", args=[self.match.id, self.home_team.id])
        self.client.force_login(self.agent)
        self.assertTrue(can_control_match(self.agent, self.match))
        self.assertEqual(self.client.post(url, self.lineup_data()).status_code, 403)

        self.client.force_login(self.other_coach)
        self.assertEqual(self.client.post(url, self.lineup_data()).status_code, 403)

        self.client.force_login(self.coach)
        response = self.client.post(url, self.lineup_data())
        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))

        lineup = MatchLineup.objects.get(match=self.match, team=self.home_team)
        self.assertEqual(lineup.entries.filter(role="STARTER").count(), 11)
        self.assertEqual(lineup.entries.filter(role="SUBSTITUTE").count(), 2)
        self.assertEqual(lineup.entries.count(), 13)

    def test_lineup_rejects_invalid_or_other_team_players(self):
        url = reverse("add_lineup", args=[self.match.id, self.home_team.id])
        self.client.force_login(self.coach)

        ten_starters = self.lineup_data(starters=self.home_players[:10])
        response = self.client.post(url, ten_starters)
        self.assertContains(response, "Select exactly 11 starting players")
        self.assertFalse(MatchLineup.objects.exists())

        duplicate = self.lineup_data(
            substitutes=[self.home_players[0], self.home_players[11]],
        )
        response = self.client.post(url, duplicate)
        self.assertContains(response, "cannot be both a starter and a substitute")
        self.assertFalse(MatchLineup.objects.exists())

        too_many_substitutes = self.lineup_data(
            substitutes=self.home_players[11:24],
        )
        response = self.client.post(url, too_many_substitutes)
        self.assertContains(response, "Select no more than 12 substitutes")
        self.assertFalse(MatchLineup.objects.exists())

        duplicate_starter = self.lineup_data()
        duplicate_starter["starting_players"].append(self.home_players[0].id)
        response = self.client.post(url, duplicate_starter)
        self.assertContains(response, "Each player can only be selected once")
        self.assertFalse(MatchLineup.objects.exists())

        foreign_player = self.lineup_data(
            substitutes=[self.other_player],
        )
        response = self.client.post(url, foreign_player)
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(MatchLineup.objects.exists())

    def test_live_transition_locks_and_admin_can_unlock_only_outside_live_play(self):
        url = reverse("add_lineup", args=[self.match.id, self.home_team.id])
        self.client.force_login(self.coach)
        self.client.post(url, self.lineup_data())

        self.match.start_first_half()
        self.match.refresh_from_db()
        self.assertTrue(self.match.lineups_locked)
        self.assertEqual(self.client.post(url, self.lineup_data()).status_code, 403)

        unlock_url = reverse("unlock_lineups", args=[self.match.id])
        self.client.force_login(self.agent)
        self.assertEqual(self.client.post(unlock_url).status_code, 403)

        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(unlock_url).status_code, 403)
        self.match.refresh_from_db()
        self.assertTrue(self.match.lineups_locked)

        self.match.status = "UPCOMING"
        self.match.phase = "NONE"
        self.match.save(update_fields=["status", "phase"])
        self.assertRedirects(
            self.client.post(unlock_url),
            reverse("match_detail", args=[self.match.id]),
        )
        self.match.refresh_from_db()
        self.assertFalse(self.match.lineups_locked)

        self.client.force_login(self.coach)
        response = self.client.post(url, self.lineup_data(substitutes=[self.home_players[12]]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MatchLineup.objects.count(), 1)

        self.match.start_first_half()
        self.match.refresh_from_db()
        self.assertTrue(self.match.lineups_locked)

    def test_match_detail_shows_public_lineups_and_only_own_coach_control(self):
        detail_url = reverse("match_detail", args=[self.match.id])
        response = self.client.get(detail_url)
        self.assertContains(response, "Lineup not submitted.", count=2)

        self.client.force_login(self.coach)
        response = self.client.get(detail_url)
        self.assertContains(
            response,
            reverse("add_lineup", args=[self.match.id, self.home_team.id]),
        )
        self.assertNotContains(
            response,
            reverse("add_lineup", args=[self.match.id, self.away_team.id]),
        )

        self.client.post(
            reverse("add_lineup", args=[self.match.id, self.home_team.id]),
            self.lineup_data(),
        )
        response = self.client.get(detail_url)
        self.assertContains(response, "Starting XI")
        self.assertContains(response, "Substitutes")
        self.assertContains(response, self.home_players[0].name)


class StartMatchConfirmationTests(TestCase):
    def setUp(self):
        self.league = Competition.objects.create(
            name="Confirmation League",
            slug="confirmation-league",
            season="2026",
        )
        self.stage = Stage.objects.create(
            competition=self.league,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        self.home_team = Team.objects.create(name="Kappa Kappa", competition=self.league)
        self.away_team = Team.objects.create(name="Mawombe Arsenal", competition=self.league)
        self.match = Match.objects.create(
            competition=self.league,
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            match_date=timezone.now(),
        )
        self.controller = User.objects.create_superuser(
            username="match-controller",
            email="controller@example.com",
            password="pass",
        )
        self.other_user = User.objects.create_user(username="unassigned", password="pass")

    def test_authorised_detail_uses_a_confirmation_modal_for_initial_start(self):
        self.client.force_login(self.controller)
        response = self.client.get(reverse("match_detail", args=[self.match.id]))

        self.assertContains(response, "Start 1st Half")
        self.assertContains(response, 'data-bs-target="#startMatchConfirmation"')
        self.assertContains(response, 'class="modal fade start-match-confirm-modal"')
        self.assertContains(response, "Start this match?")
        self.assertContains(response, "Kappa Kappa vs Mawombe Arsenal")
        self.assertContains(response, "start-match-confirm-teams")
        self.assertContains(response, "data-start-match-confirm-form")
        self.assertContains(response, reverse("start_first_half", args=[self.match.id]))
        self.assertContains(response, "data-start-match-confirm-button")
        self.assertContains(response, "Current Match Minute (optional)")
        self.assertContains(
            response,
            "Leave empty to start from 0'. If the match has already started, enter its current minute.",
        )
        self.assertContains(response, "Quick Result")

    def test_confirmed_start_posts_once_and_backend_rejects_a_duplicate(self):
        self.client.force_login(self.controller)
        start_url = reverse("start_first_half", args=[self.match.id])

        response = self.client.post(start_url)
        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "LIVE")
        self.assertEqual(self.match.phase, "FIRST_HALF")
        self.assertEqual(self.match.minute, 0)
        self.assertEqual(self.client.post(start_url).status_code, 400)

    def test_initial_minute_is_persisted_in_the_existing_server_clock(self):
        self.client.force_login(self.controller)
        start_url = reverse("start_first_half", args=[self.match.id])
        started_at = timezone.now().replace(second=0, microsecond=0)

        with patch("matches.models.timezone.now", return_value=started_at):
            response = self.client.post(start_url, {"current_minute": "10"})

        self.assertRedirects(response, reverse("match_detail", args=[self.match.id]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.minute, 10)
        self.assertEqual(
            self.match.phase_start_time,
            started_at - timedelta(minutes=10),
        )
        with patch(
            "matches.models.timezone.now",
            return_value=started_at + timedelta(minutes=5),
        ):
            self.assertEqual(self.match.get_current_minute(), 15)

        original_phase_start = self.match.phase_start_time
        self.assertEqual(
            self.client.post(start_url, {"current_minute": "0"}).status_code,
            400,
        )
        self.match.refresh_from_db()
        self.assertEqual(self.match.phase_start_time, original_phase_start)
        self.assertEqual(self.match.get_current_minute(), 10)

    def test_zero_and_invalid_initial_minutes_are_server_validated(self):
        self.client.force_login(self.controller)
        zero_match = Match.objects.create(
            competition=self.league,
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            match_date=timezone.now(),
            leg=2,
        )
        response = self.client.post(
            reverse("start_first_half", args=[zero_match.id]),
            {"current_minute": "0"},
        )
        self.assertEqual(response.status_code, 302)
        zero_match.refresh_from_db()
        self.assertEqual(zero_match.minute, 0)

        for leg, value in ((3, "-1"), (4, "not-a-minute"), (5, "46"), (6, "90+4")):
            with self.subTest(value=value):
                match = Match.objects.create(
                    competition=self.league,
                    stage=self.stage,
                    home_team=self.home_team,
                    away_team=self.away_team,
                    match_date=timezone.now(),
                    leg=leg,
                )
                response = self.client.post(
                    reverse("start_first_half", args=[match.id]),
                    {"current_minute": value},
                )
                self.assertEqual(response.status_code, 400)
                match.refresh_from_db()
                self.assertEqual(match.status, "UPCOMING")
                self.assertEqual(match.phase, "NONE")

        stoppage_match = Match.objects.create(
            competition=self.league,
            stage=self.stage,
            home_team=self.home_team,
            away_team=self.away_team,
            match_date=timezone.now(),
            leg=7,
        )
        response = self.client.post(
            reverse("start_first_half", args=[stoppage_match.id]),
            {"current_minute": "45+2"},
        )
        self.assertEqual(response.status_code, 302)
        stoppage_match.refresh_from_db()
        self.assertEqual(stoppage_match.get_display_minute(), "45+2")

    def test_custom_first_half_start_keeps_normal_half_transitions(self):
        started_at = timezone.now().replace(second=0, microsecond=0)
        with patch("matches.models.timezone.now", return_value=started_at):
            self.match.start_first_half(current_minute=10)
        with patch(
            "matches.models.timezone.now",
            return_value=started_at + timedelta(minutes=20),
        ):
            self.match.end_first_half()
        self.match.refresh_from_db()
        self.assertEqual(self.match.minute, 30)
        self.assertEqual(self.match.phase, "BREAK")

        with patch(
            "matches.models.timezone.now",
            return_value=started_at + timedelta(minutes=25),
        ):
            self.match.start_second_half()
            self.assertEqual(self.match.get_current_minute(), 45)
        with patch(
            "matches.models.timezone.now",
            return_value=started_at + timedelta(minutes=30),
        ):
            self.assertEqual(self.match.get_current_minute(), 50)

    def test_unauthorised_user_cannot_start_the_match(self):
        self.client.force_login(self.other_user)
        response = self.client.post(reverse("start_first_half", args=[self.match.id]))
        self.assertEqual(response.status_code, 403)
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "UPCOMING")
