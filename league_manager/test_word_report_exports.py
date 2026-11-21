"""End-to-end selection and export integration for reusable Word stationery."""

from io import BytesIO
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.test import TestCase
from django.urls import reverse
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches
from docx.text.paragraph import Paragraph

from league_manager.models import RegionalReportPreference
from league_manager.regional_reports import (
    ReportExportError, _render_legacy_docx, build_regional_report_data,
    render_docx, render_pdf, report_template_for,
)
from league_manager import test_fixture_letterheads as fixture_helpers
from leagues.models import Competition, CompetitionSeries, FootballOrganisation, Region


def native_template(title):
    document = Document()
    section = document.sections[0]
    section.header.paragraphs[0].text = title
    section.top_margin = Inches(1.25)
    section.footer.paragraphs[0].text = 'Official association contact'
    document.add_paragraph('OLD SAMPLE MATCH MUST NOT SURVIVE')
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def body_text(payload):
    document = Document(BytesIO(payload))
    return '\n'.join(Paragraph(element, document).text for element in document.element.body.xpath('.//w:p'))


class WordReportExportIntegrationTests(TestCase):
    def setUp(self):
        self.fixture = fixture_helpers.FixtureLetterheadTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.storage = InMemoryStorage()
        for model in (Region, FootballOrganisation, CompetitionSeries):
            field = model._meta.get_field('report_template')
            before = field.storage
            field.storage = self.storage
            self.addCleanup(setattr, field, 'storage', before)

    def attach(self, owner, title):
        owner.report_template = ContentFile(native_template(title), name='official.docx')
        owner.save(update_fields=['report_template'])

    def fresh(self, competition):
        return Competition.objects.select_related('series__region').get(pk=competition.pk)

    def test_shared_classification_and_zonal_season_retention(self):
        f = self.fixture
        for competition, owner, title in [
            (f.regional, f.region, 'MANZINI OFFICIAL'),
            (f.ewfa, f.ewfa_org, 'EWFA OFFICIAL'),
            (f.ple, f.ple_org, 'PLE OFFICIAL'),
            (f.zonal, f.zonal.series, 'NHLAMBENI OFFICIAL'),
        ]:
            self.attach(owner, title)
            self.assertEqual(report_template_for(self.fresh(competition)).name, owner.report_template.name)
        f.zonal.series.region = f.region
        self.assertEqual(report_template_for(f.zonal).name, f.zonal.series.report_template.name)
        future = Competition(name='Future season', season='2027', series=f.zonal.series)
        self.assertEqual(report_template_for(future).name, f.zonal.series.report_template.name)
        self.assertFalse(report_template_for(self.fresh(f.unbranded)))

    def test_fixture_body_replaces_sample_and_word_header_overrides_image(self):
        f = self.fixture
        self.attach(f.region, 'MANZINI OFFICIAL')
        data = f.data([self.fresh(f.regional)])
        payload = render_docx(data)
        document = Document(BytesIO(payload))
        self.assertIn('MANZINI OFFICIAL', document.sections[0].header.paragraphs[0].text)
        self.assertIn('Regional A', body_text(payload))
        self.assertNotIn('OLD SAMPLE', body_text(payload))
        footer = '\n'.join(p.text for p in document.sections[0].footer.paragraphs)
        self.assertIn('Official association contact', footer)
        self.assertIn('Follow live scores, fixtures & standings', footer)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertFalse(any(name.endswith('.png') for name in archive.namelist()))

    def test_same_template_is_used_for_results(self):
        f = self.fixture
        self.attach(f.region, 'SHARED FIXTURE AND RESULT HEADER')
        f.regional.matches.update(status='FINISHED', home_score=2, away_score=1)
        payload = render_docx(f.data([self.fresh(f.regional)], 'RESULTS'))
        document = Document(BytesIO(payload))
        self.assertIn('SHARED FIXTURE AND RESULT HEADER', document.sections[0].header.paragraphs[0].text)
        self.assertIn('RESULTS', body_text(payload))
        self.assertIn('Regional A', body_text(payload))
        self.assertNotIn('OLD SAMPLE', body_text(payload))

    
    def test_pdf_uses_image_without_word_conversion(self):
        f = self.fixture

        # A Word template exists for the region.
        self.attach(f.region, "ONE SHARED LAYOUT")

        data = f.data([self.fresh(f.regional)])

        # Conversion must never be called.
        with patch(
            "subprocess.Popen",
            side_effect=AssertionError(
                "Word conversion must not be used for PDFs"
            ),
        ) as convert:
            pdf = render_pdf(data)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertTrue(fixture_helpers.image_refs(fixture_helpers.pdf_pages(pdf)[0]))
        self.assertIn("Regional A", fixture_helpers.pdf_text(pdf))

        convert.assert_not_called()

    
    def test_pdf_without_letterhead_image_still_works(self):
        f = self.fixture

        # Keep a valid Word template.
        self.attach(f.region, "OFFICIAL")

        # Simulate an organisation without a PDF image.
        f.region.fixture_letterhead.delete(save=True)

        data = f.data([self.fresh(f.regional)])

        with patch(
            "subprocess.Popen",
            side_effect=AssertionError(
                "PDF should not require a converter"
            ),
        ) as convert:
            pdf = render_pdf(data)

        self.assertTrue(pdf.startswith(b"%PDF"))

        # No image means standard PDF layout.
        self.assertFalse(fixture_helpers.image_refs(fixture_helpers.pdf_pages(pdf)[0]))

        convert.assert_not_called()

    def test_corrupt_stored_template_fails_without_silent_branding_loss(self):
        f = self.fixture
        f.region.report_template = ContentFile(b'not a docx', name='broken.docx')
        f.region.save(update_fields=['report_template'])
        with self.assertRaises(ReportExportError):
            render_docx(f.data([self.fresh(f.regional)]))

    def test_mixed_zonal_templates_and_unbranded_league_have_independent_sections(self):
        f = self.fixture
        other = f.make_competition('Another Zonal', 'ZONAL')
        self.attach(f.zonal.series, 'NHLAMBENI OFFICIAL')
        self.attach(other.series, 'OTHER ZONAL OFFICIAL')
        payload = render_docx(f.data([self.fresh(f.zonal), self.fresh(f.unbranded), self.fresh(other)]))
        document = Document(BytesIO(payload))
        self.assertEqual(len(document.sections), 3)
        headers = ['\n'.join(p.text for p in section.header.paragraphs) for section in document.sections]
        self.assertIn('NHLAMBENI OFFICIAL', headers[0])
        self.assertEqual(headers[1].strip(), '')
        self.assertIn('OTHER ZONAL OFFICIAL', headers[2])
        self.assertFalse(document.sections[1].header.is_linked_to_previous)
        self.assertEqual(document.sections[1].top_margin.twips, 840)
        self.assertNotIn('OLD SAMPLE', body_text(payload))

    def test_shared_regional_header_keeps_multiple_leagues_together(self):
        f = self.fixture
        other = f.make_competition('Other Regional', 'REGIONAL', f.region)
        self.attach(f.region, 'SHARED REGIONAL HEADER')
        payload = render_docx(f.data([self.fresh(f.regional), self.fresh(other)]))
        document = Document(BytesIO(payload))
        self.assertEqual(len(document.sections), 1)
        self.assertIn('Regional A', body_text(payload))
        self.assertIn('Other Regional A', body_text(payload))

    def test_empty_regional_report_still_uses_the_saved_template(self):
        f = self.fixture
        self.attach(f.region, 'OFFICIAL EMPTY REPORT')
        f.regional.matches.all().delete()
        preference = RegionalReportPreference(user=f.admin, region=f.region, report_kind='FIXTURES')
        data = build_regional_report_data(region=f.region, preference=preference)
        payload = render_docx(data)
        self.assertIn('OFFICIAL EMPTY REPORT', Document(BytesIO(payload)).sections[0].header.paragraphs[0].text)
        self.assertIn('No applicable fixtures/results', body_text(payload))

    def test_no_word_template_and_standings_keep_the_existing_renderer(self):
        f = self.fixture
        data = f.data([self.fresh(f.regional)])
        self.assertEqual(render_docx(data), _render_legacy_docx(data))
        with patch('subprocess.Popen') as convert:
            self.assertTrue(render_pdf(data).startswith(b'%PDF'))
            convert.assert_not_called()
        self.attach(f.region, 'NOT FOR STANDINGS')
        data = f.data([self.fresh(f.regional)], 'STANDINGS')
        self.assertEqual(render_docx(data), _render_legacy_docx(data))

    
    def test_real_regional_download_uses_native_pdf(self):
        f = self.fixture

        self.attach(f.region, "DOWNLOAD HEADER")
        self.client.force_login(f.admin)

        url = reverse(
            "league_manager:regional_report_export",
            args=[f.region.slug, "fixtures"],
        )

        posted = {
            "selection_mode": "ALL",
            "output_format": "DOCX",
            "action": "download",
        }

        # First verify the Word export remains functional.
        response = self.client.post(url, posted)

        self.assertEqual(response.status_code, 200)

        document = Document(BytesIO(response.content))

        self.assertIn(
            "DOWNLOAD HEADER",
            document.sections[0].header.paragraphs[0].text,
        )

        # Now request PDF for the same report.
        posted["output_format"] = "PDF"

        with patch(
            "subprocess.Popen",
            side_effect=AssertionError(
                "PDF converter should not be called"
            ),
        ) as convert:
            response = self.client.post(url, posted)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/pdf",
        )
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertTrue(fixture_helpers.image_refs(fixture_helpers.pdf_pages(response.content)[0]))

        convert.assert_not_called()

    def test_word_fixture_tables_use_the_entire_width_of_wide_and_narrow_templates(self):
        f = self.fixture
        data = f.data([self.fresh(f.regional)])
        group = data["entries"][0]["sections"][0]["streams"][0]["venues"][0]["groups"][0]
        group["rows"][0].update(home="A very long football club name that can wrap in Word", pdf_time="3:00 PM", status="Upcoming")
        for margin in (0.4, 1.1):
            with self.subTest(margin=margin):
                template = Document(BytesIO(native_template("OFFICIAL STATIONERY")))
                section = template.sections[0]
                section.left_margin = section.right_margin = Inches(margin)
                section._sectPr.find(qn("w:cols")).set(qn("w:num"), "2")
                stream = BytesIO()
                template.save(stream)
                f.region.report_template = ContentFile(stream.getvalue(), name="official.docx")
                f.region.save(update_fields=["report_template"])
                data["entries"][0]["report_template"] = f.region.report_template

                doc = Document(BytesIO(render_docx(data)))
                section = doc.sections[0]
                table = doc.tables[0]
                self.assertEqual(section._sectPr.find(qn("w:cols")).get(qn("w:num")), "1")
                self.assertEqual(sum(col.width for col in table.columns), section.page_width - section.left_margin - section.right_margin)
                self.assertFalse(table.autofit)
                self.assertEqual(len(table.columns), 4)
                self.assertEqual(table.cell(1, 3).text, "KICKOFF")
                self.assertEqual(table.cell(2, 0).text, group["rows"][0]["home"])
                self.assertEqual(table.cell(2, 1).text, "VS")
                self.assertEqual(table.cell(2, 3).text, "3:00 PM\nUpcoming")
                self.assertEqual(table.cell(2, 3).paragraphs[0].alignment, 2)
                self.assertTrue(table.rows[0]._tr.xpath("./w:trPr/w:tblHeader"))
                self.assertTrue(table.rows[1]._tr.xpath("./w:trPr/w:tblHeader"))
                self.assertTrue(table.rows[2]._tr.xpath("./w:trPr/w:cantSplit"))
                self.assertTrue(doc.paragraphs[-1]._p.xpath("./w:pPr/w:rPr/w:vanish"))
                self.assertIn("OFFICIAL STATIONERY", section.header.paragraphs[0].text)

    def test_word_results_table_keeps_score_and_note_editable(self):
        f = self.fixture
        f.regional.matches.update(status="FINISHED", home_score=2, away_score=2, penalty_home_score=4, penalty_away_score=5)
        doc = Document(BytesIO(render_docx(f.data([f.regional], "RESULTS"))))
        table = doc.tables[0]
        self.assertEqual(len(table.columns), 3)
        self.assertEqual([cell.text for cell in table.rows[0].cells], ["HOME TEAM", "SCORE", "AWAY TEAM"])
        self.assertEqual([cell.text for cell in table.rows[1].cells], ["Regional A", "(4)2-2(5)", "Regional B"])
        self.assertEqual(table.cell(2, 0).text, "4-5 on penalties")
        self.assertTrue(table.cell(1, 1).paragraphs[0].paragraph_format.keep_with_next)
