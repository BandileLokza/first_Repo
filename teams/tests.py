from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from league_manager.models import LeagueMembership
from leagues.models import Competition
from teams.models import Team, TeamCoachAssignment


User = get_user_model()


class TeamCoachAssignmentManagementTests(TestCase):
    def setUp(self):
        self.league = Competition.objects.create(
            name="Coach League",
            slug="coach-league",
            season="2026",
        )
        self.team = Team.objects.create(name="Team One", competition=self.league)
        self.second_team = Team.objects.create(name="Team Two", competition=self.league)
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.admin = User.objects.create_user(username="admin", password="pass")
        self.coach = User.objects.create_user(username="coach", password="pass")
        LeagueMembership.objects.create(
            user=self.owner,
            competition=self.league,
            role=LeagueMembership.Role.OWNER,
        )
        LeagueMembership.objects.create(
            user=self.admin,
            competition=self.league,
            role=LeagueMembership.Role.ADMIN,
        )

    def test_only_owner_can_assign_and_remove_team_coaches(self):
        assign_url = reverse(
            "league_manager:team_coach_assign",
            args=[self.league.slug, self.team.id],
        )
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.post(assign_url, {"identifier": self.coach.username}).status_code,
            403,
        )

        self.client.force_login(self.owner)
        response = self.client.post(assign_url, {"identifier": self.coach.username})
        self.assertRedirects(
            response,
            reverse(
                "league_manager:team_coach_list",
                args=[self.league.slug, self.team.id],
            ),
        )
        assignment = TeamCoachAssignment.objects.get(team=self.team, user=self.coach)

        TeamCoachAssignment.objects.create(team=self.second_team, user=self.coach)
        self.assertEqual(
            TeamCoachAssignment.objects.filter(user=self.coach).count(),
            2,
        )

        remove_url = reverse(
            "league_manager:team_coach_remove",
            args=[self.league.slug, self.team.id, assignment.id],
        )
        self.assertEqual(self.client.post(remove_url).status_code, 302)
        self.assertFalse(TeamCoachAssignment.objects.filter(pk=assignment.id).exists())
