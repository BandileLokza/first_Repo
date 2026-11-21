"""Editable Word tables for the shared football report layout."""

from html import escape

from .football_report_layout import entry_caption, entry_match_groups, match_cells, match_columns


PAGE_WIDTH = 11906  # A4 in twips
PAGE_HEIGHT = 16838
MARGIN = 840
PRINTABLE_WIDTH = PAGE_WIDTH - 2 * MARGIN

_INK = "202B36"
_MUTED = "4F5B67"
_ACCENT = "234F74"
_RULE = "D5DCE2"
_STYLES = {
    # font size in half-points, bold, alignment, color, space before in twips
    "title": (20, True, "center", _ACCENT, 0),
    "competition": (28, True, "center", _INK, 180),
    "caption": (18, False, "center", _MUTED, 60),
    "week": (20, True, "left", _ACCENT, 200),
    "venue": (20, True, "left", _INK, 0),
    "group": (18, True, "left", _MUTED, 0),
    "note": (17, False, "left", _MUTED, 0),
    "result_note": (16, False, "left", _MUTED, 0),
    "team": (20, False, "left", _INK, 0),
    "vs": (16, False, "center", _MUTED, 0),
    "score": (19, True, "center", _INK, 0),
    "time": (18, False, "right", _INK, 0),
    "column_left": (16, True, "left", _MUTED, 0),
    "column_center": (16, True, "center", _MUTED, 0),
    "column_right": (16, True, "right", _MUTED, 0),
    "committee": (19, True, "center", _INK, 140),
    "committee_member": (18, False, "center", _MUTED, 40),
    "gap": (2, False, "left", _INK, 0),
    "table_gap": (2, False, "left", _INK, 50),
}


def _paragraph(text, role, *, keep=False, inline=""):
    size, bold, align, color, before = _STYLES[role]
    keep_next = "<w:keepNext/>" if keep else "<w:keepNext w:val=\"0\"/>"
    properties = (
        f'{keep_next}<w:keepLines/><w:spacing w:before="{before}" w:after="0" '
        f'w:line="{round(size * 13.5)}" w:lineRule="atLeast"/>'
        f'<w:ind w:left="0" w:right="0" w:firstLine="0"/><w:jc w:val="{align}"/>'
    )
    run = (
        '<w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>'
        f'<w:b w:val="{int(bold)}"/><w:i w:val="0"/><w:color w:val="{color}"/>'
        f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    )
    content = inline or f'<w:r><w:rPr>{run}</w:rPr><w:t xml:space="preserve">{escape(str(text or ""))}</w:t></w:r>'
    return f'<w:p><w:pPr>{properties}<w:rPr>{run}</w:rPr></w:pPr>{content}</w:p>'


def _cell(contents, width, *, first=False, last=False, span=1, top=80, bottom=80):
    merged = f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
    return (
        '<w:tc><w:tcPr>'
        f'<w:tcW w:w="{width}" w:type="dxa"/>{merged}'
        f'<w:tcMar><w:top w:w="{top}" w:type="dxa"/><w:left w:w="{0 if first else 120}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/><w:right w:w="{0 if last else 120}" w:type="dxa"/></w:tcMar>'
        f'<w:vAlign w:val="center"/></w:tcPr>{contents}</w:tc>'
    )


def _table_row(cells, *, header=False):
    repeat = "<w:tblHeader/>" if header else ""
    return f'<w:tr><w:trPr><w:cantSplit/>{repeat}</w:trPr>{"".join(cells)}</w:tr>'


def _matches(context, rows, results):
    columns = match_columns(results)
    widths = [round(PRINTABLE_WIDTH * share / 100) for _, share in columns]
    widths[-1] = PRINTABLE_WIDTH - sum(widths[:-1])
    count = len(widths)
    table_rows = []
    if context:
        heading = "".join(_paragraph(value, role, keep=True) for value, role in context)
        table_rows.append(_table_row([_cell(heading, PRINTABLE_WIDTH, first=True, last=True, span=count, top=0, bottom=0)], header=True))
    roles = ("column_left", "column_center", "column_left", "column_right")
    table_rows.append(_table_row([
        _cell(_paragraph(value, roles[index], keep=True), width, first=index == 0, last=index == count - 1)
        for index, ((value, _), width) in enumerate(zip(columns, widths))
    ], header=True))
    roles = ("team", "score" if results else "vs", "team", "time")
    for row in rows:
        note = row.get("result_note") if results else None
        cells = []
        for index, (value, width) in enumerate(zip(match_cells(row, results), widths)):
            role = roles[index]
            contents = _paragraph(value, role, keep=bool(note))
            if role == "time" and row.get("status"):
                contents += _paragraph(row["status"], role)
            cells.append(_cell(contents, width, first=index == 0, last=index == count - 1))
        table_rows.append(_table_row(cells))
        if note:
            table_rows.append(_table_row([
                _cell(_paragraph(note, "result_note"), PRINTABLE_WIDTH, first=True, last=True, span=count, top=0)
            ]))
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for width in widths)
    return (
        '<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:jc w:val="left"/>'
        '<w:tblInd w:w="0" w:type="dxa"/>'
        f'<w:tblBorders><w:bottom w:val="single" w:sz="3" w:color="{_RULE}"/>'
        f'<w:insideH w:val="single" w:sz="3" w:color="{_RULE}"/></w:tblBorders>'
        '<w:tblLayout w:type="fixed"/></w:tblPr>'
        f'<w:tblGrid>{grid}</w:tblGrid>{"".join(table_rows)}</w:tbl>'
    )


def word_picture(relationship, picture_id, label, width, height):
    cx, cy = round(width * 12700), round(height * 12700)
    return (
        '<w:r><w:drawing xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{picture_id}" name="{escape(label)}"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="{escape(label)}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{relationship}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )


def _official_closing(settings, add_artwork, *, include_signature, include_stamp):
    if not settings:
        return []
    members = list(settings.committee_members.all()) if settings.committee_enabled else []
    if not settings.signoff_enabled and not members:
        return []

    blocks = [
        '<w:p><w:pPr><w:keepNext/><w:pBdr><w:bottom w:val="single" w:sz="4" w:space="0" '
        f'w:color="{_RULE}"/></w:pBdr><w:spacing w:before="180" w:after="160" '
        'w:line="20" w:lineRule="exact"/></w:pPr></w:p>'
    ]
    if settings.signoff_enabled:
        signature = include_signature and settings.signatory_name and settings.signature_image
        stamp = include_stamp and settings.stamp_image
        left = _paragraph(settings.signoff_heading or "Yours Faithfully", "team", keep=True)
        if signature:
            picture = add_artwork(signature, "Approved signature", 150, 48)
            left += _paragraph("", "team", keep=True, inline=picture)
        else:
            left += '<w:p><w:pPr><w:keepNext/><w:spacing w:before="0" w:after="0" w:line="720" w:lineRule="exact"/></w:pPr></w:p>'
        if settings.signatory_name:
            left += _paragraph(settings.signatory_name, "venue", keep=True)
            if settings.signatory_position:
                left += _paragraph(settings.signatory_position, "note", keep=True)
        stamp_picture = add_artwork(stamp, "Approved stamp", 84, 72) if stamp else ""
        right = _paragraph("", "time", keep=True, inline=stamp_picture)
        widths = (PRINTABLE_WIDTH - 2000, 2000)
        grid = "".join(f'<w:gridCol w:w="{width}"/>' for width in widths)
        row = _table_row([_cell(left, widths[0], first=True), _cell(right, widths[1], last=True)])
        blocks.append(
            '<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:jc w:val="left"/>'
            '<w:tblInd w:w="0" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr>'
            f'<w:tblGrid>{grid}</w:tblGrid>{row}</w:tbl>'
        )
    if members:
        blocks.append(_paragraph("EXECUTIVE COMMITTEE", "committee", keep=True))
        for index, member in enumerate(members):
            details = " · ".join(filter(None, (member.position, member.name, member.contact)))
            blocks.append(_paragraph(details, "committee_member", keep=len(members) <= 6 and index < len(members) - 1))
    return blocks


def word_report_body(data, *, closing, add_artwork, include_signature, include_stamp):
    result = data["kind"] == "RESULTS"
    blocks = [_paragraph(data["heading"], "title", keep=True)]
    if data.get("subheading"):
        blocks.append(_paragraph(data["subheading"], "caption", keep=True))
    for entry in data["entries"]:
        blocks.append(_paragraph(entry["competition"].upper(), "competition", keep=True))
        caption = entry_caption(entry)
        if caption:
            blocks.append(_paragraph(caption, "caption", keep=True))
        blocks.extend(_paragraph(f"Note: {note}", "note", keep=True) for note in entry.get("notes", ()))
        groups = False
        for headings, context, rows in entry_match_groups(entry):
            groups = True
            blocks.extend(_paragraph(value, role, keep=True) for value, role in headings)
            gap = "table_gap" if blocks[-1].startswith("<w:tbl>") else "gap"
            blocks.append(_paragraph("", gap, keep=True))
            blocks.append(_matches(context, rows, result))
        if not groups:
            blocks.append(_paragraph("No final results are available for this competition.", "note"))
    if not data["entries"]:
        blocks.append(_paragraph("No applicable fixtures/results are available for this selection.", "note"))
    blocks.extend(_official_closing(closing, add_artwork, include_signature=include_signature, include_stamp=include_stamp))
    if blocks[-1].startswith("<w:tbl>"):
        # Word needs a paragraph after a terminal table. Supply a hidden,
        # one-point mark so Word cannot add a full-height blank last page.
        blocks.append(
            '<w:p><w:pPr><w:keepNext w:val="0"/><w:widowControl w:val="0"/>'
            '<w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
            '<w:rPr><w:vanish/><w:sz w:val="2"/></w:rPr></w:pPr></w:p>'
        )
    return blocks
