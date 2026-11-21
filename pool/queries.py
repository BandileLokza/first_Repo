"""Read-model queries and derived statistics for the pool domain.

The functions in this module derive values from finished fixtures and rack
records.  Nothing here persists editable copies of green apples, red apples,
one visits, or rack totals.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db.models import Q

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolMatch,
    PoolManualResultRackSummary,
    PoolPlayer,
    PoolRack,
    PoolTeamMatchScheduledRack,
    PoolTeamRoster,
)


STATISTIC_KEYS = (
    "matches_played",
    "won",
    "lost",
    "racks_won",
    "racks_lost",
    "normal_wins",
    "green_apples",
    "red_apples",
    "one_visits",
)


# Keep rack-win reporting deliberately separate from the older entry-level
# match table above.  A rack records only the winning player, so no player-loss
# or rack-percentage value can be proved for team representation.  The column
# definitions live here rather than in templates so every public surface uses
# the same outcome taxonomy as PoolRack.
RACK_WIN_OUTCOME_COLUMNS = (
    {
        "outcome": PoolRack.OutcomeType.NORMAL,
        "key": "normal_wins",
        "label": "Normal rack wins",
        "short_label": "Normal",
    },
    {
        "outcome": PoolRack.OutcomeType.GREEN_APPLE,
        "key": "green_apples",
        "label": "Green apples",
        "short_label": "Green apples",
    },
    {
        "outcome": PoolRack.OutcomeType.RED_APPLE,
        "key": "red_apples",
        "label": "Red apples",
        "short_label": "Red apples",
    },
    {
        "outcome": PoolRack.OutcomeType.ONE_VISIT,
        "key": "one_visits",
        "label": "One visits",
        "short_label": "One visits",
    },
)
RACK_WIN_STATISTIC_KEYS = (
    "racks_won",
    *(column["key"] for column in RACK_WIN_OUTCOME_COLUMNS),
)
TRACKED_RACK_STATISTIC_KEYS = (
    "tracked_racks_played",
    "tracked_racks_won",
    "tracked_racks_lost",
)
_RACK_OUTCOME_STATISTIC_KEYS = {
    column["outcome"]: column["key"] for column in RACK_WIN_OUTCOME_COLUMNS
}


def published_competitions():
    """Return only competitions explicitly published to the public site."""
    return PoolCompetition.objects.published()


def competitions_visible_to(user):
    """Return public competitions plus drafts a competition manager may preview."""
    return PoolCompetition.objects.visible_to(user)


def _entry_rows(entries: Iterable[PoolCompetitionEntry]):
    rows = {}
    for entry in entries:
        rows[entry.pk] = {
            "entry": entry,
            "participant": entry.participant,
            "player": entry.player,
            "team": entry.team,
            "display_name": entry.display_name,
            "matches_played": 0,
            "won": 0,
            "lost": 0,
            "racks_won": 0,
            "racks_lost": 0,
            "normal_wins": 0,
            "green_apples": 0,
            "red_apples": 0,
            "one_visits": 0,
        }
    return rows


def participant_statistics(entries):
    """Return derived statistics for the supplied competition entries.

    The returned list is deliberately entry-scoped.  A player or team may be
    entered in multiple competitions, and that history must not silently be
    merged into a competition table.  Use :func:`player_statistics` or
    :func:`team_statistics` for an all-competition public profile aggregate.
    """
    if hasattr(entries, "select_related"):
        entries = entries.select_related("player", "team")
    entry_list = list(entries)
    rows = _entry_rows(entry_list)
    entry_ids = list(rows)
    if not entry_ids:
        return []

    completed_matches = (
        PoolMatch.objects.filter(status=PoolMatch.Status.FINISHED)
        .filter(Q(home_entry_id__in=entry_ids) | Q(away_entry_id__in=entry_ids))
        .only(
            "id",
            "home_entry_id",
            "away_entry_id",
            "winner_id",
            "home_score",
            "away_score",
            "result_entry_method",
        )
    )
    completed_matches = list(completed_matches)
    manual_summary_by_match_entry = {
        (summary.match_id, summary.entry_id): summary
        for summary in PoolManualResultRackSummary.objects.filter(
            match_id__in=[match.pk for match in completed_matches],
            entry_id__in=entry_ids,
        )
    }
    for match in completed_matches:
        participant_ids = (match.home_entry_id, match.away_entry_id)
        for participant_id in participant_ids:
            if participant_id in rows:
                rows[participant_id]["matches_played"] += 1
                if match.winner_id == participant_id:
                    rows[participant_id]["won"] += 1
                elif match.winner_id:
                    rows[participant_id]["lost"] += 1

        # An offline result is an aggregate match result, not hidden rack
        # events. Its score supplies rack W/L; optional summaries classify
        # wins and any unspecified wins are normal.
        if (
            match.result_entry_method
            == PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT
        ):
            if match.home_entry_id in rows:
                rows[match.home_entry_id]["racks_won"] += match.home_score
                rows[match.home_entry_id]["racks_lost"] += match.away_score
                summary = manual_summary_by_match_entry.get((match.pk, match.home_entry_id))
                special = summary.special_wins if summary else 0
                rows[match.home_entry_id]["normal_wins"] += match.home_score - special
                if summary:
                    for key in ("green_apples", "red_apples", "one_visits"):
                        rows[match.home_entry_id][key] += getattr(summary, key)
            if match.away_entry_id in rows:
                rows[match.away_entry_id]["racks_won"] += match.away_score
                rows[match.away_entry_id]["racks_lost"] += match.home_score
                summary = manual_summary_by_match_entry.get((match.pk, match.away_entry_id))
                special = summary.special_wins if summary else 0
                rows[match.away_entry_id]["normal_wins"] += match.away_score - special
                if summary:
                    for key in ("green_apples", "red_apples", "one_visits"):
                        rows[match.away_entry_id][key] += getattr(summary, key)

    racks = (
        PoolRack.objects.filter(match__status=PoolMatch.Status.FINISHED)
        .filter(Q(match__home_entry_id__in=entry_ids) | Q(match__away_entry_id__in=entry_ids))
        .select_related("match")
        .only(
            "id",
            "winner_id",
            "outcome_type",
            "match__home_entry_id",
            "match__away_entry_id",
        )
    )
    outcome_keys = {
        PoolRack.OutcomeType.NORMAL: "normal_wins",
        PoolRack.OutcomeType.GREEN_APPLE: "green_apples",
        PoolRack.OutcomeType.RED_APPLE: "red_apples",
        PoolRack.OutcomeType.ONE_VISIT: "one_visits",
    }
    for rack in racks:
        winner_id = rack.winner_id
        if winner_id in rows:
            rows[winner_id]["racks_won"] += 1
            outcome_key = outcome_keys.get(rack.outcome_type)
            if outcome_key:
                rows[winner_id][outcome_key] += 1

        loser_id = (
            rack.match.away_entry_id
            if winner_id == rack.match.home_entry_id
            else rack.match.home_entry_id
        )
        if loser_id in rows:
            rows[loser_id]["racks_lost"] += 1

    return sorted(rows.values(), key=lambda row: (row["display_name"].casefold(), row["entry"].pk))


def competition_statistics(competition: PoolCompetition):
    """Return entry-level, completed-match statistics for one competition."""
    return order_public_competition_stat_rows(
        participant_statistics(competition.entries.all())
    )


def aggregate_competition_statistics(competition: PoolCompetition):
    """Compatibility alias for callers using the service-style name."""
    return competition_statistics(competition)


def match_rack_summary(match: PoolMatch):
    """Return one shared detailed-or-manual outcome summary for a match."""
    outcomes = {
        "normal_wins": 0,
        "green_apples": 0,
        "red_apples": 0,
        "one_visits": 0,
    }
    home = {"entry": match.home_entry, "racks_won": 0, **outcomes}
    away = {"entry": match.away_entry, "racks_won": 0, **outcomes}
    by_entry = {match.home_entry_id: home, match.away_entry_id: away}
    if match.result_entry_method == PoolMatch.ResultEntryMethod.MANUAL_FINAL_RESULT:
        summaries = {
            summary.entry_id: summary
            for summary in PoolManualResultRackSummary.objects.filter(match=match)
        }
        for entry_id, side, score in (
            (match.home_entry_id, home, match.home_score),
            (match.away_entry_id, away, match.away_score),
        ):
            summary = summaries.get(entry_id)
            for key in ("green_apples", "red_apples", "one_visits"):
                side[key] = getattr(summary, key, 0)
            side["normal_wins"] = score - sum(side[key] for key in ("green_apples", "red_apples", "one_visits"))
            side["racks_won"] = score
            for key in outcomes:
                outcomes[key] += side[key]
        return {
            "racks_played": match.home_score + match.away_score,
            "total_racks": match.home_score + match.away_score,
            "is_manual_aggregate": True,
            **outcomes,
            "home": home,
            "away": away,
        }
    outcome_keys = {
        PoolRack.OutcomeType.NORMAL: "normal_wins",
        PoolRack.OutcomeType.GREEN_APPLE: "green_apples",
        PoolRack.OutcomeType.RED_APPLE: "red_apples",
        PoolRack.OutcomeType.ONE_VISIT: "one_visits",
    }

    rack_count = 0
    for rack in match.racks.all().only("winner_id", "outcome_type"):
        rack_count += 1
        winner = by_entry.get(rack.winner_id)
        if winner is None:
            # Validation prevents this in normal writes.  Keeping the read
            # model defensive makes historic imported data non-fatal.
            continue
        winner["racks_won"] += 1
        outcome_key = outcome_keys.get(rack.outcome_type)
        if outcome_key:
            winner[outcome_key] += 1
            outcomes[outcome_key] += 1

    return {
        "racks_played": rack_count,
        "total_racks": rack_count,
        "is_manual_aggregate": False,
        **outcomes,
        "home": home,
        "away": away,
    }


def _aggregate_profile_rows(rows, *, participant_key, participant):
    totals = {key: 0 for key in STATISTIC_KEYS}
    for row in rows:
        for key in STATISTIC_KEYS:
            totals[key] += row[key]
    return {
        participant_key: participant,
        "participant": participant,
        "entries": [row["entry"] for row in rows],
        **totals,
    }


def _with_completed_match_win_rates(row):
    """Attach completed-match percentages without template arithmetic.

    These rates intentionally use direct-entry statistics. Team player
    attribution has a separate, narrower source of truth in
    :func:`player_rack_win_statistics` and must not be folded into a player's
    singles match record.
    """
    matches_played = row["matches_played"]
    racks_played = row["racks_won"] + row["racks_lost"]
    row["match_win_percentage"] = (
        round((row["won"] / matches_played) * 100, 1)
        if matches_played
        else None
    )
    row["rack_win_percentage"] = (
        round((row["racks_won"] / racks_played) * 100, 1)
        if racks_played
        else None
    )
    return row


def player_statistics(player, entries=None):
    """Aggregate a player's completed-match statistics across selected entries.

    Callers serving a public profile can pass only entries from published
    competitions, so private competition history is never leaked by accident.
    """
    return _with_completed_match_win_rates(
        _aggregate_profile_rows(
            participant_statistics(
                entries if entries is not None else player.competition_entries.all()
            ),
            participant_key="player",
            participant=player,
        )
    )


def player_money_match_statistics(player: PoolPlayer, *, competitions=None):
    """Return completed singles Money Match records for one player."""

    match_filter = Q(
        status=PoolMatch.Status.FINISHED,
        competition__format_type=PoolCompetition.FormatType.MONEY_MATCH,
    )
    if competitions is not None:
        match_filter &= Q(competition__in=competitions)
    match_participants = {}
    singles_matches = PoolMatch.objects.filter(
        match_filter,
        Q(home_entry__player=player) | Q(away_entry__player=player),
    ).select_related("home_entry", "away_entry").only(
        "id",
        "home_entry_id",
        "away_entry_id",
        "winner_id",
        "home_entry__player_id",
        "away_entry__player_id",
    )
    for pool_match in singles_matches:
        player_entry_id = (
            pool_match.home_entry_id
            if pool_match.home_entry.player_id == player.pk
            else pool_match.away_entry_id
        )
        match_participants[pool_match.pk] = (pool_match.winner_id, player_entry_id)
    totals = {"money_matches_played": 0, "money_match_wins": 0, "money_match_losses": 0}
    for winner_id, player_entry_id in match_participants.values():
        totals["money_matches_played"] += 1
        if player_entry_id == winner_id:
            totals["money_match_wins"] += 1
        elif winner_id:
            totals["money_match_losses"] += 1
    return totals


def team_statistics(team):
    """Aggregate a team's completed-match statistics across entries."""
    return _aggregate_profile_rows(
        participant_statistics(team.competition_entries.all()),
        participant_key="team",
        participant=team,
    )


def _empty_rack_win_statistics(*, player=None, entry=None):
    """Return one player-safe rack-win row with no invented loss fields."""
    return {
        "player": player,
        "entry": entry,
        **{key: 0 for key in RACK_WIN_STATISTIC_KEYS},
        **{key: 0 for key in TRACKED_RACK_STATISTIC_KEYS},
        "tracked_rack_win_percentage": None,
    }


def _add_rack_win_to_statistics(row, rack):
    """Apply one authoritative rack event to a player/team contribution row."""
    row["racks_won"] += 1
    outcome_key = _RACK_OUTCOME_STATISTIC_KEYS.get(rack.outcome_type)
    if outcome_key:
        row[outcome_key] += 1


def _with_outcome_values(row):
    """Attach presentation-neutral outcome values without template arithmetic."""
    row["outcome_values"] = tuple(
        {
            **column,
            "value": row[column["key"]],
        }
        for column in RACK_WIN_OUTCOME_COLUMNS
    )
    played = row.get("tracked_racks_played", 0)
    won = row.get("tracked_racks_won", 0)
    row["tracked_rack_win_percentage"] = (
        round((won / played) * 100, 1) if played else None
    )
    # Presentation aliases deliberately retain the "tracked" source of truth
    # above.  They make small table templates readable without implying that
    # legacy winner-only records have an invented played/lost denominator.
    row["racks_played"] = played
    row["racks_lost"] = row.get("tracked_racks_lost", 0)
    row["rack_win_percentage"] = row["tracked_rack_win_percentage"]
    return row


def order_public_competition_stat_rows(
    rows,
    *,
    participant_key="participant",
    statistics_key=None,
    statistics_getter=None,
):
    """Order rows for a public, competition-scoped statistics table.

    Green apples are deliberately the primary measure of the public Pool
    statistics tables, followed by red apples and one visits.  The final
    case-insensitive name and primary-key values make zero-stat rows and
    shared names deterministic as well.

    Most callers keep the outcome values on the row itself.  A statistics
    table can instead supply a nested mapping or a small getter when its
    displayed outcomes live elsewhere in the row (for example the singles
    match row's ``rack_win_stats``).
    """

    if statistics_key and statistics_getter:
        raise ValueError(
            "Use either statistics_key or statistics_getter when ordering "
            "public competition statistics."
        )

    def _statistics_values(row):
        if statistics_getter:
            return statistics_getter(row) or {}
        if statistics_key:
            return row.get(statistics_key) or {}
        return row

    def _participant(row):
        return (
            row.get(participant_key)
            or row.get("participant")
            or row.get("team")
            or row.get("player")
        )

    def _name(row, participant):
        return str(
            row.get("display_name")
            or getattr(participant, "full_name", None)
            or getattr(participant, "name", None)
            or ""
        ).casefold()

    def _value(statistics, key):
        value = statistics.get(key, 0)
        return value if isinstance(value, int) else int(value or 0)

    def _sort_key(row):
        statistics = _statistics_values(row)
        participant = _participant(row)
        # A participant name is the specified final public tie-break.  A PK
        # only settles genuinely indistinguishable imported/duplicate names.
        return (
            -_value(statistics, "green_apples"),
            -_value(statistics, "red_apples"),
            -_value(statistics, "one_visits"),
            _name(row, participant),
            getattr(participant, "pk", None)
            or getattr(row.get("entry"), "pk", None)
            or 0,
        )

    return sorted(rows, key=_sort_key)


def _fully_attributed_team_schedule_rows(
    *, entry=None, player=None, competition=None, competitions=None
):
    """Return completed team score-sheet rows with both actual participants.

    This is deliberately separate from historical winner-only rack records.
    A completed schedule row is the authoritative proof that both people
    contested the rack; a legacy ``winning_player`` alone is never used to
    fabricate a loss, played count, or percentage.
    """

    rows = PoolTeamMatchScheduledRack.objects.filter(
        status=PoolTeamMatchScheduledRack.Status.COMPLETED,
        result__isnull=False,
        home_player__isnull=False,
        away_player__isnull=False,
    ).select_related(
        "match",
        "result",
        "home_player",
        "away_player",
    )
    if competition is not None:
        rows = rows.filter(match__competition=competition)
    elif competitions is not None:
        rows = rows.filter(match__competition__in=competitions)
    if entry is not None:
        rows = rows.filter(
            Q(match__home_entry=entry) | Q(match__away_entry=entry),
            match__competition_id=entry.competition_id,
        )
    if player is not None:
        rows = rows.filter(Q(home_player=player) | Q(away_player=player))
    return rows.order_by("match_id", "sequence_number", "pk")


def _apply_tracked_team_schedule_row(row, scheduled_rack, *, entry=None, player=None):
    """Apply one verified participant record to a player contribution row."""

    if entry is not None:
        if scheduled_rack.match.home_entry_id == entry.pk:
            participant_id = scheduled_rack.home_player_id
        elif scheduled_rack.match.away_entry_id == entry.pk:
            participant_id = scheduled_rack.away_player_id
        else:
            return None
    elif player is not None:
        if player.pk == scheduled_rack.home_player_id:
            participant_id = scheduled_rack.home_player_id
        elif player.pk == scheduled_rack.away_player_id:
            participant_id = scheduled_rack.away_player_id
        else:
            return None
    else:
        return None
    row["tracked_racks_played"] += 1
    if scheduled_rack.result.winning_player_id == participant_id:
        row["tracked_racks_won"] += 1
    else:
        row["tracked_racks_lost"] += 1
    return participant_id


def _recorded_racks(*, competition=None, competitions=None, winning_entry=None):
    """Return all actual rack events in an exact competition/entry scope.

    Player contribution statistics intentionally include live and subsequently
    abandoned played racks.  Each persisted rack is an authoritative event;
    this makes an add, correction, or authorised undo visible immediately and
    avoids presenting live rack totals as completed-match standings.
    """
    racks = PoolRack.objects.select_related(
        "match",
        "winner__player",
        "winner__team",
        "winning_player",
    )
    if competition is not None:
        racks = racks.filter(match__competition=competition)
    elif competitions is not None:
        racks = racks.filter(match__competition__in=competitions)
    if winning_entry is not None:
        racks = racks.filter(
            winner=winning_entry,
            match__competition_id=winning_entry.competition_id,
        )
    return racks.order_by("match_id", "rack_number", "pk")


def _rack_player_id(rack):
    """Return a trustworthy rack winner player without inventing team history.

    New records have ``winning_player``.  Older singles records have an entry
    whose player is already unambiguous, so that existing first-party history
    can also be shown.  A team rack with no player attribution remains null:
    guessing from a roster would fabricate history.
    """
    if rack.winning_player_id:
        return rack.winning_player_id
    if rack.winner_id and rack.winner.player_id:
        return rack.winner.player_id
    return None


def player_rack_win_statistics(
    player: PoolPlayer,
    *,
    competition=None,
    competitions=None,
    team_entry=None,
):
    """Return verified rack-win totals for one player.

    ``team_entry`` makes a public team page explicitly competition and team
    scoped.  Callers must validate that it belongs to ``competition`` before
    accepting user input; the function repeats that relationship defensively.
    """
    if team_entry is not None:
        if competition is not None and team_entry.competition_id != competition.pk:
            return _with_outcome_values(
                _empty_rack_win_statistics(player=player, entry=team_entry)
            )
        competition = team_entry.competition
        competitions = None

    row = _empty_rack_win_statistics(player=player, entry=team_entry)
    for rack in _recorded_racks(
        competition=competition,
        competitions=competitions,
        winning_entry=team_entry,
    ):
        if _rack_player_id(rack) == player.pk:
            _add_rack_win_to_statistics(row, rack)
    # Team score-sheet rows are the only source that proves both individual
    # participants for a team rack.  Keep the legacy winner-only aggregation
    # above, then add this independent, honest played/lost denominator.
    schedule_scope = {"player": player}
    if team_entry is not None:
        schedule_scope["entry"] = team_entry
    elif competition is not None:
        schedule_scope["competition"] = competition
    elif competitions is not None:
        schedule_scope["competitions"] = competitions
    for scheduled_rack in _fully_attributed_team_schedule_rows(**schedule_scope):
        _apply_tracked_team_schedule_row(
            row,
            scheduled_rack,
            entry=team_entry,
            player=player,
        )
    return _with_outcome_values(row)


def competition_player_rack_statistics(
    competition: PoolCompetition,
    *,
    completed_only: bool | None = None,
):
    """Return player rack-win rows for one singles competition only.

    This preserves legacy singles racks through their player entry, but keeps
    team player contribution in :func:`competition_team_rack_statistics` where
    its roster and team context remain explicit.
    """
    entries = list(
        competition.entries.filter(player__isnull=False)
        .select_related("player")
        .order_by("player__full_name", "pk")
    )
    rows = {
        entry.player_id: _empty_rack_win_statistics(
            player=entry.player,
            entry=entry,
        )
        for entry in entries
    }
    racks = _recorded_racks(competition=competition)
    if completed_only is True:
        racks = racks.filter(match__status=PoolMatch.Status.FINISHED)
    elif completed_only is False:
        racks = racks.exclude(match__status=PoolMatch.Status.FINISHED)
    for rack in racks:
        player_id = _rack_player_id(rack)
        row = rows.get(player_id)
        if row is not None:
            _add_rack_win_to_statistics(row, rack)
    return order_public_competition_stat_rows(
        (_with_outcome_values(row) for row in rows.values()),
        participant_key="player",
    )


def team_entry_player_rack_statistics(entry: PoolCompetitionEntry):
    """Return one team entry's roster/player rack-win contribution rows.

    Players with a recorded attributed rack remain visible even if their roster
    membership was later made inactive or removed.  Current competition roster
    members are included with zeros, so the public page clearly distinguishes
    the roster from the historical contribution data.
    """
    if not entry.team_id:
        return []

    roster_memberships = list(
        PoolTeamRoster.objects.filter(competition_entry=entry)
        .select_related("player")
        .order_by("sort_order", "player__full_name", "pk")
    )
    rows = {
        membership.player_id: _empty_rack_win_statistics(
            player=membership.player,
            entry=entry,
        )
        for membership in roster_memberships
    }
    for rack in _recorded_racks(winning_entry=entry):
        player_id = _rack_player_id(rack)
        if player_id is None:
            # Team attribution is deliberately nullable for imported history.
            continue
        if player_id not in rows:
            # A recorded rack is historical evidence even if a roster row was
            # removed later.  Fetch only the one recorded player needed.
            player = rack.winning_player
            if player is None:
                # A team entry without winning_player is intentionally
                # unattributed; _rack_player_id therefore cannot reach here.
                continue
            rows[player_id] = _empty_rack_win_statistics(player=player, entry=entry)
        _add_rack_win_to_statistics(rows[player_id], rack)

    # New team operations rows contain both snapshots.  They are the only
    # evidence used for played/lost/win-rate values, so older imported racks
    # with only a winner continue to show honest win/outcome contribution but
    # never fabricate a loser.
    for scheduled_rack in _fully_attributed_team_schedule_rows(entry=entry):
        if scheduled_rack.match.home_entry_id == entry.pk:
            player = scheduled_rack.home_player
        else:
            player = scheduled_rack.away_player
        if player.pk not in rows:
            rows[player.pk] = _empty_rack_win_statistics(player=player, entry=entry)
        _apply_tracked_team_schedule_row(
            rows[player.pk],
            scheduled_rack,
            entry=entry,
        )
    return order_public_competition_stat_rows(
        (_with_outcome_values(row) for row in rows.values()),
        participant_key="player",
    )


def team_entry_rack_statistics(entry: PoolCompetitionEntry):
    """Return collective and player-attributed rack data for one team entry."""
    if not entry.team_id:
        return None

    player_rows = team_entry_player_rack_statistics(entry)
    aggregate = participant_statistics([entry])[0]
    team_totals = _empty_rack_win_statistics(entry=entry)
    for key in RACK_WIN_STATISTIC_KEYS:
        team_totals[key] = aggregate[key]
    # A live rack is already an authoritative team win, even before the
    # fixture reaches its target. Completed fixtures are covered above by
    # participant_statistics, so excluding them prevents double counting.
    for rack in _recorded_racks(winning_entry=entry).exclude(
        match__status=PoolMatch.Status.FINISHED
    ):
        _add_rack_win_to_statistics(team_totals, rack)
    attributed_totals = _empty_rack_win_statistics(entry=entry)
    for player_row in player_rows:
        for key in RACK_WIN_STATISTIC_KEYS:
            attributed_totals[key] += player_row[key]
        for key in TRACKED_RACK_STATISTIC_KEYS:
            attributed_totals[key] += player_row[key]

    return {
        "entry": entry,
        "team": entry.team,
        "player_rows": player_rows,
        "team_racks": team_totals["racks_won"] + aggregate["racks_lost"],
        "racks_lost": aggregate["racks_lost"],
        "rack_win_percentage": (
            round(
                (team_totals["racks_won"] / (team_totals["racks_won"] + aggregate["racks_lost"])) * 100,
                1,
            )
            if team_totals["racks_won"] + aggregate["racks_lost"]
            else None
        ),
        **{key: team_totals[key] for key in RACK_WIN_STATISTIC_KEYS},
        **{
            f"attributed_{key}": attributed_totals[key]
            for key in RACK_WIN_STATISTIC_KEYS
        },
        **{
            key: attributed_totals[key]
            for key in TRACKED_RACK_STATISTIC_KEYS
        },
        "tracked_rack_win_percentage": _with_outcome_values(attributed_totals)[
            "tracked_rack_win_percentage"
        ],
        "unattributed_racks_won": (
            team_totals["racks_won"] - attributed_totals["racks_won"]
        ),
        "outcome_values": _with_outcome_values(team_totals)["outcome_values"],
        "team_outcome_values": _with_outcome_values(team_totals)["outcome_values"],
    }


def competition_team_rack_statistics(competition: PoolCompetition):
    """Return team/player rack contribution sections for one competition."""
    entries = (
        competition.entries.filter(team__isnull=False)
        .select_related("team")
        .order_by("team__name", "pk")
    )
    return order_public_competition_stat_rows(
        (team_entry_rack_statistics(entry) for entry in entries),
        participant_key="team",
        statistics_getter=lambda row: {
            outcome["key"]: outcome["value"]
            for outcome in row["outcome_values"]
        },
    )
