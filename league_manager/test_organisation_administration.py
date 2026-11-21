"""Focused coverage for EWFA/PLE sections and their scoped administration."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.forms import LeagueRegionForm
from league_manager.models import (
    LeagueMembership,
    OrganisationAdministrator,
    OrganisationReportPreference,
    RegionalAdministrator,
)
from league_manager.permissions import user_manages_competition
from leagues.models import Competition, CompetitionSeries, FootballOrganisation, Region, Stage
from matches.models import Match
from teams.models import Team


User = get_user_model()


class FootballSectionTests(TestCase):
    def setUp(self):
        self.region = Region.objects.create(name="Hhohho", slug="hhohho")
        self.regional = self.make_league(
            "Hhohho Regional League",
            "hhohho-regional",
            CompetitionSeries.LeagueType.REGIONAL,
            self.region,
        )
        self.ewfa = self.make_league(
            "Women's Premier League",
            "women-premier",
            CompetitionSeries.LeagueType.EWFA,
        )
        self.ple = self.make_league(
            "Premier League",
            "premier-league",
            CompetitionSeries.LeagueType.PLE,
        )
        self.zonal = self.make_league(
            "Independent Zonal League",
            "independent-zonal",
            CompetitionSeries.LeagueType.ZONAL,
        )

    def make_league(self, name, slug, section, region=None):
        competition = Competition.objects.create(name=name, slug=slug, season="2026")
        competition.series.league_type = section
        competition.series.region = region
        competition.series.save(update_fields=["league_type", "region"])
        competition.refresh_from_db()
        return competition

    def test_sections_are_publicly_isolated_and_tabs_are_in_requested_order(self):
        response = self.client.get(reverse("league_list"))
        content = response.content.decode()

        self.assertEqual(response.context["active_directory_tab"], "regions")
        self.assertEqual(
            [response.context[name][0]["league"].id for name in ("ewfa_league_data", "ple_league_data", "zonal_league_data")],
            [self.ewfa.id, self.ple.id, self.zonal.id],
        )
        self.assertEqual(
            [content.index(label) for label in (">Regions</button>", ">EWFA</button>", ">PLE</button>", ">Zonal Leagues</button>")],
            sorted(content.index(label) for label in (">Regions</button>", ">EWFA</button>", ">PLE</button>", ">Zonal Leagues</button>")),
        )
        self.assertNotIn(self.ewfa.name, content[content.index('data-directory-panel="regions"'):content.index('data-directory-panel="ewfa"')])
        self.assertNotIn("PSL", content)
        self.assertEqual(self.regional.series.league_type, "REGIONAL")
        self.assertEqual(self.zonal.series.league_type, "ZONAL")

    def test_query_parameters_select_every_section_and_zonal_link_stays_supported(self):
        for tab in ("regions", "ewfa", "ple", "zonal"):
            response = self.client.get(f"{reverse('league_list')}?tab={tab}")
            self.assertEqual(response.context["active_directory_tab"], tab)
        self.assertEqual(
            self.client.get(reverse("league_detail", args=[self.zonal.slug])).context["back_url"],
            f"{reverse('league_list')}?tab=zonal",
        )

    def test_section_validation_requires_region_only_for_regional(self):
        regional_form = LeagueRegionForm({"league_type": "REGIONAL", "region": ""})
        self.assertFalse(regional_form.is_valid())
        self.assertIn("region", regional_form.errors)

        ewfa_form = LeagueRegionForm({"league_type": "EWFA", "region": ""})
        self.assertTrue(ewfa_form.is_valid())

        series = CompetitionSeries(name="Invalid Regional", slug="invalid-regional", league_type="REGIONAL")
        with self.assertRaises(ValidationError):
            series.full_clean()

    def test_non_regional_section_clears_stale_region(self):
        self.ewfa.series.region = self.region
        self.ewfa.series.save(update_fields=["region"])
        self.ewfa.series.refresh_from_db()
        self.assertIsNone(self.ewfa.series.region_id)


class OrganisationAdministrationTests(TestCase):
    def setUp(self):
        self.ewfa_admin = User.objects.create_user(username="ewfa-admin", password="pass")
        self.ple_admin = User.objects.create_user(username="ple-admin", password="pass")
        self.zonal_admin = User.objects.create_user(username="zonal-admin", password="pass")
        self.regional_admin = User.objects.create_user(username="regional-admin", password="pass")
        self.regular_user = User.objects.create_user(username="regular-user", password="pass")
        self.staff_user = User.objects.create_user(username="staff-user", password="pass", is_staff=True)
        self.superuser = User.objects.create_superuser(username="superuser", password="pass")
        self.owner = User.objects.create_user(username="new-owner", password="pass")
        self.region = Region.objects.create(name="Manzini", slug="manzini")
        self.ewfa = self.make_league("EWFA League", "ewfa-league", "EWFA")
        self.ple = self.make_league("PLE League", "ple-league", "PLE")
        self.zonal = self.make_league("Zonal League", "zonal-league", "ZONAL")
        self.regional = self.make_league("Manzini League", "manzini-league", "REGIONAL", self.region)
        OrganisationAdministrator.objects.create(
            user=self.ewfa_admin,
            organisation=OrganisationAdministrator.Organisation.EWFA,
        )
        OrganisationAdministrator.objects.create(
            user=self.ple_admin,
            organisation=OrganisationAdministrator.Organisation.PLE,
        )
        OrganisationAdministrator.objects.create(
            user=self.zonal_admin,
            organisation=OrganisationAdministrator.Organisation.ZONAL,
        )
        RegionalAdministrator.objects.create(user=self.regional_admin, region=self.region)

    def make_league(self, name, slug, section, region=None):
        competition = Competition.objects.create(name=name, slug=slug, season="2026")
        competition.series.league_type = section
        competition.series.region = region
        competition.series.save(update_fields=["league_type", "region"])
        return competition

    def test_administrators_only_manage_their_explicit_scopes(self):
        self.assertTrue(user_manages_competition(self.ewfa_admin, self.ewfa))
        self.assertFalse(user_manages_competition(self.ewfa_admin, self.ple))
        self.assertFalse(user_manages_competition(self.ewfa_admin, self.regional))
        self.assertTrue(user_manages_competition(self.ple_admin, self.ple))
        self.assertFalse(user_manages_competition(self.ple_admin, self.ewfa))
        self.assertTrue(user_manages_competition(self.zonal_admin, self.zonal))
        self.assertFalse(user_manages_competition(self.zonal_admin, self.ewfa))
        self.assertFalse(user_manages_competition(self.zonal_admin, self.ple))
        self.assertFalse(user_manages_competition(self.zonal_admin, self.regional))
        self.assertTrue(user_manages_competition(self.regional_admin, self.regional))
        self.assertFalse(user_manages_competition(self.regional_admin, self.ewfa))

        self.client.force_login(self.ewfa_admin)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.ewfa.slug])).status_code, 200)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.ple.slug])).status_code, 403)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.regional.slug])).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("league_manager:update_league_region", args=[self.ewfa.slug]),
                {"league_type": "PLE", "region": ""},
            ).status_code,
            403,
        )

        self.client.force_login(self.zonal_admin)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.zonal.slug])).status_code, 200)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.ewfa.slug])).status_code, 403)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.ple.slug])).status_code, 403)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.regional.slug])).status_code, 403)

    def test_only_superusers_can_appoint_zonal_administrators_in_django_admin(self):
        appointment_user = User.objects.create_user(username="appointed-zonal-admin", password="pass")
        add_url = reverse("admin:league_manager_organisationadministrator_add")
        payload = {
            "user": appointment_user.pk,
            "organisation": OrganisationAdministrator.Organisation.ZONAL,
            "is_active": "on",
            "_save": "Save",
        }

        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.get(add_url).status_code, 403)
        self.assertEqual(self.client.post(add_url, payload).status_code, 403)

        self.client.force_login(self.superuser)
        self.assertContains(self.client.get(add_url), "Zonal Leagues")
        response = self.client.post(add_url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(OrganisationAdministrator.objects.filter(
            user=appointment_user,
            organisation=OrganisationAdministrator.Organisation.ZONAL,
            is_active=True,
        ).exists())

    def test_organisation_owner_and_report_endpoints_reject_cross_scope_ids(self):
        self.client.force_login(self.ewfa_admin)
        assign = reverse(
            "league_manager:organisation_league_owner_assign",
            args=["ewfa", self.ewfa.series_id],
        )
        response = self.client.post(assign, {"identifier": self.owner.username})
        self.assertEqual(response.status_code, 302)

        foreign_assign = reverse(
            "league_manager:organisation_league_owner_assign",
            args=["ewfa", self.ple.series_id],
        )
        self.assertEqual(self.client.post(foreign_assign, {"identifier": self.owner.username}).status_code, 404)

        report = reverse("league_manager:organisation_report_export", args=["ewfa", "fixtures"])
        response = self.client.post(report, {
            "selection_mode": "CUSTOM",
            "series": [self.ple.series_id],
            "output_format": "DOCX",
            "action": "save",
        })
        self.assertEqual(response.status_code, 200)
        preference = OrganisationReportPreference.objects.get(
            user=self.ewfa_admin,
            organisation="EWFA",
            report_kind="FIXTURES",
        )
        self.assertEqual(preference.selected_series.count(), 0)
        self.assertEqual(
            self.client.get(reverse("league_manager:organisation_league_owners", args=["ple"])).status_code,
            403,
        )

        self.client.force_login(self.ple_admin)
        self.assertEqual(
            self.client.get(
                reverse("league_manager:organisation_report_export", args=["ple", "fixtures"])
            ).status_code,
            200,
        )

    def test_zonal_owner_workflow_and_reports_are_limited_to_zonal_leagues(self):
        self.client.force_login(self.zonal_admin)
        owners_url = reverse("league_manager:organisation_league_owners", args=["zonal"])
        response = self.client.get(owners_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.zonal.series.name)
        self.assertNotContains(response, self.ewfa.series.name)
        self.assertNotContains(response, self.ple.series.name)

        assign_url = reverse(
            "league_manager:organisation_league_owner_assign",
            args=["zonal", self.zonal.series_id],
        )
        self.assertEqual(self.client.post(assign_url, {"identifier": self.owner.username}).status_code, 302)
        self.assertTrue(LeagueMembership.objects.filter(
            user=self.owner,
            competition=self.zonal,
            role=LeagueMembership.Role.OWNER,
            is_active=True,
        ).exists())
        remove_url = reverse(
            "league_manager:organisation_league_owner_remove",
            args=["zonal", self.zonal.series_id, self.owner.id],
        )
        self.assertEqual(self.client.post(remove_url).status_code, 302)
        self.assertFalse(LeagueMembership.objects.filter(
            user=self.owner,
            competition=self.zonal,
            role=LeagueMembership.Role.OWNER,
            is_active=True,
        ).exists())

        foreign_assign_url = reverse(
            "league_manager:organisation_league_owner_assign",
            args=["zonal", self.ewfa.series_id],
        )
        self.assertEqual(self.client.post(foreign_assign_url, {"identifier": self.owner.username}).status_code, 404)

        fixtures_url = reverse("league_manager:organisation_report_export", args=["zonal", "fixtures"])
        injected_selection = self.client.post(fixtures_url, {
            "selection_mode": "CUSTOM",
            "series": [self.ewfa.series_id],
            "output_format": "DOCX",
            "action": "save",
        })
        self.assertEqual(injected_selection.status_code, 200)
        preference = OrganisationReportPreference.objects.get(
            user=self.zonal_admin,
            organisation="ZONAL",
            report_kind="FIXTURES",
        )
        self.assertEqual(preference.selected_series.count(), 0)

        self.assertEqual(self.client.post(fixtures_url, {
            "selection_mode": "CUSTOM",
            "series": [self.zonal.series_id],
            "output_format": "DOCX",
            "action": "save",
        }).status_code, 302)
        preference.refresh_from_db()
        self.assertEqual(preference.selection_mode, "CUSTOM")
        self.assertEqual(list(preference.selected_series.all()), [self.zonal.series])

        self.assertEqual(self.client.post(fixtures_url, {
            "selection_mode": "ALL",
            "output_format": "DOCX",
            "action": "save",
        }).status_code, 302)
        preference.refresh_from_db()
        self.assertEqual(preference.selection_mode, "ALL")
        self.assertEqual(preference.selected_series.count(), 0)

    def test_zonal_fixture_and_result_exports_support_pdf_and_docx(self):
        stage = Stage.objects.create(competition=self.zonal, name="League", stage_type="LEAGUE", order=1)
        home = Team.objects.create(name="Zonal Home", competition=self.zonal)
        away = Team.objects.create(name="Zonal Away", competition=self.zonal)
        fixture = Match.objects.create(
            competition=self.zonal,
            stage=stage,
            home_team=home,
            away_team=away,
            gameweek=1,
            status="UPCOMING",
            match_date=timezone.now(),
            leg=1,
        )
        self.client.force_login(self.zonal_admin)
        fixtures_url = reverse("league_manager:organisation_report_export", args=["zonal", "fixtures"])
        for output_format, magic in (("PDF", b"%PDF"), ("DOCX", b"PK")):
            response = self.client.post(fixtures_url, {
                "selection_mode": "ALL",
                "output_format": output_format,
                "action": "download",
            })
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(magic))

            scoped_response = self.client.post(fixtures_url, {
                "selection_mode": "ALL",
                "output_format": output_format,
                "fixture_scope": "FIRST_ROUND",
                "action": "download",
            })
            self.assertEqual(scoped_response.status_code, 200)
            self.assertTrue(scoped_response.content.startswith(magic))

        fixture.status = "FINISHED"
        fixture.home_score = 2
        fixture.away_score = 1
        fixture.save(update_fields=["status", "home_score", "away_score"])
        results_url = reverse("league_manager:organisation_report_export", args=["zonal", "results"])
        for output_format, magic in (("PDF", b"%PDF"), ("DOCX", b"PK")):
            response = self.client.post(results_url, {
                "selection_mode": "ALL",
                "output_format": output_format,
                "action": "download",
            })
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(magic))

    def test_dashboard_shows_only_authorized_organisation_navigation(self):
        self.client.force_login(self.ewfa_admin)
        response = self.client.get(reverse("league_manager:dashboard"))
        self.assertContains(response, "Manage EWFA Leagues")
        self.assertNotContains(response, "Manage PLE Leagues")
        self.assertContains(response, self.ewfa.name)
        self.assertNotContains(response, self.ple.name)

        OrganisationAdministrator.objects.create(
            user=self.ewfa_admin,
            organisation=OrganisationAdministrator.Organisation.ZONAL,
        )
        response = self.client.get(reverse("league_manager:dashboard"))
        self.assertContains(response, "Manage EWFA Leagues")
        self.assertContains(response, "Manage Zonal Leagues")
        self.assertNotContains(response, "Manage PLE Leagues")
        self.assertContains(response, self.ewfa.name)
        self.assertContains(response, self.zonal.name)
        self.assertNotContains(response, self.ple.name)

        self.client.force_login(self.zonal_admin)
        response = self.client.get(reverse("league_manager:dashboard"))
        self.assertContains(response, "Manage Zonal Leagues")
        self.assertNotContains(response, "Manage EWFA Leagues")
        self.assertNotContains(response, "Manage PLE Leagues")
        self.assertContains(response, self.zonal.name)
        self.assertNotContains(response, self.ewfa.name)
        self.assertNotContains(response, self.ple.name)


class OrganisationBrandingTests(TestCase):
    def setUp(self):
        self.ewfa, _ = FootballOrganisation.objects.update_or_create(
            code=FootballOrganisation.Code.EWFA,
            defaults={"name": "Eswatini Women's Football Association (EWFA)"},
        )
        self.ple, _ = FootballOrganisation.objects.update_or_create(
            code=FootballOrganisation.Code.PLE,
            defaults={"name": "Premier League of Eswatini (PLE)"},
        )
        self.superuser = User.objects.create_superuser(
            username="branding-superuser",
            email="branding-superuser@example.com",
            password="pass",
        )
        self.staff_user = User.objects.create_user(
            username="branding-staff",
            password="pass",
            is_staff=True,
        )

    def test_uploaded_organisation_logos_render_without_affecting_league_logos(self):
        self.ewfa.logo = "football/organisations/ewfa-logo.png"
        self.ewfa.save(update_fields=["logo"])
        self.ple.logo = "football/organisations/ple-logo.png"
        self.ple.save(update_fields=["logo"])

        competition = Competition.objects.create(
            name="Branded League",
            slug="branded-league",
            season="2026",
        )
        competition.series.league_type = CompetitionSeries.LeagueType.EWFA
        competition.series.logo = "football/leagues/league-logo.png"
        competition.series.save(update_fields=["league_type", "logo"])

        response = self.client.get(reverse("league_list"))
        self.assertContains(response, 'class="organisation-heading-logo"')
        self.assertContains(response, "(EWFA) logo")
        self.assertContains(response, "Premier League of Eswatini (PLE) logo")
        self.assertContains(response, 'class="league-card-logo"')

    def test_missing_organisation_logos_use_trophy_fallback(self):
        response = self.client.get(reverse("league_list"))
        self.assertNotContains(response, 'class="organisation-heading-logo"')
        self.assertEqual(response.content.decode().count('class="organisation-heading-fallback"'), 2)

    def test_only_superusers_can_manage_organisation_branding(self):
        change_url = reverse("admin:leagues_footballorganisation_change", args=[self.ewfa.pk])
        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.get(change_url).status_code, 403)

        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(change_url).status_code, 200)
