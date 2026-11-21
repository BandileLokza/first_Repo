import re

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from leagues.models import Competition, CompetitionSeries, FootballHomepageSection, Region


User = get_user_model()


class FootballHomepageSectionOrderingTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="section-superuser",
            email="section-superuser@example.com",
            password="pass",
        )
        self.staff_user = User.objects.create_user(
            username="section-staff",
            password="pass",
            is_staff=True,
        )
        self.region = Region.objects.create(name="Config Region", slug="config-region")
        self.regional = self.make_league(
            "Configured Regional League",
            "configured-regional",
            CompetitionSeries.LeagueType.REGIONAL,
            self.region,
        )
        self.ewfa = self.make_league(
            "Configured EWFA League",
            "configured-ewfa",
            CompetitionSeries.LeagueType.EWFA,
        )
        self.ple = self.make_league(
            "Configured PLE League",
            "configured-ple",
            CompetitionSeries.LeagueType.PLE,
        )
        self.zonal = self.make_league(
            "Configured Zonal League",
            "configured-zonal",
            CompetitionSeries.LeagueType.ZONAL,
        )

    def make_league(self, name, slug, league_type, region=None):
        competition = Competition.objects.create(name=name, slug=slug, season="2026")
        competition.series.league_type = league_type
        competition.series.region = region
        competition.series.save(update_fields=["league_type", "region"])
        return competition

    def configured_sections(self):
        return list(FootballHomepageSection.objects.values_list("section", "position"))

    def panel_content(self, response, section):
        match = re.search(
            rf'<section data-directory-panel="{section}"[^>]*>(.*?)</section>',
            response.content.decode(),
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def test_initial_configuration_preserves_the_existing_order(self):
        response = self.client.get(reverse("league_list"))

        self.assertEqual(
            self.configured_sections(),
            [("regions", 1), ("ewfa", 2), ("ple", 3), ("zonal", 4)],
        )
        self.assertEqual(
            [section["key"] for section in response.context["football_directory_sections"]],
            ["regions", "ewfa", "ple", "zonal"],
        )
        self.assertEqual(response.context["active_directory_tab"], "regions")
        content = response.content.decode()
        self.assertEqual(
            re.findall(r'data-directory-tab="([^"]+)"', content),
            ["regions", "ewfa", "ple", "zonal"],
        )
        self.assertEqual(
            re.findall(r'data-directory-panel="([^"]+)"', content),
            ["regions", "ewfa", "ple", "zonal"],
        )
        self.assertContains(response, "football-directory-tabs")
        self.assertContains(response, "ArrowLeft")

    def test_standings_heading_uses_the_competition_name_in_every_football_section(self):
        competition_names = (
            (self.regional, "Regional League Standings"),
            (self.ewfa, "EFA National Women Football League"),
            (self.ple, "Premier League of Eswatini"),
            (self.zonal, "MulaSport First Division"),
        )
        for competition, name in competition_names:
            competition.name = name
            competition.save(update_fields=["name"])
            response = self.client.get(reverse("league_standings", args=[competition.slug]))

            self.assertEqual(response.status_code, 200)
            self.assertContains(
                response,
                f'<h4 class="league-title league-standings-title">{name}</h4>',
                count=2,
            )

    def test_superuser_can_move_a_section_and_all_positions_remain_compact(self):
        ple_section = FootballHomepageSection.objects.get(
            section=FootballHomepageSection.Section.PLE
        )
        change_url = reverse(
            "admin:leagues_footballhomepagesection_change", args=[ple_section.pk]
        )

        self.client.force_login(self.superuser)
        response = self.client.post(change_url, {"position": 1, "_save": "Save"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.configured_sections(),
            [("ple", 1), ("regions", 2), ("ewfa", 3), ("zonal", 4)],
        )

    def test_non_superusers_cannot_change_or_delete_section_configuration(self):
        section = FootballHomepageSection.objects.get(
            section=FootballHomepageSection.Section.REGIONS
        )
        change_url = reverse(
            "admin:leagues_footballhomepagesection_change", args=[section.pk]
        )
        delete_url = reverse(
            "admin:leagues_footballhomepagesection_delete", args=[section.pk]
        )

        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.get(change_url).status_code, 403)

        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(delete_url).status_code, 403)

    def test_reordered_first_section_becomes_default_and_explicit_tabs_override_it(self):
        ple_section = FootballHomepageSection.objects.get(
            section=FootballHomepageSection.Section.PLE
        )
        ple_section.position = 1
        ple_section.save(update_fields=["position"])

        default_response = self.client.get(reverse("league_list"))
        self.assertEqual(default_response.context["active_directory_tab"], "ple")
        self.assertEqual(
            [section["key"] for section in default_response.context["football_directory_sections"]],
            ["ple", "regions", "ewfa", "zonal"],
        )
        content = default_response.content.decode()
        self.assertEqual(
            re.findall(r'data-directory-tab="([^"]+)"', content),
            ["ple", "regions", "ewfa", "zonal"],
        )
        self.assertEqual(
            re.findall(r'data-directory-panel="([^"]+)"', content),
            ["ple", "regions", "ewfa", "zonal"],
        )

        regions_response = self.client.get(reverse("league_list") + "?tab=regions")
        self.assertEqual(regions_response.context["active_directory_tab"], "regions")

        invalid_response = self.client.get(reverse("league_list") + "?tab=not-a-section")
        self.assertEqual(invalid_response.context["active_directory_tab"], "ple")

        legacy_response = self.client.get(reverse("league_list") + "?tab=psl")
        self.assertEqual(legacy_response.context["active_directory_tab"], "ple")

    def test_reordering_keeps_contents_and_branding_with_their_sections(self):
        zonal_section = FootballHomepageSection.objects.get(
            section=FootballHomepageSection.Section.ZONAL
        )
        zonal_section.position = 1
        zonal_section.save(update_fields=["position"])

        response = self.client.get(reverse("league_list"))
        regions_panel = self.panel_content(response, "regions")
        ewfa_panel = self.panel_content(response, "ewfa")
        ple_panel = self.panel_content(response, "ple")
        zonal_panel = self.panel_content(response, "zonal")

        self.assertIn(self.regional.name, regions_panel)
        self.assertIn(self.ewfa.name, ewfa_panel)
        self.assertIn("Eswatini Women's Football Association (EWFA)", ewfa_panel)
        self.assertIn(self.ple.name, ple_panel)
        self.assertIn("Premier League of Eswatini (PLE)", ple_panel)
        self.assertIn(self.zonal.name, zonal_panel)
        self.assertNotIn(self.ple.name, zonal_panel)
        self.assertEqual(response.context["active_directory_tab"], "zonal")

    def test_duplicate_moves_shift_other_sections_and_invalid_positions_are_rejected(self):
        ewfa_section = FootballHomepageSection.objects.get(
            section=FootballHomepageSection.Section.EWFA
        )
        ewfa_section.position = 1
        ewfa_section.save(update_fields=["position"])

        self.assertEqual(
            self.configured_sections(),
            [("ewfa", 1), ("regions", 2), ("ple", 3), ("zonal", 4)],
        )

        ple_section = FootballHomepageSection.objects.get(
            section=FootballHomepageSection.Section.PLE
        )
        ple_section.position = 0
        with self.assertRaises(ValidationError):
            ple_section.full_clean()

        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("admin:leagues_footballhomepagesection_change", args=[ple_section.pk]),
            {"position": 0, "_save": "Save"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 1.")
        ple_section.refresh_from_db()
        self.assertEqual(ple_section.position, 3)

    def test_missing_configuration_rows_fall_back_to_all_safe_default_sections(self):
        FootballHomepageSection.objects.filter(
            section=FootballHomepageSection.Section.REGIONS
        ).delete()

        response = self.client.get(reverse("league_list"))
        self.assertEqual(
            {section["key"] for section in response.context["football_directory_sections"]},
            {"regions", "ewfa", "ple", "zonal"},
        )
        self.assertEqual(response.context["active_directory_tab"], "regions")
