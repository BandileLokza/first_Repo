import re
from html import unescape

from django.test import TestCase
from django.urls import reverse

from leagues.models import Competition, CompetitionSeries, Region


class PublicRegionTests(TestCase):
    def directory_panel_markup(self, response, name):
        match = re.search(
            rf'<section data-directory-panel="{name}"[^>]*>(.*?)</section>',
            response.content.decode(),
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        return match.group(0)

    def make_league(self, name, slug, region=None, *, archived=False, order=100):
        league = Competition.objects.create(
            name=name, slug=slug, season="2026", is_archived=archived, display_order=order
        )
        league.series.region = region
        league.series.league_type = (
            CompetitionSeries.LeagueType.REGIONAL if region else CompetitionSeries.LeagueType.ZONAL
        )
        league.series.save(update_fields=["region", "league_type"])
        return league

    def test_homepage_orders_active_public_regions_and_hides_empty_or_inactive_ones(self):
        later = Region.objects.create(name="Later", slug="later", display_order=20)
        first = Region.objects.create(name="First", slug="first", display_order=10)
        inactive = Region.objects.create(name="Inactive", slug="inactive", is_active=False)
        self.make_league("Later League", "later-league", later)
        self.make_league("First League", "first-league", first)
        self.make_league("Private historical", "historical", inactive)
        self.make_league("Archived", "archived-region-league", first, archived=True)

        response = self.client.get(reverse("league_list"))
        self.assertEqual([region.id for region in response.context["regions"]], [first.id, later.id])
        self.assertNotContains(response, inactive.name)

    def test_region_page_only_exposes_its_current_leagues(self):
        region = Region.objects.create(name="Hhohho", slug="hhohho")
        other = Region.objects.create(name="Manzini", slug="manzini")
        visible = self.make_league("Visible League", "visible-region-league", region)
        self.make_league("Other League", "other-region-league", other)
        self.make_league("Old League", "old-region-league", region, archived=True)

        response = self.client.get(reverse("region_detail", args=[region.slug]))
        self.assertContains(response, visible.name)
        self.assertNotContains(response, "Other League")
        self.assertNotContains(response, "Old League")
        self.assertEqual(self.client.get(reverse("league_detail", args=[visible.slug])).status_code, 200)

    def test_long_league_names_render_fully_on_public_list_cards_and_detail_header(self):
        long_name = (
            "BAKHANYISELE MABULALA & GENTLEBREEZE MAPHALALENI LEAGUE "
            "JUBUKWENI STREAM"
        )
        region = Region.objects.create(name="Long Name Region", slug="long-name-region")
        league = self.make_league(long_name, "long-name-league", region)

        homepage = self.client.get(reverse("league_list"))
        region_page = self.client.get(reverse("region_detail", args=[region.slug]))
        detail = self.client.get(reverse("league_detail", args=[league.slug]))

        self.assertIn(long_name, unescape(homepage.content.decode()))
        self.assertIn(long_name, unescape(region_page.content.decode()))
        self.assertContains(region_page, "league-card-title")
        self.assertIn(long_name, unescape(detail.content.decode()))
        self.assertContains(detail, 'class="league-detail-info"')
        self.assertContains(detail, 'id="leagueTabs"')
        self.assertContains(detail, "Fixtures")
        self.assertContains(detail, "Results")
        self.assertContains(
            region_page,
            f'<a href="{reverse("league_detail", args=[league.slug])}">{long_name}</a>',
            html=True,
        )

        for league_type in (
            CompetitionSeries.LeagueType.EWFA,
            CompetitionSeries.LeagueType.PLE,
            CompetitionSeries.LeagueType.ZONAL,
        ):
            with self.subTest(league_type=league_type):
                section_league = self.make_league(
                    f"{long_name} {league_type}", f"long-name-{league_type.lower()}",
                )
                section_league.series.league_type = league_type
                section_league.series.save(update_fields=["league_type"])
                section_page = self.client.get(reverse("league_list"))
                self.assertContains(
                    section_page,
                    f'<a href="{reverse("league_detail", args=[section_league.slug])}">'
                    f'{section_league.name}</a>',
                    html=True,
                )

    def test_zonal_leagues_are_listed_only_in_the_zonal_homepage_data(self):
        region = Region.objects.create(name="Region", slug="region")
        regional = self.make_league("Regional League", "regional-league", region)
        zonal = self.make_league("Zonal League", "zonal-league")
        archived_zonal = self.make_league("Archived Zonal", "archived-zonal", archived=True)

        response = self.client.get(reverse("league_list"))
        self.assertContains(response, "Regions")
        self.assertContains(response, "Zonal Leagues")
        zonal_ids = [item["league"].id for item in response.context["zonal_league_data"]]
        self.assertEqual(zonal_ids, [zonal.id])
        self.assertNotIn(regional.id, zonal_ids)
        self.assertNotIn(archived_zonal.id, zonal_ids)

    def test_homepage_tab_panes_render_each_zonal_league_once(self):
        region = Region.objects.create(name="Preview Region", slug="preview-region")
        regional = self.make_league("Regional Preview", "regional-preview", region)
        first_zonal = self.make_league("First Zonal", "first-zonal")
        second_zonal = self.make_league("Second Zonal", "second-zonal")
        regions_url = reverse("league_list")

        regions_response = self.client.get(regions_url)
        regions_panel = self.directory_panel_markup(regions_response, "regions")
        zonal_panel = self.directory_panel_markup(regions_response, "zonal")
        self.assertEqual(regions_response.context["active_directory_tab"], "regions")
        self.assertNotIn("hidden", regions_panel.split(">", 1)[0])
        self.assertIn("hidden", zonal_panel.split(">", 1)[0])
        self.assertIn(reverse("league_detail", args=[regional.slug]), regions_panel)
        self.assertNotIn('class="league-block"', regions_panel)
        self.assertEqual(zonal_panel.count('class="league-block"'), 2)
        self.assertEqual(
            [item["league"].id for item in regions_response.context["zonal_league_data"]],
            [first_zonal.id, second_zonal.id],
        )

        zonal_response = self.client.get(f"{regions_url}?tab=zonal")
        zonal_panel = self.directory_panel_markup(zonal_response, "zonal")
        regions_panel = self.directory_panel_markup(zonal_response, "regions")
        self.assertEqual(zonal_response.context["active_directory_tab"], "zonal")
        self.assertIn("hidden", regions_panel.split(">", 1)[0])
        self.assertNotIn("hidden", zonal_panel.split(">", 1)[0])
        self.assertEqual(zonal_panel.count('class="league-block"'), 2)
        self.assertIn(first_zonal.name, zonal_panel)
        self.assertIn(second_zonal.name, zonal_panel)

    def test_region_card_previews_ordered_leagues_with_a_capped_overflow_count(self):
        region = Region.objects.create(name="Preview Region", slug="preview-region")
        leagues = [
            self.make_league(
                f"League {number}", f"preview-league-{number}", region, order=number
            )
            for number in range(7)
        ]
        response = self.client.get(reverse("league_list"))
        preview_region = response.context["regions"][0]
        self.assertEqual(
            [league.id for league in preview_region.preview_leagues],
            [league.id for league in leagues[:6]],
        )
        self.assertEqual(preview_region.extra_league_count, 1)
        self.assertContains(response, "League 0")
        self.assertContains(response, "+ 1 more league")

    def test_region_detail_and_zonal_tab_use_the_shared_league_card_presentation(self):
        region = Region.objects.create(name="Cards", slug="cards")
        regional = self.make_league("Regional Card", "regional-card", region)
        zonal = self.make_league("Zonal Card", "zonal-card")

        region_response = self.client.get(reverse("region_detail", args=[region.slug]))
        zonal_response = self.client.get(reverse("league_list"))
        self.assertContains(region_response, "league-block")
        self.assertContains(region_response, reverse("league_detail", args=[regional.slug]))
        self.assertContains(zonal_response, "league-block")
        self.assertContains(zonal_response, reverse("league_detail", args=[zonal.slug]))

    def test_region_card_has_safe_region_and_individual_league_links(self):
        region = Region.objects.create(name="Clickable", slug="clickable")
        league = self.make_league("Linked League", "linked-league", region)
        response = self.client.get(reverse("league_list"))

        self.assertContains(response, reverse("region_detail", args=[region.slug]))
        self.assertContains(response, reverse("league_detail", args=[league.slug]))
        self.assertContains(response, 'aria-label="View Clickable region"')
        self.assertContains(response, "region-card-overlay")
        self.assertContains(response, "region-card-foreground")
        self.assertContains(response, "region-league-link")
        self.assertContains(response, reverse("league_detail", args=[league.slug]))
        self.assertNotContains(
            response,
            f'href="{reverse("region_detail", args=[region.slug])}"><a',
        )

    def test_regional_league_back_link_returns_to_its_region(self):
        region = Region.objects.create(name="Back Region", slug="back-region")
        league = self.make_league("Regional Back", "regional-back", region)

        response = self.client.get(reverse("league_detail", args=[league.slug]))
        region_url = reverse("region_detail", args=[region.slug])
        self.assertEqual(response.context["back_url"], region_url)
        self.assertContains(response, f'href="{region_url}" class="history-back" data-history-back')
        directory_url = reverse("league_list")
        self.assertContains(self.client.get(region_url), f'href="{directory_url}" class="history-back" data-history-back')

    def test_zonal_and_incomplete_league_back_links_fall_back_safely(self):
        zonal = self.make_league("Zonal Back", "zonal-back")
        zonal_response = self.client.get(reverse("league_detail", args=[zonal.slug]))
        self.assertEqual(zonal_response.context["back_url"], reverse("league_list") + "?tab=zonal")

        incomplete = self.make_league("Incomplete Back", "incomplete-back")
        incomplete.series.league_type = CompetitionSeries.LeagueType.REGIONAL
        incomplete.series.region = None
        incomplete.series.save(update_fields=["league_type", "region"])
        incomplete_response = self.client.get(reverse("league_detail", args=[incomplete.slug]))
        self.assertEqual(incomplete_response.context["back_url"], reverse("league_list"))

    def test_directory_tab_defaults_to_regions_and_accepts_zonal_query_parameter(self):
        default_response = self.client.get(reverse("league_list"))
        zonal_response = self.client.get(reverse("league_list") + "?tab=zonal")
        self.assertEqual(default_response.context["active_directory_tab"], "regions")
        self.assertEqual(zonal_response.context["active_directory_tab"], "zonal")
        self.assertContains(zonal_response, 'data-directory-panel="zonal"')

    def test_optional_branding_fields_are_safe_and_series_logo_is_shared_by_seasons(self):
        region = Region.objects.create(name="Logo Optional", slug="logo-optional")
        first = self.make_league("Shared Identity", "shared-identity-2026", region)
        first.series.logo = "football/leagues/shared.png"
        first.series.save(update_fields=["logo"])
        second = Competition.objects.create(
            name="Shared Identity",
            slug="shared-identity-2027",
            season="2027",
            series=first.series,
        )

        region.logo = "football/regions/logo.png"
        region.save(update_fields=["logo"])
        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(first.series.logo.name, "football/leagues/shared.png")
        self.assertEqual(second.series.logo.name, "football/leagues/shared.png")
        self.assertFalse(first.sponsor_name)
        self.assertFalse(first.sponsor_logo)

    def test_public_branding_renders_when_configured_and_no_sponsor_stays_hidden(self):
        region = Region.objects.create(
            name="Branded Region", slug="branded-region", logo="football/regions/region.png"
        )
        league = self.make_league("Branded League", "branded-league", region)
        league.series.logo = "football/leagues/league.png"
        league.series.save(update_fields=["logo"])

        homepage = self.client.get(reverse("league_list"))
        region_page = self.client.get(reverse("region_detail", args=[region.slug]))
        detail = self.client.get(reverse("league_detail", args=[league.slug]))
        self.assertContains(homepage, 'class="region-logo"')
        self.assertContains(homepage, 'class="league-row-logo"')
        self.assertContains(region_page, 'class="region-detail-logo"')
        self.assertContains(region_page, 'class="league-card-logo"')
        self.assertContains(detail, 'class="league-detail-logo"')
        self.assertNotContains(detail, "Sponsored by")

        league.sponsor_name = "Example Sponsor"
        league.sponsor_logo = "football/sponsors/example.png"
        league.save(update_fields=["sponsor_name", "sponsor_logo"])
        detail = self.client.get(reverse("league_detail", args=[league.slug]))
        self.assertContains(detail, "Sponsored by")
        self.assertContains(detail, "Example Sponsor")
        self.assertContains(detail, 'class="league-sponsor-logo"')
