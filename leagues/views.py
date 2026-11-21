from datetime import datetime

from django.shortcuts import render, get_object_or_404
from django.http import Http404
from django.utils import timezone
from django.db import OperationalError, ProgrammingError
from django.db.models import F, Q, Count, Case, When, IntegerField, Prefetch, Value
from django.urls import reverse
from .models import (
    Competition,
    CompetitionSeries,
    FootballHomepageSection,
    FootballOrganisation,
    HomepageBanner,
    Region,
    Standing,
)
from matches.models import FootballKnockoutTie, FootballKnockoutTieSlot, Match
from matches.knockout import aggregate_display
from teams.models import Team
from django.db.models import Count
from matches.models import Goal
from matches.models import Player
from django.db.models.functions import Coalesce
from .standings import rank_standings
from .football import attach_standings_form, completed_table_matches, recent_form_by_team, record
# ===== REMOVE =====
# from django.contrib.auth.decorators import login_required


PUBLIC_SEASON_ROUTE_NAMES = {
    "league_detail",
    "league_fixtures",
    "league_results",
    "league_standings",
    "league_stats",
}
REGION_PREVIEW_LIMIT = 6
# Compact cards deliberately have one shared cap for non-current matchdays.
# Today's matchday is never capped: hiding a live or just-finished result is
# more confusing than a longer card on the one day it is relevant.
PUBLIC_CARD_CONTEXT_LIMIT = 8

FOOTBALL_DIRECTORY_SECTION_DETAILS = {
    FootballHomepageSection.Section.REGIONS: {
        "label": "Regions",
        "template": "leagues/partials/football_directory_regions.html",
    },
    FootballHomepageSection.Section.EWFA: {
        "label": "EWFA",
        "template": "leagues/partials/football_directory_ewfa.html",
    },
    FootballHomepageSection.Section.PLE: {
        "label": "PLE",
        "template": "leagues/partials/football_directory_ple.html",
    },
    FootballHomepageSection.Section.ZONAL: {
        "label": "Zonal Leagues",
        "template": "leagues/partials/football_directory_zonal.html",
    },
}
LEGACY_FOOTBALL_DIRECTORY_TABS = {"psl": FootballHomepageSection.Section.PLE}


def _football_homepage_sections():
    """Return configured sections once, with safe defaults for incomplete data."""
    try:
        saved_sections = {
            section.section: section
            for section in FootballHomepageSection.objects.all()
        }
    except (OperationalError, ProgrammingError):
        # A newly deployed application can serve the established directory
        # safely during the short interval before its new migration is run.
        saved_sections = {}
    configured = []
    for section in FootballHomepageSection.Section:
        saved = saved_sections.get(section)
        if saved:
            configured.append((saved.position, section, saved))

    # A defensive fallback keeps the public homepage usable if a deployment is
    # interrupted before the seed migration completes or a row was removed
    # outside Django admin. Missing sections retain their original order.
    configured.sort(key=lambda item: (item[0], item[1]))
    present = {section for _, section, _ in configured}
    configured.extend(
        (index, section, None)
        for index, section in enumerate(FootballHomepageSection.Section, start=1)
        if section not in present
    )
    configured.sort(key=lambda item: (item[0], item[1]))

    return [
        {
            "key": section,
            "label": FOOTBALL_DIRECTORY_SECTION_DETAILS[section]["label"],
            "template": FOOTBALL_DIRECTORY_SECTION_DETAILS[section]["template"],
        }
        for _, section, _ in configured
    ]


def _season_selector_context(request, competition):
    """Build links to sibling seasons on the current public league tab.

    Football competitions do not have a reliable start-date field.  Descending
    primary key is therefore the conservative ordering fallback: a season
    created later appears before its archived predecessor without parsing the
    free-form season label.
    """

    if not competition.series_id:
        return {"season_options": []}

    route_name = getattr(getattr(request, "resolver_match", None), "url_name", None)
    if route_name not in PUBLIC_SEASON_ROUTE_NAMES:
        route_name = "league_detail"

    competitions = list(
        Competition.objects.filter(series_id=competition.series_id).order_by("-id")
    )
    if len(competitions) < 2:
        return {"season_options": []}

    return {
        "season_options": [
            {
                "competition": sibling,
                "url": reverse(route_name, kwargs={"slug": sibling.slug}),
                "is_current": sibling.pk == competition.pk,
            }
            for sibling in competitions
        ]
    }


def _render_league_detail(request, competition, context):
    context.update(_season_selector_context(request, competition))
    context["back_url"] = _league_back_url(competition)
    return render(request, "leagues/league_detail.html", context)


def _league_back_url(competition):
    """Return the public directory context for a league detail page."""
    series = getattr(competition, "series", None) if competition.series_id else None
    if (
        series
        and series.league_type == CompetitionSeries.LeagueType.REGIONAL
        and series.region_id
    ):
        return reverse("region_detail", args=[series.region.slug])
    if series and series.league_type == CompetitionSeries.LeagueType.ZONAL:
        return f"{reverse('league_list')}?tab=zonal"
    if series and series.league_type == CompetitionSeries.LeagueType.EWFA:
        return f"{reverse('league_list')}?tab=ewfa"
    if series and series.league_type == CompetitionSeries.LeagueType.PLE:
        return f"{reverse('league_list')}?tab=ple"
    return reverse("league_list")


def _table_fixture_groups(fixtures):
    """Keep null-gameweek legacy fixtures visible after generated rounds."""
    grouped = []
    by_gameweek = {}
    unassigned = []
    for fixture in fixtures:
        if fixture.gameweek is None:
            unassigned.append(fixture)
        else:
            by_gameweek.setdefault(fixture.gameweek, []).append(fixture)
    for gameweek in sorted(by_gameweek):
        grouped.append({"label": f"Gameweek {gameweek}", "matches": by_gameweek[gameweek]})
    if unassigned:
        grouped.append({"label": "Unassigned Fixtures", "matches": unassigned})
    return grouped


def _table_result_groups(results):
    """Group finished table-stage fixtures without losing legacy data."""
    grouped = []
    by_gameweek = {}
    unassigned = []
    for result in results:
        if result.gameweek is None:
            unassigned.append(result)
        else:
            by_gameweek.setdefault(result.gameweek, []).append(result)
    for gameweek in sorted(by_gameweek, reverse=True):
        grouped.append({"label": f"Gameweek {gameweek}", "matches": by_gameweek[gameweek]})
    if unassigned:
        grouped.append({"label": "Other Results", "matches": unassigned})
    return grouped


def _table_fixture_ordering():
    return (F("gameweek").asc(nulls_last=True), F("match_date").asc(nulls_last=True), "id")


def _table_result_ordering():
    return (F("gameweek").desc(nulls_last=True), F("match_date").desc(nulls_last=True), "-id")


# ===== CHANGED =====
def _connected_bracket_display(competition):
    """Return an explicit tie/slot view model for the public bracket tab."""
    ties = list(
        FootballKnockoutTie.objects.filter(
            stage__competition=competition,
            position__isnull=False,
        ).select_related(
            "stage",
            "winner",
            "winner_destination",
            "winner_destination__stage",
        ).prefetch_related(
            Prefetch(
                "bracket_slots",
                queryset=FootballKnockoutTieSlot.objects.select_related("team").order_by(
                    "slot"
                ),
            ),
            Prefetch(
                "matches",
                queryset=Match.objects.select_related("home_team", "away_team").order_by(
                    "leg",
                    "id",
                ),
            ),
        ).order_by("stage__order", "position", "id")
    )
    if not ties:
        return [], None

    rounds = []
    for tie in ties:
        if not rounds or rounds[-1]["stage"].id != tie.stage_id:
            rounds.append({"stage": tie.stage, "ties": []})
        slots = {slot.slot: slot for slot in tie.bracket_slots.all()}
        matches = list(tie.matches.all())
        aggregate = aggregate_display(matches[-1]) if matches else None
        rounds[-1]["ties"].append({
            "tie": tie,
            "home_slot": slots.get(FootballKnockoutTieSlot.Slot.HOME),
            "away_slot": slots.get(FootballKnockoutTieSlot.Slot.AWAY),
            "matches": matches,
            "aggregate": aggregate,
        })

    terminal_ties = [tie for tie in ties if not tie.winner_destination_id]
    champion = terminal_ties[0].winner if len(terminal_ties) == 1 else None
    return rounds, champion


def _gameweek_group(matches, *, latest=False):
    """Select one numeric Gameweek, falling back to unassigned matches only."""

    numeric_gameweeks = {match.gameweek for match in matches if match.gameweek is not None}
    if numeric_gameweeks:
        gameweek = max(numeric_gameweeks) if latest else min(numeric_gameweeks)
        return [match for match in matches if match.gameweek == gameweek], gameweek
    return [match for match in matches if match.gameweek is None], None


def _card_label(label, gameweeks):
    """Keep gameweek context without pretending a multi-GW day is one round."""

    numbered = sorted({gameweek for gameweek in gameweeks if gameweek is not None})
    if not numbered:
        return label
    if len(numbered) == 1:
        return f"{label} · GW{numbered[0]}"
    return f"{label} · " + " & ".join(f"GW{gameweek}" for gameweek in numbered)


def _match_local_date(match):
    return timezone.localdate(match.match_date) if match.match_date else None


def _relevant_gameweek_group(matches):
    """Choose the newest numeric Gameweek, with null Gameweeks as a fallback."""

    return _gameweek_group(matches, latest=True)


def _latest_result_group(results):
    """Return the Gameweek containing the latest dated result.

    Results are selected by when they happened, rather than by their structural
    Gameweek number.  This matters when a postponed older Gameweek finishes
    after a newer one.
    """

    dated = [match for match in results if match.match_date is not None]
    if not dated:
        return _relevant_gameweek_group(results)

    latest_date = max(match.match_date for match in dated)
    latest_matches = [match for match in dated if match.match_date == latest_date]
    _, gameweek = _relevant_gameweek_group(latest_matches)
    return [match for match in results if match.gameweek == gameweek], gameweek


def _ordered_context_matches(matches, *, newest_first=False):
    """Order a compact-card context deterministically and without date leakage."""

    result_statuses = {"FINISHED", "ABANDONED"}
    if newest_first:
        earliest = datetime.min.replace(tzinfo=timezone.get_current_timezone())
        return sorted(
            matches,
            key=lambda match: (match.match_date or earliest, match.id),
            reverse=True,
        )

    latest = datetime.max.replace(tzinfo=timezone.get_current_timezone())

    def relevance(match):
        if match.status == "LIVE":
            bucket = 0
        elif match.status == "UPCOMING":
            bucket = 1
        elif match.status in result_statuses:
            bucket = 2
        else:
            bucket = 3
        return (bucket, match.match_date or latest, match.id)

    return sorted(matches, key=relevance)


def _public_league_cards(competitions):
    """Build date-first public cards from prefetched matches.

    A current local matchday is an all-or-nothing context: every eligible
    match dated today is displayed, regardless of Gameweek or status. That
    prevents future fixtures from displacing a result as soon as it is marked
    finished. Outside today, cards retain a selected Gameweek as useful
    context while never mixing in unscheduled or past-dated rows.
    """

    league_data = []
    result_statuses = {"FINISHED", "ABANDONED"}
    today = timezone.localdate()

    for league in competitions:
        matches = list(league.public_card_matches)
        match_dates = {match.id: _match_local_date(match) for match in matches}
        today_matches = [
            match for match in matches if match_dates[match.id] == today
        ]
        future_dated = [
            match for match in matches
            if (
                match.status == "UPCOMING"
                and match_dates[match.id] is not None
                and match_dates[match.id] > today
            )
        ]
        results = [
            match for match in matches
            if (
                match.status in result_statuses
                and (match_dates[match.id] is None or match_dates[match.id] <= today)
            )
        ]
        tba_upcoming = [
            match for match in matches
            if match.status == "UPCOMING" and match_dates[match.id] is None
        ]

        if today_matches:
            statuses = {match.status for match in today_matches}
            if statuses <= {"UPCOMING"}:
                label = "Today's Fixtures"
            elif statuses <= result_statuses:
                label = "Today's Results"
            elif "LIVE" in statuses and not statuses.intersection(result_statuses):
                label = "Live Matches"
            else:
                label = "Today's Fixtures & Results"
            league_data.append({
                "league": league,
                "display_matches": _ordered_context_matches(today_matches),
                "display_label": _card_label(
                    label,
                    [match.gameweek for match in today_matches],
                ),
            })
            continue
        elif future_dated:
            nearest_date = min(match.match_date for match in future_dated)
            nearest_matches = [
                match for match in future_dated if match.match_date == nearest_date
            ]
            _, gameweek = _relevant_gameweek_group(nearest_matches)
            label = "Upcoming Fixtures"
            context = [
                match for match in future_dated if match.gameweek == gameweek
            ]
            display_matches = _ordered_context_matches(context)[:PUBLIC_CARD_CONTEXT_LIMIT]
        elif results:
            results_group, gameweek = _latest_result_group(results)
            display_matches = _ordered_context_matches(
                [
                    match for match in results_group
                    if match_dates[match.id] is None or match_dates[match.id] <= today
                ],
                newest_first=True,
            )[:PUBLIC_CARD_CONTEXT_LIMIT]
            league_data.append({
                "league": league,
                "display_matches": display_matches,
                "display_label": _card_label("Latest Results", [gameweek]),
            })
            continue
        elif tba_upcoming:
            _, gameweek = _gameweek_group(tba_upcoming)
            label = "Fixtures To Be Scheduled"
            context = [match for match in tba_upcoming if match.gameweek == gameweek]
            display_matches = _ordered_context_matches(context)[:PUBLIC_CARD_CONTEXT_LIMIT]
        else:
            gameweek = None
            label = "Upcoming Fixtures"
            display_matches = []

        league_data.append({
            "league": league,
            "display_matches": display_matches,
            "display_label": _card_label(label, [gameweek]),
        })
    return league_data


def _public_competitions(queryset):
    return queryset.select_related("series", "series__region").prefetch_related(
        Prefetch(
            "matches",
            queryset=Match.objects.filter(
                status__in=["UPCOMING", "LIVE", "FINISHED", "ABANDONED"]
            ).select_related("home_team", "away_team"),
            to_attr="public_card_matches",
        )
    )


def _attach_region_previews(regions, *, limit=REGION_PREVIEW_LIMIT):
    """Attach compact, already-prefetched current-league previews to regions."""
    regions = list(regions)
    for region in regions:
        competitions = [
            competition
            for series in region.public_region_series
            for competition in series.public_region_competitions
        ]
        competitions.sort(key=lambda competition: (
            competition.display_order, competition.name, competition.id
        ))
        region.preview_leagues = competitions[:limit]
        region.extra_league_count = max(0, region.public_league_count - len(region.preview_leagues))
    return regions


def league_list(request):
    # Keep historical seasons directly reachable by their normal URLs, while
    # avoiding duplicate archived cards in current competition discovery.
    regions = Region.objects.filter(
        is_active=True,
        league_series__league_type=CompetitionSeries.LeagueType.REGIONAL,
        league_series__competitions__is_archived=False,
    ).annotate(
        public_league_count=Count(
            "league_series__competitions",
            filter=Q(
                league_series__league_type=CompetitionSeries.LeagueType.REGIONAL,
                league_series__competitions__is_archived=False,
            ),
            distinct=True,
        )
    ).order_by("display_order", "name", "id").prefetch_related(
        Prefetch(
            "league_series",
            queryset=CompetitionSeries.objects.filter(
                league_type=CompetitionSeries.LeagueType.REGIONAL,
            ).prefetch_related(
                Prefetch(
                    "competitions",
                    queryset=Competition.objects.filter(is_archived=False).select_related(
                        "series"
                    ).order_by("display_order", "name", "id")[:REGION_PREVIEW_LIMIT],
                    to_attr="public_region_competitions",
                )
            ),
            to_attr="public_region_series",
        )
    )
    homepage_banners = HomepageBanner.objects.filter(
            is_active=True
        ).order_by("order", "-created_at")

    zonal_league_data = _public_league_cards(_public_competitions(
        Competition.objects.filter(
            series__league_type=CompetitionSeries.LeagueType.ZONAL,
            is_archived=False,
        ).order_by("display_order", "name", "id")
    ))
    ewfa_league_data = _public_league_cards(_public_competitions(
        Competition.objects.filter(
            series__league_type=CompetitionSeries.LeagueType.EWFA,
            is_archived=False,
        ).order_by("display_order", "name", "id")
    ))
    ple_league_data = _public_league_cards(_public_competitions(
        Competition.objects.filter(
            series__league_type=CompetitionSeries.LeagueType.PLE,
            is_archived=False,
        ).order_by("display_order", "name", "id")
    ))
    organisations = FootballOrganisation.objects.in_bulk(field_name="code")
    directory_sections = _football_homepage_sections()
    requested_tab = request.GET.get("tab")
    requested_tab = LEGACY_FOOTBALL_DIRECTORY_TABS.get(requested_tab, requested_tab)
    configured_tab_keys = {section["key"] for section in directory_sections}
    active_directory_tab = (
        requested_tab
        if requested_tab in configured_tab_keys
        else directory_sections[0]["key"]
    )
    return render(request, "leagues/league_list.html", {
        "regions": _attach_region_previews(regions),
        "ewfa_league_data": ewfa_league_data,
        "ple_league_data": ple_league_data,
        "zonal_league_data": zonal_league_data,
        "ewfa_organisation": organisations.get(FootballOrganisation.Code.EWFA),
        "ple_organisation": organisations.get(FootballOrganisation.Code.PLE),
        # Compatibility for existing public integrations and season tests.
        "league_data": zonal_league_data,
        "football_directory_sections": directory_sections,
        "homepage_banners": homepage_banners,
        "active_directory_tab": active_directory_tab,
        "active_sport": "football",
    })


def region_detail(request, slug):
    region = get_object_or_404(Region, slug=slug, is_active=True)
    competitions = _public_competitions(
        Competition.objects.filter(
            series__region=region,
            series__league_type=CompetitionSeries.LeagueType.REGIONAL,
            is_archived=False,
        ).order_by(
            "display_order", "name", "id"
        )
    )
    return render(request, "leagues/region_detail.html", {
        "region": region,
        "league_data": _public_league_cards(competitions),
        "active_sport": "football",
    })

def league_detail(request, slug):
    competition = get_object_or_404(
        Competition.objects.select_related("series", "series__region"), slug=slug
    )
    all_fixtures = competition.matches.filter(
        status__in=["LIVE", "UPCOMING"]
    ).select_related(
        "home_team", "away_team", "stage", "group"
    ).order_by(
        *_table_fixture_ordering()
        if competition.format_type == "TABLE"
        else (
            Case(
                When(status="LIVE", then=0),
                default=1,
                output_field=IntegerField(),
            ),
            "match_date",
        )
    )

    all_results = competition.matches.filter(
        status__in=["FINISHED", "ABANDONED"]
    ).select_related(
        "home_team", "away_team", "stage", "group"
    ).order_by(*_table_result_ordering() if competition.format_type == "TABLE" else ("-match_date",))

    context = {
        "league": competition,
        "active_tab": "overview",

        # overview only
        "overview_fixtures": all_fixtures[:3],
        "overview_results": all_results[:3],

        # full tabs
        "fixtures": all_fixtures,
        "results": all_results,
    }
    if competition.format_type == "TABLE":
        context["fixture_groups"] = _table_fixture_groups(all_fixtures)
        context["result_groups"] = _table_result_groups(all_results)
    bracket_round_data, bracket_champion = _connected_bracket_display(competition)
    context["has_connected_bracket"] = bool(bracket_round_data)
    context["bracket_round_data"] = bracket_round_data
    context["bracket_champion"] = bracket_champion
    # ==================================
    # TABLE ONLY COMPETITION
    # ==================================
    if competition.format_type == "TABLE":

        standings = rank_standings(
            competition,
            Standing.objects.filter(
                competition=competition,
                group__isnull=True,
            ).select_related("team"),
        )

        context["standings"] = attach_standings_form(
            standings, completed_table_matches(competition)
        )

    # ==================================
    # GROUP + KNOCKOUT COMPETITION
    # ==================================
    elif competition.format_type == "GROUP_KNOCKOUT":

        group_stages = competition.stages.filter(stage_type="GROUP").order_by("order")

        grouped_tables = []

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

                grouped_tables.append({
                    "stage": stage,
                    "group": group,
                    "standings": standings
                })

        context["grouped_tables"] = grouped_tables

        knockout_stages = competition.stages.filter(
            stage_type="KNOCKOUT"
        ).order_by("order")

        knockout_stage_data = []

        for stage in knockout_stages:
            stage_round_matches = (
                stage.matches
                .select_related(
                    "home_team",
                    "away_team",
                    "stage",
                )
                .order_by("match_date", "id")
            )

            knockout_stage_data.append({
                "stage": stage,
                "matches": stage_round_matches,
            })

        context["knockout_stage_data"] = knockout_stage_data
    # ==================================
    # PURE KNOCKOUT COMPETITION
    # ==================================
    elif competition.format_type == "KNOCKOUT":

        knockout_stages = (
            competition.stages
            .filter(stage_type="KNOCKOUT")
            .order_by("order")
        )

        knockout_stage_data = []

        for stage in knockout_stages:
            stage_round_matches = (
                stage.matches
                .select_related(
                    "home_team",
                    "away_team",
                    "stage",
                )
                .order_by("match_date", "id")
            )

            knockout_stage_data.append({
                "stage": stage,
                "matches": stage_round_matches,
            })

        context["knockout_stage_data"] = knockout_stage_data
    top_scorers = (
        Player.objects
        .filter(team__competition=competition)
        .select_related("team")
        .annotate(
            competition_recorded_goals=Count(
                "goal",
                filter=(
                    ~Q(goal__match__status="ABANDONED")
                    & Q(
                        goal__match__competition=competition,
                        goal__match__abandonment_resolution__in=["", "RESUMED", "CONFIRMED"],
                        goal__canceled=False,
                        goal__goal_type__in=[
                            Goal.GoalType.NORMAL,
                            Goal.GoalType.PENALTY,
                        ],
                    )
                )
            )
        )
        .annotate(
            competition_total_goals=F("starting_goals") + Coalesce(
                F("competition_recorded_goals"),
                Value(0)
            )
        )
        .filter(competition_total_goals__gt=0)
        .order_by("-competition_total_goals", "-competition_recorded_goals", "name")
    )

    context["top_scorers"] = top_scorers

    return _render_league_detail(request, competition, context)


def league_fixtures(request, slug):
    competition = get_object_or_404(Competition, slug=slug)

    fixtures = competition.matches.filter(
        status__in=["LIVE", "UPCOMING"]
    ).select_related("home_team", "away_team", "stage").order_by(
        *(_table_fixture_ordering() if competition.format_type == "TABLE" else ("match_date",))
    )

    return _render_league_detail(request, competition, {
        'league': competition,
        'fixtures': fixtures,
        'active_tab': 'fixtures',
        'fixture_groups': _table_fixture_groups(fixtures) if competition.format_type == "TABLE" else [],
    })


def league_results(request, slug):
    competition = get_object_or_404(Competition, slug=slug)

    results = competition.matches.filter(
        status__in=["FINISHED", "ABANDONED"]
    ).select_related("home_team", "away_team", "stage", "group").order_by(
        *(_table_result_ordering() if competition.format_type == "TABLE" else ("-match_date",))
    )

    return _render_league_detail(request, competition, {
        'league': competition,
        'results': results,
        'active_tab': 'results',
        'result_groups': _table_result_groups(results) if competition.format_type == "TABLE" else [],
    })


def league_standings(request, slug):
    competition = get_object_or_404(Competition, slug=slug)

    context = {
        "league": competition,
        "active_tab": "standings"
    }

    # ==============================
    # TABLE FORMAT
    # ==============================
    if competition.format_type == "TABLE":

        standings = rank_standings(
            competition,
            Standing.objects.filter(
                competition=competition,
                group__isnull=True,
            ).select_related("team"),
        )

        context["standings"] = attach_standings_form(
            standings, completed_table_matches(competition)
        )

    # ==============================
    # GROUP + KNOCKOUT FORMAT
    # ==============================
    elif competition.format_type == "GROUP_KNOCKOUT":

        group_stages = competition.stages.filter(
            stage_type="GROUP"
        ).order_by("order")

        grouped_tables = []

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

                grouped_tables.append({
                    "stage": stage,
                    "group": group,
                    "standings": standings
                })

        context["grouped_tables"] = grouped_tables

        knockout_stage = competition.stages.filter(
            stage_type="KNOCKOUT"
        ).first()

        if knockout_stage:
            context["knockout_matches"] = knockout_stage.matches.order_by("match_date")

    return _render_league_detail(request, competition, context)


def team_profile(request, slug, team_id):
    """Public, season-scoped profile for one football Team record."""
    competition = get_object_or_404(Competition, slug=slug)
    team = get_object_or_404(Team, pk=team_id, competition=competition)
    if competition.format_type != "TABLE":
        raise Http404("Team profiles are available for Table Only football leagues.")

    standings = rank_standings(
        competition,
        Standing.objects.filter(
            competition=competition, group__isnull=True
        ).select_related("team"),
    )
    position = next(
        (index for index, row in enumerate(standings, start=1) if row.team_id == team.id),
        None,
    )
    standing = next((row for row in standings if row.team_id == team.id), None)
    totals = {
        "played": standing.total_played if standing else 0,
        "won": standing.total_won if standing else 0,
        "drawn": standing.total_drawn if standing else 0,
        "lost": standing.total_lost if standing else 0,
        "goals_for": standing.total_goals_for if standing else 0,
        "goals_against": standing.total_goals_against if standing else 0,
        "points": standing.total_points if standing else 0,
    }
    totals["goal_difference"] = totals["goals_for"] - totals["goals_against"]
    totals["win_percentage"] = (
        totals["won"] * 100 / totals["played"] if totals["played"] else 0
    )

    completed = completed_table_matches(competition)
    team_completed = [
        match for match in completed
        if team.id in {match.home_team_id, match.away_team_id}
    ]
    home_results = [match for match in team_completed if match.home_team_id == team.id]
    away_results = [match for match in team_completed if match.away_team_id == team.id]
    upcoming = (
        Match.objects.filter(
            competition=competition,
            stage__stage_type="LEAGUE",
            group__isnull=True,
            status__in=["UPCOMING", "LIVE"],
        )
        .filter(Q(home_team=team) | Q(away_team=team))
        .select_related("home_team", "away_team")
        .order_by(F("match_date").asc(nulls_last=True), "id")
        .first()
    )
    squad = (
        Player.objects.filter(team=team)
        .annotate(
            recorded_goals_count=Count(
                "goal",
                filter=(
                    ~Q(goal__match__status="ABANDONED")
                    & Q(
                        goal__match__competition=competition,
                        goal__match__abandonment_resolution__in=["", "RESUMED", "CONFIRMED"],
                        goal__canceled=False,
                        goal__goal_type__in=[Goal.GoalType.NORMAL, Goal.GoalType.PENALTY],
                    )
                ),
            )
        )
        .order_by("jersey_number", "name")
    )
    context = {
        "league": competition,
        "team": team,
        "team_position": position,
        "team_totals": totals,
        "recent_form": recent_form_by_team(team_completed, [team.id]).get(team.id, []),
        "home_record": record(team_completed, team_id=team.id, home=True),
        "away_record": record(team_completed, team_id=team.id, home=False),
        "home_results": home_results,
        "away_results": away_results,
        "last_result": team_completed[-1] if team_completed else None,
        "next_fixture": upcoming,
        "squad": squad,
    }
    context.update(_season_selector_context(request, competition))
    return render(request, "leagues/team_profile.html", context)


def league_stats(request, slug):
    competition = get_object_or_404(Competition, slug=slug)

    top_scorers = (
        Player.objects
        .filter(team__competition=competition)
        .select_related("team")
        .annotate(
            competition_recorded_goals=Count(
                "goal",
                filter=(
                    ~Q(goal__match__status="ABANDONED")
                    & Q(
                        goal__match__competition=competition,
                        goal__match__abandonment_resolution__in=["", "RESUMED", "CONFIRMED"],
                        goal__canceled=False,
                        goal__goal_type__in=[
                            Goal.GoalType.NORMAL,
                            Goal.GoalType.PENALTY,
                        ],
                    )
                )
            )
        )
        .annotate(
            competition_total_goals=F("starting_goals") + Coalesce(F("competition_recorded_goals"), Value(0))
        )
        .filter(competition_total_goals__gt=0)
        .order_by("-competition_total_goals", "-competition_recorded_goals", "name")
    )

    return _render_league_detail(request, competition, {
        "league": competition,
        "top_scorers": top_scorers,
        "active_tab": "stats"
    })
