"""Regression coverage for football standings exports and report branding."""

from datetime import date
from html import unescape
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from league_manager.models import (
    OrganisationAdministrator,
    OrganisationReportPreference,
    RegionalAdministrator,
    RegionalReportPreference,
)
from league_manager.test_fixture_letterheads import pdf_text
from league_manager.regional_reports import (
    REPORT_FOOTER_LABEL,
    REPORT_FOOTER_MESSAGE,
    _PDF_BOTTOM_MARGIN,
    _PDF_LINE_HEIGHT,
    _PDF_TOP_MARGIN,
    _pdf_pages,
    _pdf_row_lines,
    build_organisation_report_data,
    build_regional_report_data,
    render_docx,
    render_pdf,
    report_document_rows,
    report_filename,
)
from leagues.models import Competition, CompetitionSeries, Group, Region, Stage, Standing
from leagues.standings import rank_standings
from teams.models import Team


User = get_user_model()


class StandingsReportTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="regional-standing-admin", password="pass")
        self.other_admin = User.objects.create_user(username="other-standing-admin", password="pass")
        self.region = Region.objects.create(name="Hhohho", slug="hhohho")
        self.other_region = Region.objects.create(name="Manzini", slug="manzini")
        RegionalAdministrator.objects.create(user=self.admin, region=self.region)
        RegionalAdministrator.objects.create(user=self.other_admin, region=self.other_region)
        self.competition = self.make_competition("Hhohho Premier League", "hhohho-premier")
        self.stage = Stage.objects.create(
            competition=self.competition,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )

    def make_competition(self, name, slug, *, format_type="TABLE", archived=False):
        competition = Competition.objects.create(
            name=name,
            slug=slug,
            season="2026/27",
            format_type=format_type,
            is_archived=archived,
        )
        competition.series.region = self.region
        competition.series.league_type = CompetitionSeries.LeagueType.REGIONAL
        competition.series.save(update_fields=["region", "league_type"])
        return competition

    def make_team(self, competition, name):
        return Team.objects.create(competition=competition, name=name)

    def set_table_standing(self, team, **values):
        """Populate the default table standing the team signal already created."""
        Standing.objects.filter(
            competition=self.competition,
            stage=self.stage,
            group__isnull=True,
            team=team,
        ).update(**values)
        return Standing.objects.get(
            competition=self.competition,
            stage=self.stage,
            group__isnull=True,
            team=team,
        )

    def preference(self, *, mode="ALL", output="DOCX"):
        return RegionalReportPreference.objects.create(
            user=self.admin,
            region=self.region,
            report_kind=RegionalReportPreference.ReportKind.STANDINGS,
            selection_mode=mode,
            output_format=output,
        )

    def test_export_uses_public_ranking_and_baseline_inclusive_totals(self):
        alpha = self.make_team(self.competition, "Alpha United")
        bravo = self.make_team(self.competition, "Bravo City")
        charlie = self.make_team(self.competition, "Charlie FC")
        self.set_table_standing(
            alpha,
            played=1,
            won=0,
            drawn=1,
            goals_for=2,
            baseline_played=4,
            baseline_won=1,
            baseline_drawn=1,
            baseline_lost=2,
            baseline_goals_for=4,
            baseline_goals_against=3,
            baseline_points=4,
            points=1,
        )
        self.set_table_standing(
            bravo,
            played=3,
            won=1,
            drawn=2,
            goals_for=3,
            goals_against=3,
            points=5,
        )
        self.set_table_standing(
            charlie,
            played=2,
            won=0,
            drawn=2,
            goals_for=1,
            goals_against=1,
            points=2,
        )

        data = build_regional_report_data(
            region=self.region,
            preference=self.preference(),
            report_date=date(2026, 10, 20),
        )
        exported_rows = data["entries"][0]["tables"][0]["rows"]
        public_rows = rank_standings(
            self.competition,
            Standing.objects.filter(competition=self.competition, group__isnull=True).select_related("team"),
        )

        self.assertEqual([row["team"] for row in exported_rows], [row.team.name for row in public_rows])
        self.assertEqual(exported_rows[0], {
            "kind": "standing",
            "position": 1,
            "team": "Alpha United",
            "played": 5,
            "won": 1,
            "drawn": 2,
            "lost": 2,
            "goals_for": 6,
            "goals_against": 3,
            "goal_difference": 3,
            "points": 5,
        })
        self.assertEqual(data["entries"][0]["season"], "2026/27")

    def test_group_stage_exports_separate_tables_and_never_create_knockout_table(self):
        group_competition = self.make_competition(
            "Regional Cup",
            "regional-cup",
            format_type="GROUP_KNOCKOUT",
        )
        group_stage = Stage.objects.create(
            competition=group_competition,
            name="Group Stage",
            stage_type="GROUP",
            order=1,
        )
        knockout_stage = Stage.objects.create(
            competition=group_competition,
            name="Semi-finals",
            stage_type="KNOCKOUT",
            order=2,
        )
        group_a = Group.objects.create(stage=group_stage, name="Group A")
        group_b = Group.objects.create(stage=group_stage, name="Group B")
        team_a = self.make_team(group_competition, "Group A Leaders")
        team_b = self.make_team(group_competition, "Group B Leaders")
        Standing.objects.create(competition=group_competition, stage=group_stage, group=group_a, team=team_a, points=6)
        Standing.objects.create(competition=group_competition, stage=group_stage, group=group_b, team=team_b, points=4)

        preference = self.preference(mode="CUSTOM")
        preference.selected_series.add(group_competition.series)
        data = build_regional_report_data(region=self.region, preference=preference)
        entry = data["entries"][0]

        self.assertEqual(entry["competition"], group_competition.name)
        self.assertEqual([table["label"] for table in entry["tables"]], [
            "Group Stage — Group A",
            "Group Stage — Group B",
        ])
        self.assertNotIn(knockout_stage.name, "\n".join(str(entry["tables"])))
        self.assertEqual(entry["tables"][0]["rows"][0]["team"], team_a.name)

    def test_empty_and_archived_competitions_are_safe_and_match_existing_active_scope(self):
        empty = self.make_competition("Empty League", "empty-league")
        archived = self.make_competition("Archived League", "archived-league", archived=True)
        archived_stage = Stage.objects.create(
            competition=archived,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        archived_team = self.make_team(archived, "Archived Team")
        Standing.objects.filter(
            competition=archived,
            stage=archived_stage,
            group__isnull=True,
            team=archived_team,
        ).update(points=10)

        data = build_regional_report_data(region=self.region, preference=self.preference())
        names = [entry["competition"] for entry in data["entries"]]
        empty_entry = next(entry for entry in data["entries"] if entry["competition"] == empty.name)

        self.assertIn(self.competition.name, names)
        self.assertIn(empty.name, names)
        self.assertNotIn(archived.name, names)
        self.assertEqual(empty_entry["tables"][0]["rows"], [])
        self.assertIn("No standings data", "\n".join(
            row.get("text", "") for row in report_document_rows(data)
        ))

    def test_regional_download_preserves_selection_security_and_output_formats(self):
        team = self.make_team(self.competition, "Export Team")
        self.set_table_standing(team, points=3)
        url = reverse(
            "league_manager:regional_report_export",
            args=[self.region.slug, "standings"],
        )
        self.client.force_login(self.admin)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Export Standings")

        docx = self.client.post(url, {
            "selection_mode": "ALL",
            "output_format": "DOCX",
            "action": "download",
        })
        pdf = self.client.post(url, {
            "selection_mode": "ALL",
            "output_format": "PDF",
            "action": "download",
        })
        self.assertTrue(docx.content.startswith(b"PK"))
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("_STANDINGS_", docx["Content-Disposition"])
        self.assertIn("_STANDINGS_", pdf["Content-Disposition"])

        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_docx_and_pdf_footers_are_hyperlinked_on_every_page_without_content_overlap(self):
        for number in range(60):
            team = self.make_team(self.competition, f"Very Long Club Name Number {number:02d} Academy")
            self.set_table_standing(
                team,
                played=10,
                won=number % 4,
                drawn=number % 3,
                lost=10 - (number % 4) - (number % 3),
                goals_for=number + 1,
                goals_against=number,
                points=number,
            )
        data = build_regional_report_data(region=self.region, preference=self.preference())
        rows = report_document_rows(data)
        pages = _pdf_pages(rows)
        pdf = render_pdf(data).decode("cp1252")

        self.assertGreater(len(pages), 1)
        self.assertEqual(pdf.count(REPORT_FOOTER_MESSAGE), len(pages))
        self.assertEqual(pdf.count(f"({REPORT_FOOTER_LABEL}) Tj"), len(pages))
        self.assertEqual(pdf.count("/Subtype /Link"), len(pages))
        self.assertEqual(pdf.count("(#) Tj"), len(pages))
        self.assertIn("/URI (https://www.esportslive.site)", pdf)
        for page in pages:
            height = sum(len(lines) * _PDF_LINE_HEIGHT for _row, lines in page)
            self.assertLessEqual(height, _PDF_TOP_MARGIN - _PDF_BOTTOM_MARGIN)

        with ZipFile(BytesIO(render_docx(data))) as archive:
            document_bytes = archive.read("word/document.xml")
            footer_bytes = archive.read("word/footer1.xml")
            ElementTree.fromstring(document_bytes)
            ElementTree.fromstring(footer_bytes)
            document = unescape(document_bytes.decode("utf-8"))
            footer = unescape(footer_bytes.decode("utf-8"))
            footer_relationships = archive.read("word/_rels/footer1.xml.rels").decode("utf-8")
        self.assertIn("<w:tblHeader/>", document)
        self.assertIn(REPORT_FOOTER_MESSAGE, footer)
        self.assertIn(REPORT_FOOTER_LABEL, footer)
        self.assertIn('Target="https://www.esportslive.site"', footer_relationships)

    def test_fixture_and_results_rendering_receive_the_same_brand_footer(self):
        for kind in (
            RegionalReportPreference.ReportKind.FIXTURES,
            RegionalReportPreference.ReportKind.RESULTS,
        ):
            preference = RegionalReportPreference.objects.create(
                user=self.admin,
                region=self.region,
                report_kind=kind,
            )
            data = build_regional_report_data(region=self.region, preference=preference)
            docx = render_docx(data)
            pdf = pdf_text(render_pdf(data))
            with ZipFile(BytesIO(docx)) as archive:
                footer = unescape(archive.read("word/footer1.xml").decode("utf-8"))
            self.assertIn(REPORT_FOOTER_MESSAGE, footer)
            self.assertIn(REPORT_FOOTER_LABEL, footer)
            self.assertIn(REPORT_FOOTER_MESSAGE, pdf)
            self.assertIn(REPORT_FOOTER_LABEL, pdf)
            self.assertTrue(report_filename(data, "PDF").endswith(".pdf"))


class OrganisationStandingsReportTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ewfa-standing-admin", password="pass")
        self.foreign_admin = User.objects.create_user(username="ple-standing-admin", password="pass")
        OrganisationAdministrator.objects.create(user=self.admin, organisation="EWFA")
        OrganisationAdministrator.objects.create(user=self.foreign_admin, organisation="PLE")
        self.competition = Competition.objects.create(
            name="EWFA Premier League",
            slug="ewfa-standings",
            season="2026/27",
            format_type="TABLE",
        )
        self.competition.series.league_type = CompetitionSeries.LeagueType.EWFA
        self.competition.series.save(update_fields=["league_type"])
        self.stage = Stage.objects.create(
            competition=self.competition,
            name="League",
            stage_type="LEAGUE",
            order=1,
        )
        team = Team.objects.create(name="EWFA Team", competition=self.competition)
        Standing.objects.filter(
            competition=self.competition,
            stage=self.stage,
            group__isnull=True,
            team=team,
        ).update(points=7)

    def test_organisation_standings_use_the_same_scope_and_reject_foreign_access(self):
        preference = OrganisationReportPreference.objects.create(
            user=self.admin,
            organisation="EWFA",
            report_kind=OrganisationReportPreference.ReportKind.STANDINGS,
        )
        data = build_organisation_report_data(organisation="EWFA", preference=preference)
        self.assertEqual(data["entries"][0]["competition"], self.competition.name)
        self.assertEqual(data["entries"][0]["tables"][0]["rows"][0]["points"], 7)

        url = reverse("league_manager:organisation_report_export", args=["ewfa", "standings"])
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.foreign_admin)
        self.assertEqual(self.client.get(url).status_code, 403)
