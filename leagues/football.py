"""Derived presentation data for Table Only football competitions."""

from collections import defaultdict

from django.db.models import F

from matches.models import Match


def completed_table_matches(competition):
    """Fetch completed league-table fixtures once, oldest first."""

    return list(
        Match.objects.filter(
            competition=competition,
            stage__stage_type="LEAGUE",
            group__isnull=True,
            status="FINISHED",
        )
        .select_related("home_team", "away_team")
        .order_by(F("match_date").asc(nulls_last=True), "id")
    )


def result_for_team(match, team_id):
    if match.home_team_id == team_id:
        own, opponent = match.home_score, match.away_score
    else:
        own, opponent = match.away_score, match.home_score
    return "W" if own > opponent else "L" if own < opponent else "D"


def recent_form_by_team(matches, team_ids=None):
    forms = defaultdict(list)
    for match in matches:
        forms[match.home_team_id].append(result_for_team(match, match.home_team_id))
        forms[match.away_team_id].append(result_for_team(match, match.away_team_id))
    if team_ids is not None:
        for team_id in team_ids:
            forms[team_id] = forms[team_id][-5:]
    return {team_id: values[-5:] for team_id, values in forms.items()}


def attach_standings_form(rows, matches):
    forms = recent_form_by_team(matches, {row.team_id for row in rows})
    for row in rows:
        row.recent_form = forms.get(row.team_id, [])
    return rows


def record(matches, *, team_id, home=None):
    """Return a derived W/D/L/GF/GA record for a home, away, or all scope."""

    values = {"played": 0, "won": 0, "drawn": 0, "lost": 0, "goals_for": 0, "goals_against": 0}
    for match in matches:
        is_home = match.home_team_id == team_id
        if home is not None and is_home != home:
            continue
        if not is_home and match.away_team_id != team_id:
            continue
        own = match.home_score if is_home else match.away_score
        opponent = match.away_score if is_home else match.home_score
        values["played"] += 1
        values["goals_for"] += own
        values["goals_against"] += opponent
        values[{True: "won", False: "lost"}[own > opponent] if own != opponent else "drawn"] += 1
    return values
