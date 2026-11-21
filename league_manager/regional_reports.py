"""Shared data selection and simple newspaper-style football report rendering."""

from collections import defaultdict
from datetime import timedelta
from html import escape as xml_escape
from io import BytesIO
import logging
import re
from zoneinfo import ZoneInfo
from zipfile import ZIP_DEFLATED, ZipFile

from django.conf import settings
from django.db.models import Max, Min, Q
from django.utils import timezone

from PIL import Image, ImageOps
from requests.exceptions import RequestException

from leagues.models import Competition, CompetitionSeries, FootballOrganisation, ReportDocumentSettings, Standing
from leagues.standings import rank_standings
from matches.models import Match

from .football_report_layout import report_sections
from .football_word import MARGIN as WORD_MARGIN, PAGE_HEIGHT as WORD_PAGE_HEIGHT, PAGE_WIDTH as WORD_PAGE_WIDTH
from .football_word import PRINTABLE_WIDTH as WORD_PRINTABLE_WIDTH, word_picture, word_report_body


FIXTURE_SCOPE_NEXT_UPCOMING = "NEXT_UPCOMING"
FIXTURE_SCOPE_FIRST_ROUND = "FIRST_ROUND"
FIXTURE_SCOPE_SECOND_ROUND = "SECOND_ROUND"
FIXTURE_SCOPE_FULL_SEASON = "FULL_SEASON"
FIXTURE_EXPORT_SCOPES = (
    FIXTURE_SCOPE_NEXT_UPCOMING,
    FIXTURE_SCOPE_FIRST_ROUND,
    FIXTURE_SCOPE_SECOND_ROUND,
    FIXTURE_SCOPE_FULL_SEASON,
)
FIXTURE_SCOPE_LABELS = {
    FIXTURE_SCOPE_NEXT_UPCOMING: "Next Upcoming Gameweek",
    FIXTURE_SCOPE_FIRST_ROUND: "First Round Fixtures",
    FIXTURE_SCOPE_SECOND_ROUND: "Second Round Fixtures",
    FIXTURE_SCOPE_FULL_SEASON: "Full Season Fixtures",
}
WEEKLY_SELECTION_AUTOMATIC = "AUTOMATIC"
WEEKLY_SELECTION_MANUAL = "MANUAL"
WEEKLY_SELECTION_MODES = (
    WEEKLY_SELECTION_AUTOMATIC,
    WEEKLY_SELECTION_MANUAL,
)


# Fixture dates are stored as datetimes, while the newspaper report is planned
# around local calendar days.  Keep this explicit instead of depending on the
# active timezone of the calling request or worker.
ESWATINI_TIME_ZONE = ZoneInfo("Africa/Mbabane")
REPORT_FOOTER_MESSAGE = "Follow live scores, fixtures & standings"
REPORT_FOOTER_LABEL = "www.esportslive.site"


def _report_website_url():
    """Return the configured canonical destination used by report footers."""
    return getattr(settings, "REPORT_WEBSITE_URL", "https://www.esportslive.site")


def _active_scope_competitions(*, league_type, region=None, selected_series=None):
    """Return active competitions for exactly one administrative scope."""
    queryset = Competition.objects.filter(
        is_archived=False,
        series__league_type=league_type,
    )
    if region is not None:
        queryset = queryset.filter(series__region=region)
    if selected_series is not None:
        queryset = queryset.filter(series__in=selected_series)
    return queryset.select_related("series").order_by("series__name", "-id")


def selected_competitions(region, preference, *, selected_series_ids=None):
    """Resolve ALL dynamically, while CUSTOM stays anchored to league series."""
    selected_series = selected_series_ids if preference.selection_mode == preference.SelectionMode.CUSTOM else None
    if preference.selection_mode == preference.SelectionMode.CUSTOM and selected_series_ids is None:
        selected_series = preference.selected_series.filter(
            region=region,
            league_type=CompetitionSeries.LeagueType.REGIONAL,
        )
    return _active_scope_competitions(
        league_type=CompetitionSeries.LeagueType.REGIONAL,
        region=region,
        selected_series=selected_series,
    )


def selected_organisation_competitions(organisation, preference, *, selected_series_ids=None):
    """Resolve a non-regional scope without allowing a cross-section series."""
    selected_series = selected_series_ids if preference.selection_mode == preference.SelectionMode.CUSTOM else None
    if preference.selection_mode == preference.SelectionMode.CUSTOM and selected_series_ids is None:
        selected_series = preference.selected_series.filter(
            league_type=organisation,
        )
    return _active_scope_competitions(
        league_type=organisation,
        selected_series=selected_series,
    )


def manual_fixture_gameweek_options(competitions):
    """Return every recorded, numbered gameweek for each allowed competition."""
    competitions = list(competitions)
    gameweeks_by_competition = defaultdict(list)
    for competition_id, gameweek in Match.objects.filter(
        competition__in=competitions,
        gameweek__gt=0,
    ).values_list("competition_id", "gameweek").distinct():
        gameweeks_by_competition[competition_id].append(gameweek)
    return [
        {
            "competition_id": competition.id,
            "competition": competition.name,
            "series_id": competition.series_id,
            "gameweeks": sorted(gameweeks_by_competition[competition.id]),
        }
        for competition in competitions
    ]


def _match_queryset(competition):
    return competition.matches.select_related("home_team", "away_team", "stage", "group").order_by(
        "gameweek", "match_date", "id"
    )


def _eswatini_date(match_date):
    """Return a fixture's calendar date in Eswatini, preserving legacy naive data."""
    if not match_date:
        return None
    if timezone.is_aware(match_date):
        return timezone.localtime(match_date, ESWATINI_TIME_ZONE).date()
    return match_date.date()


def _week_bounds(day):
    """The newspaper reporting week is Monday through Sunday, in local dates."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _weekly_fixture_selection(competition, *, report_date, gameweek_override=None):
    """Select the date-aware weekly fixture programme for one competition.

    A confirmed fixture is eligible only from ``report_date`` onward in
    Africa/Mbabane.  The next reporting week containing confirmed fixtures is
    considered first.  Within that week, the later numbered gameweek is the
    primary programme; earlier gameweeks are additional fixtures rather than
    candidates that can displace it.  Fixture count is deliberately not used
    as the decision because a cluster of rescheduled older fixtures can be
    larger than the current programme.  A report note exposes any
    multi-gameweek ambiguity and points administrators to the optional
    override.
    """
    upcoming = list(_match_queryset(competition).filter(status="UPCOMING"))
    dated = [(match, _eswatini_date(match.match_date)) for match in upcoming]
    future = [(match, match_date) for match, match_date in dated if match_date and match_date >= report_date]

    warnings = []
    notes = []
    additional_matches = []

    if gameweek_override is not None:
        selected = [
            match
            for match, match_date in dated
            if match.gameweek == gameweek_override
            and (match_date is None or match_date >= report_date)
        ]
        if not selected:
            automatic = _weekly_fixture_selection(
                competition,
                report_date=report_date,
            )
            automatic["notes"].insert(
                0,
                f"Gameweek override {gameweek_override} has no future or TBA fixtures "
                "for this league; automatic weekly selection was used.",
            )
            return automatic
        notes.append(f"Gameweek {gameweek_override} was selected by administrator override.")
        selected_dates = [
            match_date
            for match, match_date in future
            if match.gameweek == gameweek_override
        ]
        if selected_dates:
            reporting_start, reporting_end = _week_bounds(min(selected_dates))
            additional_matches = [
                match
                for match, match_date in future
                if match.gameweek is not None
                and match.gameweek > 0
                and match.gameweek < gameweek_override
                and reporting_start <= match_date <= reporting_end
            ]
        return {
            "matches": selected,
            "round_label": f"Gameweek {gameweek_override}",
            "additional_matches": additional_matches,
            "notes": notes,
            "warnings": warnings,
        }

    if future:
        current_start, current_end = _week_bounds(report_date)
        in_current_week = [
            (match, match_date)
            for match, match_date in future
            if current_start <= match_date <= current_end
        ]
        if in_current_week:
            reporting_start, reporting_end = current_start, current_end
            in_reporting_week = in_current_week
        else:
            reporting_start, reporting_end = _week_bounds(min(match_date for _, match_date in future))
            in_reporting_week = [
                (match, match_date)
                for match, match_date in future
                if reporting_start <= match_date <= reporting_end
            ]

        by_gameweek = {}
        for match, match_date in in_reporting_week:
            if match.gameweek is not None and match.gameweek > 0:
                by_gameweek.setdefault(match.gameweek, []).append((match, match_date))

        if by_gameweek:
            # Fixture count cannot reliably distinguish a full current round
            # from several rescheduled earlier fixtures.  Earlier gameweeks
            # are therefore always kept as additions below the later round.
            gameweek = max(by_gameweek)
            programme = by_gameweek[gameweek]
            selected = [
                match
                for match, match_date in dated
                if match.gameweek == gameweek
                and (match_date is None or match_date >= report_date)
            ]
            additional_matches = [
                match
                for match, match_date in in_reporting_week
                if match.gameweek is not None
                and match.gameweek > 0
                and match.gameweek < gameweek
            ]
            if len(by_gameweek) > 1:
                candidates = ", ".join(f"GW{number}" for number in sorted(by_gameweek))
                notes.append(
                    f"Confirmed fixtures in the reporting week of {reporting_start:%d %B %Y} "
                    f"span {candidates}. GW{gameweek} was selected as the later "
                    f"numbered programme ({len(programme)} scheduled fixture(s)); "
                    "use the Gameweek override "
                    "if another programme is intended."
                )
            return {
                "matches": selected,
                "round_label": f"Gameweek {gameweek}",
                "additional_matches": additional_matches,
                "notes": notes,
                "warnings": warnings,
            }

        # Manual/legacy competitions can have dated fixtures without a
        # gameweek.  They still receive the next local reporting week, but no
        # fabricated gameweek is claimed.
        notes.append(
            "No numbered gameweek is assigned to the confirmed fixtures in this reporting week."
        )
        return {
            "matches": [match for match, _ in in_reporting_week],
            "round_label": f"Fixtures for week of {reporting_start:%d %B %Y}",
            "additional_matches": [],
            "notes": notes,
            "warnings": warnings,
        }

    tba_by_gameweek = {}
    for match, match_date in dated:
        if match_date is None and match.gameweek is not None and match.gameweek > 0:
            tba_by_gameweek.setdefault(match.gameweek, []).append(match)
    if tba_by_gameweek:
        gameweek, selected = sorted(
            tba_by_gameweek.items(), key=lambda item: (-len(item[1]), -item[0])
        )[0]
        message = (
            f"{competition.name} has no confirmed future fixture dates; "
            f"showing TBA fixtures for Gameweek {gameweek}."
        )
        return {
            "matches": selected,
            "round_label": f"Gameweek {gameweek}",
            "additional_matches": [],
            "notes": [message],
            "warnings": [message],
        }

    if any(match_date is None for _, match_date in dated):
        message = f"{competition.name} has only unassigned TBA upcoming fixtures."
        return {
            "matches": [match for match, match_date in dated if match_date is None],
            "round_label": "Fixtures with dates to be confirmed",
            "additional_matches": [],
            "notes": [message],
            "warnings": [message],
        }

    return {
        "matches": [],
        "round_label": "",
        "additional_matches": [],
        "notes": [],
        "warnings": [],
    }


def _manual_gameweek_fixture_selection(competition, gameweek):
    """Export every recorded fixture for an administrator-selected gameweek."""
    matches = list(_match_queryset(competition).filter(gameweek=gameweek))
    if not matches:
        return {
            "matches": [],
            "round_label": f"Gameweek {gameweek}",
            "notes": [],
            "warnings": [
                f"{competition.name} has no recorded fixtures in Gameweek {gameweek}."
            ],
        }
    return {
        "matches": matches,
        "round_label": f"Gameweek {gameweek}",
        "notes": [f"Gameweek {gameweek} was selected manually."],
        "warnings": [],
    }


def _fixture_selection(competition, *, report_date=None, gameweek_override=None):
    """Compatibility helper for consumers of the default weekly selection."""
    selection = _weekly_fixture_selection(
        competition,
        report_date=report_date or timezone.localdate(timezone=ESWATINI_TIME_ZONE),
        gameweek_override=gameweek_override,
    )
    return selection["matches"], selection["round_label"]


def _fixture_scope_label(scope):
    return FIXTURE_SCOPE_LABELS[scope].upper()


def _fixture_schedule_detail(match):
    """Give complete-round reports an explicit date/time, or an honest TBA."""
    if not match.match_date:
        return "TBA"
    value = (
        timezone.localtime(match.match_date)
        if timezone.is_aware(match.match_date)
        else match.match_date
    )
    return (
        f"{value.strftime('%A')} {value.day} {value.strftime('%B %Y')} "
        f"\u2014 {_newspaper_time(value)}"
    )


def _complete_round_selection(competition, scope):
    """Select recorded table fixtures by their persisted round/leg.

    The generator and manual-fixture form make ``leg`` the persisted round
    decision. We deliberately do not infer it from dates, status or team
    count: that would misclassify postponements, odd-team byes and legacy
    manual schedules.
    """
    if competition.format_type != "TABLE":
        return {
            "matches": Match.objects.none(),
            "round_label": _fixture_scope_label(scope),
            "notes": [],
            "warnings": [
                f"{competition.name} is not a Table Only league, so complete-round "
                "fixture exports are unavailable."
            ],
        }

    matches = _match_queryset(competition)
    table_matches = matches.filter(stage__stage_type="LEAGUE", group__isnull=True)
    invalid_stage_count = matches.exclude(
        stage__stage_type="LEAGUE", group__isnull=True
    ).count()
    invalid_leg_count = table_matches.exclude(leg__in=(1, 2)).count()
    selected_legs = {
        FIXTURE_SCOPE_FIRST_ROUND: (1,),
        FIXTURE_SCOPE_SECOND_ROUND: (2,),
        FIXTURE_SCOPE_FULL_SEASON: (1, 2),
    }[scope]
    selected = table_matches.filter(leg__in=selected_legs)

    warnings = []
    if invalid_stage_count:
        warnings.append(
            f"{competition.name} has {invalid_stage_count} fixture(s) outside its "
            "league-table stage. They cannot be assigned to a league round."
        )
    if invalid_leg_count:
        warnings.append(
            f"{competition.name} has {invalid_leg_count} league fixture(s) without "
            "a first- or second-round leg. Correct their leg before exporting a "
            "complete round."
        )
    if not selected.exists() and not warnings:
        warnings.append(
            f"{competition.name} has no recorded {_fixture_scope_label(scope).lower()}."
        )

    notes = []
    if selected.filter(gameweek__isnull=True).exists():
        notes.append(
            "Some recorded fixtures have no gameweek and are shown under "
            "Unassigned gameweek."
        )
    gameweek_boundaries = table_matches.aggregate(
        first_max=Max("gameweek", filter=Q(leg=1)),
        second_min=Min("gameweek", filter=Q(leg=2)),
    )
    if (
        gameweek_boundaries["first_max"] is not None
        and gameweek_boundaries["second_min"] is not None
        and gameweek_boundaries["second_min"] <= gameweek_boundaries["first_max"]
    ):
        notes.append(
            "Recorded first- and second-round gameweeks overlap; fixtures are "
            "classified by their persisted leg, not by gameweek number."
        )
    return {
        "matches": selected,
        "round_label": _fixture_scope_label(scope),
        "notes": notes,
        "warnings": warnings,
    }


def _result_selection(competition):
    matches = _match_queryset(competition)
    finals = matches.filter(status="FINISHED")
    with_gameweek = finals.filter(gameweek__gt=0).order_by("-gameweek", "-match_date", "-id")
    if with_gameweek.exists():
        gameweek = with_gameweek.values_list("gameweek", flat=True).first()
        return matches.filter(gameweek=gameweek), f"Gameweek {gameweek}"
    latest = finals.order_by("-match_date", "-id").first()
    if not latest:
        return matches.none(), ""
    if competition.format_type == "TABLE" and latest.match_date:
        date = timezone.localtime(latest.match_date).date() if timezone.is_aware(latest.match_date) else latest.match_date.date()
        return matches.filter(match_date__date=date), f"Results for {date.day} {date.strftime('%B %Y')}"
    return matches.filter(stage=latest.stage), latest.get_stage_label()


def _playing_description(match_date, report_date):
    if not match_date:
        return "Date to be confirmed"
    date = timezone.localtime(match_date).date() if timezone.is_aware(match_date) else match_date.date()
    if date == report_date:
        return "Playing today"
    if date == report_date + timedelta(days=1):
        return "Playing tomorrow"
    if 1 < (date - report_date).days < 7:
        return f"Playing {date.strftime('%A')}"
    return f"Playing {date.strftime('%A')} {date.day} {date.strftime('%B %Y')}"


def _fixture_playing_description(match, report_date, *, include_full_date=False):
    """Append a real gameweek without changing legacy/manual fixture labels."""
    if include_full_date and match.match_date:
        value = (
            timezone.localtime(match.match_date)
            if timezone.is_aware(match.match_date)
            else match.match_date
        )
        label = f"Playing {value.strftime('%A')} {value.day} {value.strftime('%B %Y')}"
    else:
        label = _playing_description(match.match_date, report_date)
    if match.gameweek and match.gameweek > 0:
        return f"{label} \u2014 GW{match.gameweek}"
    return label


def _newspaper_time(match_date):
    """Use the time language and rhythm of the supplied newspaper fixture sheet."""
    if not match_date:
        return ""
    value = timezone.localtime(match_date) if timezone.is_aware(match_date) else match_date
    if value.hour < 12:
        return value.strftime("%H:%M") + "am"
    if value.hour == 12:
        return value.strftime("%H:%M") + " noon"
    return value.strftime("%H:%M") + " hours"


def _fixture_row(match, *, include_schedule=False, include_status=False):
    date = match.match_date
    if date and timezone.is_aware(date):
        date = timezone.localtime(date, ESWATINI_TIME_ZONE)
    return {
        "pdf_date": f"{date:%A}, {date.day} {date:%B %Y}" if date else "Date to be confirmed",
        "pdf_time": f"{date.hour % 12 or 12}:{date:%M %p}" if date else "TBA",
        "kind": "fixture",
        "home": match.home_team.name,
        "away": match.away_team.name,
        "time": (
            _fixture_schedule_detail(match)
            if include_schedule
            else _newspaper_time(match.match_date)
        ),
        "status": match.get_status_display() if include_status else "",
    }


def _result_score(match):
    """Format the project's official result fields in newspaper score order."""
    if match.has_penalty_shootout:
        return (
            f"({match.penalty_home_score}){match.home_score}-"
            f"{match.away_score}({match.penalty_away_score})"
        )
    score = f"{match.home_score}-{match.away_score}"
    return f"{score} ({match.result_note})" if match.result_note else score


def _result_row(match):
    return {
        "kind": "result",
        "home": match.home_team.name,
        "away": match.away_team.name,
        "score": _result_score(match),
        "pdf_score": _result_score(match) if match.has_penalty_shootout else f"{match.home_score}-{match.away_score}",
        "result_note": match.result_note,
    }


def _stage_heading(match):
    """Expose a configured stream/stage without printing the generic League label."""
    if not match.stage_id:
        return ""
    name = match.stage.name.strip()
    if match.stage.stage_type == "LEAGUE" and name.casefold() in {"league", "league stage"}:
        return ""
    return name


def _groups(matches, *, report_kind, report_date, include_full_fixture_dates=False):
    """Preserve date, stream/stage, venue and group hierarchy from match data."""
    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))
    for match in matches:
        stream = _stage_heading(match)
        group = match.group.name.strip() if match.group_id else ""
        if report_kind == "FIXTURES":
            date_label = _fixture_playing_description(
                match,
                report_date,
                include_full_date=include_full_fixture_dates,
            )
            venue = match.location or ""
            grouped[date_label][stream][venue][group].append(_fixture_row(match))
        elif match.status == "FINISHED":
            grouped[""][stream][""][group].append(_result_row(match))

    sections = []
    for date_label, streams in grouped.items():
        stream_sections = []
        for stream, venues in streams.items():
            venue_sections = []
            for venue, groups in venues.items():
                venue_sections.append({
                    "venue": venue,
                    "groups": [
                        {"group": group, "rows": rows}
                        for group, rows in groups.items()
                    ],
                })
            stream_sections.append({"stream": stream, "venues": venue_sections})
        sections.append({"date_label": date_label, "streams": stream_sections})
    return sections


def _complete_round_groups(matches, *, include_status=False):
    """Group recorded fixtures by gameweek without losing report hierarchy."""
    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))
    ordered_matches = sorted(
        matches,
        key=lambda match: (
            match.gameweek is None,
            match.gameweek or 0,
            match.match_date is None,
            match.match_date.isoformat() if match.match_date else "",
            match.id,
        ),
    )
    for match in ordered_matches:
        gameweek = (
            f"Gameweek {match.gameweek}"
            if match.gameweek is not None
            else "Unassigned gameweek"
        )
        stream = _stage_heading(match)
        venue = match.location or ""
        group = match.group.name.strip() if match.group_id else ""
        grouped[gameweek][stream][venue][group].append(
            _fixture_row(
                match,
                include_schedule=True,
                include_status=include_status,
            )
        )

    sections = []
    for gameweek, streams in grouped.items():
        stream_sections = []
        for stream, venues in streams.items():
            venue_sections = []
            for venue, groups in venues.items():
                venue_sections.append({
                    "venue": venue,
                    "groups": [
                        {"group": group, "rows": rows}
                        for group, rows in groups.items()
                    ],
                })
            stream_sections.append({"stream": stream, "venues": venue_sections})
        sections.append({"date_label": gameweek, "streams": stream_sections})
    return sections


_STANDINGS_COLUMNS = ("#", "Team", "P", "W", "D", "L", "GF", "GA", "GD", "Pts")


def _standing_row(position, standing):
    """Serialize an already-ranked public standings row for both renderers."""
    return {
        "kind": "standing",
        "position": position,
        "team": standing.team.name,
        "played": standing.total_played,
        "won": standing.total_won,
        "drawn": standing.total_drawn,
        "lost": standing.total_lost,
        "goals_for": standing.total_goals_for,
        "goals_against": standing.total_goals_against,
        "goal_difference": standing.total_goal_difference,
        "points": standing.total_points,
    }


def _competition_standing_tables(competition):
    """Build the exact table/group scopes shown on the public standings page.

    Ranking is deliberately delegated to ``rank_standings``.  That service
    applies each competition's configured tie breakers, uses the persisted
    baseline-inclusive totals, and confines head-to-head records to the
    appropriate stage/group.  Export code must never reimplement it.
    """
    if competition.format_type == "TABLE":
        standings = rank_standings(
            competition,
            Standing.objects.filter(
                competition=competition,
                group__isnull=True,
            ).select_related("team"),
        )
        return [{
            "label": "League Table",
            "rows": [
                _standing_row(position, standing)
                for position, standing in enumerate(standings, start=1)
            ],
        }]

    if competition.format_type != "GROUP_KNOCKOUT":
        return []

    tables = []
    group_stages = competition.stages.filter(stage_type="GROUP").order_by("order")
    for stage in group_stages:
        for group in stage.groups.all():
            standings = rank_standings(
                competition,
                Standing.objects.filter(
                    competition=competition,
                    stage=stage,
                    group=group,
                ).select_related("team"),
            )
            tables.append({
                "label": f"{stage.name} — {group.name}",
                "rows": [
                    _standing_row(position, standing)
                    for position, standing in enumerate(standings, start=1)
                ],
            })
    return tables


class ReportExportError(Exception):
    """An actionable report problem that management screens can show safely."""


def _organisation_stationery_owner(organisation):
    if organisation not in FootballOrganisation.Code.values:
        return None
    return FootballOrganisation.objects.filter(code=organisation).first()


def report_stationery_owner(competition):
    """Resolve stationery by permanent classification, never geography alone."""
    series = competition.series
    if series.league_type == CompetitionSeries.LeagueType.REGIONAL:
        return series.region if series.region_id else None
    if series.league_type == CompetitionSeries.LeagueType.ZONAL:
        return series
    return _organisation_stationery_owner(series.league_type)


def report_letterheads_for(competition, owner):
    """Select the Word template, image, and whether the image replaces the Word header."""
    template = owner.report_template if owner else None
    image = owner.fixture_letterhead if owner else None
    use_image_in_word = False
    series = competition.series
    if series.league_type == CompetitionSeries.LeagueType.REGIONAL:
        template = series.report_template or template
        image = series.fixture_letterhead or image
        use_image_in_word = bool(series.fixture_letterhead and not series.report_template)
    return template, image, use_image_in_word


def report_template_for(competition):
    return report_letterheads_for(competition, report_stationery_owner(competition))[0]


def fixture_letterhead_for(competition):
    return report_letterheads_for(competition, report_stationery_owner(competition))[1]


def build_scope_report_data(
    *,
    preference,
    scope_name,
    scope_abbreviation="",
    scope_subheading="",
    competitions,
    scope_template=None,
    scope_letterhead=None,
    scope_use_image_in_word=False,
    scope_stationery_owner=None,
    report_date=None,
    fixture_scope=FIXTURE_SCOPE_NEXT_UPCOMING,
    fixture_gameweek_mode=WEEKLY_SELECTION_AUTOMATIC,
    manual_gameweeks=None,
    gameweek_override=None,
):
    """The one business-rule source for regional and organisation exports."""
    if fixture_scope not in FIXTURE_EXPORT_SCOPES:
        raise ValueError("Unknown fixture export scope.")
    if fixture_gameweek_mode not in WEEKLY_SELECTION_MODES:
        raise ValueError("Unknown weekly fixture selection mode.")
    if manual_gameweeks is not None and not isinstance(manual_gameweeks, dict):
        raise ValueError("Manual gameweek selections must be a mapping.")
    if gameweek_override is not None and (
        isinstance(gameweek_override, bool)
        or not isinstance(gameweek_override, int)
        or gameweek_override <= 0
    ):
        raise ValueError("Gameweek override must be a positive whole number.")
    manual_gameweeks = manual_gameweeks or {}
    if any(
        isinstance(gameweek, bool)
        or not isinstance(gameweek, int)
        or gameweek <= 0
        for gameweek in manual_gameweeks.values()
    ):
        raise ValueError("Manual gameweek selections must be positive whole numbers.")
    report_date = report_date or timezone.localdate(timezone=ESWATINI_TIME_ZONE)
    kind = preference.report_kind
    entries = []
    warnings = []
    for competition in competitions:
        if kind == preference.ReportKind.STANDINGS:
            tables = _competition_standing_tables(competition)
            entries.append({
                "competition": competition.name,
                "season": competition.season,
                "tables": tables,
                "empty_message": (
                    "No league-table or group-stage standings are available for this competition yet."
                    if not tables
                    else ""
                ),
            })
            continue

        complete_round_export = (
            kind == preference.ReportKind.FIXTURES
            and fixture_scope != FIXTURE_SCOPE_NEXT_UPCOMING
        )
        entry_notes = []
        additional_sections = []
        manual_gameweek_export = False
        if complete_round_export:
            selection = _complete_round_selection(competition, fixture_scope)
            matches = selection["matches"]
            round_label = selection["round_label"]
            entry_notes = selection["notes"]
            warnings.extend(selection["warnings"])
        elif kind == preference.ReportKind.FIXTURES:
            manual_gameweek = (
                manual_gameweeks.get(competition.id)
                if fixture_gameweek_mode == WEEKLY_SELECTION_MANUAL
                else gameweek_override
            )
            if manual_gameweek is not None:
                selection = _manual_gameweek_fixture_selection(competition, manual_gameweek)
                manual_gameweek_export = True
            else:
                selection = _weekly_fixture_selection(
                    competition,
                    report_date=report_date,
                )
            matches = selection["matches"]
            round_label = selection["round_label"]
            entry_notes = selection["notes"]
            if not manual_gameweek_export:
                additional_sections = _groups(
                    selection["additional_matches"],
                    report_kind=kind,
                    report_date=report_date,
                    include_full_fixture_dates=True,
                )
            warnings.extend(selection["warnings"])
        else:
            matches, round_label = _result_selection(competition)
        matches = list(matches)
        if kind == preference.ReportKind.RESULTS and matches:
            complete = [match for match in matches if match.status == "FINISHED"]
            if len(complete) != len(matches):
                warnings.append(
                    f"{competition.name} – {round_label or 'latest round'} has "
                    f"{len(matches)} fixtures but only {len(complete)} final results."
                )
        if matches:
            owner = report_stationery_owner(competition)
            template, image, use_image_in_word = report_letterheads_for(competition, owner)
            entries.append({
                "competition": competition.name,
                "round_label": round_label,
                "season": competition.season,
                "show_round_label": complete_round_export or manual_gameweek_export,
                "fixture_letterhead": image,
                "stationery_owner": owner,
                "fixture_new_page": kind == "FIXTURES" and competition.series.league_type == "ZONAL",
                "report_template": template,
                "use_image_in_word": use_image_in_word,
                "report_new_page": competition.series.league_type == "ZONAL",
                "notes": entry_notes,
                "additional_sections": additional_sections,
                "sections": (
                    _complete_round_groups(
                        matches,
                        include_status=manual_gameweek_export,
                    )
                    if complete_round_export or manual_gameweek_export
                    else _groups(matches, report_kind=kind, report_date=report_date)
                ),
            })

    abbreviation = scope_abbreviation.strip() or scope_name.upper()
    kind_label = {
        preference.ReportKind.FIXTURES: "FIXTURES",
        preference.ReportKind.RESULTS: "RESULTS",
        preference.ReportKind.STANDINGS: "STANDINGS",
    }[kind]
    if kind == preference.ReportKind.FIXTURES and fixture_scope != FIXTURE_SCOPE_NEXT_UPCOMING:
        heading = (
            f"{abbreviation} {_fixture_scope_label(fixture_scope)} – "
            f"{report_date.day} {report_date.strftime('%B %Y').upper()}"
        )
    elif kind == preference.ReportKind.STANDINGS:
        heading = (
            f"{abbreviation} STANDINGS AS AT "
            f"{report_date.day} {report_date.strftime('%B %Y').upper()}"
        )
    else:
        heading = f"{abbreviation} WEEKEND {kind_label} – {report_date.day} {report_date.strftime('%B %Y').upper()}"
    return {
        "heading": heading,
        "subheading": scope_subheading.strip(),
        "report_template": scope_template if kind in {"FIXTURES", "RESULTS"} else None,
        "stationery_owner": scope_stationery_owner,
        "use_image_in_word": scope_use_image_in_word,
        "fixture_letterhead": scope_letterhead if scope_letterhead is not None else (
            scope_stationery_owner.fixture_letterhead if scope_stationery_owner else None
        ),
        "kind": kind,
        "entries": entries,
        "warnings": warnings,
        "report_date": report_date,
        "abbreviation": abbreviation,
        "fixture_scope": fixture_scope,
        "fixture_gameweek_mode": fixture_gameweek_mode,
    }


def build_regional_report_data(
    *,
    region,
    preference,
    selected_series_ids=None,
    report_date=None,
    fixture_scope=FIXTURE_SCOPE_NEXT_UPCOMING,
    fixture_gameweek_mode=WEEKLY_SELECTION_AUTOMATIC,
    manual_gameweeks=None,
    gameweek_override=None,
):
    """Backward-compatible regional entry point for report consumers."""
    return build_scope_report_data(
        preference=preference,
        scope_name=region.name,
        scope_abbreviation=region.report_abbreviation,
        scope_subheading=region.report_subheading,
        scope_template=region.report_template,
        scope_stationery_owner=region,
        competitions=selected_competitions(region, preference, selected_series_ids=selected_series_ids),
        report_date=report_date,
        fixture_scope=fixture_scope,
        fixture_gameweek_mode=fixture_gameweek_mode,
        manual_gameweeks=manual_gameweeks,
        gameweek_override=gameweek_override,
    )


def build_organisation_report_data(
    *,
    organisation,
    preference,
    selected_series_ids=None,
    report_date=None,
    fixture_scope=FIXTURE_SCOPE_NEXT_UPCOMING,
    fixture_gameweek_mode=WEEKLY_SELECTION_AUTOMATIC,
    manual_gameweeks=None,
    gameweek_override=None,
):
    """Build a non-regional section report using the established renderer."""
    labels = {
        CompetitionSeries.LeagueType.EWFA: "Eswatini Women's Football Association (EWFA)",
        CompetitionSeries.LeagueType.PLE: "Premier League of Eswatini (PLE)",
        CompetitionSeries.LeagueType.ZONAL: "Zonal Leagues",
    }
    owner = _organisation_stationery_owner(organisation)
    return build_scope_report_data(
        preference=preference,
        scope_name=labels[organisation],
        scope_abbreviation=organisation,
        scope_template=owner.report_template if owner else None,
        scope_stationery_owner=owner,
        competitions=selected_organisation_competitions(organisation, preference, selected_series_ids=selected_series_ids),
        report_date=report_date,
        fixture_scope=fixture_scope,
        fixture_gameweek_mode=fixture_gameweek_mode,
        manual_gameweeks=manual_gameweeks,
        gameweek_override=gameweek_override,
    )


def report_document_rows(data):
    """Build the established Word and standings presentation rows."""
    rows = [{"kind": "blank", "text": ""}, {"kind": "title", "text": data["heading"]}]

    def append_sections(sections):
        for section in sections:
            if section["date_label"]:
                rows.append({"kind": "label", "text": section["date_label"]})
            for stream in section["streams"]:
                if stream["stream"]:
                    rows.append({"kind": "label", "text": stream["stream"]})
                for venue in stream["venues"]:
                    if venue["venue"]:
                        rows.append({"kind": "venue", "text": venue["venue"]})
                    for group in venue["groups"]:
                        if group["group"]:
                            rows.append({"kind": "label", "text": group["group"]})
                        rows.extend(group["rows"])

    if data["subheading"]:
        rows.extend([{"kind": "blank", "text": ""}, {"kind": "subtitle", "text": data["subheading"]}])
    for entry in data["entries"]:
        rows.extend([
            {"kind": "blank", "text": ""},
            {"kind": "competition", "text": entry["competition"].upper()},
        ])
        if data["kind"] == "STANDINGS":
            rows.append({"kind": "label", "text": f"SEASON: {entry['season']}"})
            for table in entry["tables"]:
                rows.append({"kind": "label", "text": table["label"]})
                if table["rows"]:
                    rows.append({
                        "kind": "standings_header",
                        "columns": _STANDINGS_COLUMNS,
                    })
                    rows.extend(table["rows"])
                else:
                    rows.append({
                        "kind": "empty",
                        "text": "No standings data has been recorded for this table yet.",
                    })
            if entry["empty_message"]:
                rows.append({"kind": "empty", "text": entry["empty_message"]})
            continue
        if entry.get("show_round_label"):
            rows.extend([
                {"kind": "label", "text": entry["round_label"]},
                {"kind": "label", "text": f"SEASON: {entry['season']}"},
            ])
        rows.extend(
            {"kind": "note", "text": f"Note: {note}"}
            for note in entry.get("notes", [])
        )
        append_sections(entry["sections"])
        if entry.get("additional_sections"):
            rows.append({"kind": "label", "text": "Additional Fixtures from Earlier Gameweeks"})
            append_sections(entry["additional_sections"])
    if not data["entries"]:
        empty_kind = "standings" if data["kind"] == "STANDINGS" else "fixtures/results"
        rows.extend([{"kind": "blank", "text": ""}, {"kind": "empty", "text": f"No applicable {empty_kind} were found."}])
    return rows


def _row_text(row):
    if row["kind"] == "fixture":
        parts = [row["home"], "vs", row["away"], row["time"]]
        if row.get("status"):
            parts.append(f"Status: {row['status']}")
        return "\t".join(parts).rstrip()
    if row["kind"] == "result":
        return "\t".join((row["home"], row["score"], row["away"]))
    if row["kind"] == "standings_header":
        return "\t".join(row["columns"])
    if row["kind"] == "standing":
        return "\t".join(str(value) for value in (
            row["position"],
            row["team"],
            row["played"],
            row["won"],
            row["drawn"],
            row["lost"],
            row["goals_for"],
            row["goals_against"],
            row["goal_difference"],
            row["points"],
        ))
    return row["text"]


def report_text_lines(data):
    """Plain-text view of the shared editorial hierarchy for previews and tests."""
    return [_row_text(row) for row in report_document_rows(data)]


def _word_run(text, *, bold=False, italic=False, size=24, color=None, underline=False):
    properties = '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/>'
    if bold:
        properties += "<w:b/>"
    if italic:
        properties += "<w:i/>"
    if color:
        properties += f'<w:color w:val="{color}"/>'
    if underline:
        properties += '<w:u w:val="single"/>'
    properties += f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>'


def _word_paragraph(row):
    kind = row["kind"]
    bold = kind in {"title", "subtitle", "competition", "label", "venue", "standings_header"}
    alignment = '<w:jc w:val="center"/>' if kind == "title" else ""
    properties = f'<w:pPr><w:pStyle w:val="NoSpacing"/>{alignment}</w:pPr>'
    contents = _word_run(row["text"], bold=bold, italic=kind == "venue") if kind != "blank" else ""
    return f"<w:p>{properties}{contents}</w:p>"


_WORD_STANDINGS_WIDTHS = (480, 4200, 480, 480, 480, 480, 520, 520, 520, 600)


def _word_standings_cell(value, width, *, bold=False):
    """Render a fixed-width, wrapping Word table cell without external libs."""
    alignment = "center" if width != _WORD_STANDINGS_WIDTHS[1] else "left"
    return (
        '<w:tc><w:tcPr>'
        f'<w:tcW w:w="{width}" w:type="dxa"/>'
        '<w:tcMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/>'
        '<w:top w:w="45" w:type="dxa"/><w:bottom w:w="45" w:type="dxa"/></w:tcMar>'
        '</w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="{alignment}"/></w:pPr>'
        f'{_word_run(str(value), bold=bold, size=16)}</w:p></w:tc>'
    )


def _word_standings_table(header, rows):
    """Create a real Word table with a repeating header row for standings."""
    header_cells = "".join(
        _word_standings_cell(value, width, bold=True)
        for value, width in zip(header["columns"], _WORD_STANDINGS_WIDTHS)
    )
    table_rows = [
        f'<w:tr><w:trPr><w:tblHeader/></w:trPr>{header_cells}</w:tr>'
    ]
    for row in rows:
        values = (
            row["position"], row["team"], row["played"], row["won"], row["drawn"],
            row["lost"], row["goals_for"], row["goals_against"], row["goal_difference"],
            row["points"],
        )
        cells = "".join(
            _word_standings_cell(value, width, bold=index in {0, 9})
            for index, (value, width) in enumerate(zip(values, _WORD_STANDINGS_WIDTHS))
        )
        table_rows.append(f"<w:tr>{cells}</w:tr>")
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for width in _WORD_STANDINGS_WIDTHS)
    return (
        '<w:tbl><w:tblPr><w:tblW w:w="9360" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="808080"/>'
        '<w:left w:val="single" w:sz="4" w:color="808080"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="808080"/>'
        '<w:right w:val="single" w:sz="4" w:color="808080"/>'
        '<w:insideH w:val="single" w:sz="2" w:color="BFBFBF"/>'
        '<w:insideV w:val="single" w:sz="2" w:color="BFBFBF"/></w:tblBorders>'
        '</w:tblPr>'
        f'<w:tblGrid>{grid}</w:tblGrid>{"".join(table_rows)}</w:tbl>'
    )


def _word_footer(*, fixture=False):
    website_url = xml_escape(_report_website_url(), quote=True)
    size = 18 if fixture else 16
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        f'{_word_run(REPORT_FOOTER_MESSAGE, size=size)}</w:p>'
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        f'<w:hyperlink r:id="rId1">{_word_run(REPORT_FOOTER_LABEL, size=size, color="0563C1", underline=True)}</w:hyperlink>'
        '</w:p></w:ftr>'
    ), website_url


def _report_parts(data):
    if data["kind"] == "STANDINGS":
        return [(None, report_document_rows(data), False)]

    parts = []
    for section, include_closing in report_sections(data, for_word=True):
        first = section["entries"][0] if section["entries"] else section
        parts.append((first.get("fixture_letterhead"), section, include_closing))
    return parts


def _letterhead_image(field, cache=None):
    """Read through configured media storage, including backends without local paths."""
    if not field:
        return None
    cache = {} if cache is None else cache
    key = (id(field.storage), field.name)
    if key not in cache:
        try:
            with field.storage.open(field.name, "rb") as stream, Image.open(stream) as original:
                image = ImageOps.exif_transpose(original).convert("RGBA")
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                output = BytesIO()
                background.save(output, format="PNG")
                cache[key] = {
                    "width": background.width, "height": background.height,
                    "png": output.getvalue(),
                }
        except (OSError, ValueError, RequestException, Image.DecompressionBombError):
            logging.getLogger(__name__).warning("Fixture letterhead unavailable; using the existing layout.", exc_info=True)
            cache[key] = None
    return cache[key]


def _letterhead_size(image, printable_width):
    # Bound tall uploads too: stationery must leave useful room for fixtures.
    scale = min(printable_width / image["width"], 126 / image["height"])
    return image["width"] * scale, image["height"] * scale


def _word_header(image, number, printable_width):
    contents = "<w:p/>"
    if image:
        width, height = _letterhead_size(image, printable_width)
        contents = (
            '<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="0" w:after="0"/></w:pPr>'
            f'{word_picture("rId1", number, "Fixture letterhead", width, height)}</w:p>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'{contents}</w:hdr>'
    )


def _render_legacy_docx(data, *, include_signature=False, include_stamp=False, include_closing=True):
    cache = {}
    parts = _report_parts(data)
    paragraphs = []
    relationships = ['<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>']
    extras = {}
    overrides = []
    standings = data["kind"] == "STANDINGS"
    page_width, page_height = (12240, 15840) if standings else (WORD_PAGE_WIDTH, WORD_PAGE_HEIGHT)
    margin = 1440 if standings else WORD_MARGIN
    bottom = 1440 if standings else 1360
    def add_artwork(content, label, max_width, max_height):
        with Image.open(BytesIO(content)) as image:
            scale = min(max_width / image.width, max_height / image.height)
            width, height = image.width * scale, image.height * scale
        number = len(parts) + len(relationships)
        relationship = f"closingImage{number}"
        target = f"media/{relationship}.png"
        relationships.append(
            f'<Relationship Id="{relationship}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}"/>'
        )
        extras[f"word/{target}"] = content
        return word_picture(relationship, number, label, width, height)

    for number, (letterhead, rows, close_section) in enumerate(parts, 1):
        image = _letterhead_image(letterhead, cache)
        if not standings:
            first = rows["entries"][0] if rows["entries"] else rows
            owner = first.get("stationery_owner")
            closing = ReportDocumentSettings.for_owner(
                owner, include_signature=include_signature, include_stamp=include_stamp,
            ) if include_closing and close_section and owner is not None else None
            paragraphs.extend(word_report_body(
                rows, closing=closing, add_artwork=add_artwork,
                include_signature=include_signature, include_stamp=include_stamp,
            ))
            rows = []
        row_index = 0
        while row_index < len(rows):
            row = rows[row_index]
            if row["kind"] == "standings_header":
                standing_rows = []
                next_index = row_index + 1
                while next_index < len(rows) and rows[next_index]["kind"] == "standing":
                    standing_rows.append(rows[next_index])
                    next_index += 1
                paragraphs.append(_word_standings_table(row, standing_rows))
                row_index = next_index
                continue
            paragraphs.append(_word_paragraph(row))
            row_index += 1
        header_reference = ""
        top, header_distance = (999, 720) if standings else (WORD_MARGIN, 560)
        # An explicit empty header breaks Word's default inheritance for an
        # unbranded league following a branded league in a combined document.
        if image or len(parts) > 1:
            header_reference = f'<w:headerReference w:type="default" r:id="header{number}"/>'
            relationships.append(f'<Relationship Id="header{number}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header{number}.xml"/>')
            extras[f"word/header{number}.xml"] = _word_header(image, number, WORD_PRINTABLE_WIDTH / 20)
            overrides.append(f'<Override PartName="/word/header{number}.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>')
            if image:
                _, height = _letterhead_size(image, WORD_PRINTABLE_WIDTH / 20)
                top = max(top, round(header_distance + height * 20 + 400))
                extras[f"word/media/letterhead{number}.png"] = image["png"]
                extras[f"word/_rels/header{number}.xml.rels"] = (
                    '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/letterhead{number}.png"/></Relationships>'
                )
        section = (
            f'<w:sectPr>{header_reference}<w:footerReference w:type="default" r:id="rId1"/>'
            f'<w:type w:val="nextPage"/><w:pgSz w:w="{page_width}" w:h="{page_height}"/>'
            f'<w:pgMar w:top="{top}" w:right="{margin}" w:bottom="{bottom}" w:left="{margin}" '
            f'w:header="{header_distance}" w:footer="720" w:gutter="0"/></w:sectPr>'
        )
        if number < len(parts):
            paragraphs.append(
                '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
                f'<w:rPr><w:sz w:val="2"/></w:rPr>{section}</w:pPr></w:p>'
            )
        else:
            paragraphs.append(section)
    footer, footer_url = _word_footer(fixture=data["kind"] == "FIXTURES")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<w:body>{"".join(paragraphs)}</w:body></w:document>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>' + "".join(overrides) + '</Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(relationships) + '</Relationships>')
        archive.writestr("word/footer1.xml", footer)
        archive.writestr("word/_rels/footer1.xml.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{footer_url}" TargetMode="External"/></Relationships>')
        for name, contents in extras.items():
            archive.writestr(name, contents)
    return output.getvalue()


def _uses_report_template(data):
    return data["kind"] in {"FIXTURES", "RESULTS"} and any(
        entry.get("report_template") for entry in (data["entries"] or [data])
    )


def _word_report_parts(data, **artwork):
    parts = []
    for section, include_closing in report_sections(data, for_word=True):
        first = section["entries"][0] if section["entries"] else section
        parts.append({
            "template": first.get("report_template"),
            "use_source_header": first.get("use_image_in_word", False),
            "document": _render_legacy_docx(section, include_closing=include_closing, **artwork),
        })
    return parts


def render_docx(data, *, include_signature=False, include_stamp=False):
    artwork = {"include_signature": include_signature, "include_stamp": include_stamp}
    if not _uses_report_template(data):
        return _render_legacy_docx(data, **artwork)
    from .report_template_rendering import ReportTemplateRenderingError, render_template_docx

    try:
        return render_template_docx(_word_report_parts(data, **artwork))
    except ReportTemplateRenderingError as error:
        raise ReportExportError(str(error)) from error


def _pdf_escape(value):
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


_PDF_PAGE_WIDTH = 612
_PDF_LEFT_MARGIN = 54
_PDF_RIGHT_MARGIN = 54
_PDF_TOP_MARGIN = 756
# Keep report content above the two-line EsportsLive footer on every page.
_PDF_BOTTOM_MARGIN = 78
_PDF_FONT_SIZE = 12
_PDF_LINE_HEIGHT = 15


# Standard Times Type 1 metrics in one-thousandth em units.  The report uses
# only these built-in Times fonts, so this gives wrapping a real page width
# rather than relying on a fixed character count.
_PDF_TIMES_WIDTHS = {
    " ": 250,
    "!": 333,
    '"': 408,
    "#": 500,
    "$": 500,
    "%": 833,
    "&": 778,
    "'": 180,
    "(": 333,
    ")": 333,
    "*": 500,
    "+": 564,
    ",": 250,
    "-": 333,
    ".": 250,
    "/": 278,
    ":": 278,
    ";": 278,
    "<": 564,
    "=": 564,
    ">": 564,
    "?": 444,
    "@": 921,
    "[": 333,
    "\\": 278,
    "]": 333,
    "^": 469,
    "_": 500,
    "`": 333,
    "{": 480,
    "|": 200,
    "}": 480,
    "~": 541,
}
_PDF_TIMES_WIDTHS.update({character: 500 for character in "0123456789"})
_PDF_TIMES_WIDTHS.update(dict(zip(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    (722, 667, 722, 722, 611, 556, 778, 778, 389, 500, 778, 667, 944,
     722, 778, 611, 778, 722, 556, 667, 722, 722, 1000, 722, 722, 667),
)))
_PDF_TIMES_WIDTHS.update(dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    (500, 556, 444, 556, 444, 333, 500, 556, 278, 333, 556, 278, 833,
     556, 500, 556, 556, 444, 389, 333, 556, 500, 722, 500, 500, 444),
)))


def _pdf_text_width(text, font_size=_PDF_FONT_SIZE):
    return sum(_PDF_TIMES_WIDTHS.get(character, 500) for character in text) * font_size / 1000


def _pdf_wrap_text(text, *, max_width):
    """Wrap text against the printable width while retaining every character."""
    if not text:
        return [""]

    lines = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and _pdf_text_width(candidate) <= max_width:
            current = candidate
            continue

        if not current and _pdf_text_width(word) <= max_width:
            current = word
            continue

        if current:
            lines.append(current)
        current = ""
        for character in word:
            if current and _pdf_text_width(current + character) > max_width:
                lines.append(current)
                current = character
            else:
                current += character
    if current:
        lines.append(current)
    return lines


def _pdf_row_lines(row):
    if row["kind"] == "standings_header":
        return [""]
    if row["kind"] == "standing":
        return _pdf_wrap_text(
            row["team"],
            max_width=_PDF_STANDINGS_COLUMNS[1][1] - 6,
        )
    text = _row_text(row).replace("\t", "    ")
    return _pdf_wrap_text(
        text,
        max_width=_PDF_PAGE_WIDTH - _PDF_LEFT_MARGIN - _PDF_RIGHT_MARGIN,
    )


_PDF_STANDINGS_COLUMNS = (
    ("#", 24),
    ("Team", 235),
    ("P", 26),
    ("W", 26),
    ("D", 26),
    ("L", 26),
    ("GF", 31),
    ("GA", 31),
    ("GD", 31),
    ("Pts", 37),
)


def _pdf_standings_cells(row, line_index):
    """Return placed table cells for a standings row or repeating header."""
    x_position = _PDF_LEFT_MARGIN
    if row["kind"] == "standings_header":
        values = row["columns"]
        bold_cells = set(range(len(values)))
    else:
        team_lines = _pdf_row_lines(row)
        values = (
            row["position"] if line_index == 0 else "",
            team_lines[line_index],
            row["played"] if line_index == 0 else "",
            row["won"] if line_index == 0 else "",
            row["drawn"] if line_index == 0 else "",
            row["lost"] if line_index == 0 else "",
            row["goals_for"] if line_index == 0 else "",
            row["goals_against"] if line_index == 0 else "",
            row["goal_difference"] if line_index == 0 else "",
            row["points"] if line_index == 0 else "",
        )
        bold_cells = {0, 9}

    cells = []
    for index, (value, (_label, width)) in enumerate(zip(values, _PDF_STANDINGS_COLUMNS)):
        text = str(value)
        if index != 1:
            x = x_position + max(0, (width - _pdf_text_width(text, 9)) / 2)
        else:
            x = x_position + 3
        cells.append((x, text, index in bold_cells))
        x_position += width
    return cells


def _pdf_pages(rows, *, top_margin=_PDF_TOP_MARGIN):
    """Lay out wrapped rows without allowing text past the Letter page margins."""
    pages = []
    page = []
    y_position = top_margin
    standings_header = None
    for row in rows:
        lines = _pdf_row_lines(row)
        height = len(lines) * _PDF_LINE_HEIGHT
        if page and y_position - height < _PDF_BOTTOM_MARGIN:
            pages.append(page)
            page = []
            y_position = top_margin
            if row["kind"] == "standing" and standings_header is not None:
                header_lines = _pdf_row_lines(standings_header)
                page.append((standings_header, header_lines))
                y_position -= len(header_lines) * _PDF_LINE_HEIGHT
        page.append((row, lines))
        y_position -= height
        if row["kind"] == "standings_header":
            standings_header = row
        elif row["kind"] != "standing":
            standings_header = None
    if page:
        pages.append(page)
    return pages or [[]]



def render_pdf(data, *, include_signature=False, include_stamp=False):
    """Generate directly from report data, independent of any Word template."""
    if data["kind"] in {"FIXTURES", "RESULTS"}:
        from .football_pdf import render_football_pdf

        return render_football_pdf(
            data, load_letterhead=_letterhead_image,
            footer_message=REPORT_FOOTER_MESSAGE, footer_label=REPORT_FOOTER_LABEL,
            website=_report_website_url(), include_signature=include_signature, include_stamp=include_stamp,
        )
    return _render_standings_pdf(data)


def _render_standings_pdf(data):
    """Keep the existing standings layout and link annotations."""
    pages = _pdf_pages(report_document_rows(data))

    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "",
    ]

    page_ids = []

    website_url = _report_website_url()

    for page_rows in pages:
        fragments = []
        y_position = _PDF_TOP_MARGIN
        for row, lines in page_rows:
            kind = row["kind"]
            font = "/F3" if kind == "venue" else ("/F2" if kind in {"title", "subtitle", "competition", "label"} else "/F1")
            for line_index, text in enumerate(lines):
                if kind in {"standings_header", "standing"}:
                    for x_position, cell_text, bold in _pdf_standings_cells(row, line_index):
                        if cell_text:
                            fragments.append(
                                f"BT {'/F2' if bold else '/F1'} 9 Tf {x_position:.2f} {y_position} Td ({_pdf_escape(cell_text)}) Tj ET"
                            )
                    if kind == "standings_header":
                        line_end = _PDF_LEFT_MARGIN + sum(width for _label, width in _PDF_STANDINGS_COLUMNS)
                        fragments.append(
                            f"{_PDF_LEFT_MARGIN} {y_position - 3} m {line_end} {y_position - 3} l S"
                        )
                    y_position -= _PDF_LINE_HEIGHT
                    continue
                x_position = (
                    max(_PDF_LEFT_MARGIN, (_PDF_PAGE_WIDTH - _pdf_text_width(text)) / 2)
                    if kind == "title"
                    else _PDF_LEFT_MARGIN
                )
                if text:
                    fragments.append(
                        f"BT {font} {_PDF_FONT_SIZE} Tf {x_position:.2f} {y_position} Td ({_pdf_escape(text)}) Tj ET"
                    )
                y_position -= _PDF_LINE_HEIGHT
        footer_size = 10 if data["kind"] == "FIXTURES" else 9
        footer_message_x = max(
            _PDF_LEFT_MARGIN,
            (_PDF_PAGE_WIDTH - _pdf_text_width(REPORT_FOOTER_MESSAGE, footer_size)) / 2,
        )
        footer_label_x = max(
            _PDF_LEFT_MARGIN,
            (_PDF_PAGE_WIDTH - _pdf_text_width(REPORT_FOOTER_LABEL, footer_size)) / 2,
        )
        fragments.extend((
            f"BT /F1 {footer_size} Tf {footer_message_x:.2f} 43 Td ({_pdf_escape(REPORT_FOOTER_MESSAGE)}) Tj ET",
            f"0 0.2 0.6 rg BT /F1 {footer_size} Tf {footer_label_x:.2f} 28 Td ({_pdf_escape(REPORT_FOOTER_LABEL)}) Tj ET",
        ))
        content = " ".join(fragments)
        page_id = len(objects) + 1
        content_id = len(objects) + 2
        font_id = len(objects) + 3
        bold_font_id = len(objects) + 4
        italic_font_id = len(objects) + 5
        annotation_id = len(objects) + 6
        page_ids.append(page_id)
        link_end = footer_label_x + _pdf_text_width(REPORT_FOOTER_LABEL, footer_size)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R /F2 {bold_font_id} 0 R /F3 {italic_font_id} 0 R >> >> /Annots [{annotation_id} 0 R] /Contents {content_id} 0 R >>")
        objects.append(f"<< /Length {len(content.encode('cp1252', 'replace'))} >>\nstream\n{content}\nendstream")
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman /Encoding /WinAnsiEncoding >>")
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Times-Bold /Encoding /WinAnsiEncoding >>")
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Times-BoldItalic /Encoding /WinAnsiEncoding >>")
        objects.append(f"<< /Type /Annot /Subtype /Link /Rect [{footer_label_x:.2f} 24 {link_end:.2f} 40] /Border [0 0 0] /A << /S /URI /URI ({_pdf_escape(website_url)}) >> >>")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] /Count {len(page_ids)} >>"
    body = "%PDF-1.4\n"
    offsets = [0]
    for index, value in enumerate(objects, start=1):
        offsets.append(len(body.encode("cp1252", "replace")))
        body += f"{index} 0 obj\n{value}\nendobj\n"
    xref = len(body.encode("cp1252", "replace"))
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    body += "".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:])
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return body.encode("cp1252", "replace")


def report_filename(data, output_format):
    stem = re.sub(r"[^A-Z0-9_-]+", "_", data["abbreviation"].upper()).strip("_") or "REGION"
    kind = data["kind"]
    date = f"{data['report_date'].day}_{data['report_date'].strftime('%B_%Y').upper()}"
    if (
        data["kind"] == "FIXTURES"
        and data.get("fixture_scope", FIXTURE_SCOPE_NEXT_UPCOMING)
        != FIXTURE_SCOPE_NEXT_UPCOMING
    ):
        return f"{stem}_{data['fixture_scope']}_{kind}_{date}.{output_format.lower()}"
    if data["kind"] == "STANDINGS":
        return f"{stem}_STANDINGS_{date}.{output_format.lower()}"
    return f"{stem}_WEEKEND_{kind}_{date}.{output_format.lower()}"
