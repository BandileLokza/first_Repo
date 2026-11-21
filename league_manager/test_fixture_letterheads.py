"""Regression coverage for scoped football fixture stationery and document sections."""

from datetime import date
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.storage import FileSystemStorage, InMemoryStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image
from pypdf import PdfReader

from league_manager.models import LeagueMembership, OrganisationAdministrator, RegionalAdministrator, RegionalReportPreference
from league_manager.regional_reports import build_scope_report_data, fixture_letterhead_for, render_docx, render_pdf
from leagues.models import Competition, CompetitionSeries, FootballOrganisation, Region, Stage
from matches.models import Match
from teams.models import Team


def pdf_pages(pdf):
    return list(PdfReader(BytesIO(pdf)).pages)


def pdf_text(pdf):
    return "\n".join(page.extract_text() for page in pdf_pages(pdf))


def image_refs(page):
    resources = page["/Resources"].get("/XObject", {})
    return {resource.idnum for resource in resources.get_object().values()} if resources else set()


def uploaded_header(name="header.png", color="navy", format="PNG"):
    output = BytesIO()
    Image.new("RGB", (1000, 140), color).save(output, format=format)
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/jpeg" if format == "JPEG" else "image/png")


class FixtureLetterheadTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = FileSystemStorage(location=self.directory.name)
        for model in (Region, FootballOrganisation, CompetitionSeries):
            field = model._meta.get_field("fixture_letterhead")
            original = field.storage
            field.storage = self.storage
            self.addCleanup(setattr, field, "storage", original)
        User = get_user_model()
        self.admin = User.objects.create_user("letterhead-admin")
        self.owner = User.objects.create_user("letterhead-owner")
        self.agent = User.objects.create_user("letterhead-agent")
        self.region = Region.objects.create(name="Header Manzini", slug="header-manzini")
        self.other_region = Region.objects.create(name="Header Hhohho", slug="header-hhohho")
        RegionalAdministrator.objects.create(user=self.admin, region=self.region)
        self.regional = self.make_competition("Regional", "REGIONAL", self.region)
        self.ewfa = self.make_competition("Women", "EWFA")
        self.ple = self.make_competition("Premier", "PLE")
        self.zonal = self.make_competition("Nhlambeni", "ZONAL")
        self.unbranded = self.make_competition("Zonal Without Header", "ZONAL")
        LeagueMembership.objects.create(user=self.owner, competition=self.zonal, role="OWNER")
        LeagueMembership.objects.create(user=self.agent, competition=self.zonal, role="AGENT")
        self.ewfa_org, _ = FootballOrganisation.objects.get_or_create(code="EWFA", defaults={"name": "EWFA"})
        self.ple_org, _ = FootballOrganisation.objects.get_or_create(code="PLE", defaults={"name": "PLE"})
        self.region.fixture_letterhead.save("region.png", uploaded_header(color="navy"))
        self.ewfa_org.fixture_letterhead.save("ewfa.jpg", uploaded_header(format="JPEG", color="purple"))
        self.ple_org.fixture_letterhead.save("ple.png", uploaded_header(color="green"))
        self.zonal.series.fixture_letterhead.save("nhlambeni.png", uploaded_header(color="maroon"))

    def make_competition(self, name, kind, region=None):
        slug = name.lower().replace(" ", "-")
        series = CompetitionSeries.objects.create(name=name, slug=slug, league_type=kind, region=region)
        competition = Competition.objects.create(name=name, slug=slug, season="2026", series=series, format_type="TABLE")
        stage = Stage.objects.create(competition=competition, name="League", stage_type="LEAGUE", order=1)
        home = Team.objects.create(name=f"{name} A", competition=competition)
        away = Team.objects.create(name=f"{name} B", competition=competition)
        Match.objects.create(competition=competition, stage=stage, home_team=home, away_team=away, gameweek=1, leg=1, location="Mavuso Ground")
        return competition

    def data(self, competitions, kind="FIXTURES"):
        preference = RegionalReportPreference(report_kind=kind)
        return build_scope_report_data(preference=preference, scope_name="Sample", scope_abbreviation="SAMPLE", competitions=competitions, report_date=date(2026, 11, 4))

    def test_classification_and_permanent_series_choose_the_correct_header(self):
        for competition, expected in ((self.regional, self.region), (self.ewfa, self.ewfa_org), (self.ple, self.ple_org), (self.zonal, self.zonal.series)):
            with self.subTest(kind=competition.series.league_type):
                self.assertEqual(fixture_letterhead_for(competition).name, expected.fixture_letterhead.name)
        # A stale geographical relation must never make a Zonal league use a region header.
        self.zonal.series.region = self.region
        self.assertEqual(fixture_letterhead_for(self.zonal).name, self.zonal.series.fixture_letterhead.name)
        next_season = Competition(name="Nhlambeni 2027", series=self.zonal.series, season="2027")
        self.assertEqual(fixture_letterhead_for(next_season).name, fixture_letterhead_for(self.zonal).name)
        self.assertFalse(fixture_letterhead_for(self.unbranded))

    def test_upload_replace_clear_and_cross_region_denial(self):
        self.client.force_login(self.admin)
        url = reverse("league_manager:regional_fixture_letterhead", args=[self.region.slug])
        response = self.client.post(url, {"fixture_letterhead": uploaded_header("replace.jpg", format="JPEG")})
        self.assertEqual(response.status_code, 302)
        self.region.refresh_from_db()
        self.assertTrue(self.region.fixture_letterhead.name.endswith(".jpg"))
        response = self.client.post(url, {"fixture_letterhead-clear": "on"})
        self.assertEqual(response.status_code, 302)
        self.region.refresh_from_db()
        self.assertFalse(self.region.fixture_letterhead)
        for target in (reverse("league_manager:regional_fixture_letterhead", args=[self.other_region.slug]), reverse("league_manager:organisation_fixture_letterhead", args=["ewfa"]), reverse("league_manager:zonal_fixture_letterhead", args=[self.zonal.series_id])):
            self.assertEqual(self.client.post(target, {"fixture_letterhead": uploaded_header()}).status_code, 403)
        self.other_region.refresh_from_db()
        self.assertFalse(self.other_region.fixture_letterhead)

    def test_organisation_and_zonal_scope_upload_denial(self):
        OrganisationAdministrator.objects.create(user=self.admin, organisation="EWFA")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("league_manager:organisation_fixture_letterhead", args=["ewfa"]), {"fixture_letterhead": uploaded_header()}).status_code, 302)
        self.assertEqual(self.client.post(reverse("league_manager:organisation_fixture_letterhead", args=["ple"]), {"fixture_letterhead": uploaded_header()}).status_code, 403)
        url = reverse("league_manager:zonal_fixture_letterhead", args=[self.zonal.series_id])
        self.client.force_login(self.agent)
        self.assertEqual(self.client.post(url, {"fixture_letterhead": uploaded_header()}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, {"fixture_letterhead": uploaded_header()}).status_code, 302)
        self.assertEqual(self.client.post(reverse("league_manager:zonal_fixture_letterhead", args=[self.unbranded.series_id]), {"fixture_letterhead": uploaded_header()}).status_code, 403)

    def test_regional_league_owner_cannot_change_shared_region_stationery(self):
        LeagueMembership.objects.create(user=self.owner, competition=self.regional, role="OWNER")
        self.client.force_login(self.owner)
        url = reverse("league_manager:regional_fixture_letterhead", args=[self.region.slug])
        before = self.region.fixture_letterhead.name
        self.assertEqual(self.client.post(url, {"fixture_letterhead": uploaded_header()}).status_code, 403)
        self.region.refresh_from_db()
        self.assertEqual(self.region.fixture_letterhead.name, before)

    def test_staff_admin_cannot_forge_cross_region_letterhead_upload(self):
        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        self.admin.user_permissions.add(Permission.objects.get(codename="change_region", content_type__app_label="leagues"))
        self.client.force_login(self.admin)
        response = self.client.post(reverse("admin:leagues_region_change", args=[self.other_region.pk]), {
            "name": self.other_region.name, "slug": self.other_region.slug,
            "display_order": 100, "is_active": "on", "_save": "Save",
            "fixture_letterhead": uploaded_header(),
        })
        self.assertEqual(response.status_code, 302)
        self.other_region.refresh_from_db()
        self.assertFalse(self.other_region.fixture_letterhead)

    def test_upload_rejects_wrong_content_even_with_png_extension(self):
        self.client.force_login(self.admin)
        url = reverse("league_manager:regional_fixture_letterhead", args=[self.region.slug])
        before = self.region.fixture_letterhead.name
        self.assertContains(self.client.post(url, {"fixture_letterhead": uploaded_header(format="GIF")}), "Upload a PNG or JPEG")
        self.region.refresh_from_db()
        self.assertEqual(self.region.fixture_letterhead.name, before)

    def test_mixed_zonal_sections_repeat_artwork_and_clear_unbranded_header(self):
        data = self.data([self.zonal, self.unbranded])
        # Force continuation pages while keeping the existing grouped fixture rows.
        group = data["entries"][0]["sections"][0]["streams"][0]["venues"][0]["groups"][0]
        group["rows"] *= 100
        pages = pdf_pages(render_pdf(data))
        self.assertGreater(len(pages), 2)
        self.assertTrue(all(image_refs(page) for page in pages[:-1]))
        self.assertFalse(image_refs(pages[-1]))
        with ZipFile(BytesIO(render_docx(data))) as archive:
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            document = ET.fromstring(archive.read("word/document.xml"))
            sections = document.findall(".//w:sectPr", ns)
            self.assertEqual(len(sections), 2)
            self.assertEqual(sections[0].find("w:headerReference", ns).get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"), "header1")
            self.assertIn(b"w:drawing", archive.read("word/header1.xml"))
            self.assertNotIn(b"w:drawing", archive.read("word/header2.xml"))
            self.assertEqual(sections[1].find("w:pgMar", ns).get("{" + ns["w"] + "}top"), "840")
            self.assertIn(b'w:val="18"', archive.read("word/footer1.xml"))

    
    def test_multipage_pdf_reuses_letterhead_image(self):
        data = self.data([self.zonal])

        group = (
            data["entries"][0]
            ["sections"][0]
            ["streams"][0]
            ["venues"][0]
            ["groups"][0]
        )

        # Force several pages of fixtures.
        group["rows"] *= 100

        pages = pdf_pages(render_pdf(data))
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(image_refs(page) for page in pages))
        self.assertEqual(len(set.union(*(image_refs(page) for page in pages))), 1)

    def test_storage_without_local_paths_and_unbranded_layout(self):
        storage = InMemoryStorage()
        field = self.region._meta.get_field("fixture_letterhead")
        with patch.object(field, "storage", storage):
            region = Region.objects.get(pk=self.region.pk)
            region.fixture_letterhead.save("remote.png", uploaded_header())
            competition = Competition.objects.select_related("series__region").get(pk=self.regional.pk)
            data = self.data([competition])
            self.assertTrue(image_refs(pdf_pages(render_pdf(data))[0]))
            with ZipFile(BytesIO(render_docx(data))) as archive:
                self.assertIn("word/media/letterhead1.png", archive.namelist())
        data = self.data([self.unbranded])
        self.assertFalse(image_refs(pdf_pages(render_pdf(data))[0]))
        with ZipFile(BytesIO(render_docx(data))) as archive:
            self.assertNotIn("word/header1.xml", archive.namelist())
            self.assertIn(b'w:top="840"', archive.read("word/document.xml"))

    
    def test_result_letterhead_and_standings_footer_regression(self):
        # Ensure a completed match exists for the results report.
        self.regional.matches.update(
            status="FINISHED",
            home_score=2,
            away_score=1,
        )

        # Results PDFs now use the uploaded letterhead image.
        results = self.data([self.regional], "RESULTS")
        results_pdf = render_pdf(results)

        self.assertTrue(image_refs(pdf_pages(results_pdf)[0]))
        self.assertIn("www.esportslive.site", pdf_text(results_pdf))

        # Word results fall back to the same image if no DOCX template exists.
        with ZipFile(BytesIO(render_docx(results))) as archive:
            self.assertIn(b'w:val="16"', archive.read("word/footer1.xml"))
            self.assertIn(b"w:drawing", archive.read("word/header1.xml"))

        # Standings retain the original PDF layout.
        standings = self.data([self.regional], "STANDINGS")
        standings_pdf = render_pdf(standings)

        self.assertNotIn(b"/Letterhead Do", standings_pdf)
        self.assertIn(b"/F1 9 Tf", standings_pdf)

        with ZipFile(BytesIO(render_docx(standings))) as archive:
            self.assertIn(b'w:val="16"', archive.read("word/footer1.xml"))
            self.assertNotIn("word/header1.xml", archive.namelist())
