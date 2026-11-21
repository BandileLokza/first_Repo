"""Package-level regression tests for editable native Word report stationery."""

from io import BytesIO
import posixpath
from types import SimpleNamespace
import unittest
from zipfile import ZipFile

from django.test import override_settings
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from lxml import etree
from PIL import Image

from league_manager.report_template_rendering import (
    FOOTER_MESSAGE, NS, R, W, ReportTemplateRenderingError, render_template_docx,
)


class _Storage:
    """Deliberately supplies no local path method, like remote media storage."""
    def __init__(self, contents):
        self.contents = contents
        self.opened = []

    def open(self, name, mode):
        self.opened.append((name, mode))
        return BytesIO(self.contents)


def _saved(document):
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def _field(contents):
    return SimpleNamespace(name="official/header.docx", storage=_Storage(contents))


def _hyperlink(paragraph, text, url):
    rid = paragraph.part.relate_to(url, R + "/hyperlink", is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    run = OxmlElement("w:r")
    value = OxmlElement("w:t")
    value.text = text
    run.append(value)
    link.append(run)
    paragraph._p.append(link)


def _report(title, *, image=False):
    document = Document()
    document.add_paragraph(title)
    document.add_paragraph("Home FC vs Away United 15:00")
    footer = document.sections[0].footer
    footer.paragraphs[0].text = FOOTER_MESSAGE
    _hyperlink(footer.add_paragraph(), "www.esportslive.site", "https://www.esportslive.site")
    if image:
        document.sections[0].header.paragraphs[0].text = "LEGACY IMAGE HEADER"
    return _saved(document)


def _template(title, color="123456", *, first_page=False, duplicate_footer=False):
    document = Document()
    section = document.sections[0]
    section.page_width = Inches(8.3)
    section.page_height = Inches(11.7)
    section.top_margin = Inches(.6)
    section.bottom_margin = Inches(.6)
    section.header_distance = Inches(.2)
    section.footer_distance = Inches(.2)
    section.different_first_page_header_footer = first_page
    style = document.styles.add_style("OfficialLetterhead", WD_STYLE_TYPE.PARAGRAPH)
    style.font.color.rgb = RGBColor.from_string(color)
    style.font.size = Pt(14)
    header = section.first_page_header if first_page else section.header
    header.paragraphs[0].text = title
    header.paragraphs[0].style = style
    _hyperlink(header.add_paragraph(), "Association website", "https://association.example/" + title)
    image = BytesIO()
    Image.new("RGB", (300, 100), "#" + color).save(image, "PNG")
    image.seek(0)
    header.add_paragraph().add_run().add_picture(image, width=Inches(3))
    section.footer.paragraphs[0].text = FOOTER_MESSAGE if duplicate_footer else title + " native footer"
    if duplicate_footer:
        _hyperlink(section.footer.add_paragraph(), "www.esportslive.site", "https://www.esportslive.site")
    document.add_paragraph("SAMPLE BODY MUST BE REMOVED")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "SAMPLE TABLE MUST BE REMOVED"
    return _field(_saved(document))


def _xml(archive, path):
    return etree.fromstring(archive.read(path))


def _target(archive, part, rid):
    directory, name = posixpath.split(part)
    rels = _xml(archive, posixpath.join(directory, "_rels", name + ".rels"))
    relationship = next(node for node in rels if node.get("Id") == rid)
    return posixpath.normpath(posixpath.join(directory, relationship.get("Target")))


class NativeWordReportTemplateTests(unittest.TestCase):
    def test_native_header_artwork_styles_links_and_page_setup_survive(self):
        template = _template("REGIONAL ASSOCIATION")
        output = render_template_docx([{"template": template, "document": _report("Fixtures for Regional League")}])
        self.assertEqual(template.storage.opened, [(template.name, "rb")])
        document = Document(BytesIO(output))
        self.assertIn("Fixtures for Regional League", "\n".join(paragraph.text for paragraph in document.paragraphs))
        self.assertNotIn("SAMPLE", "\n".join(paragraph.text for paragraph in document.paragraphs))
        self.assertFalse(document.tables)
        self.assertAlmostEqual(document.sections[0].page_width.inches, 8.3, places=2)
        with ZipFile(BytesIO(output)) as archive:
            main = _xml(archive, "word/document.xml")
            section = main.find(".//w:sectPr", NS)
            header_path = _target(archive, "word/document.xml", section.find("w:headerReference", NS).get(qn("r:id")))
            header = _xml(archive, header_path)
            self.assertIn("REGIONAL ASSOCIATION", "".join(header.itertext()))
            style_id = header.find(".//w:pStyle", NS).get(qn("w:val"))
            styles = _xml(archive, "word/styles.xml")
            selected_style = next(node for node in styles if node.get(qn("w:styleId")) == style_id)
            self.assertEqual(selected_style.find("w:rPr/w:color", NS).get(qn("w:val")), "123456")
            for image in header.findall(".//a:blip", NS):
                self.assertTrue(archive.read(_target(archive, header_path, image.get(qn("r:embed")))).startswith(b"\x89PNG"))
            self.assertGreater(int(section.find("w:pgMar", NS).get(qn("w:top"))), round(.6 * 1440))
            self._assert_relationships_resolve(archive)

    def test_template_footer_is_retained_and_platform_footer_added_once(self):
        output = render_template_docx([{"template": _template("EWFA"), "document": _report("Women Fixtures")}])
        with ZipFile(BytesIO(output)) as archive:
            main = _xml(archive, "word/document.xml")
            footer_path = _target(archive, "word/document.xml", main.find(".//w:footerReference", NS).get(qn("r:id")))
            footer = _xml(archive, footer_path)
            text = "".join(footer.itertext())
            self.assertIn("EWFA native footer", text)
            self.assertEqual(text.count(FOOTER_MESSAGE), 1)
            self.assertIn("www.esportslive.site", text)
            self._assert_relationships_resolve(archive)

    def test_existing_platform_footer_is_not_duplicated(self):
        output = render_template_docx([{"template": _template("PLE", duplicate_footer=True), "document": _report("PLE Fixtures")}])
        document = Document(BytesIO(output))
        text = "".join(node.text or "" for node in document.sections[0].footer._element.findall(".//w:t", NS))
        self.assertEqual(text.count(FOOTER_MESSAGE), 1)

    def test_mixed_zonal_sections_do_not_inherit_artwork_or_styles(self):
        output = render_template_docx([
            {"template": _template("NHLAMBENI", "112233"), "document": _report("First league")},
            {"template": None, "document": _report("League without branding")},
            {"template": None, "document": _report("Legacy image league", image=True)},
            {"template": _template("SECOND ZONE", "AA2233"), "document": _report("Fourth league")},
        ])
        document = Document(BytesIO(output))
        self.assertEqual(len(document.sections), 4)
        texts = ["".join(section.header._element.itertext()) for section in document.sections]
        self.assertIn("NHLAMBENI", texts[0])
        self.assertEqual(texts[1], "")
        self.assertIn("LEGACY IMAGE HEADER", texts[2])
        self.assertIn("SECOND ZONE", texts[3])
        for section in document.sections:
            self.assertFalse(section.header.is_linked_to_previous)
            self.assertFalse(section.footer.is_linked_to_previous)
        # The unbranded section keeps the exact legacy page margin.
        self.assertEqual(document.sections[1].top_margin, Document(BytesIO(_report("base"))).sections[0].top_margin)
        with ZipFile(BytesIO(output)) as archive:
            styles = _xml(archive, "word/styles.xml")
            official = [node for node in styles if node.get(qn("w:styleId"), "").endswith("_OfficialLetterhead")]
            self.assertEqual(len(official), 2)
            self.assertEqual({node.find("w:rPr/w:color", NS).get(qn("w:val")) for node in official}, {"112233", "AA2233"})
            numbering = _xml(archive, "word/numbering.xml")
            ranks = {qn("w:numPicBullet"): 0, qn("w:abstractNum"): 1, qn("w:num"): 2}
            self.assertEqual([ranks.get(node.tag, 3) for node in numbering], sorted(ranks.get(node.tag, 3) for node in numbering))
            self._assert_relationships_resolve(archive)

    def test_first_page_only_native_header_is_promoted_to_repeating_header(self):
        template = _template("FIRST HEADER", first_page=True)
        # Word can leave an explicit blank default header behind when a user
        # chooses a distinct first-page header. The artwork must still repeat.
        document = Document(BytesIO(template.storage.contents))
        document.sections[0].header.paragraphs[0].text = ""
        output = render_template_docx([{"template": _field(_saved(document)), "document": _report("Long report")}])
        document = Document(BytesIO(output))
        self.assertFalse(document.sections[0].different_first_page_header_footer)
        self.assertIn("FIRST HEADER", "".join(document.sections[0].header._element.itertext()))

    def test_invalid_and_missing_stored_templates_fail_explicitly(self):
        for field in (_field(b"not a word package"), SimpleNamespace(name="missing.docx", storage=SimpleNamespace(open=lambda *args: (_ for _ in ()).throw(OSError("missing"))))):
            with self.subTest(field=field.name), self.assertRaises(ReportTemplateRenderingError):
                render_template_docx([{"template": field, "document": _report("Fixtures")}])

    def test_body_only_artwork_cannot_silently_replace_native_header(self):
        document = Document()
        document.add_paragraph("BODY LETTERHEAD")
        with self.assertRaisesRegex(ReportTemplateRenderingError, "Header"):
            render_template_docx([{"template": _field(_saved(document)), "document": _report("Fixtures")}])

    @override_settings(REPORT_TEMPLATE_MAX_BYTES=64)
    def test_storage_read_is_bounded_and_oversized_stored_template_is_rejected(self):
        sizes = []

        class Reader(BytesIO):
            def read(self, size=-1):
                sizes.append(size)
                return super().read(size)

        field = SimpleNamespace(name="oversized.docx", storage=SimpleNamespace(open=lambda *args: Reader(b"x" * 500)))
        with self.assertRaisesRegex(ReportTemplateRenderingError, "upload size"):
            render_template_docx([{"template": field, "document": _report("Fixtures")}])
        self.assertEqual(sizes, [65])

    def test_stored_template_is_validated_again_before_resource_import(self):
        template = _template("SAFE HEADER")
        altered = BytesIO()
        with ZipFile(BytesIO(template.storage.contents)) as source, ZipFile(altered, "w") as target:
            for name in source.namelist():
                target.writestr(name, source.read(name))
            target.writestr("word/embeddings/object.bin", b"active embedded object")
        with self.assertRaisesRegex(ReportTemplateRenderingError, "macro-free"):
            render_template_docx([{"template": _field(altered.getvalue()), "document": _report("Fixtures")}])

    def test_fixed_report_table_fits_native_printable_width(self):
        source = Document(BytesIO(_report("Standings")))
        table = source.add_table(rows=2, cols=2)
        table.autofit = False
        table.columns[0].width = Inches(4)
        table.columns[1].width = Inches(3)
        table.cell(0, 0).text = "Team"
        table.cell(1, 0).text = "A long but still editable football club name"
        output = render_template_docx([{"template": _template("ASSOCIATION"), "document": _saved(source)}])
        document = Document(BytesIO(output))
        section = document.sections[0]
        self.assertLessEqual(sum(column.width for column in document.tables[0].columns), section.page_width - section.left_margin - section.right_margin + 635)
        self.assertEqual(document.tables[0].cell(1, 0).text, "A long but still editable football club name")

    def test_native_printer_settings_are_not_copied_to_the_report(self):
        template = _template("WORD SAVED HEADER")
        with ZipFile(BytesIO(template.storage.contents)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        document = etree.fromstring(files["word/document.xml"])
        section = document.find(".//w:sectPr", NS)
        etree.SubElement(section, qn("w:printerSettings"), {qn("r:id"): "nativePrinter"})
        files["word/document.xml"] = etree.tostring(document)
        relationships = etree.fromstring(files["word/_rels/document.xml.rels"])
        etree.SubElement(relationships, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship", Id="nativePrinter", Type=R + "/printerSettings", Target="printerSettings/printerSettings1.bin")
        files["word/_rels/document.xml.rels"] = etree.tostring(relationships)
        types = etree.fromstring(files["[Content_Types].xml"])
        etree.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override", PartName="/word/printerSettings/printerSettings1.bin", ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.printerSettings")
        files["[Content_Types].xml"] = etree.tostring(types)
        files["word/printerSettings/printerSettings1.bin"] = b"native machine printer configuration"
        stored = BytesIO()
        with ZipFile(stored, "w") as archive:
            for name, contents in files.items():
                archive.writestr(name, contents)
        output = render_template_docx([{"template": _field(stored.getvalue()), "document": _report("Fixtures")}])
        with ZipFile(BytesIO(output)) as archive:
            self.assertFalse(any("printerSettings" in name for name in archive.namelist()))
            self.assertFalse(_xml(archive, "word/document.xml").findall(".//w:printerSettings", NS))
            for name in archive.namelist():
                if name.endswith(".rels"):
                    self.assertFalse(any(node.get("Type", "").endswith("/printerSettings") for node in _xml(archive, name)))
            self._assert_relationships_resolve(archive)

    def _assert_relationships_resolve(self, archive):
        names = set(archive.namelist())
        for path in names:
            if not path.endswith(".rels"):
                continue
            owner_directory = posixpath.dirname(posixpath.dirname(path))
            for relationship in _xml(archive, path):
                if relationship.get("TargetMode") == "External":
                    continue
                target = relationship.get("Target")
                resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(owner_directory, target))
                self.assertIn(resolved, names, (path, target))


if __name__ == "__main__":
    unittest.main()
