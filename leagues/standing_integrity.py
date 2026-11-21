"""Diagnostic helpers for football standings that no query filter should hide."""

from django.db.models import F, Q

from .models import Standing


def invalid_standings():
    """Return standings whose team, stage, or group scope is inconsistent."""

    return Standing.objects.filter(
        Q(competition_id__isnull=False)
    ).filter(
        ~Q(competition_id=F("team__competition_id"))
        | ~Q(stage__isnull=True) & ~Q(stage__competition_id=F("competition_id"))
        | ~Q(group__isnull=True) & ~Q(group__stage__competition_id=F("competition_id"))
        | Q(group__isnull=False, stage__isnull=True)
        | ~Q(group__isnull=True) & ~Q(group__stage_id=F("stage_id"))
    )


def standing_integrity_issues(standing):
    """List human-readable cross-relation problems for one standing row."""

    issues = []
    if standing.competition_id != standing.team.competition_id:
        issues.append("team belongs to another competition")
    if standing.stage_id and standing.stage.competition_id != standing.competition_id:
        issues.append("stage belongs to another competition")
    if standing.group_id:
        if standing.group.stage.competition_id != standing.competition_id:
            issues.append("group belongs to another competition")
        if standing.stage_id != standing.group.stage_id:
            issues.append("group does not belong to the standing stage")
    return issues
