from functools import lru_cache

from django.contrib.staticfiles import finders
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, When
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.templatetags.static import static
from django.views.decorators.http import require_GET

from leagues.models import HomepageBanner

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolCompetitionTieBreaker,
    PoolGroup,
    PoolKnockoutBracket,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    PoolTeam,
    PoolTeamMatchLineup,
    PoolTeamMatchAuditEvent,
    PoolTeamMatchScheduledRack,
    PoolTeamMatchSubstitution,
    PoolTeamRoster,
)
from .homepage import homepage_match_prefetches, homepage_match_rows
from .permissions import (
    user_can_manage_team_lineup,
    user_can_manage_competition,
    user_can_score_pool_match,
    user_manages_pool_competition,
)
from .queries import (
    competition_statistics,
    competition_player_rack_statistics,
    competition_team_rack_statistics,
    match_rack_summary,
    order_public_competition_stat_rows,
    player_rack_win_statistics,
    player_money_match_statistics,
    player_statistics,
    published_competitions,
    RACK_WIN_OUTCOME_COLUMNS,
    RACK_WIN_STATISTIC_KEYS,
    team_entry_rack_statistics,
)
from .standings import (
    configured_pool_margin_point_rules,
    configured_pool_tie_breakers,
    maximum_meaningful_pool_margin,
    standings_for_stage,
    standings_for_stage_groups,
)
from .team_operations import is_team_fixture, team_match_schedule_state


PUBLIC_COMPETITION_VISIBILITIES = (
    PoolCompetition.Visibility.PUBLISHED,
)


PUBLIC_COMPETITION_STATUS_FILTERS = {
    "active": PoolCompetition.OperationalState.ACTIVE,
    "upcoming": PoolCompetition.OperationalState.UPCOMING,
    "finished": PoolCompetition.OperationalState.COMPLETED,
}
PUBLIC_COMPETITION_STATUS_VALUES = frozenset(
    (*PUBLIC_COMPETITION_STATUS_FILTERS, "all")
)
PUBLIC_COMPETITION_CATEGORY_VALUES = frozenset(
    {"leagues", "tournaments", "money_matches"}
)
PUBLIC_COMPETITION_MODE_VALUES = frozenset({"singles", "teams"})
ENTRANT_PREVIEW_LIMIT = 8
POOL_ASSOCIATION_LOGO_PATHS = (
    "pool/images/cue-sports-association-logo.svg",
    "pool/images/cue-sports-association-logo.png",
)


@lru_cache
def _pool_association_logo_url():
    """Use the approved association asset when it has been supplied locally."""

    for asset_path in POOL_ASSOCIATION_LOGO_PATHS:
        if finders.find(asset_path):
            return static(asset_path)
    return ""


def _manager_preview_requested(request):
    return request.GET.get("preview") in {"1", "true", "True"}


def _public_or_preview_competitions(request):
    """Return public competitions, plus private competitions for an explicit manager preview."""
    public_competitions = published_competitions()

    if not _manager_preview_requested(request):
        return public_competitions

    user = request.user
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return public_competitions

    return PoolCompetition.objects.visible_to(user)


def _competition_is_public_or_manageable(request, competition):
    return (
        competition.visibility in PUBLIC_COMPETITION_VISIBILITIES
        or user_manages_pool_competition(request.user, competition)
    )


def _match_queryset():
    return PoolMatch.objects.select_related(
        "competition",
        "stage",
        "group",
        "knockout_round",
        "home_entry__player",
        "home_entry__team",
        "away_entry__player",
        "away_entry__team",
        "home_entry_life",
        "away_entry_life",
        "winner__player",
        "winner__team",
        "winner_entry_life",
    )


def _competition_standings_sections(competition):
    """Build public, exact-scope tables without storing editable totals."""
    stages = list(
        competition.stages.filter(
            stage_type__in=(
                PoolCompetitionStage.StageType.LEAGUE,
                PoolCompetitionStage.StageType.GROUP,
            )
        ).order_by("order", "id")
    )
    sections = []
    for stage in stages:
        if stage.stage_type == PoolCompetitionStage.StageType.GROUP:
            for group, rows in standings_for_stage_groups(stage):
                sections.append({"stage": stage, "group": group, "rows": rows})
        else:
            sections.append(
                {"stage": stage, "group": None, "rows": standings_for_stage(stage)}
            )
    return sections


def _competition_bracket_display(competition):
    """Presentation-only wrapper around persistent pool bracket topology."""
    brackets = (
        PoolKnockoutBracket.objects.filter(competition=competition)
        .select_related("stage")
        .prefetch_related(
            "ties__match",
            "ties__resolved_winner__player",
            "ties__resolved_winner__team",
            "ties__resolved_winner_life__competition_entry__player",
            "ties__slots__entry__player",
            "ties__slots__entry__team",
            "ties__slots__entry_life__competition_entry__player",
            "ties__slots__source_entry__player",
            "ties__slots__source_entry__team",
            "ties__slots__source_entry_life__competition_entry__player",
            "ties__slots__source_group",
            "ties__slots__source_tie",
        )
        .order_by("stage__order", "id")
    )
    def slot_label(slot):
        if slot is None:
            return "TBD"
        life = getattr(slot, "entry_life", None)
        if life is not None:
            return life.display_name
        if slot.entry_id:
            return slot.entry.display_name
        return slot.source_label or "TBD"

    displays = []
    for bracket in brackets:
        ties = list(bracket.ties.all())
        rounds = []
        large_draw = bracket.size >= 64
        first_round_half_tie_count = bracket.size // 4
        for tie in ties:
            if tie.is_third_place:
                continue
            if not rounds or rounds[-1]["round_number"] != tie.round_number:
                rounds.append(
                    {
                        "round_number": tie.round_number,
                        "name": tie.round_name,
                        "ties": [],
                    }
                )
            slots = {slot.slot: slot for slot in tie.slots.all()}
            home_slot = slots.get("HOME")
            away_slot = slots.get("AWAY")
            draw_half = ""
            half_heading = ""
            if large_draw:
                # Keep each branch of a 64+ draw addressable all the way to
                # its semi-final, rather than labelling only round one.  The
                # template's responsive half selector can then hide the
                # unrelated branch while retaining the shared final.
                tie_count = bracket.size // (2**tie.round_number)
                if tie_count > 1:
                    ties_per_half = tie_count // 2
                    draw_half = (
                        "top" if tie.position <= ties_per_half else "bottom"
                    )
                    if tie.round_number == 1 and tie.position == 1:
                        half_heading = "Top half"
                    elif (
                        tie.round_number == 1
                        and tie.position == first_round_half_tie_count + 1
                    ):
                        half_heading = "Bottom half"
                else:
                    draw_half = "final"
            rounds[-1]["ties"].append(
                {
                    "tie": tie,
                    "home_slot": home_slot,
                    "away_slot": away_slot,
                    "home_label": slot_label(home_slot),
                    "away_label": slot_label(away_slot),
                    "same_entry_final": (
                        tie.resolution_method == "SAME_ENTRY_FINAL"
                    ),
                    "champion_label": (
                        tie.resolved_winner.display_name
                        if tie.resolution_method == "SAME_ENTRY_FINAL"
                        and tie.resolved_winner_id
                        else ""
                    ),
                    "draw_half": draw_half,
                    "half_heading": half_heading,
                }
            )
        third_place = next((tie for tie in ties if tie.is_third_place), None)
        third_place_slots = (
            {slot.slot: slot for slot in third_place.slots.all()}
            if third_place is not None
            else {}
        )
        same_entry_third_place = bool(
            third_place
            and getattr(third_place, "resolution_method", "")
            == "SAME_ENTRY_THIRD"
        )
        third_place_label = (
            third_place.resolved_winner.display_name
            if same_entry_third_place and third_place.resolved_winner_id
            else ""
        )
        displays.append(
            {
                "bracket": bracket,
                "rounds": rounds,
                "third_place": third_place,
                "third_place_home_slot": third_place_slots.get("HOME"),
                "third_place_away_slot": third_place_slots.get("AWAY"),
                "third_place_home_label": slot_label(third_place_slots.get("HOME")),
                "third_place_away_label": slot_label(third_place_slots.get("AWAY")),
                "large_draw": large_draw,
                "first_round_half_tie_count": first_round_half_tie_count,
                # Newer bracket services may provide this directly.  The
                # public fallback derives it when the persisted tie exposes
                # the same resolution method, and otherwise leaves it false.
                "same_entry_third_place": same_entry_third_place,
                "third_place_label": third_place_label,
            }
        )
    return displays


def _competition_rules(competition):
    """Return presentation data from the central standings configuration.

    Knockout-only competitions have no table, so intentionally return no
    points summary instead of presenting their fallback values as rules that
    influence bracket play.
    """
    if not competition.has_league_or_group_stage:
        return None

    labels = dict(PoolCompetitionTieBreaker.Criterion.choices)
    criteria = configured_pool_tie_breakers(competition)
    is_margin_based = (
        competition.points_mode == PoolCompetition.PointsMode.MARGIN_BASED
    )
    try:
        margin_rules = configured_pool_margin_point_rules(
            competition,
            require_complete=is_margin_based,
        )
    except ValidationError:
        # A damaged imported configuration must not make an otherwise public
        # competition page fail.  The manager-facing central service remains
        # the only place that can repair it; public copy avoids claiming that
        # the fixed fallback applies to played matches in this state.
        margin_rules = ()
        margin_rules_valid = False
    else:
        margin_rules_valid = True
    return {
        "points_mode": competition.points_mode,
        "points_mode_label": competition.get_points_mode_display(),
        "is_margin_based": is_margin_based,
        "points_for_win": competition.points_for_win,
        "points_for_loss": competition.points_for_loss,
        "margin_rules": margin_rules,
        "margin_rules_valid": margin_rules_valid,
        # A legacy terminal row may have no persisted maximum.  It remains
        # read-only-compatible, but its public label must end at the actual
        # table target rather than imply a fictional open-ended margin.
        "maximum_meaningful_margin": maximum_meaningful_pool_margin(competition),
        "tie_breakers": [labels.get(criterion, criterion) for criterion in criteria],
    }


def _competition_entrant_data(competition, *, query="", region=""):
    """Build safe, exact-competition entrant presentation data.

    The public entrant list deliberately starts from real competition entries,
    rather than bracket slots or tournament lives.  That makes one player one
    visible entrant even when a singles knockout gives them two bracket lives.
    """

    query = (query or "").strip()[:100]
    region = (region or "").strip()[:100]
    base_entries = competition.entries.all()
    is_singles = (
        competition.participant_mode == PoolCompetition.ParticipantMode.SINGLES
    )
    data = {
        "entrant_count": base_entries.count(),
        "entrant_noun": "player" if is_singles else "team",
        "entrant_query": query,
        "entrant_region": region if is_singles else "",
        "entrant_regions": (),
        "tournament_life_count": None,
    }

    if is_singles:
        data["entrant_regions"] = tuple(
            base_entries.filter(player__region__gt="")
            .order_by("player__region")
            .values_list("player__region", flat=True)
            .distinct()
        )
        entries = base_entries.select_related("player").annotate(
            public_life_count=Count("lives", distinct=True),
        )
        if query:
            entries = entries.filter(
                Q(player__full_name__icontains=query)
                | Q(player__nickname__icontains=query)
            )
        if region:
            entries = entries.filter(player__region=region)
        if competition.allow_two_lives:
            life_total = base_entries.aggregate(total=Count("lives"))["total"] or 0
            # A second count earns space only when it conveys something that
            # the physical entrant count does not.  Life 1 is intentionally
            # invisible to fans; a Life 2 is what makes the distinction
            # useful on this registered-people view.
            data["tournament_life_count"] = (
                life_total if life_total > data["entrant_count"] else None
            )
        data["entrants"] = entries.order_by(
            "player__full_name",
            "player__id",
            "pk",
        )
    else:
        entries = base_entries.select_related("team").annotate(
            public_roster_count=Count(
                "roster_members",
                filter=Q(roster_members__is_active=True),
                distinct=True,
            ),
        )
        if query:
            entries = entries.filter(team__name__icontains=query)
        data["entrants"] = entries.order_by("team__name", "team__id", "pk")

    return data


def _public_home_filter_value(value, valid_values):
    """Return a recognised public discovery filter or an empty selection."""

    return value if value in valid_values else ""


def _competition_matches_public_home_filters(
    competition,
    *,
    category="",
    mode="",
    status="",
):
    """Apply the public home-card filters to preloaded presentation rows."""

    return (
        (not category or competition.public_filter_category == category)
        and (not mode or competition.public_filter_mode == mode)
        and (
            not status
            or status == "all"
            or competition.public_filter_status == status
        )
        and not (
            status == "finished"
            and not category
            and competition.is_money_match
        )
    )


def home(request):
    """Render public, filterable competition cards with bounded live-score rows."""

    category = _public_home_filter_value(
        request.GET.get("category"),
        PUBLIC_COMPETITION_CATEGORY_VALUES,
    )
    mode = _public_home_filter_value(
        request.GET.get("mode"),
        PUBLIC_COMPETITION_MODE_VALUES,
    )
    requested_status = _public_home_filter_value(
        request.GET.get("status"),
        PUBLIC_COMPETITION_STATUS_VALUES,
    )
    knockout_stage_exists = PoolCompetitionStage.objects.filter(
        competition_id=OuterRef("pk"),
        stage_type=PoolCompetitionStage.StageType.KNOCKOUT,
    )
    competitions = list(
        _public_or_preview_competitions(request)
        .filter(is_archived=False)
        .annotate(
            entrant_count=Count("entries", distinct=True),
            public_filter_has_knockout=Exists(knockout_stage_exists),
        )
        .prefetch_related(*homepage_match_prefetches())
        .distinct()
        .order_by("start_date", "name")
    )
    status_by_operational_state = {
        PoolCompetition.OperationalState.ACTIVE: "active",
        PoolCompetition.OperationalState.UPCOMING: "upcoming",
        PoolCompetition.OperationalState.COMPLETED: "finished",
    }
    for competition in competitions:
        # Match the old queryset semantics exactly: a competition is a
        # tournament when its persisted topology contains a knockout stage,
        # not merely when its format setting suggests one will be created.
        competition.public_filter_category = (
            "money_matches"
            if competition.is_money_match
            else "tournaments"
            if competition.public_filter_has_knockout
            else "leagues"
        )
        competition.public_filter_mode = competition.participant_mode.lower()
        competition.public_filter_status = status_by_operational_state[
            competition.operational_state
        ]
        competition.public_state_label = (
            "Finished"
            if competition.operational_state
            == PoolCompetition.OperationalState.COMPLETED
            else competition.get_operational_state_display()
        )
        competition.homepage_match_rows = homepage_match_rows(competition)
        competition.money_match_fixture = (
            competition.homepage_match_rows[0]["match"]
            if competition.is_money_match and competition.homepage_match_rows
            else None
        )

    category_and_mode_competitions = [
        competition
        for competition in competitions
        if _competition_matches_public_home_filters(
            competition,
            category=category,
            mode=mode,
        )
    ]
    # Preserve the established default after category/mode filtering: an
    # unavailable Active view falls back to Upcoming, even if only completed
    # competitions remain.
    current_status = requested_status or (
        "active"
        if any(
            competition.public_filter_status == "active"
            for competition in category_and_mode_competitions
        )
        else "upcoming"
    )
    visible_competitions = [
        competition
        for competition in category_and_mode_competitions
        if _competition_matches_public_home_filters(
            competition,
            category=category,
            status=current_status,
        )
    ]
    visible_competition_ids = {competition.pk for competition in visible_competitions}
    for competition in competitions:
        competition.public_filter_initially_visible = (
            competition.pk in visible_competition_ids
        )

    return render(
        request,
        "pool/home.html",
        {
            "active_sport": "pool",
            "pool_association_logo_url": _pool_association_logo_url(),
            "homepage_banners": HomepageBanner.objects.filter(is_active=True).order_by(
                "order", "-created_at"
            ),
            "competitions": competitions,
            "current_status": current_status,
            "home_filter_category": category,
            "home_filter_mode": mode,
            "home_has_requested_filters": any(
                request.GET.get(name) for name in ("category", "mode", "status")
            ),
            "visible_competition_count": len(visible_competitions),
        },
    )


def _public_competition_tab_state(competition):
    """Return the structurally available public tabs for one competition."""

    has_standings = PoolCompetitionStage.objects.filter(
        competition=competition,
        stage_type__in=(
            PoolCompetitionStage.StageType.LEAGUE,
            PoolCompetitionStage.StageType.GROUP,
        ),
    ).exists()
    has_knockout = PoolCompetitionStage.objects.filter(
        competition=competition,
        stage_type=PoolCompetitionStage.StageType.KNOCKOUT,
    ).exists()
    valid_tabs = {"overview", "entrants", "fixtures", "results", "stats"}
    if has_standings:
        valid_tabs.add("standings")
    if has_knockout:
        valid_tabs.add("bracket")
    return has_standings, has_knockout, valid_tabs


def _competition_fixture_rows(competition, *, limit=None):
    """Return public upcoming/live fixtures without loading result history."""

    fixtures = (
        _match_queryset()
        .filter(
            competition=competition,
            status__in=(
                PoolMatch.Status.UPCOMING,
                PoolMatch.Status.LIVE,
                PoolMatch.Status.POSTPONED,
            ),
        )
        .annotate(
            status_priority=Case(
                When(status=PoolMatch.Status.LIVE, then=0),
                When(status=PoolMatch.Status.UPCOMING, then=1),
                default=2,
                output_field=IntegerField(),
            )
        )
        .order_by("status_priority", "scheduled_at", "pk")
    )
    if limit is not None:
        fixtures = fixtures[:limit]
    return list(fixtures)


def _competition_result_rows(competition, *, limit=None):
    """Return completed public results without loading upcoming fixtures."""

    results = (
        _match_queryset()
        .filter(competition=competition, status=PoolMatch.Status.FINISHED)
        .order_by("-completed_at", "-scheduled_at", "-pk")
    )
    if limit is not None:
        results = results[:limit]
    return list(results)


def _gameweek_sections(matches, *, newest_first=False):
    """Group a complete public tab by persisted gameweek metadata.

    The fixture source is deliberately irrelevant here: manually entered and
    generated fixtures share the same scheduling metadata and therefore the
    same display group.
    """

    grouped = {}
    unassigned = []
    for pool_match in matches:
        if pool_match.gameweek is None:
            unassigned.append(pool_match)
        else:
            # A group is deliberately not part of this key.  Group-table
            # rounds with the same persisted gameweek form one public Game;
            # individual groups are nested beneath that Game below.
            key = (
                pool_match.stage_id,
                pool_match.fixture_round_term,
                pool_match.gameweek,
            )
            grouped.setdefault(key, []).append(pool_match)
    if not grouped:
        # Keep manually scheduled, knockout, and historic rows that do not
        # carry table-round metadata in the same explicit fallback section
        # used when they accompany numbered fixtures.
        return (
            [{"gameweek": None, "round_term": None, "matches": unassigned, "groups": []}]
            if unassigned
            else []
        )
    sections = [
        {
            "gameweek": key[2],
            "round_term": key[1],
            "matches": grouped[key],
            "groups": (
                [
                    {"group": group, "matches": group_matches}
                    for group, group_matches in _matches_by_group(grouped[key])
                ]
                if key[1] == "Game"
                else []
            ),
        }
        for key in sorted(grouped, key=lambda key: (key[2], key[0]), reverse=newest_first)
    ]
    if unassigned:
        sections.append(
            {"gameweek": None, "round_term": None, "matches": unassigned, "groups": []}
        )
    return sections


def _matches_by_group(matches):
    """Keep Group + Knockout group identity under each shared game number."""

    grouped = {}
    for pool_match in matches:
        grouped.setdefault(pool_match.group, []).append(pool_match)
    return sorted(
        grouped.items(),
        key=lambda item: (item[0].order if item[0] else 0, item[0].pk if item[0] else 0),
    )


def _competition_overview_entrant_data(competition):
    """Build the small entrant summary used by the initial Overview tab."""

    is_singles = (
        competition.participant_mode == PoolCompetition.ParticipantMode.SINGLES
    )
    base_entries = competition.entries.all()
    entrant_count = base_entries.count()
    data = {
        "entrant_count": entrant_count,
        "entrant_noun": "player" if is_singles else "team",
        "tournament_life_count": None,
        "entrant_preview": (),
    }
    if competition.allow_two_lives and is_singles:
        life_total = base_entries.aggregate(total=Count("lives"))["total"] or 0
        if life_total > entrant_count:
            data["tournament_life_count"] = life_total

    if competition.operational_state != PoolCompetition.OperationalState.UPCOMING:
        return data

    if is_singles:
        data["entrant_preview"] = list(
            base_entries.select_related("player")
            .annotate(public_life_count=Count("lives", distinct=True))
            .order_by("player__full_name", "player__id", "pk")[:ENTRANT_PREVIEW_LIMIT]
        )
    else:
        data["entrant_preview"] = list(
            base_entries.select_related("team")
            .annotate(
                public_roster_count=Count(
                    "roster_members",
                    filter=Q(roster_members__is_active=True),
                    distinct=True,
                )
            )
            .order_by("team__name", "team__id", "pk")[:ENTRANT_PREVIEW_LIMIT]
        )
    return data


def _competition_overview_context(competition):
    """Keep initial competition pages limited to their actual Overview data."""

    context = _competition_overview_entrant_data(competition)
    context.update(
        {
            "fixtures": _competition_fixture_rows(competition, limit=5),
            "results": _competition_result_rows(competition, limit=5),
            "competition_rules": _competition_rules(competition),
        }
    )
    return context


def _competition_entrants_context(request, competition):
    entrant_data = _competition_entrant_data(
        competition,
        query=request.GET.get("q", ""),
        region=request.GET.get("region", ""),
    )
    entrants = list(entrant_data["entrants"])
    return {
        "entrants": entrants,
        "entrant_count": entrant_data["entrant_count"],
        "entrant_filtered_count": len(entrants),
        "entrant_noun": entrant_data["entrant_noun"],
        "entrant_query": entrant_data["entrant_query"],
        "entrant_region": entrant_data["entrant_region"],
        "entrant_regions": entrant_data["entrant_regions"],
        "entrant_has_filters": bool(
            entrant_data["entrant_query"] or entrant_data["entrant_region"]
        ),
        "tournament_life_count": entrant_data["tournament_life_count"],
    }


def _competition_standings_context(competition):
    standings_sections = []
    standings_error = None
    try:
        standings_sections = _competition_standings_sections(competition)
    except ValidationError:
        # Keep the calculation service strict: a damaged active margin policy
        # must never silently fall back to fixed points. Public visitors still
        # receive a usable tab while the authorised organiser repairs it.
        standings_error = (
            "Standings are temporarily unavailable while the competition's "
            "points policy is repaired."
        )
    return {
        "standings_sections": standings_sections,
        "standings_error": standings_error,
        "competition_rules": _competition_rules(competition),
    }


def _competition_statistics_context(competition):
    stats = competition_statistics(competition)
    context = {
        "stats": stats,
        "team_rack_stats": [],
        "singles_rack_stats": [],
        "rack_outcome_columns": RACK_WIN_OUTCOME_COLUMNS,
    }
    if competition.participant_mode == PoolCompetition.ParticipantMode.TEAMS:
        context["team_rack_stats"] = competition_team_rack_statistics(competition)
        return context

    live_rack_stats = {
        row["player"].pk: row
        for row in competition_player_rack_statistics(
            competition,
            completed_only=False,
        )
    }
    for stat in stats:
        # ``participant_statistics`` is the completed-result source for both
        # detailed racks and manual aggregate summaries. Add only unfinished
        # PoolRack events so public statistics retain live scoring without
        # merging a completed manual score a second time.
        rack_win_stats = {
            key: stat[key]
            for key in RACK_WIN_STATISTIC_KEYS
        }
        live_row = live_rack_stats.get(stat["participant"].pk)
        if live_row:
            for key in RACK_WIN_STATISTIC_KEYS:
                rack_win_stats[key] += live_row[key]
        stat["rack_win_stats"] = rack_win_stats
    context["stats"] = order_public_competition_stat_rows(
        stats,
        statistics_key="rack_win_stats",
    )
    return context


def _competition_tab_context(request, competition, active_tab):
    """Calculate only the data needed by one public competition tab."""

    if active_tab == "overview":
        return _competition_overview_context(competition)
    if active_tab == "entrants":
        return _competition_entrants_context(request, competition)
    if active_tab == "fixtures":
        fixtures = _competition_fixture_rows(competition)
        return {
            "fixtures": fixtures,
            "fixture_gameweek_sections": _gameweek_sections(fixtures),
        }
    if active_tab == "results":
        results = _competition_result_rows(competition)
        return {
            "results": results,
            "result_gameweek_sections": _gameweek_sections(
                results, newest_first=True
            ),
        }
    if active_tab == "standings":
        return _competition_standings_context(competition)
    if active_tab == "bracket":
        return {"bracket_displays": _competition_bracket_display(competition)}
    if active_tab == "stats":
        return _competition_statistics_context(competition)
    raise Http404("Competition tab not found.")


def _competition_detail_context(
    request,
    competition,
    *,
    active_tab,
    has_standings,
    has_knockout,
):
    season_selector_competitions = []
    if competition.series_id:
        season_query = competition.series.competitions.all()
        # A new season begins as a draft.  Do not turn a public historical
        # selector into a route-discovery mechanism for private setup work.
        if not user_manages_pool_competition(request.user, competition):
            season_query = season_query.filter(
                visibility=PoolCompetition.Visibility.PUBLISHED
            )
        season_selector_competitions = list(
            season_query.order_by(
                "is_archived",
                "-start_date",
                "-created_at",
                "-pk",
            )
        )
    context = {
        "competition": competition,
        "competition_state_label": (
            "Finished"
            if competition.operational_state
            == PoolCompetition.OperationalState.COMPLETED
            else competition.get_operational_state_display()
        ),
        "active_tab": active_tab,
        "has_standings": has_standings,
        "has_knockout": has_knockout,
        # The tab navigation is rendered before an entrants fragment may have
        # been requested. Keep its filter values safely blank in that case.
        "entrant_query": "",
        "entrant_region": "",
        "season_selector_competitions": season_selector_competitions,
    }
    context.update(_competition_tab_context(request, competition, active_tab))
    return context


def competition_detail(request, slug):
    competition = get_object_or_404(PoolCompetition, slug=slug)
    if not _competition_is_public_or_manageable(request, competition):
        raise Http404("Competition not found.")

    has_standings, has_knockout, valid_tabs = _public_competition_tab_state(
        competition
    )
    active_tab = request.GET.get("tab", "overview")
    if active_tab not in valid_tabs:
        active_tab = "overview"

    context = _competition_detail_context(
        request,
        competition,
        active_tab=active_tab,
        has_standings=has_standings,
        has_knockout=has_knockout,
    )
    context["can_manage_competition"] = user_manages_pool_competition(
        request.user,
        competition,
    )
    return render(request, "pool/competition_detail.html", context)


@require_GET
def competition_tab_content(request, slug, tab):
    """Return one permission-checked public-tab fragment for page-lifetime JS caching."""

    competition = get_object_or_404(PoolCompetition, slug=slug)
    if not _competition_is_public_or_manageable(request, competition):
        raise Http404("Competition not found.")

    has_standings, has_knockout, valid_tabs = _public_competition_tab_state(
        competition
    )
    if tab not in valid_tabs:
        raise Http404("Competition tab not found.")

    return render(
        request,
        "pool/_competition_tab_content.html",
        _competition_detail_context(
            request,
            competition,
            active_tab=tab,
            has_standings=has_standings,
            has_knockout=has_knockout,
        ),
    )


def match_detail(request, pk):
    pool_match = get_object_or_404(_match_queryset(), pk=pk)
    # An exact-fixture scorer is deliberately allowed through this otherwise
    # public/manager visibility gate, but receives no broader competition
    # access.  The scorer permission itself is match-specific.
    can_score_match = user_can_score_pool_match(request.user, pool_match)
    if (
        not _competition_is_public_or_manageable(request, pool_match.competition)
        and not can_score_match
    ):
        raise Http404("Match not found.")

    racks = pool_match.racks.select_related(
        "winner__player",
        "winner__team",
        "winning_player",
        "breaker__player",
        "breaker__team",
    ).order_by("rack_number", "pk")
    is_team_match = is_team_fixture(pool_match)
    team_schedule_rows = []
    team_live_racks = []
    team_up_next = []
    team_lineup_cards = []
    can_open_live_scorer = (
        can_score_match and pool_match.is_live_scoring_available
    )
    can_manage_match = user_can_manage_competition(
        request.user,
        pool_match.competition,
    )
    has_match_controls = can_open_live_scorer or can_manage_match
    if is_team_match:
        schedule_state = team_match_schedule_state(pool_match)
        team_schedule_rows = [
            {
                "scheduled_rack": scheduled_rack,
                "home_player": getattr(
                    scheduled_rack,
                    "current_home_player",
                    scheduled_rack.home_player,
                ),
                "away_player": getattr(
                    scheduled_rack,
                    "current_away_player",
                    scheduled_rack.away_player,
                ),
                "rack": scheduled_rack.result,
            }
            for scheduled_rack in schedule_state["schedules"]
        ]
        team_live_racks = [
            row
            for row in team_schedule_rows
            if row["scheduled_rack"].status
            == row["scheduled_rack"].Status.LIVE
        ]
        team_up_next = [
            {
                "scheduled_rack": scheduled_rack,
                "home_player": getattr(
                    scheduled_rack,
                    "current_home_player",
                    None,
                ),
                "away_player": getattr(
                    scheduled_rack,
                    "current_away_player",
                    None,
                ),
            }
            for scheduled_rack in schedule_state["up_next"]
        ]

        lineups = {
            lineup.team_entry_id: lineup
            for lineup in PoolTeamMatchLineup.objects.filter(match=pool_match)
            .select_related("team_entry__team")
            .prefetch_related("positions__player", "members__player")
        }
        entry_ids = (pool_match.home_entry_id, pool_match.away_entry_id)
        audit_events_by_entry = {entry_id: [] for entry_id in entry_ids}
        audit_events = PoolTeamMatchAuditEvent.objects.filter(
            match=pool_match,
            team_entry_id__in=entry_ids,
            action__in=(
                PoolTeamMatchAuditEvent.Action.LINEUP_SAVED,
                PoolTeamMatchAuditEvent.Action.LINEUP_CORRECTED,
            ),
        ).order_by("team_entry_id", "-created_at", "-pk")
        for event in audit_events:
            audit_events_by_entry.setdefault(event.team_entry_id, []).append(event)

        snapshots = {}
        snapshot_player_ids = set()
        for entry_id, events in audit_events_by_entry.items():
            for event in events:
                payload = event.payload if isinstance(event.payload, dict) else {}
                raw_positions = payload.get("positions")
                if not isinstance(raw_positions, dict):
                    continue
                position_keys = {str(position) for position in range(1, 6)}
                if not position_keys.issubset({str(key) for key in raw_positions}):
                    continue
                positions = {}
                valid_snapshot = True
                for position in range(1, 6):
                    player_id = raw_positions.get(str(position), raw_positions.get(position))
                    if player_id is not None and (
                        not isinstance(player_id, int) or isinstance(player_id, bool)
                    ):
                        valid_snapshot = False
                        break
                    positions[position] = player_id
                    if player_id:
                        snapshot_player_ids.add(player_id)
                if not valid_snapshot:
                    continue
                raw_substitute_ids = payload.get("substitute_ids", [])
                substitute_ids = (
                    [player_id for player_id in raw_substitute_ids if isinstance(player_id, int) and not isinstance(player_id, bool)]
                    if isinstance(raw_substitute_ids, list)
                    else []
                )
                snapshot_player_ids.update(substitute_ids)
                snapshots[entry_id] = (positions, substitute_ids)
                break

        snapshot_players = PoolPlayer.objects.in_bulk(snapshot_player_ids)
        substitutions_by_entry = {entry_id: [] for entry_id in entry_ids}
        for substitution in PoolTeamMatchSubstitution.objects.filter(
            match=pool_match,
            team_entry_id__in=entry_ids,
        ).select_related("outgoing_player", "incoming_player").order_by(
            "performed_at", "pk"
        ):
            substitutions_by_entry.setdefault(substitution.team_entry_id, []).append(
                substitution
            )
        for entry in (pool_match.home_entry, pool_match.away_entry):
            lineup = lineups.get(entry.pk)
            current_positions = list(lineup.positions.all()) if lineup else []
            members = list(lineup.members.all()) if lineup else []
            snapshot = snapshots.get(entry.pk)
            substitutions = substitutions_by_entry.get(entry.pk, [])
            if snapshot:
                snapshot_positions, substitute_ids = snapshot
                starting_positions = [
                    {
                        "position": position,
                        "player": snapshot_players.get(snapshot_positions[position]),
                        "was_subbed_off": any(
                            substitution.outgoing_player_id == snapshot_positions[position]
                            for substitution in substitutions
                        ),
                    }
                    for position in range(1, 6)
                ]
                submitted_substitutes = [
                    snapshot_players[player_id]
                    for player_id in substitute_ids
                    if player_id in snapshot_players
                ]
            else:
                # Older matches can lack audit snapshots. Keep their existing
                # presentation as a safe, readable fallback.
                starter_ids = {position.player_id for position in current_positions}
                starting_positions = [
                    {
                        "position": position.position,
                        "player": position.player,
                        "was_subbed_off": False,
                    }
                    for position in current_positions
                ]
                submitted_substitutes = [
                    member.player
                    for member in members
                    if member.player_id not in starter_ids
                ]
            team_lineup_cards.append(
                {
                    "entry": entry,
                    "starting_positions": starting_positions,
                    "submitted_substitutes": submitted_substitutes,
                    "substitutions": substitutions,
                    "can_manage_lineup": user_can_manage_team_lineup(
                        request.user,
                        pool_match,
                        entry,
                    ),
                }
            )
    context = {
            "match": pool_match,
            "racks": racks,
            "rack_summary": match_rack_summary(pool_match),
            "is_team_match": is_team_match,
            "team_schedule_rows": team_schedule_rows,
            "team_live_racks": team_live_racks,
            "team_lineup_cards": team_lineup_cards,
            "team_up_next": team_up_next,
            # Keep the old context key for downstream team-only templates,
            # while all new public scorer links use the generic permission.
            "can_score_team_match": (
                can_open_live_scorer if is_team_match else False
            ),
            "can_score_match": can_score_match,
            "can_open_live_scorer": can_open_live_scorer,
            "can_manage_match": can_manage_match,
            "has_match_controls": has_match_controls,
            "can_expire_match": can_open_live_scorer,
    }
    if has_match_controls:
        # Keep the manager views' existing forms and schedule preparation as
        # the single source for scorer controls.  Scorer mode is intentional
        # here even for a manager: manager-only administration remains on the
        # management route rather than leaking into the public fixture page.
        from .manager_views import (
            _standard_match_operations_context,
            _team_match_operations_context,
        )

        scorer_context = (
            _team_match_operations_context(
                request,
                pool_match.competition,
                pool_match,
                scorer_mode=not can_manage_match,
            )
            if is_team_match
            else _standard_match_operations_context(
                request,
                pool_match.competition,
                pool_match,
                scorer_mode=not can_manage_match,
            )
        )
        context.update(scorer_context)
        context["inline_scoring"] = True
        context["scoring_surface"] = "match_detail"
    return render(request, "pool/match_detail.html", context)


def player_detail(request, public_id):
    player = get_object_or_404(PoolPlayer, public_id=public_id)
    scope_competition = None
    scope_team_entry = None
    competition_slug = request.GET.get("competition")
    if competition_slug:
        scope_competition = get_object_or_404(
            PoolCompetition.objects.published(),
            slug=competition_slug,
        )
        team_id = request.GET.get("team")
        if team_id:
            scope_team_entry = get_object_or_404(
                PoolCompetitionEntry.objects.select_related("competition", "team").filter(
                    competition=scope_competition,
                    team__isnull=False,
                ),
                pk=team_id,
            )
            # Do not let an arbitrary public team query become a player-page
            # context.  A scoped page represents either a roster membership or
            # an existing attributed rack contribution by this player.
            has_team_context = PoolTeamRoster.objects.filter(
                competition_entry=scope_team_entry,
                player=player,
            ).exists() or PoolRack.objects.filter(
                match__competition=scope_competition,
                winner=scope_team_entry,
                winning_player=player,
            ).exists() or PoolTeamMatchScheduledRack.objects.filter(
                match__competition=scope_competition,
                status=PoolTeamMatchScheduledRack.Status.COMPLETED,
                result__isnull=False,
            ).filter(
                Q(
                    match__home_entry=scope_team_entry,
                    home_player=player,
                )
                | Q(
                    match__away_entry=scope_team_entry,
                    away_player=player,
                )
            ).exists()
            if not has_team_context:
                raise Http404("Player is not represented by this competition team.")
    entries = PoolCompetitionEntry.objects.filter(
        player=player,
        competition__visibility__in=PUBLIC_COMPETITION_VISIBILITIES,
    ).select_related("competition")
    is_singles_competition_scope = bool(
        scope_competition
        and scope_competition.participant_mode
        == PoolCompetition.ParticipantMode.SINGLES
    )
    is_team_competition_scope = bool(
        scope_competition
        and scope_competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
    )
    recent_matches = _match_queryset().filter(
        Q(home_entry__player=player)
        | Q(away_entry__player=player),
        competition__visibility__in=PUBLIC_COMPETITION_VISIBILITIES,
        status=PoolMatch.Status.FINISHED,
    ).distinct().order_by("-completed_at", "-scheduled_at", "-pk")[:10]

    return render(
        request,
        "pool/player_detail.html",
        {
            "player": player,
            "player_entries": entries.order_by("-competition__start_date", "competition__name"),
            "player_recent_matches": recent_matches,
            "player_stats": player_statistics(player, entries=entries),
            "player_money_match_stats": player_money_match_statistics(
                player,
                competitions=PoolCompetition.objects.published(),
            ),
            "player_competition_stats": (
                player_statistics(
                    player,
                    entries=entries.filter(competition=scope_competition),
                )
                if is_singles_competition_scope
                else None
            ),
            "player_rack_stats": player_rack_win_statistics(
                player,
                competition=scope_competition,
                competitions=(
                    None if scope_competition is not None else PoolCompetition.objects.published()
                ),
                team_entry=scope_team_entry,
            ),
            "player_stats_competition": scope_competition,
            "player_stats_team_entry": scope_team_entry,
            "is_singles_competition_scope": is_singles_competition_scope,
            "is_team_competition_scope": is_team_competition_scope,
            "rack_outcome_columns": RACK_WIN_OUTCOME_COLUMNS,
        },
    )


def team_detail(request, pk):
    team = get_object_or_404(PoolTeam, pk=pk)
    entries = PoolCompetitionEntry.objects.filter(
        team=team,
        competition__visibility__in=PUBLIC_COMPETITION_VISIBILITIES,
    ).select_related("competition")
    competition_slug = request.GET.get("competition")
    if competition_slug:
        entries = entries.filter(competition__slug=competition_slug)

    competition_ids = entries.values("competition_id")
    rosters = PoolTeamRoster.objects.filter(
        competition_entry__team=team,
        competition_entry__competition_id__in=competition_ids,
    ).select_related("competition_entry__competition", "player").order_by(
        "competition_entry__competition__name", "player__full_name"
    )
    recent_matches = _match_queryset().filter(
        Q(home_entry__team=team) | Q(away_entry__team=team),
        competition__visibility__in=PUBLIC_COMPETITION_VISIBILITIES,
        status=PoolMatch.Status.FINISHED,
    )
    if competition_slug:
        recent_matches = recent_matches.filter(competition__slug=competition_slug)

    team_rack_stats = [
        team_entry_rack_statistics(entry)
        for entry in entries.select_related("competition", "team").order_by(
            "-competition__start_date", "competition__name", "pk"
        )
    ]

    return render(
        request,
        "pool/team_detail.html",
        {
            "team": team,
            "team_entries": entries.order_by("-competition__start_date", "competition__name"),
            "team_rosters": rosters,
            "team_recent_matches": recent_matches.order_by(
                "-completed_at", "-scheduled_at", "-pk"
            )[:10],
            "team_rack_stats": team_rack_stats,
            "rack_outcome_columns": RACK_WIN_OUTCOME_COLUMNS,
        },
    )
