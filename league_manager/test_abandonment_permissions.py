from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from league_manager.models import LeagueMembership, OrganisationAdministrator, RegionalAdministrator
from league_manager.permissions import can_manage_abandonment_decisions
from leagues.models import Competition, CompetitionSeries, Region, Stage
from matches.abandonment import reopen_abandoned_resolution, void_abandoned_match
from matches.models import Match
from teams.models import Team


class AbandonmentPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.superuser = users.create_superuser(username="resolution-super")
        cls.regional = users.create_user(username="regional-resolution")
        cls.other_region = users.create_user(username="foreign-region-resolution")
        cls.ewfa = users.create_user(username="ewfa-resolution")
        cls.zonal = users.create_user(username="zonal-resolution")
        cls.ple = users.create_user(username="ple-resolution")
        cls.owner = users.create_user(username="resolution-league-owner", is_staff=True)
        cls.league_admin = users.create_user(username="resolution-league-admin")
        cls.agent = users.create_user(username="resolution-agent")
        cls.owner.user_permissions.add(Permission.objects.get(content_type__app_label="matches", codename="view_match"))
        region = Region.objects.create(name="Scope A", slug="abandonment-scope-a")
        other = Region.objects.create(name="Scope B", slug="abandonment-scope-b")
        RegionalAdministrator.objects.create(user=cls.regional, region=region)
        RegionalAdministrator.objects.create(user=cls.other_region, region=other)
        for code, actor in (("EWFA", cls.ewfa), ("ZONAL", cls.zonal), ("PLE", cls.ple)):
            OrganisationAdministrator.objects.create(user=actor, organisation=code)

        cls.matches = {}
        for code in ("REGIONAL", "EWFA", "ZONAL", "PLE"):
            slug = f"abandonment-test-{code.lower()}"
            series = CompetitionSeries.objects.create(name=slug, slug=slug, league_type=code, region=region if code == "REGIONAL" else None)
            competition = Competition.objects.create(name=slug, slug=slug, series=series, season="2026/27", format_type="TABLE")
            stage = Stage.objects.create(name="League", competition=competition, stage_type="LEAGUE", order=1)
            home = Team.objects.create(name="Home", competition=competition)
            away = Team.objects.create(name="Away", competition=competition)
            match = Match.objects.create(competition=competition, stage=stage, home_team=home, away_team=away, status="LIVE", phase="FIRST_HALF")
            match.abandon_match("Unsafe field")
            cls.matches[code] = match
            for actor, role in ((cls.owner, "OWNER"), (cls.league_admin, "ADMIN"), (cls.agent, "AGENT")):
                LeagueMembership.objects.create(user=actor, competition=competition, role=role)

    def resolution_url(self, match):
        return reverse("league_manager:resolve_abandoned_match", args=[match.competition.slug, match.pk])

    def test_scope_matrix_and_archived_superuser_access(self):
        expected = {
            "REGIONAL": {self.superuser, self.regional},
            "EWFA": {self.superuser, self.ewfa},
            "ZONAL": {self.superuser, self.zonal},
            "PLE": {self.superuser, self.ple, self.owner, self.league_admin},
        }
        actors = (self.superuser, self.regional, self.other_region, self.ewfa, self.zonal, self.ple, self.owner, self.league_admin, self.agent)
        for code, match in self.matches.items():
            for actor in actors:
                with self.subTest(code=code, actor=actor.username):
                    self.assertEqual(can_manage_abandonment_decisions(actor, match.competition), actor in expected[code])
        league = self.matches["REGIONAL"].competition
        league.is_archived = True
        self.assertFalse(can_manage_abandonment_decisions(self.regional, league))
        self.assertTrue(can_manage_abandonment_decisions(self.superuser, league))
        OrganisationAdministrator.objects.filter(user=self.zonal).update(is_active=False)
        self.assertFalse(can_manage_abandonment_decisions(self.zonal, self.matches["ZONAL"].competition))

    def test_buttons_and_routes_use_the_same_scope(self):
        for code, administrator in (("REGIONAL", self.regional), ("EWFA", self.ewfa), ("ZONAL", self.zonal)):
            match = self.matches[code]
            resolution = self.resolution_url(match)
            detail = reverse("match_detail", args=[match.pk])
            fixtures = reverse("league_manager:fixture_list", args=[match.competition.slug]) + "?status=completed"
            for actor, allowed in ((self.owner, False), (self.league_admin, False), (administrator, True), (self.superuser, True)):
                with self.subTest(code=code, actor=actor.username):
                    self.client.force_login(actor)
                    for screen in (detail, fixtures):
                        response = self.client.get(screen)
                        (self.assertContains if allowed else self.assertNotContains)(response, f'href="{resolution}"')
                    response = self.client.get(resolution)
                    if allowed:
                        self.assertContains(response, f'href="{detail}" class="history-back" data-history-back')
                    else:
                        self.assertEqual(response.status_code, 403)
                        self.assertEqual(self.client.post(resolution, {"decision": "VOID"}).status_code, 403)

    def test_services_and_staff_route_cannot_bypass_scope(self):
        match = self.matches["REGIONAL"]
        with self.assertRaises(PermissionDenied):
            void_abandoned_match(match_id=match.pk, actor=self.owner)
        void_abandoned_match(match_id=match.pk, actor=self.regional)
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse("match_detail", args=[match.pk])), self.resolution_url(match))
        self.assertNotContains(self.client.get(reverse("admin:matches_match_change", args=[match.pk])), "Resolve abandoned match")
        self.assertEqual(self.client.get(reverse("admin:matches_match_resolve_abandoned", args=[match.pk])).status_code, 403)
        self.assertEqual(self.client.post(self.resolution_url(match), {"action": "reopen"}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            reopen_abandoned_resolution(match_id=match.pk, actor=self.owner)
        self.client.force_login(self.regional)
        self.assertContains(self.client.get(reverse("match_detail", args=[match.pk])), "Review / Correct Decision")
        reopened, _ = reopen_abandoned_resolution(match_id=match.pk, actor=self.regional)
        self.assertEqual(reopened.abandonment_resolution, "")
