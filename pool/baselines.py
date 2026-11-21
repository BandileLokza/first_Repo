"""Imported Pool table baselines and their validated write boundary.

Pool tables remain query-derived.  This module owns the small, explicit set
of imported values that are added before eSportsLive-recorded match results.
It deliberately has no head-to-head logic: an aggregate imported table cannot
truthfully reconstruct historic meetings.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolGroup,
    PoolGroupEntry,
    PoolLeagueAuditEvent,
    PoolStandingBaseline,
)


BASELINE_FIELDS = (
    "played",
    "won",
    "lost",
    "racks_for",
    "racks_against",
    "points",
)


class PoolBaselineValidationError(ValidationError):
    """Raised when an imported pool table scope or value is unsafe."""


def validate_baseline_scope(
    stage: PoolCompetitionStage,
    group: PoolGroup | None = None,
) -> tuple[PoolCompetitionStage, PoolGroup | None]:
    """Return one valid league/group table scope without implicit fallbacks."""

    if not isinstance(stage, PoolCompetitionStage) or not stage.pk:
        raise PoolBaselineValidationError("A saved pool league or group stage is required.")
    if stage.stage_type not in {
        PoolCompetitionStage.StageType.LEAGUE,
        PoolCompetitionStage.StageType.GROUP,
    }:
        raise PoolBaselineValidationError(
            "Baseline standings are available only for league and group stages."
        )
    if stage.stage_type == PoolCompetitionStage.StageType.LEAGUE:
        if group is not None:
            raise PoolBaselineValidationError("A league-stage baseline cannot select a group.")
        return stage, None
    if not isinstance(group, PoolGroup) or not group.pk:
        raise PoolBaselineValidationError("Select a group for a group-stage baseline.")
    if group.stage_id != stage.pk:
        raise PoolBaselineValidationError("The selected group does not belong to this stage.")
    return stage, group


def baseline_entries_for_scope(
    stage: PoolCompetitionStage,
    *,
    group: PoolGroup | None = None,
) -> list[PoolCompetitionEntry]:
    """Return the entrants belonging to exactly this importable table."""

    stage, group = validate_baseline_scope(stage, group)
    if group is None:
        return list(
            PoolCompetitionEntry.objects.filter(competition_id=stage.competition_id)
            .select_related("player", "team")
            .order_by("seed", "id")
        )
    memberships = (
        PoolGroupEntry.objects.filter(stage_id=stage.pk, group_id=group.pk)
        .select_related("entry__player", "entry__team")
        .order_by("sort_order", "entry__seed", "entry__id")
    )
    return [membership.entry for membership in memberships]


def baseline_values_by_entry(
    stage: PoolCompetitionStage,
    *,
    group: PoolGroup | None = None,
    entry_ids: Iterable[int] | None = None,
) -> dict[int, PoolStandingBaseline]:
    """Fetch existing baseline rows in a single exact-scope query."""

    stage, group = validate_baseline_scope(stage, group)
    rows = PoolStandingBaseline.objects.filter(stage_id=stage.pk)
    if group is None:
        rows = rows.filter(group__isnull=True)
    else:
        rows = rows.filter(group_id=group.pk)
    if entry_ids is not None:
        rows = rows.filter(entry_id__in=list(entry_ids))
    return {row.entry_id: row for row in rows}


def scope_has_baselines(
    stage: PoolCompetitionStage,
    *,
    group: PoolGroup | None = None,
) -> bool:
    """Whether one table scope contains any nonzero imported row."""

    return any(
        any(getattr(row, field) for field in BASELINE_FIELDS)
        for row in baseline_values_by_entry(stage, group=group).values()
    )


def _normalise_baseline_rows(rows: Iterable[Mapping]) -> dict[int, dict[str, int]]:
    normalised: dict[int, dict[str, int]] = {}
    for row in rows:
        entry = row.get("entry")
        entry_id = getattr(entry, "pk", entry)
        if not isinstance(entry_id, int) or isinstance(entry_id, bool) or entry_id < 1:
            raise PoolBaselineValidationError("Every baseline row must name a valid entrant.")
        if entry_id in normalised:
            raise PoolBaselineValidationError("Each entrant may appear only once in baseline standings.")
        values: dict[str, int] = {}
        for field in BASELINE_FIELDS:
            value = row.get(field, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PoolBaselineValidationError(
                    f"{field.replace('_', ' ').capitalize()} must be a non-negative whole number."
                )
            values[field] = value
        if values["won"] + values["lost"] > values["played"]:
            raise PoolBaselineValidationError("Wins plus losses cannot exceed played.")
        normalised[entry_id] = values
    return normalised


@transaction.atomic
def save_pool_standing_baselines(
    stage: PoolCompetitionStage,
    rows: Iterable[Mapping],
    *,
    group: PoolGroup | None = None,
    actor=None,
) -> dict[str, int]:
    """Atomically replace the editable import rows for one exact table.

    All zero rows are intentionally removed: a missing baseline is the
    canonical zero value and does not make future entrants require a database
    record.  The competition root is locked before table rows, matching the
    Pool fixture/result services' root-first discipline.
    """

    stage, group = validate_baseline_scope(stage, group)
    submitted = _normalise_baseline_rows(rows)
    competition = PoolCompetition.objects.select_for_update(of=("self",)).get(
        pk=stage.competition_id
    )
    locked_stage = PoolCompetitionStage.objects.select_for_update(of=("self",)).get(
        pk=stage.pk,
        competition=competition,
    )
    locked_group = None
    if group is not None:
        locked_group = PoolGroup.objects.select_for_update(of=("self",)).get(
            pk=group.pk,
            stage=locked_stage,
        )

    scope_entries = baseline_entries_for_scope(locked_stage, group=locked_group)
    scope_entry_ids = {entry.pk for entry in scope_entries}
    if set(submitted) != scope_entry_ids:
        raise PoolBaselineValidationError(
            "The baseline table changed. Reload it before saving your edits."
        )

    existing_query = PoolStandingBaseline.objects.select_for_update(of=("self",)).filter(
        stage=locked_stage,
    )
    if locked_group is None:
        existing_query = existing_query.filter(group__isnull=True)
    else:
        existing_query = existing_query.filter(group=locked_group)
    existing = {baseline.entry_id: baseline for baseline in existing_query}
    created = updated = deleted = 0

    for entry in scope_entries:
        values = submitted[entry.pk]
        current = existing.get(entry.pk)
        if not any(values.values()):
            if current is not None:
                current.delete()
                deleted += 1
            continue
        if current is None:
            PoolStandingBaseline.objects.create(
                competition=competition,
                stage=locked_stage,
                group=locked_group,
                entry=entry,
                **values,
            )
            created += 1
            continue
        if any(getattr(current, field) != values[field] for field in BASELINE_FIELDS):
            for field, value in values.items():
                setattr(current, field, value)
            current.save(update_fields=[*BASELINE_FIELDS, "updated_at"])
            updated += 1

    PoolLeagueAuditEvent.objects.create(
        competition=competition,
        stage=locked_stage,
        group=locked_group,
        actor=actor,
        action=PoolLeagueAuditEvent.Action.BASELINES_UPDATED,
        message="Imported baseline standings were updated.",
        payload={
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "entrant_count": len(scope_entries),
        },
    )
    return {"created": created, "updated": updated, "deleted": deleted}
