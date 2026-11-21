"""Apply native Word stationery to editable football report bodies.

Each input document is one league, already rendered by regional_reports.  A
template supplies its native header, footer and page setup; its sample body is
discarded.  Package relationships are copied recursively, rather than turning
the stationery or the report into a screenshot.  Each league has an explicit
header and footer, so Word cannot inherit branding from the previous league.
"""

from copy import deepcopy
from io import BytesIO
import posixpath
import re
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from django.conf import settings
from django.core.exceptions import ValidationError
from lxml import etree

from leagues.validators import validate_report_template_bytes


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
C = "http://schemas.openxmlformats.org/package/2006/content-types"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS = {"w": W, "r": R, "a": A, "wp": WP}
FOOTER_MESSAGE = "Follow live scores, fixtures & standings"
STYLE_REFS = {"pStyle", "rStyle", "tblStyle", "basedOn", "next", "link", "styleLink", "numStyleLink"}


class ReportTemplateRenderingError(ValueError):
    """The stored Word stationery cannot be used to produce an official report."""


def _xml(data):
    return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))


def _bytes(root):
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _w(name):
    return f"{{{W}}}{name}"


def _relationship_path(part):
    directory, name = posixpath.split(part)
    return posixpath.join(directory, "_rels", name + ".rels")


class _Package:
    def __init__(self, data, prefix):
        try:
            with ZipFile(BytesIO(data)) as archive:
                self.files = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
            types = _xml(self.files["[Content_Types].xml"])
            self.overrides = {node.get("PartName").lstrip("/"): node.get("ContentType") for node in types if etree.QName(node).localname == "Override"}
            self.defaults = {node.get("Extension"): node.get("ContentType") for node in types if etree.QName(node).localname == "Default"}
            root_rels = _xml(self.files["_rels/.rels"])
            office = next(node for node in root_rels if node.get("Type", "").endswith("/officeDocument"))
            self.document_path = office.get("Target").lstrip("/")
            self.document = _xml(self.files[self.document_path])
            self.body = self.document.find(_w("body"))
            if self.body is None:
                raise ValueError("Word document has no body")
        except (BadZipFile, KeyError, StopIteration, ValueError, etree.XMLSyntaxError) as exc:
            raise ReportTemplateRenderingError("The report template is not a readable .docx document. Replace it with a valid Word template.") from exc
        self.prefix = prefix
        self.styles = {}
        self.numbers = {}
        self.abstract_numbers = {}
        self.picture_bullets = {}
        self.default_style = None
        self.default_table_style = None
        self.theme_fonts = {}
        self.theme_colors = {}

    def rels(self, part):
        contents = self.files.get(_relationship_path(part))
        return _xml(contents) if contents else etree.Element(f"{{{P}}}Relationships", nsmap={None: P})

    def target(self, part, relationship):
        target = relationship.get("Target", "")
        result = posixpath.normpath(posixpath.join(posixpath.dirname(part), target)) if not target.startswith("/") else target.lstrip("/")
        if result.startswith("../") or result not in self.files:
            raise ReportTemplateRenderingError("The Word template has a missing or invalid embedded resource. Re-save it in Word and upload it again.")
        return result

    def related(self, part, kind):
        relationship = next((node for node in self.rels(part) if node.get("Type", "").endswith("/" + kind)), None)
        return self.target(part, relationship) if relationship is not None else None

    def section(self):
        sections = self.body.findall(".//w:sectPr", NS)
        if not sections:
            raise ReportTemplateRenderingError("The Word template has no page setup. Re-save it in Word and upload it again.")
        return next((section for section in sections if section.find(_w("headerReference")) is not None), sections[0])

    def reference(self, section, kind):
        references = section.findall(_w(kind + "Reference"))
        if kind == "header":
            meaningful = []
            for candidate in references:
                relation = next((node for node in self.rels(self.document_path) if node.get("Id") == candidate.get(f"{{{R}}}id")), None)
                if relation is not None:
                    header = _xml(self.files[self.target(self.document_path, relation)])
                    if any((node.tag == _w("t") and (node.text or "").strip()) or node.tag in {_w("drawing"), _w("pict")} for node in header.iter()):
                        meaningful.append(candidate)
            references = meaningful or references
        reference = next((node for node in references if node.get(_w("type")) == "default"), references[0] if references else None)
        if reference is None:
            return None
        rid = reference.get(f"{{{R}}}id")
        relationship = next((node for node in self.rels(self.document_path) if node.get("Id") == rid), None)
        if relationship is None:
            raise ReportTemplateRenderingError("The Word template has a broken header or footer relationship.")
        return self.target(self.document_path, relationship)

    def content_type(self, part):
        return self.overrides.get(part) or self.defaults.get(part.rsplit(".", 1)[-1], "application/octet-stream")


class _Writer:
    def __init__(self):
        self.files = {}
        self.content_types = {}
        self.imported = {}
        self.relationships = etree.Element(f"{{{P}}}Relationships", nsmap={None: P})
        self.styles = etree.Element(_w("styles"), nsmap={"w": W})
        self.numbering = etree.Element(_w("numbering"), nsmap={"w": W})
        self.next_number = self.next_abstract = self.next_bullet = 1
        # Generated report paragraphs use this existing style, even in the
        # original minimal DOCX packages which have no styles.xml of their own.
        style = etree.SubElement(self.styles, _w("style"), {_w("type"): "paragraph", _w("styleId"): "NoSpacing"})
        etree.SubElement(style, _w("name"), {_w("val"): "No Spacing"})
        properties = etree.SubElement(style, _w("pPr"))
        etree.SubElement(properties, _w("spacing"), {_w("before"): "0", _w("after"): "0"})

    def _rel(self, root, kind, target, external=False):
        rid = f"rId{len(root) + 1}"
        node = etree.SubElement(root, f"{{{P}}}Relationship", Id=rid, Type=kind, Target=target)
        if external:
            node.set("TargetMode", "External")
        return rid

    def main_rel(self, kind, path):
        return self._rel(self.relationships, R + "/" + kind, posixpath.relpath(path, "word"))

    def _theme(self, source):
        path = source.related(source.document_path, "theme")
        if path is None:
            return
        root = _xml(source.files[path])
        for family in ("major", "minor"):
            latin = root.find(f".//a:{family}Font/a:latin", NS)
            if latin is not None:
                source.theme_fonts[family] = latin.get("typeface")
        colors = root.find(".//a:clrScheme", NS)
        if colors is not None:
            for color in colors:
                if len(color):
                    value = color[0].get("val") or color[0].get("lastClr")
                    if value and re.fullmatch(r"[0-9A-Fa-f]{6}", value):
                        source.theme_colors[etree.QName(color).localname] = value

    def rewrite(self, root, source, *, header=False):
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            local = etree.QName(node).localname
            if node.tag.startswith("{" + W + "}"):
                value = node.get(_w("val"))
                if local in STYLE_REFS and value in source.styles:
                    node.set(_w("val"), source.styles[value])
                elif local == "numId" and value in source.numbers:
                    node.set(_w("val"), source.numbers[value])
                elif local == "abstractNumId" and value in source.abstract_numbers:
                    node.set(_w("val"), source.abstract_numbers[value])
                elif local in {"numPicBulletId", "lvlPicBulletId"} and value in source.picture_bullets:
                    node.set(_w("val"), source.picture_bullets[value])
                if local == "rFonts":
                    for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
                        theme = node.get(_w(attribute + "Theme"))
                        family = "major" if theme and theme.startswith("major") else "minor"
                        if theme and source.theme_fonts.get(family):
                            node.set(_w(attribute), source.theme_fonts[family])
                            node.attrib.pop(_w(attribute + "Theme"), None)
                for theme_attribute, concrete in (("themeColor", "val" if local == "color" else "color"), ("themeFill", "fill")):
                    theme = node.get(_w(theme_attribute))
                    aliases = {"text1": "dk1", "text2": "dk2", "background1": "lt1", "background2": "lt2"}
                    if theme and source.theme_colors.get(aliases.get(theme, theme)):
                        node.set(_w(concrete), source.theme_colors[aliases.get(theme, theme)])
                        node.attrib.pop(_w(theme_attribute), None)
            elif node.tag == f"{{{A}}}schemeClr" and node.get("val") in source.theme_colors:
                node.tag = f"{{{A}}}srgbClr"
                node.set("val", source.theme_colors[node.get("val")])
        if header and source.default_style:
            for paragraph in root.findall(".//w:p", NS):
                properties = paragraph.find(_w("pPr"))
                if properties is None:
                    properties = etree.Element(_w("pPr"))
                    paragraph.insert(0, properties)
                if properties.find(_w("pStyle")) is None:
                    properties.insert(0, etree.Element(_w("pStyle"), {_w("val"): source.default_style}))
        if header and source.default_table_style:
            for table in root.findall(".//w:tbl", NS):
                properties = table.find(_w("tblPr"))
                if properties is None:
                    properties = etree.Element(_w("tblPr"))
                    table.insert(0, properties)
                if properties.find(_w("tblStyle")) is None:
                    properties.insert(0, etree.Element(_w("tblStyle"), {_w("val"): source.default_table_style}))

    def prepare(self, source):
        self._theme(source)
        numbering_path = source.related(source.document_path, "numbering")
        numbering = _xml(source.files[numbering_path]) if numbering_path else None
        if numbering is not None:
            for node in numbering:
                local = etree.QName(node).localname
                if local == "num":
                    source.numbers[node.get(_w("numId"))] = str(self.next_number)
                    self.next_number += 1
                elif local == "abstractNum":
                    source.abstract_numbers[node.get(_w("abstractNumId"))] = str(self.next_abstract)
                    self.next_abstract += 1
                elif local == "numPicBullet":
                    source.picture_bullets[node.get(_w("numPicBulletId"))] = str(self.next_bullet)
                    self.next_bullet += 1
        style_path = source.related(source.document_path, "styles")
        styles = _xml(source.files[style_path]) if style_path else None
        if styles is not None:
            for node in styles.findall(_w("style")):
                old = node.get(_w("styleId"))
                source.styles[old] = source.prefix + "_" + old
                if node.get(_w("default")) in {"1", "true", "on"} and node.get(_w("type")) == "paragraph":
                    source.default_style = source.styles[old]
                if node.get(_w("default")) in {"1", "true", "on"} and node.get(_w("type")) == "table":
                    source.default_table_style = source.styles[old]
            source.default_style = source.default_style or source.styles.get("Normal")
            for node in styles.findall(_w("style")):
                copy = deepcopy(node)
                copy.set(_w("styleId"), source.styles[node.get(_w("styleId"))])
                copy.attrib.pop(_w("default"), None)
                name = copy.find(_w("name"))
                if name is not None:
                    name.set(_w("val"), source.prefix + " " + name.get(_w("val"), ""))
                self.rewrite(copy, source)
                # Preserve the template's implicit document defaults through
                # its own explicit paragraph style, rather than global state.
                if copy.get(_w("styleId")) == source.default_style:
                    for tag in ("pPr", "rPr"):
                        defaults = styles.find(f"w:docDefaults/w:{tag}Default/w:{tag}", NS)
                        if defaults is not None:
                            properties = copy.find(_w(tag))
                            if properties is None:
                                properties = etree.SubElement(copy, _w(tag))
                            for item in defaults:
                                if properties.find(item.tag) is None:
                                    properties.append(deepcopy(item))
                    self.rewrite(copy, source)
                self.styles.append(copy)
        if numbering is not None:
            for node in numbering:
                copy = deepcopy(node)
                for name, mapping in (("numId", source.numbers), ("abstractNumId", source.abstract_numbers), ("numPicBulletId", source.picture_bullets)):
                    old = copy.get(_w(name))
                    if old in mapping:
                        copy.set(_w(name), mapping[old])
                self.rewrite(copy, source)
                self._copy_relationships(copy, source, numbering_path, "word/numbering.xml")
                self.numbering.append(copy)

    def _copy_relationships(self, root, source, source_path, target_path, *, only_used=False):
        rel_path = _relationship_path(target_path)
        target_rels = self.relationships if target_path == "word/document.xml" else (_xml(self.files[rel_path]) if rel_path in self.files else etree.Element(f"{{{P}}}Relationships", nsmap={None: P}))
        mapping = {}
        attributes = (f"{{{R}}}id", f"{{{R}}}embed", f"{{{R}}}link", "{urn:schemas-microsoft-com:office:office}relid")
        used = {node.get(attribute) for node in root.iter() for attribute in attributes if node.get(attribute)}
        for relationship in source.rels(source_path):
            if only_used and relationship.get("Id") not in used:
                continue
            external = relationship.get("TargetMode") == "External"
            target = relationship.get("Target") if external else posixpath.relpath(self.import_part(source, source.target(source_path, relationship)), posixpath.dirname(target_path))
            mapping[relationship.get("Id")] = self._rel(target_rels, relationship.get("Type"), target, external)
        for node in root.iter():
            for attribute in attributes:
                if node.get(attribute) in mapping:
                    node.set(attribute, mapping[node.get(attribute)])
        if len(target_rels):
            self.files[rel_path] = _bytes(target_rels)

    def import_part(self, source, path):
        key = (source.prefix, path)
        if key in self.imported:
            return self.imported[key]
        target = "word/imported/" + source.prefix + "/" + path
        self.imported[key] = target
        self.content_types[target] = source.content_type(path)
        contents = source.files[path]
        if path.endswith((".xml", ".vml", ".svg")) or self.content_types[target].endswith("+xml"):
            root = _xml(contents)
            self.rewrite(root, source, header=root.tag in {_w("hdr"), _w("ftr")})
            self._copy_relationships(root, source, path, target)
            contents = _bytes(root)
        elif source.rels(path).getchildren():
            # Non-XML parts are rare but can still have package relationships.
            dummy = etree.Element("part")
            self._copy_relationships(dummy, source, path, target)
        self.files[target] = contents
        return target

    def empty_header(self, index):
        path = f"word/header-empty-{index}.xml"
        root = etree.Element(_w("hdr"), nsmap={"w": W})
        etree.SubElement(root, _w("p"))
        self.files[path] = _bytes(root)
        self.content_types[path] = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
        return path

    def footer(self, template, template_path, source, source_path):
        if not template_path:
            return self.import_part(source, source_path) if source_path else None
        path = self.import_part(template, template_path)
        root = _xml(self.files[path])
        text = "".join(node.text or "" for node in root.findall(".//w:t", NS))
        if FOOTER_MESSAGE not in text and source_path:
            footer = _xml(source.files[source_path])
            self.rewrite(footer, source, header=True)
            self._copy_relationships(footer, source, source_path, path)
            root.extend(deepcopy(child) for child in footer)
            self.files[path] = _bytes(root)
        return path

    def save(self, body):
        document = etree.Element(_w("document"), nsmap={"w": W, "r": R})
        document.append(body)
        self.files["word/document.xml"] = _bytes(document)
        self.files["word/styles.xml"] = _bytes(self.styles)
        self.main_rel("styles", "word/styles.xml")
        self.content_types["word/document.xml"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        self.content_types["word/styles.xml"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
        if len(self.numbering):
            # OOXML requires all abstract definitions before concrete lists;
            # importing several packages otherwise interleaves these groups.
            order = {_w("numPicBullet"): 0, _w("abstractNum"): 1, _w("num"): 2}
            self.numbering[:] = sorted(self.numbering, key=lambda node: order.get(node.tag, 3))
            self.files["word/numbering.xml"] = _bytes(self.numbering)
            self.main_rel("numbering", "word/numbering.xml")
            self.content_types["word/numbering.xml"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
        self.files["word/_rels/document.xml.rels"] = _bytes(self.relationships)
        root_rels = etree.Element(f"{{{P}}}Relationships", nsmap={None: P})
        self._rel(root_rels, R + "/officeDocument", "word/document.xml")
        self.files["_rels/.rels"] = _bytes(root_rels)
        types = etree.Element(f"{{{C}}}Types", nsmap={None: C})
        etree.SubElement(types, f"{{{C}}}Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
        etree.SubElement(types, f"{{{C}}}Default", Extension="xml", ContentType="application/xml")
        for path, content_type in sorted(self.content_types.items()):
            etree.SubElement(types, f"{{{C}}}Override", PartName="/" + path, ContentType=content_type)
        self.files["[Content_Types].xml"] = _bytes(types)
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for path, contents in self.files.items():
                archive.writestr(path, contents)
        return output.getvalue()


def _number(node, name, default):
    try:
        return int(node.get(_w(name), default)) if node is not None else default
    except (TypeError, ValueError):
        return default


def _height(root, width):
    """Conservatively reserve native text/table/inline artwork header space."""
    def paragraph(node):
        text = "".join(text.text or "" for text in node.findall(".//w:t", NS))
        lines = max(1, (len(text) * 110 + max(width, 1) - 1) // max(width, 1)) + len(node.findall(".//w:br", NS))
        spacing = node.find("w:pPr/w:spacing", NS)
        sizes = [_number(size, "val", 24) * 10 for size in node.findall(".//w:sz", NS)]
        line = max(sizes or [240]) * 1.2
        drawings = [int(extent.get("cy", "0")) / 635 for extent in node.findall(".//wp:inline/wp:extent", NS)]
        return max(lines * line, max(drawings or [0])) + _number(spacing, "before", 0) + _number(spacing, "after", 0)
    total = 0
    for node in root:
        if node.tag == _w("p"):
            total += paragraph(node)
        elif node.tag == _w("tbl"):
            for row in node.findall("w:tr", NS):
                heights = [_height(cell, max(width // max(len(row.findall(_w("tc"))), 1), 1)) for cell in row.findall(_w("tc"))]
                declared = row.find("w:trPr/w:trHeight", NS)
                total += max(max(heights or [240]) + 60, _number(declared, "val", 0))
    # Anchored drawings can sit outside the header's text flow.
    for anchor in root.findall(".//wp:anchor", NS):
        position = anchor.find("wp:positionV", NS)
        # Page-positioned logos/bottom strips are handled against their page
        # coordinates separately; treating a footer's page offset as its height
        # would incorrectly consume the whole document.
        if position is not None and position.get("relativeFrom") == "page":
            continue
        extent = anchor.find("wp:extent", NS)
        offset = anchor.find("wp:positionV/wp:posOffset", NS)
        if extent is not None:
            bottom = int(extent.get("cy", "0")) / 635
            if offset is not None:
                bottom += max(0, int(offset.text or "0") / 635)
            total = max(total, bottom)
    return round(total)


def _reserve_stationery_space(section, writer, header_path, footer_path):
    margins = section.find(_w("pgMar"))
    if margins is None:
        margins = etree.SubElement(section, _w("pgMar"))
    size = section.find(_w("pgSz"))
    width = _number(size, "w", 12240) - _number(margins, "left", 1440) - _number(margins, "right", 1440)
    page_height = _number(size, "h", 15840)
    header = _xml(writer.files[header_path])
    footer = _xml(writer.files[footer_path]) if footer_path else None
    top = max(_number(margins, "top", 1440), _number(margins, "header", 720) + _height(header, width) + 160)
    bottom = _number(margins, "bottom", 1440)
    if footer_path:
        bottom = max(bottom, _number(margins, "footer", 720) + _height(footer, width) + 120)
    for root in (header, footer):
        if root is None:
            continue
        for anchor in root.findall(".//wp:anchor", NS):
            position = anchor.find("wp:positionV", NS)
            extent = anchor.find("wp:extent", NS)
            if position is None or extent is None or position.get("relativeFrom") != "page":
                continue
            height = round(int(extent.get("cy", "0")) / 635)
            # Full-page watermarks are background artwork, not a top strip.
            if height > page_height // 2:
                continue
            offset = position.find("wp:posOffset", NS)
            alignment = position.find("wp:align", NS)
            if offset is not None:
                y = round(int(offset.text or "0") / 635)
            elif alignment is not None and alignment.text == "bottom":
                y = page_height - height
            elif alignment is not None and alignment.text == "center":
                continue
            else:
                y = 0
            if y + height // 2 < page_height // 2:
                top = max(top, y + height + 160)
            else:
                bottom = max(bottom, page_height - y + 120)
    if page_height - top - bottom < 1440:
        raise ReportTemplateRenderingError("The Word template header and footer leave too little space for the report. Reduce the stationery height and upload it again.")
    margins.set(_w("top"), str(top))
    margins.set(_w("bottom"), str(bottom))


def _template_bytes(field):
    try:
        # Use the configured storage, including backends without local paths.
        with field.storage.open(field.name, "rb") as stream:
            raw = stream.read(getattr(settings, "REPORT_TEMPLATE_MAX_BYTES", 10 * 1024 * 1024) + 1)
    except Exception as exc:
        raise ReportTemplateRenderingError("The saved Word report template is unavailable. Upload it again or ask an administrator to check media storage.") from exc
    try:
        # Stored files are checked again before extraction and PDF conversion;
        # upload-time validation cannot protect against later media corruption.
        validate_report_template_bytes(raw)
    except ValidationError as exc:
        raise ReportTemplateRenderingError("The saved Word report template cannot be used. " + " ".join(exc.messages)) from exc
    return raw


def _printable_width(section):
    margins = section.find(_w("pgMar"))
    return (
        _number(section.find(_w("pgSz")), "w", 12240)
        - _number(margins, "left", 1440) - _number(margins, "right", 1440)
        - _number(margins, "gutter", 0)
    )


def _fit_inline_drawings(container, width_twips):
    available = max(1, width_twips) * 635
    for drawing in container.findall(".//wp:inline", NS):
        extent = drawing.find("wp:extent", NS)
        if extent is None or int(extent.get("cx", "0")) <= available:
            continue
        height = round(int(extent.get("cy", "0")) * available / int(extent.get("cx")))
        extent.set("cx", str(available))
        extent.set("cy", str(height))
        picture_extent = drawing.find(".//a:xfrm/a:ext", NS)
        if picture_extent is not None:
            picture_extent.set("cx", str(available))
            picture_extent.set("cy", str(height))


def _fit_report_tables(body, section):
    """Size generated match tables to the template's full printable width."""
    width = _printable_width(section)
    for table in body.findall(".//w:tbl", NS):
        columns = table.findall("w:tblGrid/w:gridCol", NS)
        old_width = sum(_number(column, "w", 0) for column in columns)
        preferred = table.find("w:tblPr/w:tblW", NS)
        full_width = preferred is not None and preferred.get(_w("type")) == "pct" and preferred.get(_w("w")) == "5000"
        if not old_width or (not full_width and old_width <= width):
            continue
        if full_width:
            if width < 2880:
                raise ReportTemplateRenderingError("The Word template margins leave too little width for the match columns.")
            layout_columns = section.find(_w("cols"))
            if layout_columns is not None:
                layout_columns.clear()
                layout_columns.set(_w("num"), "1")
            sizes = [round(_number(column, "w", 0) * width / old_width) for column in columns]
            sizes[-1] = width - sum(sizes[:-1])
            for column, size in zip(columns, sizes):
                column.set(_w("w"), str(size))
            for row in table.findall("w:tr", NS):
                index = 0
                for cell in row.findall("w:tc/w:tcPr", NS):
                    span = _number(cell.find(_w("gridSpan")), "val", 1)
                    cell_width = cell.find(_w("tcW"))
                    if cell_width is None:
                        cell_width = etree.Element(_w("tcW"))
                        cell.insert(0, cell_width)
                    cell_width.set(_w("type"), "dxa")
                    width_twips = sum(sizes[index:index + span])
                    cell_width.set(_w("w"), str(width_twips))
                    padding = cell.find(_w("tcMar"))
                    left = padding.find(_w("left")) if padding is not None else None
                    right = padding.find(_w("right")) if padding is not None else None
                    _fit_inline_drawings(cell.getparent(), width_twips - _number(left, "w", 0) - _number(right, "w", 0))
                    index += span
        else:
            nodes = columns + table.findall("w:tblPr/w:tblW", NS) + table.findall("w:tr/w:tc/w:tcPr/w:tcW", NS)
            for node in nodes:
                if node.get(_w("type"), "dxa") == "dxa":
                    node.set(_w("w"), str(max(1, round(_number(node, "w", 0) * width / old_width))))


def render_template_docx(parts):
    """Return DOCX bytes for ordered ``{'template': FieldFile|None, 'document': bytes}`` parts.

    Every document body remains editable.  The template body is deliberately
    ignored, and no placeholder convention is required. A part can set
    ``use_source_header`` to replace the template header with its league image.
    """
    parts = list(parts)
    if not parts:
        raise ReportTemplateRenderingError("There are no leagues to include in this report.")
    writer = _Writer()
    body = etree.Element(_w("body"), nsmap={"w": W, "r": R})
    next_drawing_id = 1
    try:
        for index, part in enumerate(parts, 1):
            source = _Package(part["document"], f"report{index}")
            writer.prepare(source)
            field = part.get("template")
            template = _Package(_template_bytes(field), f"template{index}") if field else None
            if template:
                writer.prepare(template)
            stationery = template or source
            section = deepcopy(stationery.section())
            header = stationery.reference(section, "header")
            if template and not header:
                raise ReportTemplateRenderingError("The Word template needs an official header in Word's Header area. Add the letterhead there and upload it again.")
            source_header = source.reference(source.section(), "header") if template and part.get("use_source_header") else None
            if source_header:
                header_path = writer.import_part(source, source_header)
                artwork = _xml(writer.files[header_path])
                _fit_inline_drawings(artwork, _printable_width(section))
                writer.files[header_path] = _bytes(artwork)
            else:
                header_path = writer.import_part(stationery, header) if header else writer.empty_header(index)
            source_footer = source.reference(source.section(), "footer")
            template_footer = template.reference(section, "footer") if template else None
            footer_path = writer.footer(template, template_footer, source, source_footer)
            # Explicit default references and no first/even-page variants make
            # the same stationery repeat, including after an unbranded league.
            for node in list(section):
                if node.tag in {_w("headerReference"), _w("footerReference"), _w("titlePg"), _w("type"), _w("printerSettings")}:
                    section.remove(node)
            # Printer settings belong to the template author's machine; the
            # report keeps page geometry without importing printer binaries.
            writer._copy_relationships(section, stationery, stationery.document_path, "word/document.xml", only_used=True)
            section.insert(0, etree.Element(_w("headerReference"), {_w("type"): "default", f"{{{R}}}id": writer.main_rel("header", header_path)}))
            if footer_path:
                section.insert(1, etree.Element(_w("footerReference"), {_w("type"): "default", f"{{{R}}}id": writer.main_rel("footer", footer_path)}))
            else:
                # A blank explicit footer also prevents previous-section carryover.
                footer_path = writer.empty_header(f"footer-{index}")
                empty = _xml(writer.files[footer_path])
                empty.tag = _w("ftr")
                writer.files[footer_path] = _bytes(empty)
                writer.content_types[footer_path] = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
                section.insert(1, etree.Element(_w("footerReference"), {_w("type"): "default", f"{{{R}}}id": writer.main_rel("footer", footer_path)}))
            section.insert(2, etree.Element(_w("type"), {_w("val"): "nextPage"}))
            if template:
                _reserve_stationery_space(section, writer, header_path, footer_path)
            report_body = deepcopy(source.body)
            for old_section in report_body.findall(".//w:sectPr", NS):
                old_section.getparent().remove(old_section)
            writer.rewrite(report_body, source)
            for drawing in report_body.findall(".//wp:docPr", NS):
                drawing.set("id", str(next_drawing_id))
                next_drawing_id += 1
            writer._copy_relationships(report_body, source, source.document_path, "word/document.xml", only_used=True)
            if template:
                _fit_report_tables(report_body, section)
            body.extend(report_body)
            if index < len(parts):
                paragraph = etree.SubElement(body, _w("p"))
                properties = etree.SubElement(paragraph, _w("pPr"))
                etree.SubElement(properties, _w("spacing"), {
                    _w("before"): "0", _w("after"): "0", _w("line"): "20", _w("lineRule"): "exact",
                })
                mark = etree.SubElement(properties, _w("rPr"))
                etree.SubElement(mark, _w("sz"), {_w("val"): "2"})
                properties.append(section)
            else:
                body.append(section)
        return writer.save(body)
    except ReportTemplateRenderingError:
        raise
    except (KeyError, ValueError, TypeError, etree.XMLSyntaxError) as exc:
        raise ReportTemplateRenderingError("The Word report template could not be rendered. Re-save the template in Word and upload it again.") from exc
