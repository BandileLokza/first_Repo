from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from .models import Competition, CompetitionTieBreaker, Stage


DEFAULT_COMPETITION_TIE_BREAKERS = (
    (1, CompetitionTieBreaker.Criterion.POINTS),
    (2, CompetitionTieBreaker.Criterion.OVERALL_GOAL_DIFFERENCE),
    (3, CompetitionTieBreaker.Criterion.OVERALL_GOALS_SCORED),
)


def validate_tie_breaker_configuration(configuration):
    """Normalize and validate one complete competition configuration."""

    valid_criteria = set(CompetitionTieBreaker.Criterion.values)
    normalized = []
    errors = []

    for position, criterion in configuration:
        criterion = getattr(criterion, "value", criterion)

        if not isinstance(position, int) or isinstance(position, bool):
            errors.append("Each tie-breaker position must be a whole number.")
            continue
        if criterion not in valid_criteria:
            errors.append("Select a valid tie-breaker criterion.")
            continue

        normalized.append((position, criterion))

    positions = [position for position, _criterion in normalized]
    criteria = [criterion for _position, criterion in normalized]

    if len(positions) != len(set(positions)):
        errors.append("Each tie-breaker position may only be used once.")
    if len(criteria) != len(set(criteria)):
        errors.append("Each tie-breaker criterion may only be selected once.")

    points_positions = [
        position
        for position, criterion in normalized
        if criterion == CompetitionTieBreaker.Criterion.POINTS
    ]
    if len(points_positions) != 1:
        errors.append("Configure Points exactly once as the primary criterion.")
    elif points_positions[0] != 1:
        errors.append("Points must remain at position 1.")

    if any(
        criterion != CompetitionTieBreaker.Criterion.POINTS and position < 2
        for position, criterion in normalized
    ):
        errors.append(
            "Only Points may use position 1; other criteria start at position 2."
        )

    if errors:
        raise ValidationError(errors)

    return tuple(sorted(normalized))


@transaction.atomic
def replace_competition_tie_breakers(competition, configuration):
    """Atomically replace a competition's complete tie-breaker configuration.

    The model intentionally keeps unique database constraints on both
    criterion and position. Rebuilding a fully validated configuration avoids
    transient collisions when an administrator swaps or replaces rows.
    """

    desired_configuration = validate_tie_breaker_configuration(configuration)
    Competition.objects.select_for_update().get(pk=competition.pk)
    existing_rows = list(
        CompetitionTieBreaker.objects.select_for_update()
        .filter(competition=competition)
        .order_by("order", "id")
    )
    existing_configuration = tuple(
        (row.order, row.criterion) for row in existing_rows
    )

    if existing_configuration == desired_configuration:
        return [], []

    CompetitionTieBreaker.objects.filter(competition=competition).delete()
    created_rows = CompetitionTieBreaker.objects.bulk_create(
        [
            CompetitionTieBreaker(
                competition=competition,
                order=position,
                criterion=criterion,
            )
            for position, criterion in desired_configuration
        ]
    )
    return created_rows, existing_rows


@transaction.atomic
def ensure_default_tie_breakers(competition):
    """Give a newly created competition the former rankings configuration.

    The helper intentionally only fills an entirely empty configuration.  It
    never alters an administrator's explicit choices or a partially repaired
    historical configuration.
    """

    if not competition.pk or CompetitionTieBreaker.objects.filter(
        competition=competition
    ).exists():
        return []

    return CompetitionTieBreaker.objects.bulk_create(
        [
            CompetitionTieBreaker(
                competition=competition,
                order=position,
                criterion=criterion,
            )
            for position, criterion in DEFAULT_COMPETITION_TIE_BREAKERS
        ]
    )


FORMAT_STAGE_BLUEPRINTS = {
    "TABLE": [
        {
            "name": "League Stage",
            "stage_type": "LEAGUE",
            "order": 1,
        },
    ],

    "GROUP_KNOCKOUT": [
        {
            "name": "Group Stage",
            "stage_type": "GROUP",
            "order": 1,
        },
    ],

    "KNOCKOUT": [],
}

KNOCKOUT_ROUND_BLUEPRINTS = {
    2: [
        "Final",
    ],
    4: [
        "Semi-finals",
        "Final",
    ],
    8: [
        "Quarter-finals",
        "Semi-finals",
        "Final",
    ],
    16: [
        "Round of 16",
        "Quarter-finals",
        "Semi-finals",
        "Final",
    ],
    32: [
        "Round of 32",
        "Round of 16",
        "Quarter-finals",
        "Semi-finals",
        "Final",
    ],
}

@transaction.atomic
def configure_knockout_rounds(competition, knockout_size):
    """
    Create the knockout stages for a competition.

    The structure cannot be changed after a knockout fixture exists.
    """

    Match = apps.get_model("matches", "Match")
    FootballKnockoutRoundRule = apps.get_model(
        "matches",
        "FootballKnockoutRoundRule",
    )
    FootballKnockoutTie = apps.get_model(
        "matches",
        "FootballKnockoutTie",
    )

    if competition.format_type not in {
        "GROUP_KNOCKOUT",
        "KNOCKOUT",
    }:
        raise ValidationError(
            "This competition does not contain a knockout section."
        )

    try:
        knockout_size = int(knockout_size)
    except (TypeError, ValueError):
        raise ValidationError(
            "Select a valid number of knockout teams."
        )

    round_names = KNOCKOUT_ROUND_BLUEPRINTS.get(
        knockout_size
    )

    if not round_names:
        raise ValidationError(
            "The selected knockout size is not supported."
        )

    existing_knockout_stages = (
        Stage.objects
        .filter(
            competition=competition,
            stage_type="KNOCKOUT",
        )
        .order_by("order", "id")
    )

    knockout_matches_exist = Match.objects.filter(
        competition_id=competition.id,
        stage__stage_type="KNOCKOUT",
    ).exists()
    bracket_structure_exists = FootballKnockoutTie.objects.filter(
        stage__competition_id=competition.id,
        position__isnull=False,
    ).exists()

    current_round_names = list(
        existing_knockout_stages.values_list(
            "name",
            flat=True,
        )
    )

    if knockout_matches_exist or bracket_structure_exists:
        if current_round_names != round_names:
            raise ValidationError(
                "The knockout structure cannot be changed because "
                "knockout fixtures or a connected bracket already exist."
            )

        if competition.knockout_size != knockout_size:
            competition.knockout_size = knockout_size
            competition.save(
                update_fields=["knockout_size"]
            )

        return list(existing_knockout_stages)

    # No knockout matches exist, so safely replace the old structure.
    existing_knockout_stages.delete()

    highest_existing_order = (
        Stage.objects
        .filter(competition=competition)
        .aggregate(max_order=Max("order"))
        .get("max_order")
        or 0
    )

    created_stages = []

    for position, round_name in enumerate(
        round_names,
        start=1,
    ):
        stage = Stage.objects.create(
            competition=competition,
            name=round_name,
            stage_type="KNOCKOUT",
            order=highest_existing_order + position,
        )

        created_stages.append(stage)
        FootballKnockoutRoundRule.objects.create(stage=stage)

    competition.knockout_size = knockout_size
    competition.save(
        update_fields=["knockout_size"]
    )

    return created_stages

def competition_has_format_data(competition):
    """
    Return True when changing the competition format would be unsafe.
    """

    Team = apps.get_model("teams", "Team")
    Match = apps.get_model("matches", "Match")
    Group = apps.get_model("leagues", "Group")
    Standing = apps.get_model("leagues", "Standing")

    return (
        Team.objects.filter(
            competition_id=competition.id
        ).exists()
        or Match.objects.filter(
            competition_id=competition.id
        ).exists()
        or Group.objects.filter(
            stage__competition_id=competition.id
        ).exists()
        or Standing.objects.filter(
            competition_id=competition.id
        ).exists()
    )


@transaction.atomic
def ensure_competition_structure(competition):
    """
    Create any required stages that are missing.

    Existing stages are not deleted or changed.
    """

    blueprints = FORMAT_STAGE_BLUEPRINTS.get(
        competition.format_type,
        [],
    )

    created_stages = []

    for blueprint in blueprints:
        existing_stage = (
            Stage.objects
            .filter(
                competition=competition,
                stage_type=blueprint["stage_type"],
            )
            .order_by("order", "id")
            .first()
        )

        if existing_stage:
            continue

        stage = Stage.objects.create(
            competition=competition,
            name=blueprint["name"],
            stage_type=blueprint["stage_type"],
            order=blueprint["order"],
        )

        created_stages.append(stage)

    return created_stages


@transaction.atomic
def rebuild_competition_structure(competition):
    """
    Rebuild stages after the superuser changes the format.

    This is only allowed while the competition has no teams,
    groups, matches or standings.
    """

    if competition_has_format_data(competition):
        raise ValidationError(
            "The competition format cannot be changed because "
            "teams, groups, matches or standings already exist."
        )

    Stage.objects.filter(
        competition=competition
    ).delete()

    competition.knockout_size = None
    competition.save(
        update_fields=["knockout_size"]
    )

    blueprints = FORMAT_STAGE_BLUEPRINTS.get(
        competition.format_type,
        [],
    )

    return [
        Stage.objects.create(
            competition=competition,
            name=blueprint["name"],
            stage_type=blueprint["stage_type"],
            order=blueprint["order"],
        )
        for blueprint in blueprints
    ]
