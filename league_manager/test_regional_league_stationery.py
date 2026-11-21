from io import BytesIO

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from docx import Document
from docx.oxml.ns import qn
from PIL import Image, ImageColor

from league_manager import test_fixture_letterheads as fixtures
from league_manager.models import LeagueMembership
from league_manager.regional_reports import fixture_letterhead_for, render_docx, render_pdf, report_template_for
from league_manager.test_word_report_exports import body_text, native_template
from leagues.models import Competition, CompetitionSeries, Region, ReportCommitteeMember, ReportDocumentSettings


class RegionalLeagueStationeryTests(TestCase):
    def setUp(self):
        self.f = fixtures.FixtureLetterheadTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        template_storage = InMemoryStorage()
        for model in (Region, CompetitionSeries):
            field = model._meta.get_field("report_template")
            self.addCleanup(setattr, field, "storage", field.storage)
            field.storage = template_storage
        self.custom = self.f.make_competition("Custom League", "REGIONAL", self.f.region)

    def url(self, competition=None, region=None):
        return reverse("league_manager:regional_league_fixture_letterhead", args=[
            (region or self.f.region).slug, (competition or self.custom).series_id,
        ])

    def attach_template(self, owner, heading):
        owner.report_template = ContentFile(native_template(heading), name="official.docx")
        owner.save(update_fields=["report_template"])

    def word_header_color(self, section):
        blip = section.header._element.xpath(".//a:blip")[0]
        image = section.header.part.related_parts[blip.get(qn("r:embed"))]
        with Image.open(BytesIO(image.blob)) as pixels:
            return pixels.convert("RGB").getpixel((0, 0))

    def test_regional_administrator_can_upload_and_clear_overrides(self):
        self.attach_template(self.f.region, "SHARED REGIONAL HEADER")
        closing = ReportDocumentSettings.objects.create(region=self.f.region, signatory_name="Regional Signatory")
        self.client.force_login(self.f.admin)
        owners_url = reverse("league_manager:regional_league_owners", args=[self.f.region.slug])
        listing = self.client.get(owners_url)
        self.assertContains(listing, self.url())
        self.assertContains(listing, "Uses shared regional stationery")
        self.assertNotContains(self.client.get(self.url()), 'name="official_settings"')
        response = self.client.post(self.url(), {
            "fixture_letterhead": fixtures.uploaded_header(color="red"),
            "report_template": SimpleUploadedFile("league.docx", native_template("LEAGUE HEADER")),
            "official_settings": "1",
            "signatory_name": "Must not replace regional signatory",
        })
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(owners_url), "Custom stationery configured")
        series = CompetitionSeries.objects.get(pk=self.custom.series_id)
        next_season = Competition(series=series, name="Future Season", season="2027")
        self.assertEqual(report_template_for(next_season).name, series.report_template.name)
        self.assertEqual(fixture_letterhead_for(next_season).name, series.fixture_letterhead.name)
        self.assertEqual(self.client.post(self.url(), {"fixture_letterhead-clear": "on", "report_template-clear": "on"}).status_code, 302)
        fresh = Competition.objects.get(pk=self.custom.pk)
        self.assertEqual(report_template_for(fresh).name, self.f.region.report_template.name)
        self.assertEqual(fixture_letterhead_for(fresh).name, self.f.region.fixture_letterhead.name)
        closing.refresh_from_db()
        self.assertEqual(closing.signatory_name, "Regional Signatory")
        self.assertFalse(ReportDocumentSettings.objects.filter(series=series).exists())

    def test_override_route_is_scoped_to_the_regions_own_regional_leagues(self):
        LeagueMembership.objects.create(user=self.f.owner, competition=self.custom, role="OWNER")
        self.client.force_login(self.f.owner)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.client.post(self.url(), {"fixture_letterhead": fixtures.uploaded_header()}).status_code, 403)
        self.client.force_login(self.f.admin)
        other_league = self.f.make_competition("Other Region League", "REGIONAL", self.f.other_region)
        for league in (other_league, self.f.zonal, self.f.ewfa, self.f.ple):
            with self.subTest(league=league):
                self.assertEqual(self.client.get(self.url(league)).status_code, 404)
                self.assertEqual(self.client.post(self.url(league), {"fixture_letterhead": fixtures.uploaded_header()}).status_code, 404)
        self.assertEqual(self.client.get(self.url(other_league, self.f.other_region)).status_code, 403)

    def test_combined_exports_switch_letterheads_without_repeating_the_regional_closing(self):
        self.custom.series.fixture_letterhead.save("custom.png", fixtures.uploaded_header(color="red"))
        closing = ReportDocumentSettings.objects.create(
            region=self.f.region, signoff_enabled=True, signatory_name="Regional Signer", committee_enabled=True,
        )
        ReportCommitteeMember.objects.create(settings=closing, position="Chair", name="Regional Member")
        other = self.f.make_competition("Another Shared League", "REGIONAL", self.f.region)
        competitions = [self.f.regional, self.custom, other]
        expected = [ImageColor.getrgb(color) for color in ("navy", "red", "navy")]

        pdf = render_pdf(self.f.data(competitions))
        pages = fixtures.pdf_pages(pdf)
        self.assertEqual(len(pages), 3)
        self.assertEqual([page.images[0].image.convert("RGB").getpixel((0, 0)) for page in pages], expected)
        self.assertEqual(fixtures.image_refs(pages[0]), fixtures.image_refs(pages[2]))
        for page, league in zip(pages, competitions):
            self.assertIn(league.name.upper(), page.extract_text())
        self.assertNotIn("Regional Signer", "\n".join(page.extract_text() for page in pages[:2]))
        self.assertIn("Regional Signer", pages[-1].extract_text())

        word = render_docx(self.f.data(competitions))
        document = Document(BytesIO(word))
        self.assertEqual(len(document.sections), 3)
        self.assertEqual([self.word_header_color(section) for section in document.sections], expected)
        self.assertEqual(body_text(word).count("Regional Signer"), 1)

        self.attach_template(self.f.region, "SHARED REGIONAL WORD HEADER")
        word = render_docx(self.f.data(competitions))
        document = Document(BytesIO(word))
        self.assertEqual(len(document.sections), 3)
        for index in (0, 2):
            self.assertIn("SHARED REGIONAL WORD HEADER", document.sections[index].header.paragraphs[0].text)
        self.assertEqual(self.word_header_color(document.sections[1]), ImageColor.getrgb("red"))
        self.assertEqual(document.sections[1].page_width, document.sections[0].page_width)
        self.assertEqual(document.sections[1].left_margin, document.sections[0].left_margin)
        self.assertIn("Official association contact", document.sections[1].footer.paragraphs[0].text)
        width = document.sections[1].page_width - document.sections[1].left_margin - document.sections[1].right_margin
        extent = document.sections[1].header._element.xpath(".//wp:extent")[0]
        self.assertLessEqual(int(extent.get("cx")), width)
        text = body_text(word)
        self.assertEqual(text.count("Regional Signer"), 1)
        self.assertEqual(text.count("Regional Member"), 1)
        self.assertGreater(text.index("Regional Signer"), text.index("Another Shared League B"))

    def test_league_word_template_keeps_the_shared_pdf_letterhead_in_results(self):
        self.attach_template(self.f.region, "SHARED RESULTS LETTERHEAD")
        self.attach_template(self.custom.series, "CUSTOM RESULTS LETTERHEAD")
        for league in (self.f.regional, self.custom):
            league.matches.update(status="FINISHED", home_score=2, away_score=1)
        data = self.f.data([self.f.regional, self.custom], "RESULTS")
        pdf = fixtures.pdf_pages(render_pdf(data))
        self.assertEqual(len(pdf), 1)
        self.assertIn("Regional A", pdf[0].extract_text())
        self.assertIn("Custom League A", pdf[0].extract_text())
        self.assertEqual(pdf[0].images[0].image.convert("RGB").getpixel((0, 0)), ImageColor.getrgb("navy"))
        word = Document(BytesIO(render_docx(data)))
        self.assertEqual(len(word.sections), 2)
        self.assertIn("SHARED RESULTS LETTERHEAD", word.sections[0].header.paragraphs[0].text)
        self.assertIn("CUSTOM RESULTS LETTERHEAD", word.sections[1].header.paragraphs[0].text)

    def test_single_league_empty_downloads_use_the_override(self):
        self.attach_template(self.f.region, "SHARED REGIONAL WORD HEADER")
        self.custom.series.fixture_letterhead.save("custom.png", fixtures.uploaded_header(color="red"))
        self.custom.matches.all().delete()
        self.client.force_login(self.f.admin)
        url = reverse("league_manager:league_report_export", args=[self.custom.slug, "fixtures"])
        self.assertContains(self.client.get(url), self.url())
        pdf = self.client.post(url, {"output_format": "PDF"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        page = fixtures.pdf_pages(pdf.content)[0]
        self.assertIn("No fixtures/results", page.extract_text())
        self.assertEqual(page.images[0].image.convert("RGB").getpixel((0, 0)), ImageColor.getrgb("red"))
        docx = self.client.post(url, {"output_format": "DOCX"})
        self.assertIn("No applicable fixtures/results", body_text(docx.content))
        document = Document(BytesIO(docx.content))
        self.assertEqual(self.word_header_color(document.sections[0]), ImageColor.getrgb("red"))
