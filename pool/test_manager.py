"""HTTP-level access and visibility coverage for the pool manager."""

from allauth.socialaccount.models import SocialApp
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionMembership,
    PoolCompetitionStage,
    PoolMatch,
    PoolPlayer,
    PoolTeam,
)
from .services import record_rack, start_match


User = get_user_model()


class PoolManagerTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="pool-manager-owner",
            password="test-password",
        )
        self.other_owner = User.objects.create_user(
            username="other-pool-owner",
            password="test-password",
        )
        self.admin = User.objects.create_user(
            username="pool-competition-admin",
            password="test-password",
        )
        self.ordinary = User.objects.create_user(
            username="ordinary-pool-user",
            password="test-password",
        )
        self.superuser = User.objects.create_superuser(
            username="pool-management-superuser",
            email="pool-management-superuser@example.test",
            password="test-password",
        )
        self._competition_number = 0

    def make_competition(self, *, owner=None, **overrides):
        self._competition_number += 1
        values = {
            "name": f"Managed Pool Competition {self._competition_number}",
            "slug": f"managed-pool-competition-{self._competition_number}",
            "owner": self.owner if owner is None else owner,
            "visibility": PoolCompetition.Visibility.DRAFT,
            "operational_state": PoolCompetition.OperationalState.UPCOMING,
        }
        values.update(overrides)
        return PoolCompetition.objects.create(**values)

    def make_match(self, competition):
        stage = competition.stages.get(
            stage_type=PoolCompetitionStage.StageType.LEAGUE
        )
        home = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name=f"Home {competition.pk}"),
        )
        away = PoolCompetitionEntry.objects.create(
            competition=competition,
            player=PoolPlayer.objects.create(full_name=f"Away {competition.pk}"),
        )
        return PoolMatch.objects.create(
            competition=competition,
            stage=stage,
            home_entry=home,
            away_entry=away,
            scheduled_at=timezone.now(),
        )


class PoolManagerAccessTests(PoolManagerTestCase):
    def test_anonymous_manager_access_redirects_to_manual_login_with_next(self):
        response = self.client.get(reverse("pool:manage_dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/auth/login-page/?next=/pool/manage/")

    def test_local_manager_login_rendering_does_not_require_social_app(self):
        SocialApp.objects.all().delete()

        response = self.client.get(reverse("pool:manage_dashboard"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="signInForm"')
        self.assertNotContains(response, "Continue with Google")

    def test_ordinary_user_cannot_open_manager_dashboard(self):
        self.make_competition()
        self.client.force_login(self.ordinary)

        response = self.client.get(reverse("pool:manage_dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_owner_and_admin_only_see_and_open_assigned_competitions(self):
        owned = self.make_competition()
        other = self.make_competition(owner=self.other_owner)
        PoolCompetitionMembership.objects.create(
            competition=owned,
            user=self.admin,
            role=PoolCompetitionMembership.Role.ADMIN,
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse("pool:manage_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, owned.name)
        self.assertNotContains(response, other.name)
        self.assertEqual(
            self.client.get(
                reverse("pool:manage_competition_dashboard", args=[other.slug])
            ).status_code,
            404,
        )

        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(
                reverse("pool:manage_competition_dashboard", args=[owned.slug])
            ).status_code,
            200,
        )

    def test_superuser_can_manage_every_competition(self):
        first = self.make_competition()
        second = self.make_competition(owner=self.other_owner)
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("pool:manage_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, first.name)
        self.assertContains(response, second.name)

    def test_match_identifier_cannot_cross_competition_management_scope(self):
        owned = self.make_competition()
        other = self.make_competition(owner=self.other_owner)
        other_match = self.make_match(other)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse(
                "pool:manage_match",
                args=[owned.slug, other_match.pk],
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_only_owner_can_manage_competition_administrators(self):
        competition = self.make_competition()
        PoolCompetitionMembership.objects.create(
            competition=competition,
            user=self.admin,
            role=PoolCompetitionMembership.Role.ADMIN,
        )
        prospective_admin = User.objects.create_user(
            username="new-pool-competition-admin",
            email="new-admin@example.test",
            password="test-password",
        )

        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(
                reverse("pool:manage_competition_admins", args=[competition.slug])
            ).status_code,
            403,
        )

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("pool:manage_competition_admin_add", args=[competition.slug]),
            {"identifier": prospective_admin.username},
        )
        self.assertRedirects(
            response,
            reverse("pool:manage_competition_admins", args=[competition.slug]),
        )
        self.assertTrue(
            PoolCompetitionMembership.objects.filter(
                competition=competition,
                user=prospective_admin,
                role=PoolCompetitionMembership.Role.ADMIN,
                is_active=True,
            ).exists()
        )

        owner_membership = PoolCompetitionMembership.objects.get(
            competition=competition,
            user=self.owner,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "pool:manage_competition_admin_toggle",
                    args=[competition.slug, owner_membership.pk],
                )
            ).status_code,
            404,
        )

    def test_manager_pages_render_for_assigned_singles_and_team_competitions(self):
        singles = self.make_competition()
        singles_match = self.make_match(singles)
        teams = self.make_competition(
            participant_mode=PoolCompetition.ParticipantMode.TEAMS,
        )
        team_entry = PoolCompetitionEntry.objects.create(
            competition=teams,
            team=PoolTeam.objects.create(name="Manager Template Team"),
        )
        self.client.force_login(self.owner)

        singles_urls = (
            reverse("pool:manage_competition_information", args=[singles.slug]),
            reverse("pool:manage_competition_entries", args=[singles.slug]),
            reverse("pool:manage_competition_entry_create", args=[singles.slug]),
            reverse("pool:manage_competition_stages", args=[singles.slug]),
            reverse("pool:manage_competition_stage_create", args=[singles.slug]),
            reverse("pool:manage_competition_fixtures", args=[singles.slug]),
            reverse("pool:manage_competition_fixture_create", args=[singles.slug]),
            reverse("pool:manage_match", args=[singles.slug, singles_match.pk]),
            reverse("pool:manage_competition_admins", args=[singles.slug]),
            reverse("pool:manage_competition_admin_add", args=[singles.slug]),
        )
        team_urls = (
            reverse("pool:manage_competition_teams", args=[teams.slug]),
            reverse("pool:manage_competition_team_create", args=[teams.slug]),
            reverse(
                "pool:manage_competition_team_roster",
                args=[teams.slug, team_entry.pk],
            ),
            reverse(
                "pool:manage_competition_team_roster_add",
                args=[teams.slug, team_entry.pk],
            ),
        )

        for url in singles_urls + team_urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class PoolCompetitionVisibilityTests(PoolManagerTestCase):
    def test_draft_stays_private_but_published_completed_history_is_public(self):
        draft = self.make_competition(
            name="Private Pool Event",
            slug="private-pool",
        )

        completed = self.make_competition(
            name="Published Completed Pool Event",
            slug="published-completed-pool",
            visibility=PoolCompetition.Visibility.PUBLISHED,
            operational_state=PoolCompetition.OperationalState.COMPLETED,
            default_race_target=1,
        )

        completed_match = self.make_match(completed)
        start_match(completed_match)
        record_rack(
            completed_match,
            winner=completed_match.home_entry,
        )

        # Draft competitions must never appear publicly.
        home = self.client.get(reverse("pool:home"))
        self.assertNotContains(home, draft.name)

        # Completed published competitions belong under the Finished filter.
        finished_home = self.client.get(
            reverse("pool:home"),
            {"status": "finished"},
        )
        self.assertContains(finished_home, completed.name)
        self.assertNotContains(finished_home, draft.name)

        # Completed published competitions must also remain visible under All.
        all_home = self.client.get(
            reverse("pool:home"),
            {"status": "all"},
        )
        self.assertContains(all_home, completed.name)
        self.assertNotContains(all_home, draft.name)

        # Draft competition detail remains private to anonymous users.
        self.assertEqual(
            self.client.get(
                reverse(
                    "pool:competition_detail",
                    args=[draft.slug],
                )
            ).status_code,
            404,
        )

        # Published completed competition history remains publicly accessible.
        self.assertEqual(
            self.client.get(
                reverse(
                    "pool:competition_detail",
                    args=[completed.slug],
                )
            ).status_code,
            200,
        )

        self.assertEqual(
            self.client.get(
                reverse(
                    "pool:match_detail",
                    args=[completed_match.pk],
                )
            ).status_code,
            200,
        )

        # Competition owner can still access their draft competition.
        self.client.force_login(self.owner)

        self.assertEqual(
            self.client.get(
                reverse(
                    "pool:competition_detail",
                    args=[draft.slug],
                )
            ).status_code,
            200,
        )
