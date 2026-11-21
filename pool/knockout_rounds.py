"""Independent configuration for manually drawn knockout rounds.

This module deliberately has no dependency on bracket topology.  A manager
can configure read-only round metadata, create fresh manual fixtures in those
rounds, or later use the separate bracket workflow without a mode switch.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    SUPPORTED_KNOCKOUT_ROUND_SIZES,
    PoolBracketAuditEvent,
    PoolBracketDrawPreview,
    PoolCompetitionStage,
    PoolKnockoutBracket,
    PoolKnockoutRoundDefinition,
    PoolMatch,
    canonical_knockout_round_name,
)


def knockout_round_structure(competitor_count):
    """Return canonical ``(order, size, name)`` rows for a field size.

    ``competitor_count`` is the real expected field, while the first generated
    round uses the next supported power of two.  The resulting rows are
    structure only; BYEs and actual fixtures are still explicit tournament
    operations.
    """

    try:
        competitor_count = int(competitor_count)
    except (TypeError, ValueError):
        raise ValidationError("Enter a whole number of competitors.")
    if competitor_count < 2:
        raise ValidationError("At least two competitors are required.")
    maximum = SUPPORTED_KNOCKOUT_ROUND_SIZES[-1]
    if competitor_count > maximum:
        raise ValidationError(
            f"At most {maximum} competitors are supported for knockout rounds."
        )

    initial_size = next(
        size for size in SUPPORTED_KNOCKOUT_ROUND_SIZES if size >= competitor_count
    )
    structure = []
    round_size = initial_size
    order = 1
    while round_size >= 2:
        structure.append(
            (order, round_size, canonical_knockout_round_name(round_size))
        )
        round_size //= 2
        order += 1
    return tuple(structure)


@transaction.atomic
def configure_knockout_rounds(stage, competitor_count, *, actor=None):
    """Replace unused round metadata for one exact knockout stage safely.

    Existing manual fixtures reference their round by foreign key.  As soon as
    any such row exists, configuration is frozen rather than deleting or
    renaming historical round identity.  A generated bracket intentionally
    does not reference these metadata rows, so simply configuring rounds never
    blocks that separate workflow.
    """

    if not getattr(stage, "pk", None):
        raise ValidationError("Choose a saved knockout stage.")
    structure = knockout_round_structure(competitor_count)
    locked_stage = (
        PoolCompetitionStage.objects.select_for_update()
        .select_related("competition")
        .filter(pk=stage.pk)
        .first()
    )
    if locked_stage is None:
        raise ValidationError("The knockout stage no longer exists.")
    if locked_stage.stage_type != PoolCompetitionStage.StageType.KNOCKOUT:
        raise ValidationError("Rounds can be configured only for a knockout stage.")

    existing = list(
        PoolKnockoutRoundDefinition.objects.select_for_update()
        .filter(stage=locked_stage)
        .order_by("order", "id")
    )
    existing_structure = tuple(
        (definition.order, definition.round_size, definition.name)
        for definition in existing
    )
    if existing_structure == structure:
        return tuple(existing)

    dependent_matches = PoolMatch.objects.filter(
        knockout_round__stage=locked_stage
    )
    dependent_previews = PoolBracketDrawPreview.objects.filter(
        handoff_from_round__stage=locked_stage
    )
    dependent_brackets = PoolKnockoutBracket.objects.filter(
        handoff_from_round__stage=locked_stage
    )
    if (
        dependent_matches.exists()
        or dependent_previews.exists()
        or dependent_brackets.exists()
    ):
        raise ValidationError(
            "Knockout rounds cannot be regenerated after a manual fixture or "
            "bracket handoff preview references them. Keep the existing round "
            "history and configure a new stage if a different structure is required."
        )

    # The definitions are deliberately independent from PoolKnockoutBracket.
    # Delete/recreate only this unused metadata collection; no fixtures,
    # results, bracket ties, or entrant positions are touched.
    if existing:
        PoolKnockoutRoundDefinition.objects.filter(
            pk__in=[definition.pk for definition in existing]
        ).delete()
    definitions = [
        PoolKnockoutRoundDefinition(
            stage=locked_stage,
            order=order,
            round_size=round_size,
            name=name,
        )
        for order, round_size, name in structure
    ]
    PoolKnockoutRoundDefinition.objects.bulk_create(definitions)
    definitions = tuple(
        PoolKnockoutRoundDefinition.objects.filter(stage=locked_stage).order_by(
            "order", "id"
        )
    )
    PoolBracketAuditEvent.objects.create(
        competition=locked_stage.competition,
        stage=locked_stage,
        actor=actor,
        action=PoolBracketAuditEvent.Action.ROUND_DEFS_CONFIGURED,
        message="Knockout round definitions were configured.",
        payload={
            "competitor_count": int(competitor_count),
            "round_sizes": [definition.round_size for definition in definitions],
        },
    )
    return definitions
