"""Typeset official football fixtures and results directly as PDF."""

from html import escape
from io import BytesIO
from pathlib import Path

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import HRFlowable, Image, Paragraph, Spacer, Table

from leagues.models import ReportDocumentSettings

from .football_report_layout import entry_caption, entry_match_groups, match_cells, match_columns, report_sections


PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 42
WIDTH = PAGE_WIDTH - 2 * MARGIN
BOTTOM = 68
HEADER_LIMIT = 136
INK = colors.HexColor("#202b36")
MUTED = colors.HexColor("#4f5b67")
RULE = colors.HexColor("#d5dce2")
ACCENT = colors.HexColor("#234f74")

# ReportLab ships these Unicode fonts on both Windows and Linux.
_FONT_DIR = Path(reportlab.__file__).with_name("fonts")
pdfmetrics.registerFont(TTFont("FootballReport", str(_FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("FootballReportBold", str(_FONT_DIR / "VeraBd.ttf")))


def _style(name, size, *, bold=False, alignment=0, color=INK):
    return ParagraphStyle(
        name, fontName="FootballReportBold" if bold else "FootballReport",
        fontSize=size, leading=size * 1.35, alignment=alignment, textColor=color,
    )


_BODY = _style("body", 10)
_SMALL = _style("small", 8.5, color=MUTED)
_NOTE = _style("note", 8, color=MUTED)
_TITLE = _style("title", 14, bold=True, alignment=TA_CENTER)
_REPORT_TITLE = _style("report title", 10, bold=True, alignment=TA_CENTER, color=ACCENT)
_CENTER_SMALL = _style("center small", 9, alignment=TA_CENTER, color=MUTED)
_WEEK = _style("week", 10, bold=True, color=ACCENT)
_VENUE = _style("venue", 10, bold=True)
_GROUP = _style("group", 9, bold=True, color=MUTED)
_CENTER = _style("center", 9.5, bold=True, alignment=TA_CENTER)
_VERSUS = _style("versus", 8, alignment=TA_CENTER, color=MUTED)
_TIME = _style("time", 9, alignment=TA_RIGHT)
_COLUMN_LEFT = _style("left column", 8, bold=True, color=MUTED)
_COLUMN_CENTER = _style("center column", 8, bold=True, alignment=TA_CENTER, color=MUTED)
_COLUMN_RIGHT = _style("right column", 8, bold=True, alignment=TA_RIGHT, color=MUTED)
_COMMITTEE = _style("committee", 9, bold=True, alignment=TA_CENTER)


def _paragraph(text, style=_BODY):
    return Paragraph(escape(str(text or "")), style)


def _rule():
    return HRFlowable(width="100%", thickness=0.55, color=RULE, spaceBefore=0, spaceAfter=0)


def _fit(width, height, max_width, max_height):
    scale = min(max_width / width, max_height / height)
    return width * scale, height * scale


class _Pages:
    """Lay out measured, indivisible groups within the printable area."""

    def __init__(self, document, image, footer_message, footer_label, website):
        self.document = document
        self.image = image
        self.footer_message = footer_message
        self.footer_label = footer_label
        self.website = website
        self.top = PAGE_HEIGHT - 42
        if image:
            self.header_size = _fit(*image.getSize(), WIDTH, HEADER_LIMIT)
            self.top = PAGE_HEIGHT - 28 - self.header_size[1] - 20
        self.start()

    def start(self):
        document = self.document
        if self.image:
            width, height = self.header_size
            document.drawImage(self.image, (PAGE_WIDTH - width) / 2, PAGE_HEIGHT - 28 - height, width, height)
        self.y = self.top

    def finish(self):
        document = self.document
        document.setStrokeColor(RULE)
        document.setLineWidth(0.5)
        document.line(MARGIN, 54, PAGE_WIDTH - MARGIN, 54)
        document.setFillColor(MUTED)
        document.setFont("FootballReport", 8)
        document.drawCentredString(PAGE_WIDTH / 2, 39, self.footer_message)
        document.setFillColor(ACCENT)
        document.setFont("FootballReportBold", 8.5)
        document.drawCentredString(PAGE_WIDTH / 2, 26, self.footer_label)
        half = pdfmetrics.stringWidth(self.footer_label, "FootballReportBold", 8.5) / 2
        document.linkURL(self.website, (PAGE_WIDTH / 2 - half, 23, PAGE_WIDTH / 2 + half, 36), relative=0)
        document.showPage()

    def measure(self, blocks):
        return [block.wrapOn(self.document, WIDTH, self.top - BOTTOM)[1] for block in blocks]

    def place(self, blocks, *, continuation=()):
        heights = self.measure(blocks)
        if self.y - sum(heights) < BOTTOM and self.y < self.top:
            self.finish()
            self.start()
            blocks = [*continuation, *blocks]
            heights = self.measure(blocks)
        for block, height in zip(blocks, heights):
            self.y -= height
            block.drawOn(self.document, MARGIN, self.y)


def _match_table(row, *, results=False):
    columns = match_columns(results)
    widths = tuple(WIDTH * share / 100 for _, share in columns)
    header = row is None
    if header:
        styles = (_COLUMN_LEFT, _COLUMN_CENTER, _COLUMN_LEFT, _COLUMN_RIGHT)
        cells = [_paragraph(label, style) for (label, _), style in zip(columns, styles)]
    else:
        styles = (_BODY, _CENTER if results else _VERSUS, _BODY, _TIME)
        cells = [_paragraph(value, style) for value, style in zip(match_cells(row, results), styles)]
        if not results and row.get("status"):
            cells[-1] = [cells[-1], _paragraph(row["status"], _TIME)]
    values = [cells]
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("LINEBELOW", (0, -1), (-1, -1), .65 if header else .35, RULE),
    ]
    if row and row.get("result_note"):
        values.append([_paragraph(row["result_note"], _NOTE), *([""] * (len(cells) - 1))])
        commands.extend([("SPAN", (0, 1), (-1, 1)), ("TOPPADDING", (0, 1), (-1, 1), 0)])
    return Table(values, colWidths=widths, style=commands)


def _report_title(data):
    blocks = [_paragraph(data["heading"], _REPORT_TITLE)]
    if data.get("subheading"):
        blocks.extend([Spacer(1, 3), _paragraph(data["subheading"], _CENTER_SMALL)])
    return blocks


def _entry_content(pages, data, entry, pending):
    results = data["kind"] == "RESULTS"
    name = entry["competition"].upper()
    pending.extend([Spacer(1, 9), _paragraph(name, _TITLE)])
    heading = entry_caption(entry)
    if heading:
        pending.extend([Spacer(1, 3), _paragraph(heading, _CENTER_SMALL)])
    pending.extend(_paragraph(f"Note: {note}", _SMALL) for note in entry.get("notes", ()))
    continued = [_paragraph(f"{name} (continued)", _REPORT_TITLE), Spacer(1, 7)]
    styles = {"week": _WEEK, "group": _GROUP, "venue": _VENUE}
    for headings, context, rows in entry_match_groups(entry):
        for value, role in headings:
            pending.extend([Spacer(1, 10 if role == "week" else 4), _paragraph(value, styles[role])])
        context = [_paragraph(value, styles[role]) for value, role in context]
        header = _match_table(None, results=results)
        pending.extend([Spacer(1, 5), *context, header])
        pages.place([*pending, _match_table(rows[0], results=results)])
        pending.clear()
        for row in rows[1:]:
            pages.place([_match_table(row, results=results)], continuation=[*continued, *context, header])
    if pending:
        pending.append(_paragraph("No final results are available for this competition.", _SMALL))
        pages.place(pending)
        pending.clear()


def _artwork(content, width, height):
    source = BytesIO(content)
    image = Image(source, hAlign="LEFT")
    image.drawWidth, image.drawHeight = _fit(image.imageWidth, image.imageHeight, width, height)
    return image


def _sign_off(settings, include_signature, include_stamp):
    left = [_paragraph(settings.signoff_heading or "Yours Faithfully"), Spacer(1, 5)]
    if include_signature and settings.signature_image and settings.signatory_name:
        left.append(_artwork(settings.signature_image, 150, 48))
    else:
        left.append(Spacer(1, 36))
    if settings.signatory_name:
        left.extend([Spacer(1, 4), _paragraph(settings.signatory_name, _VENUE)])
        if settings.signatory_position:
            left.append(_paragraph(settings.signatory_position, _SMALL))
    stamp = _artwork(settings.stamp_image, 84, 72) if include_stamp and settings.stamp_image else ""
    return Table([[left, stamp]], colWidths=[WIDTH - 100, 100], style=[
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ])


def _closing(pages, settings, include_signature, include_stamp, continuation):
    if settings is None:
        return
    signoff = [_sign_off(settings, include_signature, include_stamp)] if settings.signoff_enabled else []
    members = list(settings.committee_members.all()) if settings.committee_enabled else []
    if not signoff and not members:
        return
    start = [Spacer(1, 15), _rule(), Spacer(1, 9), *signoff]
    heading = [Spacer(1, 8), _paragraph("EXECUTIVE COMMITTEE", _COMMITTEE), Spacer(1, 4)]
    rows = [
        [Spacer(1, 2), _paragraph(" · ".join(filter(None, (member.position, member.name, member.contact))), _CENTER_SMALL)]
        for member in members
    ]
    if not rows:
        pages.place(start, continuation=continuation)
        return
    all_blocks = [*start, *heading, *(block for row in rows for block in row)]
    if sum(pages.measure(all_blocks)) <= pages.top - BOTTOM - sum(pages.measure(continuation)):
        pages.place(all_blocks, continuation=continuation)
    else:
        pages.place([*start, *heading, *rows[0]], continuation=continuation)
        for row in rows[1:]:
            pages.place(row, continuation=[*continuation, *heading])


def render_football_pdf(data, *, load_letterhead, footer_message, footer_label, website,
                        include_signature=False, include_stamp=False):
    output = BytesIO()
    document = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    document.setTitle(data["heading"])
    document.setCreator("EsportsLive")
    images = {}
    for section, include_closing in report_sections(data):
        entries = section["entries"]
        first = entries[0] if entries else section
        owner = first.get("stationery_owner")
        letterhead = first.get("fixture_letterhead")
        if letterhead:
            key = (id(letterhead.storage), letterhead.name)
            if key not in images:
                image = load_letterhead(letterhead)
                images[key] = ImageReader(BytesIO(image["png"])) if image else None
            image = images[key]
        else:
            image = None
        pages = _Pages(document, image, footer_message, footer_label, website)
        pending = _report_title(data)
        for entry in entries:
            _entry_content(pages, section, entry, pending)
        if not entries:
            pages.place([*pending, Spacer(1, 10), _paragraph("No fixtures/results are available for this selection.", _SMALL)])
        if include_closing and owner is not None:
            closing = ReportDocumentSettings.for_owner(owner, include_signature=include_signature, include_stamp=include_stamp)
            section_title = first["competition"] if first.get("report_new_page") else data["heading"]
            _closing(pages, closing, include_signature, include_stamp, [_paragraph(section_title, _REPORT_TITLE)])
        pages.finish()
    document.save()
    return output.getvalue()
