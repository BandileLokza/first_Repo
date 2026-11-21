"""Security and report-data regression tests for regional administration."""

from datetime import date, datetime, timedelta
from html import unescape
from io import BytesIO
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from league_manager.models import LeagueMembership, RegionalAdministrator, RegionalReportPreference
from league_manager.permissions import can_manage_region, user_manages_competition
from league_manager.test_fixture_letterheads import pdf_text
from league_manager.regional_reports import (
    _groups,
    _pdf_row_lines,
    _pdf_text_width,
    build_regional_report_data,
    render_docx,
    render_pdf,
    report_document_rows,
    report_text_lines,
    selected_competitions,
)
from leagues.models import Competition, CompetitionSeries, Group, Region, Stage
from matches.models import Match
from teams.models import Team


User = get_user_model()
LONG_COMPETITION_NAME = (
    "BAKHANYISELE MABULALA & GENTLEBREEZE MAPHALALENI LEAGUE "
    "JUBUKWENI STREAM"
)


class RegionalAdministrationAndReportTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="regional-admin", password="pass")
        self.owner = User.objects.create_user(username="league-owner", password="pass")
        self.regular = User.objects.create_user(username="regular-user", password="pass")
        self.superuser = User.objects.create_superuser(username="super", email="super@example.com", password="pass")
        self.hhohho = Region.objects.create(name="Hhohho", slug="hhohho", report_abbreviation="HRFA", report_subheading="HHOHHO YOUTH DEVELOPMENT")
        self.manzini = Region.objects.create(name="Manzini", slug="manzini", report_abbreviation="MRFA")
        self.home_competition = self.make_competition("Hhohho League", "hhohho-league", self.hhohho)
        self.other_competition = self.make_competition("Manzini League", "manzini-league", self.manzini)
        RegionalAdministrator.objects.create(user=self.admin, region=self.hhohho)
        LeagueMembership.objects.create(user=self.owner, competition=self.home_competition, role=LeagueMembership.Role.OWNER)
        self.home_stage = Stage.objects.create(competition=self.home_competition, name="League", stage_type="LEAGUE", order=1)
        self.other_stage = Stage.objects.create(competition=self.other_competition, name="League", stage_type="LEAGUE", order=1)
        self.home_a = Team.objects.create(name="Hhohho A", competition=self.home_competition)
        self.home_b = Team.objects.create(name="Hhohho B", competition=self.home_competition)
        self.other_a = Team.objects.create(name="Manzini A", competition=self.other_competition)
        self.other_b = Team.objects.create(name="Manzini B", competition=self.other_competition)

    def make_competition(self, name, slug, region, *, season="2026", archived=False):
        competition = Competition.objects.create(name=name, slug=slug, season=season, is_archived=archived)
        competition.series.region = region
        competition.series.league_type = CompetitionSeries.LeagueType.REGIONAL
        competition.series.save(update_fields=["region", "league_type"])
        return competition

    def preference(self, kind, mode="ALL", output="DOCX"):
        return RegionalReportPreference.objects.create(
            user=self.admin, region=self.hhohho, report_kind=kind,
            selection_mode=mode, output_format=output,
        )

    def make_home_match(
        self,
        *,
        gameweek,
        status="UPCOMING",
        home_score=0,
        away_score=0,
        location="Main Ground",
        match_date=None,
    ):
        return Match.objects.create(
            competition=self.home_competition, stage=self.home_stage,
            home_team=self.home_a, away_team=self.home_b, gameweek=gameweek,
            status=status, home_score=home_score, away_score=away_score,
            match_date=match_date or timezone.now() + timedelta(days=1), location=location,
            leg=Match.objects.filter(competition=self.home_competition).count() + 1,
        )

    def test_regional_scope_grants_own_league_management_only(self):
        self.assertTrue(can_manage_region(self.admin, self.hhohho))
        self.assertFalse(can_manage_region(self.admin, self.manzini))
        self.assertTrue(user_manages_competition(self.admin, self.home_competition))
        self.assertFalse(user_manages_competition(self.admin, self.other_competition))
        self.assertFalse(can_manage_region(self.owner, self.hhohho))
        self.assertTrue(can_manage_region(self.superuser, self.manzini))

        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.home_competition.slug])).status_code, 200)
        self.assertEqual(self.client.get(reverse("league_manager:team_list", args=[self.other_competition.slug])).status_code, 403)

    def test_public_region_page_never_renders_regional_administration_controls(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("region_detail", args=[self.hhohho.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.home_competition.name)
        self.assertNotContains(response, "Regional Administration")
        self.assertNotContains(
            response,
            reverse("league_manager:regional_league_owners", args=[self.hhohho.slug]),
        )
        self.assertNotContains(
            response,
            reverse(
                "league_manager:regional_report_export",
                args=[self.hhohho.slug, "fixtures"],
            ),
        )

    def test_dashboard_shows_regional_tools_only_for_authorized_users(self):
        extra_region = Region.objects.create(name="Shiselweni", slug="shiselweni")
        RegionalAdministrator.objects.create(user=self.admin, region=extra_region)
        match_agent = User.objects.create_user(username="match-agent", password="pass")
        LeagueMembership.objects.create(
            user=match_agent,
            competition=self.home_competition,
            role=LeagueMembership.Role.AGENT,
        )
        dashboard_url = reverse("league_manager:dashboard")
        hhohho_owner_url = reverse(
            "league_manager:regional_league_owners", args=[self.hhohho.slug]
        )
        shiselweni_owner_url = reverse(
            "league_manager:regional_league_owners", args=[extra_region.slug]
        )
        manzini_owner_url = reverse(
            "league_manager:regional_league_owners", args=[self.manzini.slug]
        )

        self.client.force_login(self.admin)
        response = self.client.get(dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Regional Administration")
        self.assertContains(response, self.home_competition.name)
        self.assertContains(response, hhohho_owner_url)
        self.assertContains(response, shiselweni_owner_url)
        self.assertNotContains(response, manzini_owner_url)
        self.assertContains(
            response,
            reverse(
                "league_manager:regional_report_export",
                args=[self.hhohho.slug, "fixtures"],
            ),
        )
        self.assertContains(
            response,
            reverse(
                "league_manager:regional_report_export",
                args=[extra_region.slug, "results"],
            ),
        )
        self.assertEqual(
            list(response.context["regional_regions"]), [self.hhohho, extra_region]
        )

        self.client.force_login(self.owner)
        response = self.client.get(dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.home_competition.name)
        self.assertNotContains(response, "Regional Administration")
        self.assertNotContains(response, hhohho_owner_url)

        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(dashboard_url).status_code, 403)

        self.client.force_login(match_agent)
        self.assertEqual(self.client.get(dashboard_url).status_code, 403)

        self.client.force_login(self.superuser)
        response = self.client.get(dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, hhohho_owner_url)
        self.assertContains(response, manzini_owner_url)
        self.assertContains(response, shiselweni_owner_url)

        self.client.logout()
        self.assertEqual(self.client.get(dashboard_url).status_code, 302)
        self.assertEqual(self.client.get(hhohho_owner_url).status_code, 302)

    def test_regional_tools_keep_server_authorization_and_return_to_dashboard(self):
        dashboard_url = reverse("league_manager:dashboard")
        owners_url = reverse(
            "league_manager:regional_league_owners", args=[self.hhohho.slug]
        )
        fixtures_url = reverse(
            "league_manager:regional_report_export",
            args=[self.hhohho.slug, "fixtures"],
        )
        results_url = reverse(
            "league_manager:regional_report_export",
            args=[self.hhohho.slug, "results"],
        )

        self.client.force_login(self.admin)
        for url in (owners_url, fixtures_url, results_url):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f'href="{dashboard_url}"')

        self.assertEqual(
            self.client.get(
                reverse(
                    "league_manager:regional_league_owners",
                    args=[self.manzini.slug],
                )
            ).status_code,
            403,
        )

        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(owners_url).status_code, 403)
        self.assertEqual(self.client.get(fixtures_url).status_code, 403)

    def test_regional_admin_can_assign_only_own_region_owners_and_never_regional_admins(self):
        user = User.objects.create_user(username="new-owner", password="pass")
        self.client.force_login(self.admin)
        assign_url = reverse("league_manager:regional_league_owner_assign", args=[self.hhohho.slug, self.home_competition.series_id])
        response = self.client.post(assign_url, {"identifier": user.username})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(LeagueMembership.objects.filter(user=user, competition=self.home_competition, role="OWNER", is_active=True).exists())

        other_url = reverse("league_manager:regional_league_owner_assign", args=[self.hhohho.slug, self.other_competition.series_id])
        self.assertEqual(self.client.post(other_url, {"identifier": user.username}).status_code, 404)
        self.client.post(assign_url, {"identifier": self.admin.username})
        self.assertFalse(LeagueMembership.objects.filter(user=self.admin, competition=self.home_competition, role="OWNER").exists())

    def test_report_all_is_dynamic_custom_survives_series_season_rollover_and_preferences_are_independent(self):
        fixture_preference = self.preference(RegionalReportPreference.ReportKind.FIXTURES)
        self.make_home_match(gameweek=7)
        first = build_regional_report_data(region=self.hhohho, preference=fixture_preference)
        self.assertEqual([entry["competition"] for entry in first["entries"]], [self.home_competition.name])

        new = self.make_competition("Hhohho Juniors", "hhohho-juniors", self.hhohho)
        self.assertIn(new, selected_competitions(self.hhohho, fixture_preference))

        custom = self.preference(RegionalReportPreference.ReportKind.RESULTS, mode="CUSTOM", output="PDF")
        custom.selected_series.add(self.home_competition.series)
        self.home_competition.is_archived = True
        self.home_competition.save(update_fields=["is_archived"])
        next_season = Competition.objects.create(name="Hhohho League", slug="hhohho-league-2027", season="2027", series=self.home_competition.series)
        stage = Stage.objects.create(competition=next_season, name="League", stage_type="LEAGUE", order=1)
        team_a = Team.objects.create(name="Next A", competition=next_season)
        team_b = Team.objects.create(name="Next B", competition=next_season)
        Match.objects.create(competition=next_season, stage=stage, home_team=team_a, away_team=team_b, gameweek=4, status="FINISHED", home_score=2, away_score=1, match_date=timezone.now())
        results = build_regional_report_data(region=self.hhohho, preference=custom)
        self.assertEqual([entry["competition"] for entry in results["entries"]], [next_season.name])
        self.assertEqual(fixture_preference.output_format, "DOCX")
        self.assertEqual(custom.output_format, "PDF")

    def test_report_uses_each_leagues_round_detects_incomplete_results_and_renders_both_formats(self):
        report_date = date(2026, 10, 20)
        self.make_home_match(
            gameweek=5,
            status="UPCOMING",
            location="Mavuso Ground",
            match_date=timezone.make_aware(datetime(2026, 10, 24, 15, 0)),
        )
        self.make_home_match(
            gameweek=6,
            status="UPCOMING",
            match_date=timezone.make_aware(datetime(2026, 10, 31, 15, 0)),
        )
        fixture = self.preference(RegionalReportPreference.ReportKind.FIXTURES)
        fixture_data = build_regional_report_data(
            region=self.hhohho,
            preference=fixture,
            report_date=report_date,
        )
        self.assertEqual(fixture_data["entries"][0]["round_label"], "Gameweek 5")
        self.assertTrue(render_docx(fixture_data).startswith(b"PK"))
        self.assertTrue(render_pdf(fixture_data).startswith(b"%PDF"))

        Match.objects.filter(competition=self.home_competition, gameweek=5).update(status="FINISHED", home_score=1, away_score=0)
        self.make_home_match(gameweek=5, status="UPCOMING")
        result = self.preference(RegionalReportPreference.ReportKind.RESULTS)
        result_data = build_regional_report_data(region=self.hhohho, preference=result)
        self.assertTrue(result_data["warnings"])

    def test_fixture_report_adds_real_gameweek_to_its_playing_line_in_both_formats(self):
        match = self.make_home_match(
            gameweek=1,
            match_date=timezone.make_aware(datetime(2026, 10, 10, 13, 15)),
        )
        fixture = self.preference(RegionalReportPreference.ReportKind.FIXTURES)
        data = build_regional_report_data(
            region=self.hhohho,
            preference=fixture,
            report_date=date(2026, 10, 1),
        )
        label = "Playing Saturday 10 October 2026 — GW1"
        self.assertIn(label, report_text_lines(data))

        with ZipFile(BytesIO(render_docx(data))) as archive:
            document_xml = unescape(archive.read("word/document.xml").decode("utf-8"))
        pdf = pdf_text(render_pdf(data))
        self.assertIn(label, document_xml)
        self.assertIn(label, pdf)
        self.assertEqual(match.gameweek, 1)

    def test_fixture_report_handles_gameweek_twelve_and_omits_blank_gameweeks(self):
        fixture = self.preference(RegionalReportPreference.ReportKind.FIXTURES)
        self.make_home_match(
            gameweek=12,
            match_date=timezone.make_aware(datetime(2026, 10, 11, 13, 15)),
        )
        gameweek_data = build_regional_report_data(
            region=self.hhohho,
            preference=fixture,
            report_date=date(2026, 10, 1),
        )
        self.assertIn(
            "Playing Sunday 11 October 2026 — GW12",
            report_text_lines(gameweek_data),
        )

        Match.objects.all().delete()
        self.make_home_match(
            gameweek=None,
            match_date=timezone.make_aware(datetime(2026, 10, 10, 13, 15)),
        )
        no_gameweek_data = build_regional_report_data(
            region=self.hhohho,
            preference=fixture,
            report_date=date(2026, 10, 1),
        )
        playing_lines = [
            line for line in report_text_lines(no_gameweek_data)
            if line.startswith("Playing ")
        ]
        self.assertEqual(playing_lines, ["Playing Saturday 10 October 2026"])

    def test_fixture_grouping_keeps_different_gameweeks_separate(self):
        gameweek_four = self.make_home_match(
            gameweek=4,
            match_date=timezone.make_aware(datetime(2026, 10, 10, 13, 15)),
        )
        gameweek_five = self.make_home_match(
            gameweek=5,
            match_date=timezone.make_aware(datetime(2026, 10, 10, 15, 15)),
        )
        sections = _groups(
            [gameweek_four, gameweek_five],
            report_kind="FIXTURES",
            report_date=date(2026, 10, 1),
        )
        self.assertEqual(
            [section["date_label"] for section in sections],
            [
                "Playing Saturday 10 October 2026 — GW4",
                "Playing Saturday 10 October 2026 — GW5",
            ],
        )

    def test_long_competition_names_wrap_without_loss_in_fixture_and_results_reports(self):
        self.home_competition.name = LONG_COMPETITION_NAME
        self.home_competition.save(update_fields=["name"])
        match = self.make_home_match(
            gameweek=12,
            match_date=timezone.make_aware(datetime(2026, 10, 10, 13, 15)),
        )
        fixture = self.preference(RegionalReportPreference.ReportKind.FIXTURES)
        fixture_data = build_regional_report_data(
            region=self.hhohho,
            preference=fixture,
            report_date=date(2026, 10, 1),
        )
        self.assertEqual(fixture_data["entries"][0]["competition"], LONG_COMPETITION_NAME)
        competition_row = next(
            row for row in report_document_rows(fixture_data)
            if row["kind"] == "competition"
        )
        wrapped_heading = _pdf_row_lines(competition_row)
        self.assertGreater(len(wrapped_heading), 1)
        self.assertEqual(" ".join(wrapped_heading), LONG_COMPETITION_NAME.upper())
        self.assertTrue(all(_pdf_text_width(line) <= 504 for line in wrapped_heading))

        with ZipFile(BytesIO(render_docx(fixture_data))) as archive:
            fixture_docx = unescape(archive.read("word/document.xml").decode("utf-8"))
        fixture_pdf = pdf_text(render_pdf(fixture_data))
        self.assertIn(LONG_COMPETITION_NAME.upper(), fixture_docx)
        self.assertIn(LONG_COMPETITION_NAME.upper(), " ".join(fixture_pdf.split()))

        match.status = "FINISHED"
        match.home_score = 2
        match.away_score = 1
        match.save(update_fields=["status", "home_score", "away_score"])
        results = self.preference(RegionalReportPreference.ReportKind.RESULTS)
        results_data = build_regional_report_data(
            region=self.hhohho,
            preference=results,
            report_date=date(2026, 10, 1),
        )
        self.assertEqual(results_data["entries"][0]["competition"], LONG_COMPETITION_NAME)
        with ZipFile(BytesIO(render_docx(results_data))) as archive:
            results_docx = unescape(archive.read("word/document.xml").decode("utf-8"))
        self.assertIn(LONG_COMPETITION_NAME.upper(), results_docx)
        results_pdf = render_pdf(results_data)
        self.assertTrue(results_pdf.startswith(b"%PDF"))
        self.assertIn(LONG_COMPETITION_NAME.upper(), " ".join(pdf_text(results_pdf).split()))

    def test_report_data_preserves_newspaper_hierarchy_and_is_shared_by_both_renderers(self):
        stream = Stage.objects.create(
            competition=self.home_competition,
            name="Lobamba Stream",
            stage_type="GROUP",
            order=2,
        )
        division = Group.objects.create(stage=stream, name="U13")
        division.teams.add(self.home_a, self.home_b)
        Match.objects.create(
            competition=self.home_competition,
            stage=stream,
            group=division,
            home_team=self.home_a,
            away_team=self.home_b,
            gameweek=8,
            status="UPCOMING",
            match_date=timezone.now() + timedelta(days=1),
            location="Lobamba Sports Ground",
            leg=1,
        )
        fixture = self.preference(RegionalReportPreference.ReportKind.FIXTURES)
        fixture_data = build_regional_report_data(
            region=self.hhohho,
            preference=fixture,
            report_date=timezone.localdate(),
        )
        rows = report_document_rows(fixture_data)
        self.assertIn({"kind": "label", "text": "Lobamba Stream"}, rows)
        self.assertIn({"kind": "label", "text": "U13"}, rows)
        self.assertIn({"kind": "venue", "text": "Lobamba Sports Ground"}, rows)
        self.assertIn("Playing tomorrow — GW8", report_text_lines(fixture_data))

        docx = render_docx(fixture_data)
        pdf = pdf_text(render_pdf(fixture_data))
        with ZipFile(BytesIO(docx)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn('<w:jc w:val="center"/>', document_xml)
        self.assertIn('<w:pgMar w:top="840" w:right="840" w:bottom="1360" w:left="840"', document_xml)
        self.assertIn("Arial", document_xml)
        self.assertNotIn("<w:tab/>", document_xml)
        self.assertIn("<w:tblGrid>", document_xml)
        for value in ("Playing tomorrow — GW8", "Lobamba Stream", "Lobamba Sports Ground", "U13"):
            self.assertIn(value, pdf)

        knockout = Stage.objects.create(
            competition=self.home_competition,
            name="Quarter-final",
            stage_type="KNOCKOUT",
            order=3,
        )
        Match.objects.create(
            competition=self.home_competition,
            stage=knockout,
            home_team=self.home_a,
            away_team=self.home_b,
            gameweek=9,
            status="FINISHED",
            home_score=2,
            away_score=2,
            penalty_home_score=4,
            penalty_away_score=5,
            match_date=timezone.now(),
            leg=1,
        )
        result = self.preference(RegionalReportPreference.ReportKind.RESULTS)
        result_data = build_regional_report_data(region=self.hhohho, preference=result)
        self.assertIn("(4)2-2(5)", "\n".join(report_text_lines(result_data)))

    def test_report_post_rejects_cross_region_series_injection_and_foreign_region_export(self):
        self.client.force_login(self.admin)
        export_url = reverse("league_manager:regional_report_export", args=[self.hhohho.slug, "fixtures"])
        response = self.client.post(export_url, {
            "selection_mode": "CUSTOM", "series": [self.other_competition.series_id],
            "output_format": "DOCX", "action": "save",
        })
        self.assertEqual(response.status_code, 200)
        preference = RegionalReportPreference.objects.get(user=self.admin, region=self.hhohho, report_kind="FIXTURES")
        self.assertEqual(preference.selected_series.count(), 0)
        self.assertEqual(self.client.get(reverse("league_manager:regional_report_export", args=[self.manzini.slug, "fixtures"])).status_code, 403)

        self.client.force_login(self.superuser)
        self.assertEqual(
            self.client.get(reverse("league_manager:regional_report_export", args=[self.manzini.slug, "fixtures"])).status_code,
            200,
        )
