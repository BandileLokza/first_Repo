"""Shared fixture and result layout for editable Word and printed PDF reports."""

from collections import defaultdict
from itertools import groupby


def _stationery_owner_key(entry):
    if entry.get("report_new_page", entry.get("fixture_new_page")):
        return id(entry)
    owner = entry.get("stationery_owner")
    return (type(owner), owner.pk) if owner else None


def _file_key(field):
    return (id(field.storage), field.name) if field else None


def _letterhead_key(entry, for_word):
    template = entry.get("report_template") if for_word else None
    image = entry.get("fixture_letterhead") if not template or entry.get("use_image_in_word") else None
    return _file_key(template), _file_key(image)


def report_sections(data, *, for_word=False):
    """Break on a change of letterhead and close once per administrative scope."""
    if not data["entries"]:
        yield data, True
        return
    for _, owner_entries in groupby(data["entries"], key=_stationery_owner_key):
        sections = [list(section) for _, section in groupby(
            owner_entries, key=lambda entry: _letterhead_key(entry, for_word),
        )]
        for index, entries in enumerate(sections):
            yield {**data, "entries": entries}, index == len(sections) - 1


def match_columns(results):
    if results:
        return (("HOME TEAM", 43), ("SCORE", 14), ("AWAY TEAM", 43))
    return (("HOME TEAM", 41), ("", 7), ("AWAY TEAM", 37), ("KICKOFF", 15))


def match_cells(row, results):
    if results:
        return (row["home"], row.get("pdf_score", row.get("score")), row["away"])
    return (row["home"], "VS", row["away"], row.get("pdf_time", row.get("time")) or "TBA")


def entry_caption(entry):
    return " · ".join(value for value in (entry.get("round_label"), entry.get("season")) if value)


def entry_match_groups(entry):
    """Yield preceding headings, repeated venue context, and selected match rows."""
    pending = []
    sections = (("", entry["sections"]), ("Additional Fixtures from Earlier Gameweeks", entry.get("additional_sections", ())))
    for section_heading, section_list in sections:
        if section_list and section_heading:
            pending.append((section_heading, "week"))
        for section in section_list:
            if section["date_label"]:
                pending.append((section["date_label"], "week"))
            for stream in section["streams"]:
                if stream["stream"]:
                    pending.append((stream["stream"], "group"))
                for venue in stream["venues"]:
                    for group in venue["groups"]:
                        by_date = defaultdict(list)
                        for row in group["rows"]:
                            by_date[row.get("pdf_date", "")].append(row)
                        for date, rows in by_date.items():
                            location = " — ".join(value for value in (venue["venue"], date) if value)
                            context = []
                            if location:
                                context.append((location, "venue"))
                            if group["group"]:
                                context.append((group["group"], "group"))
                            yield pending, context, rows
                            pending = []
