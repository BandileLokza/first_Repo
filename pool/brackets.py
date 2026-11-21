"""Persistent pool knockout brackets, qualification, and safe progression.

The graph is deliberately explicit: a :class:`PoolKnockoutTieSlot` describes
where each participant comes from, while a normal :class:`PoolMatch` is only
created when both real participants of a tie are known.  That keeps a bye out
of rack history and lets public pages render ``Winner of …``/``TBD`` without
inventing a fixture.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict, deque
from math import log2

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    PoolBracketAuditEvent,
    PoolBracketDrawPreview,
    PoolCompetition,
    PoolCompetitionEntry,
    PoolCompetitionStage,
    PoolGroup,
    PoolGroupQualificationRule,
    PoolKnockoutBracket,
    PoolKnockoutRoundDefinition,
    PoolKnockoutRoundRule,
    PoolKnockoutTie,
    PoolKnockoutTieSlot,
    PoolMatch,
    PoolPlayer,
    PoolRack,
    PoolTeamMatchScheduledRack,
    _late_knockout_entry_creation_scope,
)
from .standings import standings_for_stage


SUPPORTED_BRACKET_SIZES = (2, 4, 8, 16, 32, 64, 128, 256)


class PoolBracketValidationError(ValidationError):
    """A draw, topology, advancement, or safe-correction invariant failed."""


class PoolBracketCorrectionBlocked(PoolBracketValidationError):
    """A source result cannot change because its downstream work has activity."""


def _round_name(round_number: int, round_count: int) -> str:
    entrants = 2 ** (round_count - round_number + 1)
    names = {
        2: "Final",
        4: "Semi-finals",
        8: "Quarter-finals",
        16: "Round of 16",
        32: "Round of 32",
        64: "Round of 64",
        128: "Round of 128",
        256: "Round of 256",
    }
    return names.get(entrants, f"Round {round_number}")


def _round_count(size: int) -> int:
    if size not in SUPPORTED_BRACKET_SIZES:
        raise PoolBracketValidationError(
            "Supported pool bracket sizes are 2, 4, 8, 16, 32, 64, 128, and 256."
        )
    return int(log2(size))


def _next_supported_size(count: int) -> int:
    if count < 2:
        raise PoolBracketValidationError("A bracket needs at least two entrants.")
    for size in SUPPORTED_BRACKET_SIZES:
        if count <= size:
            return size
    raise PoolBracketValidationError(
        "This pool bracket supports a maximum of 256 entrants or tournament lives."
    )


def _model_has_field(model, field_name: str) -> bool:
    """Return whether a staged model migration has supplied ``field_name``.

    The life-aware bracket engine is deliberately compatible with historical
    brackets.  Keeping these checks local also lets an ordinary, already
    migrated deployment retain its entry-only behaviour while a deployment
    rolls out the accompanying nullable life fields.
    """

    try:
        model._meta.get_field(field_name)
    except Exception:  # pragma: no cover - defensive during staged deploys.
        if field_name.endswith("_id"):
            try:
                model._meta.get_field(field_name[:-3])
            except Exception:  # pragma: no cover - defensive during staged deploys.
                return False
        else:
            return False
    return True


def _life_model():
    """Resolve the optional tournament-life model only when it is needed."""

    try:
        return apps.get_model("pool", "PoolCompetitionEntryLife")
    except LookupError as exc:
        raise PoolBracketValidationError(
            "Tournament lives are not available until the pool life migration is applied."
        ) from exc


def _two_lives_enabled(competition: PoolCompetition) -> bool:
    """Whether this is the narrowly supported direct singles knockout mode."""

    return bool(
        getattr(competition, "allow_two_lives", False)
        and competition.participant_mode == PoolCompetition.ParticipantMode.SINGLES
        and competition.format_type == PoolCompetition.FormatType.KNOCKOUT
    )


def _life_field_kwargs(model, **values):
    """Only send nullable life fields once their migration exists."""

    return {
        field_name: value
        for field_name, value in values.items()
        if _model_has_field(model, field_name)
    }


def _slot_entry_life_id(slot):
    return getattr(slot, "entry_life_id", None)


def _slot_source_entry_life_id(slot):
    return getattr(slot, "source_entry_life_id", None)


def _tie_resolved_winner_life_id(tie):
    return getattr(tie, "resolved_winner_life_id", None)


def _match_life_id(pool_match, side: str):
    return getattr(pool_match, f"{side}_entry_life_id", None)


def _preview_life_draw_order(preview):
    """Return a list for life-aware previews, preserving old JSON rows."""

    value = getattr(preview, "life_draw_order", None)
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise PoolBracketValidationError(
            "The saved life-aware draw order is invalid. Create a new draw preview."
        )
    return list(value)


def _bracket_life_draw_order(bracket):
    value = getattr(bracket, "life_draw_order", None)
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise PoolBracketValidationError("The saved bracket life order is invalid.")
    return list(value)


def _competition_life_map(competition: PoolCompetition, *, lock=False):
    """Return every registered life for a direct two-life competition.

    A life is bracket metadata, not a replacement competition entry.  The
    returned map is keyed by life primary key while every row remains tied to
    its real ``PoolCompetitionEntry`` for fixtures and statistics.
    """

    Life = _life_model()
    queryset = Life.objects.filter(
        competition_entry__competition=competition
    ).select_related("competition_entry", "competition_entry__player")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    lives = list(queryset.order_by("competition_entry_id", "life_number", "pk"))
    entries = _entry_map(competition)
    by_entry = defaultdict(list)
    for life in lives:
        entry_id = life.competition_entry_id
        if entry_id not in entries:
            raise PoolBracketValidationError(
                "A tournament life belongs to an entry outside this competition."
            )
        if getattr(life, "life_number", None) not in {1, 2}:
            raise PoolBracketValidationError(
                "Tournament lives must be numbered 1 or 2."
            )
        by_entry[entry_id].append(life)

    missing_entries = [entry.display_name for entry_id, entry in entries.items() if not by_entry[entry_id]]
    if missing_entries:
        raise PoolBracketValidationError(
            "Every competition entry needs Life 1 before a two-life draw can be created."
        )
    if any(len(entry_lives) > 2 for entry_lives in by_entry.values()):
        raise PoolBracketValidationError(
            "A competition entry cannot have more than two tournament lives."
        )
    entries_without_life_one = [
        entries[entry_id].display_name
        for entry_id, entry_lives in by_entry.items()
        if 1 not in {life.life_number for life in entry_lives}
    ]
    if entries_without_life_one:
        raise PoolBracketValidationError(
            "Every competition entry must retain Life 1 before a two-life draw "
            "can be created."
        )
    return {life.pk: life for life in lives}


def _life_draw_entry_order(life_draw_order, lives):
    """Materialise a legacy-friendly entry order from a life order."""

    return [
        lives[life_id].competition_entry_id if life_id is not None else None
        for life_id in life_draw_order
    ]


def _required_life_bracket_size(lives):
    """Return the smallest supported topology that keeps life paths separate.

    A two-life draw is not merely a normal bracket with each player's siblings
    separated.  Its upper tree is the Life 1 path and its lower tree is the
    Life 2 path.  Consequently each half must have enough structural capacity
    for its own life number, even when the combined number of lives would fit
    in a smaller bracket.
    """

    life_one_count = sum(
        1 for life in lives.values() if getattr(life, "life_number", None) == 1
    )
    life_two_count = sum(
        1 for life in lives.values() if getattr(life, "life_number", None) == 2
    )
    required_slots = max(len(lives), life_one_count * 2, life_two_count * 2)
    return _next_supported_size(required_slots)


def _validate_life_bracket_size(bracket_size, lives):
    """Require the exact supported size needed by the two isolated halves."""

    if bracket_size not in SUPPORTED_BRACKET_SIZES:
        raise PoolBracketValidationError(
            "Bracket size must be 2, 4, 8, 16, 32, 64, 128, or 256."
        )
    if len(lives) < 2:
        raise PoolBracketValidationError("A bracket needs at least two tournament lives.")
    required_size = _required_life_bracket_size(lives)
    if bracket_size != required_size:
        raise PoolBracketValidationError(
            "Use the supported bracket size required to keep Life 1 and Life 2 "
            "in separate halves."
        )


def _validate_opposite_life_halves(life_draw_order, lives, bracket_size):
    """Enforce Life 1 in the upper half and Life 2 in the lower half.

    The historical sibling-only check was too weak: it allowed one player's
    Life 1 to meet another player's Life 2 before the final.  This central
    validator deliberately checks persisted draw order as well as preview
    input, so no draw method can reintroduce mixed life paths.
    """

    half_boundary = bracket_size // 2
    for position, life_id in enumerate(life_draw_order, start=1):
        if life_id is None:
            continue
        life = lives.get(life_id)
        if life is None:
            raise PoolBracketValidationError(
                "Life-aware draw slots must use tournament lives from this competition only."
            )
        expected_life_number = 1 if position <= half_boundary else 2
        if life.life_number != expected_life_number:
            half_label = "top" if expected_life_number == 1 else "bottom"
            raise PoolBracketValidationError(
                f"{half_label.capitalize()}-half bracket slots may contain only "
                f"Life {expected_life_number}."
            )


def _validate_life_draw_order(
    life_draw_order,
    *,
    lives,
    bracket_size,
    require_all_lives=True,
):
    """Validate an explicit life-aware slot order at preview and confirmation."""

    if len(life_draw_order) != bracket_size:
        raise PoolBracketValidationError(
            "Life-aware draws must provide every selected bracket slot."
        )
    try:
        normalized = [
            int(getattr(life_id, "pk", life_id)) if life_id is not None else None
            for life_id in life_draw_order
        ]
    except (TypeError, ValueError) as exc:
        raise PoolBracketValidationError(
            "Life-aware draw slots must contain valid tournament lives."
        ) from exc
    selected = [life_id for life_id in normalized if life_id is not None]
    if any(life_id not in lives for life_id in selected):
        raise PoolBracketValidationError(
            "Life-aware draw slots must use tournament lives from this competition only."
        )
    if len(selected) != len(set(selected)):
        raise PoolBracketValidationError(
            "The same tournament life may appear in only one bracket slot."
        )
    _validate_life_bracket_size(bracket_size, lives)
    if require_all_lives and set(selected) != set(lives):
        raise PoolBracketValidationError(
            "Life-aware draws must place every active tournament life exactly once."
        )
    if any(
        normalized[index] is None and normalized[index + 1] is None
        for index in range(0, bracket_size, 2)
    ):
        raise PoolBracketValidationError(
            "Life-aware draws cannot leave both slots of a first-round tie empty."
        )
    _validate_opposite_life_halves(normalized, lives, bracket_size)
    return normalized


def _life_half_draw_order(bracket_size, *, life_one_ids, life_two_ids):
    """Place two already ordered life-number lists inside their legal halves."""

    half_size = bracket_size // 2
    minimum_per_half = half_size // 2
    if len(life_one_ids) > half_size or len(life_two_ids) > half_size:
        raise PoolBracketValidationError(
            "Tournament lives cannot fit into their required bracket halves."
        )
    # The explicit bracket graph cannot safely represent an entirely empty
    # first-round tie.  Failing closed here is preferable to creating a lower
    # life path that can never resolve to the final.
    if len(life_one_ids) < minimum_per_half or len(life_two_ids) < minimum_per_half:
        raise PoolBracketValidationError(
            "Tournament lives cannot form separate Life 1 and Life 2 paths "
            "without an empty first-round tie."
        )
    return _fair_bye_slots(half_size, life_one_ids) + _fair_bye_slots(
        half_size, life_two_ids
    )


def _random_life_draw_order(bracket_size, lives):
    """Randomise Life 1 and Life 2 independently inside fixed bracket halves."""

    life_one_ids = [
        life.pk for life in lives.values() if getattr(life, "life_number", None) == 1
    ]
    life_two_ids = [
        life.pk for life in lives.values() if getattr(life, "life_number", None) == 2
    ]
    rng = random.SystemRandom()
    rng.shuffle(life_one_ids)
    rng.shuffle(life_two_ids)
    return _life_half_draw_order(
        bracket_size,
        life_one_ids=life_one_ids,
        life_two_ids=life_two_ids,
    )


def _seeded_life_draw_order(bracket_size, lives, *, handoff=False):
    """Apply the normal seed order independently to the two legal life paths."""

    entries_by_id = {
        life.competition_entry_id: life.competition_entry for life in lives.values()
    }
    order_entries = _handoff_seeded_order if handoff else _seeded_order
    ordered_entries = order_entries(list(entries_by_id.values()))
    lives_by_entry_and_number = {
        (life.competition_entry_id, life.life_number): life.pk
        for life in lives.values()
    }
    life_one_ids = [
        lives_by_entry_and_number[(entry.pk, 1)]
        for entry in ordered_entries
        if (entry.pk, 1) in lives_by_entry_and_number
    ]
    life_two_ids = [
        lives_by_entry_and_number[(entry.pk, 2)]
        for entry in ordered_entries
        if (entry.pk, 2) in lives_by_entry_and_number
    ]
    return _life_half_draw_order(
        bracket_size,
        life_one_ids=life_one_ids,
        life_two_ids=life_two_ids,
    )


def _locked_competition(competition: PoolCompetition) -> PoolCompetition:
    if not isinstance(competition, PoolCompetition) or not competition.pk:
        raise PoolBracketValidationError("A saved pool competition is required.")
    return PoolCompetition.objects.select_for_update().get(pk=competition.pk)


def _locked_stage(competition: PoolCompetition, stage) -> PoolCompetitionStage:
    stage_id = getattr(stage, "pk", stage)
    try:
        stage = PoolCompetitionStage.objects.select_for_update().get(
            pk=stage_id,
            competition=competition,
            stage_type=PoolCompetitionStage.StageType.KNOCKOUT,
        )
    except PoolCompetitionStage.DoesNotExist as exc:
        raise PoolBracketValidationError(
            "Choose a knockout stage from this competition."
        ) from exc
    return stage


def _handoff_round_field(competition, stage, source_round, *, lock=True):
    """Return the exact, authoritative survivor field for a manual round.

    Manual knockout rounds intentionally have no bracket ties, so the only
    trustworthy handoff input is their completed fixture history.  This helper
    is intentionally conservative: a partially played round, a correction
    window, a legacy fixture without a configured round, or a later manual
    fixture is ambiguous and must be resolved by an organiser before a new
    bracket can take ownership of the remaining field.
    """

    source_round_id = getattr(source_round, "pk", source_round)
    queryset = PoolKnockoutRoundDefinition.objects.select_related("stage")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    try:
        source_round = queryset.get(
            pk=source_round_id,
            stage=stage,
            stage__competition=competition,
        )
    except PoolKnockoutRoundDefinition.DoesNotExist as exc:
        raise PoolBracketValidationError(
            "Choose a configured knockout round from this competition stage."
        ) from exc

    definitions = list(
        PoolKnockoutRoundDefinition.objects.filter(
            stage=stage,
            order__lte=source_round.order,
        ).order_by("order", "id")
    )
    matches_queryset = PoolMatch.objects.filter(stage=stage).select_related(
        "knockout_round",
        "home_entry",
        "away_entry",
        "winner",
        "home_entry_life",
        "away_entry_life",
        "winner_entry_life",
    )
    if lock:
        matches_queryset = matches_queryset.select_for_update(of=("self",))
    stage_matches = list(matches_queryset.order_by("knockout_round__order", "pk"))
    if any(not match.knockout_round_id for match in stage_matches):
        raise PoolBracketValidationError(
            "Every existing knockout fixture must use a configured round before a bracket handoff."
        )
    if any(
        match.knockout_round.order > source_round.order for match in stage_matches
    ):
        raise PoolBracketValidationError(
            "A later manual knockout fixture already exists, so the remaining field is not safe to hand off."
        )

    matches_by_round = defaultdict(list)
    for match in stage_matches:
        matches_by_round[match.knockout_round_id].append(match)

    life_aware = _two_lives_enabled(competition)
    previous_winners = None
    source_matches = matches_by_round.get(source_round.pk, [])
    if len(source_matches) != source_round.round_size // 2:
        raise PoolBracketValidationError(
            f"{source_round.name} must contain all {source_round.round_size // 2} finished fixtures before a bracket handoff."
        )

    completed_match_ids = []
    for definition in definitions:
        round_matches = matches_by_round.get(definition.pk, [])
        if not round_matches:
            # Unused structural definitions before the first actual manual
            # round are allowed.  A gap after completed manual play would make
            # the source field impossible to verify, so fail closed.
            if previous_winners is not None and definition.order < source_round.order:
                raise PoolBracketValidationError(
                    "Manual knockout rounds contain a gap before the selected handoff round."
                )
            continue
        if len(round_matches) != definition.round_size // 2:
            raise PoolBracketValidationError(
                f"{definition.name} is incomplete and cannot supply a safe handoff field."
            )

        participant_ids = []
        winner_ids = []
        for match in round_matches:
            if (
                match.status != PoolMatch.Status.FINISHED
                or match.is_in_correction_mode
                or not match.winner_id
            ):
                raise PoolBracketValidationError(
                    f"Finish and lock every {definition.name} result before handing off to a bracket."
                )
            if life_aware:
                life_ids = (match.home_entry_life_id, match.away_entry_life_id)
                if not all(life_ids) or not match.winner_entry_life_id:
                    raise PoolBracketValidationError(
                        "Two-life manual fixtures need both participant lives and the winning life before handoff."
                    )
                if match.winner_entry_life_id not in life_ids:
                    raise PoolBracketValidationError(
                        "A manual fixture's winning tournament life must be one of its two participants."
                    )
                participant_ids.extend(life_ids)
                winner_ids.append(match.winner_entry_life_id)
            else:
                participant_ids.extend((match.home_entry_id, match.away_entry_id))
                winner_ids.append(match.winner_id)
            completed_match_ids.append(match.pk)

        if len(participant_ids) != len(set(participant_ids)):
            raise PoolBracketValidationError(
                f"{definition.name} contains a duplicate participant identity."
            )
        if len(winner_ids) != len(set(winner_ids)):
            raise PoolBracketValidationError(
                f"{definition.name} contains a duplicate surviving participant."
            )
        if previous_winners is not None and set(participant_ids) != previous_winners:
            raise PoolBracketValidationError(
                f"{definition.name} does not contain exactly the winners of the preceding manual round."
            )
        previous_winners = set(winner_ids)

    if previous_winners is None or len(previous_winners) < 2:
        raise PoolBracketValidationError(
            "At least two unambiguous manual-round survivors are required for a bracket handoff."
        )

    if life_aware:
        all_lives = _competition_life_map(competition, lock=lock)
        lives = {
            life_id: all_lives[life_id]
            for life_id in previous_winners
            if life_id in all_lives
        }
        if len(lives) != len(previous_winners):
            raise PoolBracketValidationError(
                "A manual-round survivor is not a valid tournament life in this competition."
            )
        bracket_size = _required_life_bracket_size(lives)
        # This also checks that each isolated half can be represented without
        # an empty first-round tie in the persistent topology.
        _life_half_draw_order(
            bracket_size,
            life_one_ids=[
                life_id for life_id, life in lives.items() if life.life_number == 1
            ],
            life_two_ids=[
                life_id for life_id, life in lives.items() if life.life_number == 2
            ],
        )
        return {
            "source_round": source_round,
            "entry_ids": tuple(
                sorted({life.competition_entry_id for life in lives.values()})
            ),
            "life_ids": tuple(sorted(lives)),
            "match_ids": tuple(sorted(completed_match_ids)),
            "bracket_size": bracket_size,
        }

    entries = _entry_map(competition)
    survivor_ids = set(previous_winners)
    if not survivor_ids.issubset(entries):
        raise PoolBracketValidationError(
            "A manual-round survivor is not an entry in this competition."
        )
    return {
        "source_round": source_round,
        "entry_ids": tuple(sorted(survivor_ids)),
        "life_ids": (),
        "match_ids": tuple(sorted(completed_match_ids)),
        "bracket_size": _next_supported_size(len(survivor_ids)),
    }


@transaction.atomic
def handoff_field_for_round(competition, *, stage, source_round):
    """Read the safe survivor field used by a manual-round bracket handoff."""

    competition = _locked_competition(competition)
    stage = _locked_stage(competition, stage)
    return _handoff_round_field(competition, stage, source_round, lock=True)


def lock_bracket_dependencies_for_match(match) -> tuple[int, ...]:
    """Lock every bracket structurally dependent on a match before its row.

    Pool result services deliberately take locks in this order:

    ``bracket(s) -> pool match -> tie/slot/rack``.

    A group fixture can feed more than one generated qualification bracket,
    while a knockout fixture feeds exactly its own bracket.  Locking those
    bracket roots in primary-key order prevents a source correction from
    racing a downstream fixture start/completion and gives every lifecycle
    service the same transaction order.  The initial lookup is advisory only;
    once the bracket rows are locked, the caller locks and re-reads its match.
    """

    match_id = getattr(match, "pk", match)
    if not match_id:
        return ()
    match_scope = PoolMatch.objects.filter(pk=match_id).values(
        "stage_id", "knockout_round_id"
    ).first()
    if not match_scope:
        return ()
    bracket_ids = set(
        PoolKnockoutTie.objects.filter(match_id=match_id).values_list(
            "bracket_id", flat=True
        )
    )
    stage_id = match_scope["stage_id"]
    if stage_id:
        bracket_ids.update(
            PoolKnockoutTieSlot.objects.filter(
                source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
                source_group__stage_id=stage_id,
            ).values_list("tie__bracket_id", flat=True)
        )
    # A generated bracket can deliberately take over only after a completed
    # manual round. Its source history is then an immutable dependency just
    # like a bracket tie source, so lock that handoff bracket before a source
    # match operation can attempt a correction or deletion.
    if match_scope["knockout_round_id"]:
        source_round = PoolKnockoutRoundDefinition.objects.filter(
            pk=match_scope["knockout_round_id"]
        ).values("stage_id", "order").first()
        if source_round:
            bracket_ids.update(
                PoolKnockoutBracket.objects.filter(
                    handoff_from_round__stage_id=source_round["stage_id"],
                    handoff_from_round__order__gte=source_round["order"],
                ).values_list("pk", flat=True)
            )
    ids = tuple(sorted(bracket_id for bracket_id in bracket_ids if bracket_id))
    if ids:
        list(
            PoolKnockoutBracket.objects.select_for_update(of=("self",))
            .filter(pk__in=ids)
            .order_by("pk")
        )
    return ids


def _audit(bracket, *, action, actor=None, message="", payload=None):
    return PoolBracketAuditEvent.objects.create(
        bracket=bracket,
        competition_id=bracket.competition_id,
        stage_id=bracket.stage_id,
        actor=actor,
        action=action,
        message=message,
        payload=payload or {},
    )


def _entry_map(competition: PoolCompetition):
    return {
        entry.pk: entry
        for entry in competition.entries.select_related("player", "team").order_by(
            "seed", "id"
        )
    }


def _validate_draw_size(size: int, entrant_count: int, *, manual=False):
    if size not in SUPPORTED_BRACKET_SIZES:
        raise PoolBracketValidationError(
            "Bracket size must be 2, 4, 8, 16, 32, 64, 128, or 256."
        )
    if entrant_count < 2:
        raise PoolBracketValidationError("A bracket needs at least two entrants.")
    if entrant_count > size:
        raise PoolBracketValidationError(
            "The selected bracket size does not have enough slots for these entrants."
        )
    if size != _next_supported_size(entrant_count):
        raise PoolBracketValidationError(
            "Use the next supported bracket size for the current entrant count."
        )


def _seeded_order(entries):
    seen = set()
    missing = []
    by_seed = {}
    for entry in entries:
        if entry.seed is None:
            missing.append(entry.display_name)
            continue
        if entry.seed in seen:
            raise PoolBracketValidationError(
                "Seeded draw requires each entrant to have a unique seed position."
            )
        seen.add(entry.seed)
        by_seed[entry.seed] = entry
    if missing:
        raise PoolBracketValidationError(
            "Seeded draw requires a seed position for every selected entrant."
        )
    expected = set(range(1, len(entries) + 1))
    if set(by_seed) != expected:
        raise PoolBracketValidationError(
            "Seeded draw positions must run consecutively from 1."
        )
    return [by_seed[position] for position in sorted(by_seed)]


def _handoff_seeded_order(entries):
    """Order manual-round survivors by their original seed without renumbering.

    Winners from a Round of 32 legitimately retain seed values such as 1, 9,
    17, and 25. Requiring that reduced field to be reseeded 1..4 would make a
    safe handoff fail and would invite a rewrite of historical seeding. Normal
    initial draws continue to use the strict consecutive-seed helper above.
    """

    entries = list(entries)
    missing = [entry.display_name for entry in entries if entry.seed is None]
    if missing:
        raise PoolBracketValidationError(
            "Seeded handoff draws require a seed position for every surviving entrant."
        )
    seeds = [entry.seed for entry in entries]
    if len(seeds) != len(set(seeds)):
        raise PoolBracketValidationError(
            "Seeded handoff draws require unique surviving entrant seeds."
        )
    return sorted(entries, key=lambda entry: (entry.seed, entry.pk))


def _fair_bye_slots(size: int, entrant_ids: list[int]) -> list[int | None]:
    """Place entrants in alternating first-round pair positions where possible.

    The deterministic pattern makes every bye visible and prevents two byes
    meeting.  In seeded mode the leading seeds occupy the early odd positions,
    so they receive available byes predictably rather than randomly.
    """
    slots: list[int | None] = [None] * size
    pair_count = size // 2

    # Put the first entrant in every first-round tie before filling the other
    # side of a tie.  This means that when byes are needed, higher seeds (or
    # the first entries in a random draw) receive them predictably.  Remaining
    # entrants are paired from the lower end, so the final occupied ties are
    # competitive rather than creating a hidden double bye.
    for index, entry_id in enumerate(entrant_ids[:pair_count]):
        slots[index * 2] = entry_id
    for index, entry_id in enumerate(entrant_ids[pair_count:]):
        slots[(pair_count - 1 - index) * 2 + 1] = entry_id
    return slots


def _qualification_order(competition, stage, size):
    rules = list(
        PoolGroupQualificationRule.objects.select_related("group", "source_stage")
        .filter(knockout_stage=stage, source_stage__competition=competition)
        .order_by("destination_slot", "id")
    )
    if not rules:
        raise PoolBracketValidationError(
            "Configure group qualification rules before creating this bracket."
        )
    destinations = [rule.destination_slot for rule in rules]
    if len(destinations) != len(set(destinations)) or any(
        destination < 1 or destination > size for destination in destinations
    ):
        raise PoolBracketValidationError(
            "Qualification destinations must be unique valid bracket slots."
        )
    if len(rules) > size:
        raise PoolBracketValidationError(
            "Qualification rules exceed the selected bracket size."
        )
    _validate_group_separation_preference(rules, size=size)
    _assert_group_stage_ready(rules)
    return rules


def _validate_group_separation_preference(rules, *, size):
    """Honour explicit same-group separation preferences without redrawing.

    Qualification rules deliberately own exact destination slots.  The
    service must therefore never silently swap organisers' mappings at draw
    confirmation.  Where a selected preference can plainly be honoured by
    putting another qualifying group in the paired first-round slot, reject
    the conflicting mapping with an actionable message instead.
    """
    rules_by_destination = {rule.destination_slot: rule for rule in rules}
    preferred_group_ids = {
        rule.group_id for rule in rules if rule.keep_groups_apart
    }
    if len({rule.group_id for rule in rules}) < 2 or not preferred_group_ids:
        return
    group_counts = Counter(rule.group_id for rule in rules)

    for first_slot in range(1, max(rules_by_destination) + 1, 2):
        left = rules_by_destination.get(first_slot)
        right = rules_by_destination.get(first_slot + 1)
        if not left or not right or left.group_id != right.group_id:
            continue
        if left.group_id not in preferred_group_ids:
            continue
        # This is feasible when enough other qualified entrants *or bracket
        # byes* exist to pair every qualifier from the group externally.  A
        # dominant group (for example three Group A qualifiers and one Group
        # B qualifier in a four-slot draw) cannot avoid a same-group meeting,
        # so its explicit mapping remains valid as required by "where
        # possible".
        if group_counts[left.group_id] <= size - group_counts[left.group_id]:
            raise PoolBracketValidationError(
                f"{left.group.name} is paired with itself in first-round slots "
                f"{first_slot} and {first_slot + 1}. Move one qualifier to a "
                "different destination slot or turn off its separation preference."
            )


def _assert_group_stage_ready(rules):
    by_stage = defaultdict(list)
    for rule in rules:
        by_stage[rule.source_stage_id].append(rule)
    terminal_non_counting = []
    for source_stage_id, source_rules in by_stage.items():
        source_stage = source_rules[0].source_stage
        pending = PoolMatch.objects.filter(
            stage=source_stage,
            status__in=(
                PoolMatch.Status.UPCOMING,
                PoolMatch.Status.LIVE,
                PoolMatch.Status.POSTPONED,
            ),
        )
        if pending.exists():
            raise PoolBracketValidationError(
                "Group qualification is blocked while required group matches are upcoming, live, or postponed."
            )
        terminal_non_counting.extend(
            PoolMatch.objects.filter(
                stage=source_stage,
                status__in=(PoolMatch.Status.CANCELLED, PoolMatch.Status.ABANDONED),
            ).values_list("pk", flat=True)
        )
        for rule in source_rules:
            entry_count = rule.group.memberships.count()
            if rule.group.qualifiers > entry_count:
                raise PoolBracketValidationError(
                    f"{rule.group.name} is configured to qualify {rule.group.qualifiers} "
                    f"participants but only has {entry_count} valid entries."
                )
            if rule.position > entry_count:
                raise PoolBracketValidationError(
                    f"{rule.group.name} does not have a participant at position {rule.position}."
                )
        groups = list(source_stage.groups.order_by("order", "id"))
        for group in groups:
            configured_positions = sorted(
                rule.position for rule in source_rules if rule.group_id == group.pk
            )
            expected_positions = list(range(1, group.qualifiers + 1))
            if configured_positions != expected_positions:
                raise PoolBracketValidationError(
                    f"Configure {group.name} qualification positions 1 through "
                    f"{group.qualifiers} exactly once before generating the bracket."
                )
    if terminal_non_counting:
        raise PoolBracketValidationError(
            "Group qualification is blocked because cancelled or unresolved abandoned group matches need an organiser decision."
        )


def _resolve_qualification_entries(rules):
    # Several positions commonly come from the same group.  Standings are
    # intentionally calculated on demand, but there is no reason to rebuild
    # the same group table once for every one of its qualification rules in a
    # single preview/confirmation request.
    standings_by_group = {}
    resolved = {}
    for rule in rules:
        rows = standings_by_group.get(rule.group_id)
        if rows is None:
            rows = standings_for_stage(rule.source_stage, group=rule.group)
            standings_by_group[rule.group_id] = rows
        if rule.position > len(rows):
            raise PoolBracketValidationError(
                f"{rule.group.name} has no entry at qualification position {rule.position}."
            )
        resolved[rule.destination_slot] = rows[rule.position - 1].entry_id
    if len(set(resolved.values())) != len(resolved):
        raise PoolBracketValidationError(
            "A participant cannot occupy more than one qualifying bracket slot."
        )
    return resolved


def _create_draw_preview(
    competition: PoolCompetition,
    *,
    stage,
    draw_method,
    bracket_size,
    manual_entry_ids=None,
    manual_life_ids=None,
    third_place_enabled=False,
    round_race_targets=None,
    actor=None,
    _allow_random=False,
    _handoff_field=None,
) -> PoolBracketDrawPreview:
    """Build a persisted preview while the caller owns its transaction.

    ``RANDOM`` is deliberately private to the one-shot confirmation path.
    A normal preview is shown to an organiser before it becomes official, so
    allowing a random order here would make it possible to discard a draw and
    try again.  The private flag is intentionally not exposed by the public
    preview service below.
    """
    competition = _locked_competition(competition)
    stage = _locked_stage(competition, stage)
    if PoolKnockoutBracket.objects.filter(stage=stage).exists():
        if draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM:
            raise PoolBracketValidationError(
                "This random draw has already been confirmed and cannot be drawn again."
            )
        raise PoolBracketValidationError("This knockout stage already has a bracket.")
    if _handoff_field is None and PoolMatch.objects.filter(stage=stage).exists():
        raise PoolBracketValidationError(
            "This knockout stage already has fixtures. Remove the untouched fixtures before generating its bracket."
        )
    if _handoff_field is not None:
        source_round = _handoff_field.get("source_round")
        if not source_round or source_round.stage_id != stage.pk:
            raise PoolBracketValidationError(
                "The manual-round handoff source must belong to this knockout stage."
            )
        if draw_method == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION:
            raise PoolBracketValidationError(
                "Group qualification cannot be combined with a manual-round bracket handoff."
            )
    if draw_method not in PoolBracketDrawPreview.DrawMethod.values:
        raise PoolBracketValidationError("Choose a valid pool bracket draw method.")
    if (
        draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM
        and not _allow_random
    ):
        raise PoolBracketValidationError(
            "Random draws are confirmed once without a preview. "
            "Confirm the random draw to generate the bracket."
        )
    try:
        bracket_size = int(bracket_size)
    except (TypeError, ValueError) as exc:
        raise PoolBracketValidationError("Choose a valid bracket size.") from exc
    if third_place_enabled and bracket_size < 4:
        raise PoolBracketValidationError(
            "A third-place match requires a bracket with at least four entrants."
        )

    entries = _entry_map(competition)
    if _handoff_field is not None:
        expected_entry_ids = set(_handoff_field["entry_ids"])
        entries = {
            entry_id: entry
            for entry_id, entry in entries.items()
            if entry_id in expected_entry_ids
        }
        if set(entries) != expected_entry_ids:
            raise PoolBracketValidationError(
                "The manual-round survivor field changed before the handoff preview was created."
            )
    life_enabled = _two_lives_enabled(competition)
    life_draw_order = []
    if life_enabled and not _model_has_field(
        PoolBracketDrawPreview, "life_draw_order"
    ):
        raise PoolBracketValidationError(
            "Tournament lives are not available until the pool life migration is applied."
        )
    if life_enabled and draw_method == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION:
        raise PoolBracketValidationError(
            "Group qualification draws do not support tournament lives."
        )

    draw_order: list[int | None]
    if life_enabled:
        lives = _competition_life_map(competition, lock=True)
        if _handoff_field is not None:
            expected_life_ids = set(_handoff_field["life_ids"])
            lives = {
                life_id: life
                for life_id, life in lives.items()
                if life_id in expected_life_ids
            }
            if set(lives) != expected_life_ids:
                raise PoolBracketValidationError(
                    "The manual-round tournament-life field changed before the handoff preview was created."
                )
        _validate_life_bracket_size(bracket_size, lives)
        if draw_method == PoolBracketDrawPreview.DrawMethod.MANUAL:
            # ``manual_life_ids`` is explicit for new callers.  The fallback
            # keeps a transitional manager route usable while it switches its
            # slot picker from entries to tournament lives.
            supplied = (
                manual_life_ids
                if manual_life_ids is not None
                else manual_entry_ids
            )
            if supplied is None:
                raise PoolBracketValidationError(
                    "Select a tournament life for every occupied manual draw slot."
                )
            life_draw_order = _validate_life_draw_order(
                list(supplied),
                lives=lives,
                bracket_size=bracket_size,
            )
        elif draw_method == PoolBracketDrawPreview.DrawMethod.SEEDED:
            life_draw_order = _seeded_life_draw_order(
                bracket_size,
                lives,
                handoff=_handoff_field is not None,
            )
            life_draw_order = _validate_life_draw_order(
                life_draw_order,
                lives=lives,
                bracket_size=bracket_size,
            )
        else:
            life_draw_order = _random_life_draw_order(bracket_size, lives)
            life_draw_order = _validate_life_draw_order(
                life_draw_order,
                lives=lives,
                bracket_size=bracket_size,
            )
        draw_order = _life_draw_entry_order(life_draw_order, lives)
    elif draw_method == PoolBracketDrawPreview.DrawMethod.MANUAL:
        supplied = list(manual_entry_ids or [])
        if len(supplied) != bracket_size:
            raise PoolBracketValidationError(
                "Manual draws must provide every selected bracket slot."
            )
        try:
            normalized = [
                int(entry_id) if entry_id is not None else None
                for entry_id in supplied
            ]
        except (TypeError, ValueError) as exc:
            raise PoolBracketValidationError(
                "Manual draw slots must contain valid competition entries."
            ) from exc
        selected = [entry_id for entry_id in normalized if entry_id is not None]
        if any(entry_id not in entries for entry_id in selected):
            raise PoolBracketValidationError(
                "Manual draw slots must use entries from this competition only."
            )
        if len(selected) != len(set(selected)):
            raise PoolBracketValidationError(
                "An entrant may appear in only one manual bracket slot."
            )
        if set(selected) != set(entries):
            raise PoolBracketValidationError(
                "Manual draws must place every entry in this competition exactly once."
            )
        _validate_draw_size(bracket_size, len(selected))
        # A first round containing no player in both positions would result in
        # an invisible double bye rather than a competitive topology.
        if any(
            normalized[index] is None and normalized[index + 1] is None
            for index in range(0, bracket_size, 2)
        ):
            raise PoolBracketValidationError(
                "Manual draws cannot leave both slots of a first-round tie empty."
            )
        draw_order = normalized
    elif draw_method == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION:
        rules = _qualification_order(competition, stage, bracket_size)
        resolved = _resolve_qualification_entries(rules)
        _validate_draw_size(bracket_size, len(resolved))
        draw_order = [resolved.get(slot) for slot in range(1, bracket_size + 1)]
        if any(
            draw_order[index] is None and draw_order[index + 1] is None
            for index in range(0, bracket_size, 2)
        ):
            raise PoolBracketValidationError(
                "Qualification mappings cannot leave an empty first-round tie."
            )
    else:
        selected_entries = list(entries.values())
        _validate_draw_size(bracket_size, len(selected_entries))
        if draw_method == PoolBracketDrawPreview.DrawMethod.SEEDED:
            ordered = (
                _handoff_seeded_order(selected_entries)
                if _handoff_field is not None
                else _seeded_order(selected_entries)
            )
        else:
            ordered = list(selected_entries)
            random.SystemRandom().shuffle(ordered)
        draw_order = _fair_bye_slots(bracket_size, [entry.pk for entry in ordered])

    targets = {str(key): int(value) for key, value in (round_race_targets or {}).items()}
    rounds = _round_count(bracket_size)
    for round_number in range(1, rounds + 1):
        targets.setdefault(str(round_number), competition.default_race_target)
        if targets[str(round_number)] < 1:
            raise PoolBracketValidationError("Round race targets must be at least 1.")
    if third_place_enabled:
        targets.setdefault("third_place", competition.default_race_target)
        if targets["third_place"] < 1:
            raise PoolBracketValidationError("Third-place race target must be at least 1.")

    preview_kwargs = {
        "competition": competition,
        "stage": stage,
        "draw_method": draw_method,
        "bracket_size": bracket_size,
        "third_place_enabled": bool(third_place_enabled),
        "draw_order": draw_order,
        "round_race_targets": targets,
        "created_by": actor,
    }
    if _handoff_field is not None:
        preview_kwargs["handoff_from_round"] = _handoff_field["source_round"]
    preview_kwargs.update(
        _life_field_kwargs(
            PoolBracketDrawPreview,
            life_draw_order=life_draw_order,
        )
    )
    preview = PoolBracketDrawPreview.objects.create(
        **preview_kwargs,
    )
    # Preview has no bracket yet. Its persisted draw order is sufficient to
    # reproduce confirmation and forms part of the generated bracket audit.
    return preview


@transaction.atomic
def create_draw_preview(
    competition: PoolCompetition,
    *,
    stage,
    draw_method,
    bracket_size,
    manual_entry_ids=None,
    manual_life_ids=None,
    third_place_enabled=False,
    round_race_targets=None,
    actor=None,
) -> PoolBracketDrawPreview:
    """Persist a reviewable manual, seeded, or qualification draw preview.

    Random orders are intentionally excluded: their first reveal must be the
    already-persisted official bracket, not a discardable preview.
    """
    return _create_draw_preview(
        competition,
        stage=stage,
        draw_method=draw_method,
        bracket_size=bracket_size,
        manual_entry_ids=manual_entry_ids,
        manual_life_ids=manual_life_ids,
        third_place_enabled=third_place_enabled,
        round_race_targets=round_race_targets,
        actor=actor,
    )


def _slot_source_for_first_round(
    bracket,
    stage,
    position,
    entry_id,
    draw_method,
    *,
    entry_life_id=None,
    qualification_rules_by_slot=None,
):
    if draw_method == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION:
        # Confirmation has already loaded and validated every qualification
        # rule.  Reusing that mapping avoids one remote query per first-round
        # slot.  Keep the fallback for direct/private callers of this helper.
        if qualification_rules_by_slot is None:
            rule = PoolGroupQualificationRule.objects.filter(
                knockout_stage=stage,
                destination_slot=position,
            ).select_related("group").first()
        else:
            rule = qualification_rules_by_slot.get(position)
        if rule is not None:
            return {
                "source_type": PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
                "source_group": rule.group,
                "source_position": rule.position,
                "entry_id": entry_id,
            }
    if entry_id is None:
        return {
            "source_type": PoolKnockoutTieSlot.SourceType.BYE,
            "entry_id": None,
        }
    source = {
        "source_type": PoolKnockoutTieSlot.SourceType.DIRECT_ENTRY,
        "source_entry_id": entry_id,
        "entry_id": entry_id,
    }
    source.update(
        _life_field_kwargs(
            PoolKnockoutTieSlot,
            entry_life_id=entry_life_id,
            source_entry_life_id=entry_life_id,
        )
    )
    return source


def _tie_slot_select_related_relations():
    relations = [
        "entry__player",
        "entry__team",
        "source_entry",
        "source_group",
        "source_tie",
    ]
    if _model_has_field(PoolKnockoutTieSlot, "entry_life"):
        relations.extend(
            [
                "entry_life__competition_entry__player",
                "source_entry_life__competition_entry__player",
            ]
        )
    return relations


def _tie_slots_for_ties(ties, *, lock=False):
    """Return explicit slot maps for several ties with one slot query.

    Topology validation is deliberately graph-wide.  Fetching each pair of
    slots independently made validation of a 256-slot bracket issue roughly
    one query per tie before any fixture was even created.  Normal live
    operations may still ask for one locked tie through ``_tie_slots`` below.
    """

    tie_ids = sorted(
        {
            getattr(tie, "pk", tie)
            for tie in ties
            if getattr(tie, "pk", tie)
        }
    )
    if not tie_ids:
        return {}
    queryset = PoolKnockoutTieSlot.objects.filter(tie_id__in=tie_ids).select_related(
        *_tie_slot_select_related_relations()
    )
    if lock:
        # ``entry`` and every source relation are nullable.  PostgreSQL cannot
        # apply a row lock to the nullable side of the resulting outer joins,
        # so lock only the slot rows; callers lock target ties explicitly.
        queryset = queryset.select_for_update(of=("self",))
    slots_by_tie = defaultdict(dict)
    for slot in queryset.order_by("tie_id", "slot", "pk"):
        slots_by_tie[slot.tie_id][slot.slot] = slot
    return dict(slots_by_tie)


def _tie_slot_identities_for_ties(ties, *, lock=False):
    """Return only the locked slot identities needed by live progression.

    The full slot loader above intentionally selects player, team, source, and
    group relations for bracket rendering and graph validation.  A completed
    knockout fixture needs only the two entry/life identities.  Keeping this
    private, narrow projection avoids transferring a large nullable join tree
    over the production database connection on every live advancement.
    """

    tie_ids = sorted(
        {
            getattr(tie, "pk", tie)
            for tie in ties
            if getattr(tie, "pk", tie)
        }
    )
    if not tie_ids:
        return {}
    queryset = PoolKnockoutTieSlot.objects.filter(tie_id__in=tie_ids)
    if _model_has_field(PoolKnockoutTieSlot, "entry_life"):
        queryset = queryset.select_related("entry_life")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    slots_by_tie = defaultdict(dict)
    for slot in queryset.order_by("tie_id", "slot", "pk"):
        slots_by_tie[slot.tie_id][slot.slot] = slot
    return dict(slots_by_tie)


def _tie_slots(tie, *, lock=False):
    tie_id = getattr(tie, "pk", tie)
    return _tie_slots_for_ties((tie_id,), lock=lock).get(tie_id, {})


def _lock_tie_match_for_activity(tie):
    """Return the current tie and lock its nullable fixture separately.

    A result correction can hold a source fixture while inspecting its
    downstream tie, whereas a manager can start that downstream fixture at
    the same time.  Locking and re-reading the downstream ``PoolMatch`` here
    prevents a stale UPCOMING cache from being treated as safe and deleted
    after the competing transaction has made it LIVE.  The match lock is
    deliberately separate from the nullable ``select_related`` join for
    PostgreSQL compatibility.
    """

    tie_id = getattr(tie, "pk", tie)
    bracket_id = getattr(tie, "bracket_id", None)
    if bracket_id is None:
        bracket_id = PoolKnockoutTie.objects.filter(pk=tie_id).values_list(
            "bracket_id", flat=True
        ).first()
    if bracket_id is None:
        raise PoolBracketValidationError("The bracket tie no longer exists.")
    # This is intentionally the first write lock in every path that reaches
    # a bracket tie.  See ``lock_bracket_dependencies_for_match`` for the
    # matching result-service path.
    PoolKnockoutBracket.objects.select_for_update(of=("self",)).get(
        pk=bracket_id
    )
    locked_tie = (
        PoolKnockoutTie.objects.select_for_update(of=("self",))
        # The bracket context is non-null and is needed by both normal
        # progression and the trusted generated-fixture path below.  Loading
        # it with the locked tie avoids an additional remote read for every
        # newly playable tie without widening the row lock beyond ``self``.
        .select_related("match", "bracket__competition", "bracket__stage")
        .get(pk=tie_id)
    )
    if locked_tie.match_id:
        locked_match = PoolMatch.objects.select_for_update(of=("self",)).get(
            pk=locked_tie.match_id
        )
        locked_tie._state.fields_cache["match"] = locked_match
    return locked_tie


def _lock_ties(tie_ids):
    """Lock bracket nodes in one canonical order and return them by id.

    Two independent upstream results can feed the home and away slots of the
    same downstream tie.  Taking those target tie locks before either source
    locks/updates its slot serialises the work and avoids a PostgreSQL
    deadlock between the two completion transactions.
    """

    ids = sorted({tie_id for tie_id in tie_ids if tie_id})
    if not ids:
        return {}
    return {
        tied.pk: tied
        for tied in PoolKnockoutTie.objects.select_for_update(of=("self",))
        .select_related("bracket__competition", "bracket__stage")
        .filter(pk__in=ids)
        .order_by("pk")
    }


def _lock_matches_for_ties(ties):
    """Attach current, locked fixtures to an already locked set of ties.

    ``_lock_ties()`` deliberately locks only topology rows so callers can take
    several target locks in one deterministic statement.  A subsequent
    activity check must still lock any nullable fixture rows before it trusts
    an UPCOMING cache.  Doing that once for the set avoids re-locking the same
    target fixture for each changed source slot.
    """

    ties = list(ties)
    match_ids = sorted({tie.match_id for tie in ties if tie.match_id})
    locked_matches = {}
    if match_ids:
        locked_matches = {
            pool_match.pk: pool_match
            for pool_match in PoolMatch.objects.select_for_update(of=("self",))
            .filter(pk__in=match_ids)
            .order_by("pk")
        }
    for tie in ties:
        if tie.match_id:
            pool_match = locked_matches.get(tie.match_id)
            if pool_match is None:
                raise PoolBracketValidationError(
                    "The bracket fixture no longer exists."
                )
            tie._state.fields_cache["match"] = pool_match
    return ties


def _tie_has_competitive_activity(
    tie,
    *,
    locked=False,
    rack_match_ids=None,
) -> bool:
    # A bye is a structural, rack-free propagation. It can be recalculated
    # safely while every real descendant fixture is still untouched; treating
    # it as competitive activity would incorrectly block a group correction
    # before anyone has played.
    if not locked:
        tie = _lock_tie_match_for_activity(tie)
    if (
        tie.resolved_winner_id
        and tie.resolution_method != PoolKnockoutTie.ResolutionMethod.BYE
    ):
        return True
    pool_match = tie.match
    if pool_match is None:
        return False
    has_racks = (
        pool_match.pk in rack_match_ids
        if rack_match_ids is not None
        else pool_match.racks.exists()
    )
    return (
        pool_match.status
        not in {PoolMatch.Status.UPCOMING, PoolMatch.Status.POSTPONED}
        or has_racks
        or pool_match.winner_id is not None
        or pool_match.home_score != 0
        or pool_match.away_score != 0
        or pool_match.result_method != PoolMatch.ResultMethod.PLAYED
        or pool_match.completed_at is not None
        or pool_match.is_in_correction_mode
    )


def _tie_has_reset_activity(tie) -> bool:
    """Return whether a generated fixture makes a full bracket reset unsafe.

    An automatic bye resolves a *topology node* but never creates a fixture,
    rack, or result.  It must therefore not by itself prevent an owner from
    resetting an otherwise untouched initial bracket.  A real fixture is safe
    to remove only while it is still an empty upcoming/postponed placeholder.
    """

    tie = _lock_tie_match_for_activity(tie)
    if tie.resolved_winner_id and _is_same_entry_convergence_method(
        tie.resolution_method
    ):
        # Convergence is a terminal championship resolution, not a harmless
        # structural bye.  It must not make a completed competition redrawable.
        return True
    pool_match = tie.match
    if pool_match is None:
        return False
    return (
        pool_match.status not in {
            PoolMatch.Status.UPCOMING,
            PoolMatch.Status.POSTPONED,
        }
        or pool_match.racks.exists()
        or pool_match.winner_id is not None
        or pool_match.home_score != 0
        or pool_match.away_score != 0
        or pool_match.result_method != PoolMatch.ResultMethod.PLAYED
        or pool_match.completed_at is not None
        or pool_match.is_in_correction_mode
    )


def _descendant_tie_ids(tie):
    """Resolve one tie's downstream topology with a single graph read.

    A live quarter-final in a 64/128/256-slot bracket can have a long chain
    of descendants.  Walking that chain with one ``source_tie`` query per
    round made normal result completion grow with bracket depth.  Topology is
    immutable after confirmation, so fetch its directed source edges once and
    traverse the small in-memory graph instead.
    """

    children_by_source = defaultdict(set)
    for source_tie_id, target_tie_id in PoolKnockoutTieSlot.objects.filter(
        tie__bracket_id=tie.bracket_id,
        source_tie_id__isnull=False,
    ).values_list("source_tie_id", "tie_id"):
        children_by_source[source_tie_id].add(target_tie_id)
    seen = set()
    pending = [tie.pk]
    while pending:
        source_tie_id = pending.pop()
        for dependent_id in children_by_source.get(source_tie_id, ()):
            if dependent_id not in seen:
                seen.add(dependent_id)
                pending.append(dependent_id)
    return sorted(seen)


def _locked_rack_match_ids(ties):
    """Get rack presence once for a set of already locked fixture rows."""

    match_ids = sorted({tie.match_id for tie in ties if tie.match_id})
    if not match_ids:
        return set()
    return set(
        PoolRack.objects.filter(match_id__in=match_ids).values_list(
            "match_id",
            flat=True,
        )
    )


def _assert_safe_to_replace_slot(
    slot,
    *,
    exclude_tie_id=None,
    locked_tie=None,
):
    """Check a replacement without dropping the normal descendant guard.

    The progression path already locks each direct target tie and its fixture
    in canonical order.  Reusing that exact locked object prevents a second
    bracket -> tie -> fixture lock cycle for every changed slot.  Descendants
    remain checked through their normal locking path because they are not
    necessarily part of the immediate propagation batch.
    """

    tie = locked_tie or _lock_tie_match_for_activity(slot.tie)
    if locked_tie is not None and tie.pk != slot.tie_id:
        raise PoolBracketValidationError(
            "A locked bracket target does not match the slot being updated."
        )
    if exclude_tie_id is not None and tie.pk == exclude_tie_id:
        return
    descendant_ties = _lock_ties(_descendant_tie_ids(tie))
    _lock_matches_for_ties(descendant_ties.values())
    activity_ties = [tie, *descendant_ties.values()]
    rack_match_ids = _locked_rack_match_ids(activity_ties)
    if _tie_has_competitive_activity(
        tie,
        locked=True,
        rack_match_ids=rack_match_ids,
    ):
        raise PoolBracketCorrectionBlocked(
            f"Cannot change this result because dependent {tie.round_name} {tie.position} has started or has a result."
        )
    for descendant in descendant_ties.values():
        if _tie_has_competitive_activity(
            descendant,
            locked=True,
            rack_match_ids=rack_match_ids,
        ):
            raise PoolBracketCorrectionBlocked(
                f"Cannot change this result because dependent {descendant.round_name} {descendant.position} has started or has a result."
            )


def _same_entry_final_resolution_method():
    """Use the explicit persisted convergence method supplied by the model."""

    return getattr(
        PoolKnockoutTie.ResolutionMethod,
        "SAME_ENTRY_FINAL",
        "SAME_ENTRY_FINAL",
    )


def _same_entry_third_resolution_method():
    """Use the explicit persisted third-place convergence method."""

    return getattr(
        PoolKnockoutTie.ResolutionMethod,
        "SAME_ENTRY_THIRD",
        "SAME_ENTRY_THIRD",
    )


def _is_same_entry_convergence_method(method):
    return method in {
        _same_entry_final_resolution_method(),
        _same_entry_third_resolution_method(),
    }


def _locked_lives(life_ids):
    """Lock selected life rows in a stable order for status transitions."""

    ids = sorted({life_id for life_id in life_ids if life_id})
    if not ids:
        return []
    Life = _life_model()
    return list(
        Life.objects.select_for_update(of=("self",))
        .filter(pk__in=ids)
        .order_by("pk")
    )


def _set_life_statuses(life_ids, *, status, eliminated=False):
    """Persist life elimination/champion state without touching entries."""

    Life = _life_model()
    for life in _locked_lives(life_ids):
        desired_eliminated_at = timezone.now() if eliminated else None
        if (
            life.status == status
            and (
                bool(life.eliminated_at) if eliminated else life.eliminated_at is None
            )
        ):
            continue
        life.status = status
        life.eliminated_at = desired_eliminated_at
        life.save(update_fields=["status", "eliminated_at", "updated_at"])


def _tie_life_ids(tie, *, slots=None):
    return {
        _slot_entry_life_id(slot)
        for slot in (slots if slots is not None else _tie_slots(tie, lock=True)).values()
        if _slot_entry_life_id(slot)
    }


def _restore_life_statuses_for_resolution(tie):
    """Undo only this tie's terminal life state during a safe correction."""

    if not _tie_resolved_winner_life_id(tie) and not _tie_life_ids(tie):
        return
    Life = _life_model()
    _set_life_statuses(
        _tie_life_ids(tie),
        status=Life.Status.ACTIVE,
        eliminated=False,
    )


def _apply_life_statuses_for_resolution(
    tie,
    *,
    winner_life_id,
    method,
    slots=None,
):
    """Eliminate only the losing life and crown life-aware championship paths."""

    life_ids = _tie_life_ids(tie, slots=slots)
    if not life_ids:
        return
    Life = _life_model()
    if method == _same_entry_final_resolution_method():
        _set_life_statuses(
            life_ids,
            status=Life.Status.CHAMPION,
            eliminated=False,
        )
        return
    if method == _same_entry_third_resolution_method():
        # Neither life is a tournament champion. Both have completed their
        # final authorised path, so do not leave a phantom ACTIVE life after
        # the no-self-match third-place convergence.
        _set_life_statuses(
            life_ids,
            status=Life.Status.ELIMINATED,
            eliminated=True,
        )
        return
    if tie.is_third_place:
        # A third-place fixture is each remaining life's final tournament
        # path. The result determines placement, but neither side remains an
        # active championship contender afterwards.
        _set_life_statuses(
            life_ids,
            status=Life.Status.ELIMINATED,
            eliminated=True,
        )
        return
    if winner_life_id is None:
        return
    if winner_life_id not in life_ids:
        raise PoolBracketValidationError(
            "The resolved tournament life must occupy the resolved bracket tie."
        )
    loser_life_ids = life_ids - {winner_life_id}
    loser_continues = PoolKnockoutTieSlot.objects.filter(
        source_tie=tie,
        source_type=PoolKnockoutTieSlot.SourceType.TIE_LOSER,
    ).exists()
    if loser_life_ids and not loser_continues:
        _set_life_statuses(
            loser_life_ids,
            status=Life.Status.ELIMINATED,
            eliminated=True,
        )
    elif loser_life_ids:
        # A semi-final loser can still have an authorised third-place path;
        # it is not eliminated until that final playable tie resolves.
        _set_life_statuses(
            loser_life_ids,
            status=Life.Status.ACTIVE,
            eliminated=False,
        )
    _set_life_statuses(
        {winner_life_id},
        status=(Life.Status.CHAMPION if _is_championship_final(tie) else Life.Status.ACTIVE),
        eliminated=False,
    )


def _is_championship_final(tie):
    return (
        not tie.is_third_place
        and tie.round_number == _round_count(tie.bracket.size)
    )


def _slot_life_belongs_to_entry(slot):
    life_id = _slot_entry_life_id(slot)
    if life_id is None:
        return True
    if not slot.entry_id:
        return False
    life = getattr(slot, "entry_life", None)
    return life is not None and life.competition_entry_id == slot.entry_id


def _save_slot_entry(slot, *, entry_id, entry_life_id):
    """Update materialised slot identity atomically with its life metadata."""

    slot.entry_id = entry_id
    update_fields = ["entry", "updated_at"]
    if _model_has_field(PoolKnockoutTieSlot, "entry_life"):
        slot.entry_life_id = entry_life_id
        update_fields.insert(1, "entry_life")
    slot.save(update_fields=update_fields)


def _resolve_same_entry_final(tie, *, entry_id, actor=None):
    """Resolve two sibling lives converging in the championship without a match."""

    if tie.match_id:
        tie = _clear_tie_match_if_safe(tie)
    _set_tie_resolution(
        tie,
        entry_id,
        life_id=None,
        method=_same_entry_final_resolution_method(),
        actor=actor,
        message="Both tournament lives reached the final; the shared competition entry is champion.",
    )
    return tie


def _resolve_same_entry_third_place(tie, *, entry_id, actor=None):
    """Resolve sibling lives sharing third place without a self-fixture."""

    if tie.match_id:
        tie = _clear_tie_match_if_safe(tie)
    _set_tie_resolution(
        tie,
        entry_id,
        life_id=None,
        method=_same_entry_third_resolution_method(),
        actor=actor,
        message=(
            "Both tournament lives reached the third-place playoff; the "
            "shared competition entry takes third place."
        ),
    )
    return tie


def _materialise_trusted_bracket_match(tie, home, away):
    """Insert one validated bracket-derived fixture without model-save reads.

    This is intentionally narrower than a general ``PoolMatch`` creation
    shortcut.  Callers have locked the bracket, tie, and its slots; the
    explicit topology has already supplied the competition/stage, distinct
    entries, life identity, and race target.  The shared projection builder
    below repeats the remaining match-level constraints that are not implied
    by that graph.  Manual/admin fixture creation continues through
    ``PoolMatch.save()`` and its complete validation boundary.

    ``bulk_create`` still runs field ``pre_save`` hooks (including timestamps)
    but avoids the general model's read-heavy prospective-margin and relation
    validation.  It is a single-row insert here because a live result can
    unblock only a small number of direct target ties; initial draw creation
    batches the same projections separately.
    """

    pool_match = _generated_match_projection(tie, home, away)
    PoolMatch.objects.bulk_create([pool_match])
    if pool_match.pk is not None:
        return pool_match

    # PostgreSQL (production) and current SQLite return primary keys from a
    # bulk insert.  Retain a deterministic fallback for a backend without
    # RETURNING rather than reverting the normal live path to ``save()``.  A
    # generated knockout stage has no generic fixtures and the target tie row
    # is locked, so this complete fixture identity identifies exactly this
    # newly inserted projection.
    lookup = PoolMatch.objects.filter(
        competition_id=tie.bracket.competition_id,
        stage_id=tie.bracket.stage_id,
        home_entry_id=home.entry_id,
        away_entry_id=away.entry_id,
        race_target=tie.race_target,
        round_label=tie.round_name,
        venue=tie.bracket.competition.venue,
        status=PoolMatch.Status.UPCOMING,
        home_score=0,
        away_score=0,
        winner__isnull=True,
    )
    life_fields = _life_field_kwargs(
        PoolMatch,
        home_entry_life_id=_slot_entry_life_id(home),
        away_entry_life_id=_slot_entry_life_id(away),
    )
    if life_fields:
        lookup = lookup.filter(**life_fields)
    matches = list(lookup.order_by("-pk")[:2])
    if len(matches) != 1:
        raise PoolBracketValidationError(
            "The generated bracket fixture could not be identified."
        )
    return matches[0]


def _ensure_match_for_tie(
    tie,
    *,
    actor=None,
    _locked_tie=False,
    slots=None,
):
    """Create the normal PoolMatch projection when both bracket slots resolve.

    ``_locked_tie`` is private to the propagation batch.  Its caller has
    already locked the bracket root, tie row, nullable fixture (if present),
    and supplied locked slot rows.  The ordinary/default path preserves the
    full bracket -> tie -> fixture locking protocol for every other caller.
    """

    if not _locked_tie:
        tie = _lock_tie_match_for_activity(tie)
    if slots is None:
        slots = _tie_slots(tie, lock=True)
    home = slots.get(PoolKnockoutTieSlot.Slot.HOME)
    away = slots.get(PoolKnockoutTieSlot.Slot.AWAY)
    if not home or not away:
        raise PoolBracketValidationError("Bracket tie does not have two slots.")
    if not home.entry_id or not away.entry_id:
        _resolve_bye_if_ready(tie, actor=actor)
        return tie
    home_life_id = _slot_entry_life_id(home)
    away_life_id = _slot_entry_life_id(away)
    if not _slot_life_belongs_to_entry(home) or not _slot_life_belongs_to_entry(away):
        raise PoolBracketValidationError(
            "A bracket slot's tournament life must belong to its materialised entry."
        )
    if bool(home_life_id) != bool(away_life_id):
        raise PoolBracketValidationError(
            "A life-aware bracket match must identify both tournament lives."
        )
    if home.entry_id == away.entry_id:
        if home_life_id and away_life_id and home_life_id != away_life_id:
            if _is_championship_final(tie):
                return _resolve_same_entry_final(
                    tie,
                    entry_id=home.entry_id,
                    actor=actor,
                )
            if tie.is_third_place:
                return _resolve_same_entry_third_place(
                    tie,
                    entry_id=home.entry_id,
                    actor=actor,
                )
        raise PoolBracketValidationError(
            "The same entrant cannot occupy both sides of a bracket tie."
        )
    if tie.match_id:
        pool_match = tie.match
        if (
            pool_match.home_entry_id != home.entry_id
            or pool_match.away_entry_id != away.entry_id
            or _match_life_id(pool_match, "home") != home_life_id
            or _match_life_id(pool_match, "away") != away_life_id
        ):
            if _tie_has_competitive_activity(tie, locked=_locked_tie):
                raise PoolBracketCorrectionBlocked(
                    f"Cannot replace participants in {tie.round_name} {tie.position} after activity has begun."
                )
            pool_match.home_entry_id = home.entry_id
            pool_match.away_entry_id = away.entry_id
            if _model_has_field(PoolMatch, "home_entry_life"):
                pool_match.home_entry_life_id = home_life_id
                pool_match.away_entry_life_id = away_life_id
            pool_match.save()
        return tie

    pool_match = _materialise_trusted_bracket_match(tie, home, away)
    now = timezone.now()
    linked = PoolKnockoutTie.objects.filter(
        pk=tie.pk,
        match__isnull=True,
    ).update(
        match_id=pool_match.pk,
        updated_at=now,
    )
    if linked != 1:  # pragma: no cover - held tie lock prevents this race.
        raise PoolBracketValidationError(
            "The generated bracket fixture could not be linked to its tie."
        )
    tie.match_id = pool_match.pk
    tie.updated_at = now
    tie._state.fields_cache["match"] = pool_match
    return tie


def _clear_tie_match_if_safe(tie):
    tie = _lock_tie_match_for_activity(tie)
    if not tie.match_id:
        return tie
    pool_match = tie.match
    if _tie_has_competitive_activity(tie):
        raise PoolBracketCorrectionBlocked(
            f"Cannot clear {tie.round_name} {tie.position} after activity has begun."
        )
    tie.match = None
    tie.save(update_fields=["match", "updated_at"])
    pool_match.delete()
    return tie


def _set_tie_resolution(tie, entry_id, *, life_id=None, method, actor=None, message=""):
    changed = (
        tie.resolved_winner_id != entry_id
        or tie.resolution_method != method
        or _tie_resolved_winner_life_id(tie) != life_id
    )
    if not changed:
        return tie
    if tie.resolution_method != PoolKnockoutTie.ResolutionMethod.PENDING:
        _restore_life_statuses_for_resolution(tie)
    tie.resolved_winner_id = entry_id
    if _model_has_field(PoolKnockoutTie, "resolved_winner_life"):
        tie.resolved_winner_life_id = life_id
    tie.resolution_method = method
    tie.resolved_at = timezone.now()
    update_fields = ["resolved_winner", "resolution_method", "resolved_at", "updated_at"]
    if _model_has_field(PoolKnockoutTie, "resolved_winner_life"):
        update_fields.insert(1, "resolved_winner_life")
    tie.save(update_fields=update_fields)
    _apply_life_statuses_for_resolution(
        tie,
        winner_life_id=life_id,
        method=method,
    )
    if method == PoolKnockoutTie.ResolutionMethod.BYE:
        _audit(
            tie.bracket,
            action=PoolBracketAuditEvent.Action.BYE_ADVANCED,
            actor=actor,
            message=message or f"{tie.round_name} {tie.position} advanced by bye.",
            payload={"tie_id": tie.pk, "entry_id": entry_id, "life_id": life_id},
        )
    elif method == _same_entry_final_resolution_method():
        _audit(
            tie.bracket,
            action=getattr(
                PoolBracketAuditEvent.Action,
                "SAME_ENTRY_FINAL",
                PoolBracketAuditEvent.Action.SLOT_UPDATED,
            ),
            actor=actor,
            message=message
            or "Both tournament lives reached the final; the shared entry is champion.",
            payload={"tie_id": tie.pk, "entry_id": entry_id},
        )
    elif method == _same_entry_third_resolution_method():
        _audit(
            tie.bracket,
            action=getattr(
                PoolBracketAuditEvent.Action,
                "SAME_ENTRY_THIRD",
                PoolBracketAuditEvent.Action.SLOT_UPDATED,
            ),
            actor=actor,
            message=message
            or "Both tournament lives shared the third-place playoff.",
            payload={"tie_id": tie.pk, "entry_id": entry_id},
        )
    return tie


def _trusted_match_resolution_slots(tie, pool_match, *, winner_life_id):
    """Validate the small invariant set for one locked match-derived tie.

    ``PoolKnockoutTie.save()`` normally runs complete model validation, which
    is essential for manual/admin changes and corrections.  The first result
    of an already generated tie is different: the enclosing result service
    holds the bracket root, source tie, and source match.  Re-check the exact
    topology identities in memory so this private path can avoid re-reading
    those same relations through ``full_clean()`` without weakening the
    general model boundary.
    """

    if tie.match_id != pool_match.pk:
        raise PoolBracketValidationError(
            "The completed fixture is not linked to its bracket tie."
        )
    if (
        tie.bracket.competition_id != pool_match.competition_id
        or tie.bracket.stage_id != pool_match.stage_id
        or pool_match.group_id is not None
    ):
        raise PoolBracketValidationError(
            "A bracket fixture must remain in its bracket competition and stage."
        )
    if tie.race_target < 1 or pool_match.race_target != tie.race_target:
        raise PoolBracketValidationError(
            "A bracket fixture must retain the tie race target."
        )
    if pool_match.status != PoolMatch.Status.FINISHED:
        raise PoolBracketValidationError(
            "Only a finished fixture can resolve a bracket tie."
        )
    if pool_match.winner_id not in {
        pool_match.home_entry_id,
        pool_match.away_entry_id,
    }:
        raise PoolBracketValidationError(
            "A bracket result winner must be one of the fixture participants."
        )

    slots = _tie_slot_identities_for_ties((tie,), lock=True).get(tie.pk, {})
    expected_slots = {
        PoolKnockoutTieSlot.Slot.HOME,
        PoolKnockoutTieSlot.Slot.AWAY,
    }
    if set(slots) != expected_slots:
        raise PoolBracketValidationError("Bracket tie does not have two slots.")
    home = slots[PoolKnockoutTieSlot.Slot.HOME]
    away = slots[PoolKnockoutTieSlot.Slot.AWAY]
    if (
        home.entry_id != pool_match.home_entry_id
        or away.entry_id != pool_match.away_entry_id
        or not home.entry_id
        or not away.entry_id
        or home.entry_id == away.entry_id
    ):
        raise PoolBracketValidationError(
            "Bracket fixture participants must match the explicit tie slots."
        )

    home_life_id = _slot_entry_life_id(home)
    away_life_id = _slot_entry_life_id(away)
    if (
        _match_life_id(pool_match, "home") != home_life_id
        or _match_life_id(pool_match, "away") != away_life_id
        or bool(home_life_id) != bool(away_life_id)
        or (home_life_id and home_life_id == away_life_id)
    ):
        raise PoolBracketValidationError(
            "Bracket fixture tournament lives must match the explicit tie slots."
        )
    if home_life_id and (
        not _two_lives_enabled(tie.bracket.competition)
        or not _slot_life_belongs_to_entry(home)
        or not _slot_life_belongs_to_entry(away)
    ):
        raise PoolBracketValidationError(
            "A bracket tournament life must belong to its enabled competition entry."
        )

    expected_winner_life_id = (
        home_life_id
        if pool_match.winner_id == pool_match.home_entry_id
        else away_life_id
    )
    if (
        winner_life_id != expected_winner_life_id
        or _match_life_id(pool_match, "winner") != winner_life_id
    ):
        raise PoolBracketValidationError(
            "The bracket winner must retain the exact winning tournament life."
        )
    return slots


def _set_trusted_match_tie_resolution(tie, pool_match, *, winner_life_id):
    """Persist a first-time, locked match resolution without ``full_clean``.

    Returning ``None`` deliberately sends a correction, a replay, or a
    historic partial state through the normal ``PoolKnockoutTie.save`` path.
    Only an entirely pending tie whose authoritative fixture just completed
    can use this optimisation.
    """

    if (
        tie.resolution_method != PoolKnockoutTie.ResolutionMethod.PENDING
        or tie.resolved_winner_id is not None
        or _tie_resolved_winner_life_id(tie) is not None
    ):
        return None

    slots = _trusted_match_resolution_slots(
        tie,
        pool_match,
        winner_life_id=winner_life_id,
    )
    now = timezone.now()
    filters = {
        "pk": tie.pk,
        "resolution_method": PoolKnockoutTie.ResolutionMethod.PENDING,
        "resolved_winner__isnull": True,
    }
    updates = {
        "resolved_winner_id": pool_match.winner_id,
        "resolution_method": PoolKnockoutTie.ResolutionMethod.MATCH,
        "resolved_at": now,
        "updated_at": now,
    }
    if _model_has_field(PoolKnockoutTie, "resolved_winner_life"):
        filters["resolved_winner_life__isnull"] = True
        updates["resolved_winner_life_id"] = winner_life_id
    updated = PoolKnockoutTie.objects.filter(**filters).update(**updates)
    if updated != 1:  # pragma: no cover - source tie is locked by the caller.
        raise PoolBracketValidationError(
            "The completed bracket tie could not be resolved."
        )
    tie.resolved_winner_id = pool_match.winner_id
    if _model_has_field(PoolKnockoutTie, "resolved_winner_life"):
        tie.resolved_winner_life_id = winner_life_id
    tie.resolution_method = PoolKnockoutTie.ResolutionMethod.MATCH
    tie.resolved_at = now
    tie.updated_at = now
    # The normal lifecycle helper only has work when life metadata exists;
    # pass the already locked identity slots so it does not re-query them.
    if winner_life_id is not None:
        _apply_life_statuses_for_resolution(
            tie,
            winner_life_id=winner_life_id,
            method=PoolKnockoutTie.ResolutionMethod.MATCH,
            slots=slots,
        )
    return slots


def _clear_tie_resolution(tie):
    if (
        not tie.resolved_winner_id
        and _tie_resolved_winner_life_id(tie) is None
        and tie.resolution_method == PoolKnockoutTie.ResolutionMethod.PENDING
    ):
        return tie
    _restore_life_statuses_for_resolution(tie)
    tie.resolved_winner = None
    if _model_has_field(PoolKnockoutTie, "resolved_winner_life"):
        tie.resolved_winner_life = None
    tie.resolution_method = PoolKnockoutTie.ResolutionMethod.PENDING
    tie.resolved_at = None
    update_fields = ["resolved_winner", "resolution_method", "resolved_at", "updated_at"]
    if _model_has_field(PoolKnockoutTie, "resolved_winner_life"):
        update_fields.insert(1, "resolved_winner_life")
    tie.save(update_fields=update_fields)
    return tie


def _resolve_bye_if_ready(tie, *, actor=None):
    slots = _tie_slots(tie, lock=True)
    home = slots.get(PoolKnockoutTieSlot.Slot.HOME)
    away = slots.get(PoolKnockoutTieSlot.Slot.AWAY)
    if not home or not away:
        return tie
    entries = [slot.entry_id for slot in (home, away) if slot.entry_id]
    sources = {home.source_type, away.source_type}
    if len(entries) == 1 and PoolKnockoutTieSlot.SourceType.BYE in sources:
        _set_tie_resolution(
            tie,
            entries[0],
            life_id=next(
                (
                    _slot_entry_life_id(slot)
                    for slot in (home, away)
                    if slot.entry_id
                ),
                None,
            ),
            method=PoolKnockoutTie.ResolutionMethod.BYE,
            actor=actor,
        )
        _propagate_tie_resolution(tie, actor=actor)
    elif not entries and sources == {PoolKnockoutTieSlot.SourceType.BYE}:
        raise PoolBracketValidationError("A bracket tie cannot contain two byes.")
    return tie


def _write_trusted_propagated_slot(
    slot,
    *,
    source_tie,
    source_slots,
    entry_id,
    entry_life_id,
):
    """Apply one first-time match advancement after explicit lock checks.

    This is private to the fast path that has just verified a finished source
    fixture and locked both the source and target topology.  It deliberately
    retains the normal descendant activity check in the caller; this function
    merely avoids asking ``PoolKnockoutTieSlot.full_clean()`` to re-fetch the
    same source tie, target tie, and entrant relations.
    """

    target_tie = slot.tie
    if (
        target_tie.bracket_id != source_tie.bracket_id
        or slot.source_tie_id != source_tie.pk
        or slot.source_type
        not in {
            PoolKnockoutTieSlot.SourceType.TIE_WINNER,
            PoolKnockoutTieSlot.SourceType.TIE_LOSER,
        }
    ):
        raise PoolBracketValidationError(
            "A bracket advancement slot does not match its locked source tie."
        )
    if slot.source_type == PoolKnockoutTieSlot.SourceType.TIE_WINNER:
        expected_identity = (
            source_tie.resolved_winner_id,
            _tie_resolved_winner_life_id(source_tie),
        )
    else:
        expected_identity = _loser_identity_for_tie(
            source_tie,
            slots=source_slots,
        )
    if (
        (entry_id, entry_life_id) != expected_identity
        or entry_id is None
        or bool(entry_life_id)
        != bool(_tie_resolved_winner_life_id(source_tie))
    ):
        raise PoolBracketValidationError(
            "A bracket advancement must retain the resolved entrant and life."
        )

    now = timezone.now()
    updates = {"entry_id": entry_id, "updated_at": now}
    if _model_has_field(PoolKnockoutTieSlot, "entry_life"):
        updates["entry_life_id"] = entry_life_id
    updated = PoolKnockoutTieSlot.objects.filter(
        pk=slot.pk,
        tie_id=slot.tie_id,
        source_tie_id=source_tie.pk,
        source_type=slot.source_type,
    ).update(**updates)
    if updated != 1:  # pragma: no cover - target slot is locked by caller.
        raise PoolBracketValidationError(
            "The resolved bracket participant could not be advanced."
        )
    slot.entry_id = entry_id
    if _model_has_field(PoolKnockoutTieSlot, "entry_life"):
        slot.entry_life_id = entry_life_id
    slot.updated_at = now
    slot._state.fields_cache.pop("entry", None)
    slot._state.fields_cache.pop("entry_life", None)


def _write_trusted_late_bye_slot(slot, *, tie, entry, entry_life=None):
    """Convert one locked, empty first-round BYE to a direct entrant.

    This narrowly mirrors the trusted advancement writer above.  Late-BYE
    registration has already locked and verified the bracket, source tie,
    candidate slot, entrant, and any tournament life.  Calling the normal
    model ``save()`` here would make ``full_clean()`` fetch that exact FK graph
    again over the production database connection.  The conditional update is
    deliberately stricter than the in-memory checks: a concurrent mutation of
    the BYE must fail the whole atomic registration rather than be retried or
    silently overwritten.
    """

    if not getattr(slot, "pk", None) or not getattr(tie, "pk", None):
        raise PoolBracketValidationError(
            "A saved first-round BYE slot is required for late registration."
        )
    if (
        slot.tie_id != tie.pk
        or tie.round_number != 1
        or tie.is_third_place
    ):
        raise PoolBracketValidationError(
            "Late registration may update only a first-round bracket BYE."
        )
    bracket = tie.bracket
    if entry.competition_id != bracket.competition_id:
        raise PoolBracketValidationError(
            "The late entrant must belong to the bracket competition."
        )
    has_life = _model_has_field(PoolKnockoutTieSlot, "entry_life")
    if entry_life is not None:
        if not has_life:
            raise PoolBracketValidationError(
                "Tournament-life slots are unavailable until the life migration is applied."
            )
        if entry_life.competition_entry_id != entry.pk:
            raise PoolBracketValidationError(
                "The supplied tournament life does not belong to the late entrant."
            )

    now = timezone.now()
    filters = {
        "pk": slot.pk,
        "tie_id": tie.pk,
        "source_type": PoolKnockoutTieSlot.SourceType.BYE,
        "entry_id__isnull": True,
        "source_entry_id__isnull": True,
        "source_group_id__isnull": True,
        "source_position__isnull": True,
        "source_tie_id__isnull": True,
    }
    if has_life:
        filters.update(
            {
                "entry_life_id__isnull": True,
                "source_entry_life_id__isnull": True,
            }
        )
    updates = {
        "source_type": PoolKnockoutTieSlot.SourceType.DIRECT_ENTRY,
        "entry_id": entry.pk,
        "source_entry_id": entry.pk,
        "source_group_id": None,
        "source_position": None,
        "source_tie_id": None,
        "updated_at": now,
    }
    if has_life:
        updates.update(
            {
                "entry_life_id": getattr(entry_life, "pk", None),
                "source_entry_life_id": getattr(entry_life, "pk", None),
            }
        )
    updated = PoolKnockoutTieSlot.objects.filter(**filters).update(**updates)
    if updated != 1:
        raise PoolBracketValidationError(
            "That first-round BYE changed while late registration was being processed."
        )

    # Keep the object passed to the remaining topology validation/materialising
    # path coherent without issuing a relation refresh.
    slot.source_type = PoolKnockoutTieSlot.SourceType.DIRECT_ENTRY
    slot.entry = entry
    slot.source_entry = entry
    slot.source_group_id = None
    slot.source_position = None
    slot.source_tie_id = None
    if has_life:
        slot.entry_life = entry_life
        slot.source_entry_life = entry_life
    slot.updated_at = now
    for relation in ("source_group", "source_tie"):
        slot._state.fields_cache.pop(relation, None)
    return slot


def _audit_trusted_slot_update(
    bracket,
    *,
    target_tie,
    slot,
    source_tie,
    entry_id,
    entry_life_id,
    actor=None,
):
    """Persist the normal slot audit without redundant relation validation."""

    PoolBracketAuditEvent.objects.bulk_create(
        [
            _generated_audit_event(
                bracket,
                actor=actor,
                action=PoolBracketAuditEvent.Action.SLOT_UPDATED,
                message=(
                    f"Updated {target_tie.round_name} {target_tie.position} "
                    f"{slot.get_slot_display()} source."
                ),
                payload={
                    "slot_id": slot.pk,
                    "entry_id": entry_id,
                    "life_id": entry_life_id,
                    "source_tie_id": source_tie.pk,
                },
            )
        ]
    )


def _propagate_tie_resolution(
    tie,
    *,
    actor=None,
    _trusted_match_slots=None,
):
    """Write every direct dependent source slot idempotently, then materialise it."""
    dependent_tie_ids = list(
        PoolKnockoutTieSlot.objects.filter(source_tie=tie)
        .order_by("tie_id")
        .values_list("tie_id", flat=True)
        .distinct()
    )
    locked_ties = _lock_ties(dependent_tie_ids)
    _lock_matches_for_ties(locked_ties.values())
    dependents = list(
        PoolKnockoutTieSlot.objects.select_for_update(of=("self",))
        .filter(source_tie=tie)
        .order_by("tie_id", "slot", "pk")
    )
    for slot in dependents:
        target_tie = locked_ties.get(slot.tie_id)
        if target_tie is None:  # pragma: no cover - topology is immutable.
            raise PoolBracketValidationError(
                "A bracket source points outside its locked topology."
            )
        # Keep all later activity checks and audit labels on the exact target
        # row and nullable fixture that were just locked in canonical order.
        slot._state.fields_cache["tie"] = target_tie

    desired_by_slot = {}
    loser_identity = None
    for slot in dependents:
        if slot.source_type == PoolKnockoutTieSlot.SourceType.TIE_WINNER:
            desired_by_slot[slot.pk] = (
                tie.resolved_winner_id,
                _tie_resolved_winner_life_id(tie),
            )
        elif slot.source_type == PoolKnockoutTieSlot.SourceType.TIE_LOSER:
            if loser_identity is None:
                loser_identity = _loser_identity_for_tie(
                    tie,
                    slots=(
                        _trusted_match_slots
                        if _trusted_match_slots is not None
                        else _tie_slots(tie, lock=True)
                    ),
                )
            desired_by_slot[slot.pk] = loser_identity

    for slot in dependents:
        if slot.pk not in desired_by_slot:
            continue
        desired_entry_id, desired_life_id = desired_by_slot[slot.pk]
        if (
            slot.entry_id == desired_entry_id
            and _slot_entry_life_id(slot) == desired_life_id
        ):
            continue
        _assert_safe_to_replace_slot(
            slot,
            locked_tie=locked_ties[slot.tie_id],
        )

    changed_tie_ids = set()
    for slot in dependents:
        if slot.pk not in desired_by_slot:
            continue
        desired_entry_id, desired_life_id = desired_by_slot[slot.pk]
        if (
            slot.entry_id == desired_entry_id
            and _slot_entry_life_id(slot) == desired_life_id
        ):
            continue
        if _trusted_match_slots is not None:
            _write_trusted_propagated_slot(
                slot,
                source_tie=tie,
                source_slots=_trusted_match_slots,
                entry_id=desired_entry_id,
                entry_life_id=desired_life_id,
            )
            _audit_trusted_slot_update(
                tie.bracket,
                target_tie=locked_ties[slot.tie_id],
                slot=slot,
                source_tie=tie,
                entry_id=desired_entry_id,
                entry_life_id=desired_life_id,
                actor=actor,
            )
        else:
            _save_slot_entry(
                slot,
                entry_id=desired_entry_id,
                entry_life_id=desired_life_id,
            )
            _audit(
                tie.bracket,
                action=PoolBracketAuditEvent.Action.SLOT_UPDATED,
                actor=actor,
                message=f"Updated {slot.tie.round_name} {slot.tie.position} {slot.get_slot_display()} source.",
                payload={
                    "slot_id": slot.pk,
                    "entry_id": desired_entry_id,
                    "life_id": desired_life_id,
                    "source_tie_id": tie.pk,
                },
            )
        changed_tie_ids.add(slot.tie_id)
    # A direct source contributes only one side of a later tie.  Read the
    # complete pair once after every changed source slot is persisted so the
    # materialiser sees both the newly advanced participant and the other
    # already-resolved side.
    target_slots_by_tie = _tie_slot_identities_for_ties(
        changed_tie_ids,
        lock=True,
    )
    for dependent_tie_id in sorted(changed_tie_ids):
        _ensure_match_for_tie(
            locked_ties[dependent_tie_id],
            actor=actor,
            _locked_tie=True,
            slots=target_slots_by_tie[dependent_tie_id],
        )
    return tie


def _loser_identity_for_tie(tie, *, slots=None):
    """Return the entry/life identity of one resolved competitive loser."""

    if not tie.resolved_winner_id:
        return None, None
    slots = slots if slots is not None else _tie_slots(tie)
    losers = [
        slot
        for slot in slots.values()
        if slot.entry_id and slot.entry_id != tie.resolved_winner_id
    ]
    if len(losers) != 1:
        return None, None
    loser = losers[0]
    return loser.entry_id, _slot_entry_life_id(loser)


def _loser_for_tie(tie):
    return _loser_identity_for_tie(tie)[0]


def _loser_life_for_tie(tie):
    """Return the exact losing life without changing entry-level statistics."""

    return _loser_identity_for_tie(tie)[1]


def _assign_bulk_created_tie_ids(bracket, ties):
    """Fill primary keys on backends that do not return bulk insert rows.

    PostgreSQL (the production database) and current SQLite both populate the
    objects passed to ``bulk_create``.  The fallback keeps topology creation
    deterministic on a backend without ``RETURNING`` without reverting to an
    insert per tie.
    """

    if all(tie.pk for tie in ties):
        return
    persisted = {
        (tie.round_number, tie.position, tie.is_third_place): tie.pk
        for tie in PoolKnockoutTie.objects.filter(bracket=bracket).only(
            "pk", "round_number", "position", "is_third_place"
        )
    }
    for tie in ties:
        tie.pk = persisted.get(
            (tie.round_number, tie.position, tie.is_third_place)
        )
        if not tie.pk:
            raise PoolBracketValidationError(
                "The generated bracket topology is incomplete."
            )
        tie._state.adding = False
        tie._state.db = bracket._state.db


def _create_topology(
    bracket,
    preview,
    *,
    qualification_rules_by_slot=None,
):
    """Create a complete bracket graph in a small, bounded set of writes.

    The graph is generated from an already locked, validated preview.  Its
    rows can therefore be constructed in memory and bulk inserted safely,
    then validated as a persisted graph before any fixtures are materialised.
    This avoids hundreds of remote round-trip inserts for 128/256-slot draws.
    """

    size = bracket.size
    rounds = _round_count(size)
    target_map = preview.round_race_targets or {}
    life_draw_order = _preview_life_draw_order(preview)
    if life_draw_order and len(life_draw_order) != size:
        raise PoolBracketValidationError(
            "The saved life-aware draw order does not match its bracket size."
        )
    if (
        preview.draw_method
        == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION
        and qualification_rules_by_slot is None
    ):
        qualification_rules_by_slot = {
            rule.destination_slot: rule
            for rule in _qualification_order(bracket.competition, bracket.stage, size)
        }

    ties_by_round: dict[int, list[PoolKnockoutTie]] = {}
    round_rules = []
    ties = []
    for round_number in range(1, rounds + 1):
        round_name = _round_name(round_number, rounds)
        race_target = int(target_map.get(str(round_number), bracket.competition.default_race_target))
        round_rules.append(
            PoolKnockoutRoundRule(
                bracket=bracket,
                round_number=round_number,
                name=round_name,
                race_target=race_target,
            )
        )
        tie_count = size // (2**round_number)
        ties_by_round[round_number] = [
            PoolKnockoutTie(
                bracket=bracket,
                round_number=round_number,
                position=position,
                round_name=round_name,
                race_target=race_target,
            )
            for position in range(1, tie_count + 1)
        ]
        ties.extend(ties_by_round[round_number])

    if bracket.third_place_enabled:
        if size < 4:
            raise PoolBracketValidationError(
                "A third-place match requires a bracket with at least four entrants."
            )
        target = int(target_map.get("third_place", bracket.competition.default_race_target))
        round_rules.append(
            PoolKnockoutRoundRule(
                bracket=bracket,
                round_number=rounds - 1,
                name="Third-place match",
                race_target=target,
                is_third_place=True,
            )
        )
        third_place = PoolKnockoutTie(
            bracket=bracket,
            round_number=rounds - 1,
            position=1,
            round_name="Third-place match",
            race_target=target,
            is_third_place=True,
        )
        ties.append(third_place)
    else:
        third_place = None

    PoolKnockoutRoundRule.objects.bulk_create(round_rules)
    PoolKnockoutTie.objects.bulk_create(ties)
    _assign_bulk_created_tie_ids(bracket, ties)

    # Explicit direct/qualifier/bye sources for each first-round slot.
    first_round = ties_by_round[1]
    slots = []
    for index, entry_id in enumerate(preview.draw_order, start=1):
        tie = first_round[(index - 1) // 2]
        side = (
            PoolKnockoutTieSlot.Slot.HOME
            if index % 2
            else PoolKnockoutTieSlot.Slot.AWAY
        )
        source = _slot_source_for_first_round(
            bracket,
            bracket.stage,
            index,
            entry_id,
            preview.draw_method,
            entry_life_id=(
                life_draw_order[index - 1] if life_draw_order else None
            ),
            qualification_rules_by_slot=qualification_rules_by_slot,
        )
        slots.append(PoolKnockoutTieSlot(tie=tie, slot=side, **source))

    # Explicit winner sources for each later-round slot; no match-ID ordering
    # is ever used to infer advancement.
    for round_number in range(2, rounds + 1):
        prior_ties = ties_by_round[round_number - 1]
        for tie in ties_by_round[round_number]:
            prior_home = prior_ties[(tie.position - 1) * 2]
            prior_away = prior_ties[(tie.position - 1) * 2 + 1]
            slots.extend(
                (
                    PoolKnockoutTieSlot(
                        tie=tie,
                        slot=PoolKnockoutTieSlot.Slot.HOME,
                        source_type=PoolKnockoutTieSlot.SourceType.TIE_WINNER,
                        source_tie=prior_home,
                    ),
                    PoolKnockoutTieSlot(
                        tie=tie,
                        slot=PoolKnockoutTieSlot.Slot.AWAY,
                        source_type=PoolKnockoutTieSlot.SourceType.TIE_WINNER,
                        source_tie=prior_away,
                    ),
                )
            )

    if third_place is not None:
        semis = ties_by_round[rounds - 1]
        for side, semi in zip(
            (PoolKnockoutTieSlot.Slot.HOME, PoolKnockoutTieSlot.Slot.AWAY), semis
        ):
            slots.append(
                PoolKnockoutTieSlot(
                    tie=third_place,
                    slot=side,
                    source_type=PoolKnockoutTieSlot.SourceType.TIE_LOSER,
                    source_tie=semi,
                )
            )
    PoolKnockoutTieSlot.objects.bulk_create(slots)
    return ties_by_round


def validate_bracket(bracket, *, require_resolved=False):
    """Validate one persisted pool bracket graph without inferring its shape.

    This is intentionally stricter than a presentation helper: all topology
    comes from explicit ties and slot sources, never queryset or match ID
    order.  It is called before generated fixtures are materialised and is
    also a safe boundary for future import/admin tooling.
    """

    bracket_id = getattr(bracket, "pk", bracket)
    try:
        bracket = PoolKnockoutBracket.objects.select_related(
            "competition", "stage", "handoff_from_round"
        ).get(
            pk=bracket_id
        )
    except PoolKnockoutBracket.DoesNotExist as exc:
        raise PoolBracketValidationError("The pool bracket no longer exists.") from exc

    round_count = _round_count(bracket.size)
    ties = list(
        PoolKnockoutTie.objects.filter(bracket=bracket)
        .order_by("round_number", "is_third_place", "position", "id")
    )
    slots_by_tie = _tie_slots_for_ties(ties)
    regular_ties = [tie for tie in ties if not tie.is_third_place]
    expected_regular_count = bracket.size - 1
    if len(regular_ties) != expected_regular_count:
        raise PoolBracketValidationError("The bracket does not contain its complete round topology.")

    life_draw_order = _bracket_life_draw_order(bracket)
    life_aware = bool(life_draw_order)
    if _two_lives_enabled(bracket.competition) and not life_aware:
        raise PoolBracketValidationError(
            "A two-life knockout bracket must retain its persisted life draw order."
        )
    lives = {}
    if life_aware:
        lives = _competition_life_map(bracket.competition)
        if bracket.handoff_from_round_id:
            handoff_field = _handoff_round_field(
                bracket.competition,
                bracket.stage,
                bracket.handoff_from_round,
                lock=False,
            )
            expected_life_ids = set(handoff_field["life_ids"])
            lives = {
                life_id: life
                for life_id, life in lives.items()
                if life_id in expected_life_ids
            }
            if set(lives) != expected_life_ids:
                raise PoolBracketValidationError(
                    "The handoff bracket no longer matches its manual-round survivor lives."
                )
        life_draw_order = _validate_life_draw_order(
            life_draw_order,
            lives=lives,
            bracket_size=bracket.size,
        )
        if bracket.draw_order != _life_draw_entry_order(life_draw_order, lives):
            raise PoolBracketValidationError(
                "The bracket entry order no longer matches its persisted life order."
            )
    elif bracket.handoff_from_round_id:
        handoff_field = _handoff_round_field(
            bracket.competition,
            bracket.stage,
            bracket.handoff_from_round,
            lock=False,
        )
        first_round_entry_ids = {
            entry_id for entry_id in bracket.draw_order if entry_id is not None
        }
        if first_round_entry_ids != set(handoff_field["entry_ids"]):
            raise PoolBracketValidationError(
                "The handoff bracket no longer matches its manual-round survivor field."
            )

    identities_seen = set()
    first_round_life_order = [None] * bracket.size
    ties_by_round = defaultdict(dict)
    for tie in regular_ties:
        ties_by_round[tie.round_number][tie.position] = tie
    expected_round_counts = {
        round_number: bracket.size // (2**round_number)
        for round_number in range(1, round_count + 1)
    }
    for round_number, expected_count in expected_round_counts.items():
        round_ties = [
            tie
            for tie in regular_ties
            if tie.round_number == round_number
        ]
        if len(round_ties) != expected_count or {
            tie.position for tie in round_ties
        } != set(range(1, expected_count + 1)):
            raise PoolBracketValidationError(
                "The bracket has an incomplete or duplicated round position."
            )
        for tie in round_ties:
            slots = slots_by_tie.get(tie.pk, {})
            if set(slots) != {
                PoolKnockoutTieSlot.Slot.HOME,
                PoolKnockoutTieSlot.Slot.AWAY,
            }:
                raise PoolBracketValidationError(
                    "Every bracket tie must have one home and one away slot."
                )
            sources = {slot.source_type for slot in slots.values()}
            if round_number == 1:
                allowed = {
                    PoolKnockoutTieSlot.SourceType.DIRECT_ENTRY,
                    PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
                    PoolKnockoutTieSlot.SourceType.BYE,
                }
                if not sources.issubset(allowed):
                    raise PoolBracketValidationError(
                        "First-round bracket slots must be entrants, qualifiers, or byes."
                    )
                if sources == {PoolKnockoutTieSlot.SourceType.BYE}:
                    raise PoolBracketValidationError(
                        "A first-round bracket tie cannot contain two byes."
                    )
                for slot in slots.values():
                    if slot.entry_id is not None:
                        identity = (
                            _slot_entry_life_id(slot)
                            if life_aware
                            else slot.entry_id
                        )
                        if identity is None:
                            raise PoolBracketValidationError(
                                "A life-aware first-round slot must identify its tournament life."
                            )
                        if identity in identities_seen:
                            raise PoolBracketValidationError(
                                "An entrant or tournament life appears in more than one first-round bracket slot."
                            )
                        identities_seen.add(identity)
                        if life_aware:
                            if not _slot_life_belongs_to_entry(slot):
                                raise PoolBracketValidationError(
                                    "A bracket life must belong to its materialised entry."
                                )
                            position = (tie.position - 1) * 2 + (
                                1
                                if slot.slot == PoolKnockoutTieSlot.Slot.HOME
                                else 2
                            )
                            first_round_life_order[position - 1] = identity
                    if slot.source_type == PoolKnockoutTieSlot.SourceType.DIRECT_ENTRY:
                        if slot.source_entry_id != slot.entry_id:
                            raise PoolBracketValidationError(
                                "A direct bracket source must match its materialised entrant."
                            )
                        if life_aware and (
                            _slot_source_entry_life_id(slot)
                            != _slot_entry_life_id(slot)
                        ):
                            raise PoolBracketValidationError(
                                "A direct life-aware source must match its materialised tournament life."
                            )
            else:
                if sources != {PoolKnockoutTieSlot.SourceType.TIE_WINNER}:
                    raise PoolBracketValidationError(
                        "Later championship rounds must receive winners of earlier ties."
                    )
                expected_home = ties_by_round[round_number - 1][
                    (tie.position - 1) * 2 + 1
                ]
                expected_away = ties_by_round[round_number - 1][tie.position * 2]
                if (
                    slots[PoolKnockoutTieSlot.Slot.HOME].source_tie_id
                    != expected_home.pk
                    or slots[PoolKnockoutTieSlot.Slot.AWAY].source_tie_id
                    != expected_away.pk
                ):
                    raise PoolBracketValidationError(
                        "A knockout round must receive the winners of its two canonical prior ties."
                    )

            if life_aware:
                for slot in slots.values():
                    if slot.entry_id and not _slot_life_belongs_to_entry(slot):
                        raise PoolBracketValidationError(
                            "A bracket life must belong to its materialised entry."
                        )
                    if require_resolved and slot.entry_id and not _slot_entry_life_id(slot):
                        raise PoolBracketValidationError(
                            "A resolved life-aware bracket slot is missing its tournament life."
                        )

            if require_resolved and not all(slot.entry_id for slot in slots.values()):
                raise PoolBracketValidationError(
                    "This operation requires every bracket slot to be resolved."
                )

    if life_aware:
        if first_round_life_order != life_draw_order:
            raise PoolBracketValidationError(
                "The persisted life draw order does not match the first-round bracket slots."
            )
        _validate_opposite_life_halves(first_round_life_order, lives, bracket.size)

    third_place_ties = [tie for tie in ties if tie.is_third_place]
    if bool(third_place_ties) != bool(bracket.third_place_enabled):
        raise PoolBracketValidationError(
            "Third-place topology does not match this bracket's configuration."
        )
    if third_place_ties:
        if len(third_place_ties) != 1 or bracket.size < 4:
            raise PoolBracketValidationError("The bracket has an invalid third-place match.")
        third_slots = slots_by_tie.get(third_place_ties[0].pk, {})
        if set(third_slots) != {
            PoolKnockoutTieSlot.Slot.HOME,
            PoolKnockoutTieSlot.Slot.AWAY,
        } or {
            slot.source_type for slot in third_slots.values()
        } != {PoolKnockoutTieSlot.SourceType.TIE_LOSER}:
            raise PoolBracketValidationError(
                "A third-place match must receive the two semi-final losers."
            )
        semi_final_ties = ties_by_round[round_count]
        # The third-place race is played after the semi-finals, whose round
        # number is one less than the final.  It must consume both distinct
        # semifinal losers in stable bracket order.
        semi_final_ties = ties_by_round[round_count - 1]
        if (
            third_slots[PoolKnockoutTieSlot.Slot.HOME].source_tie_id
            != semi_final_ties[1].pk
            or third_slots[PoolKnockoutTieSlot.Slot.AWAY].source_tie_id
            != semi_final_ties[2].pk
        ):
            raise PoolBracketValidationError(
                "A third-place match must receive the two distinct semi-final losers."
            )
    return bracket


def _generated_match_projection(tie, home, away):
    """Build one trusted bracket-derived fixture projection without clean().

    This helper is intentionally private to bracket materialisation.  At this
    point the bracket, stage, locked slots, entry identities, and race targets
    have all been validated inside the same transaction.  The normal
    ``PoolMatch.save()`` boundary remains in force for manager-created and
    edited fixtures; invoking it for every generated first-round tie, or for
    a newly unblocked live target tie, adds avoidable remote validation round
    trips.
    """

    if home.tie_id != tie.pk or away.tie_id != tie.pk:
        raise PoolBracketValidationError(
            "Generated fixture slots must belong to their target bracket tie."
        )
    if not home.entry_id or not away.entry_id:
        raise PoolBracketValidationError(
            "A generated bracket fixture needs two resolved entrants."
        )
    competition = tie.bracket.competition
    if (
        tie.bracket.stage.stage_type
        != PoolCompetitionStage.StageType.KNOCKOUT
        or tie.bracket.stage.competition_id != competition.pk
        or tie.race_target < 1
    ):
        raise PoolBracketValidationError(
            "Generated fixtures require a valid knockout bracket topology."
        )

    home_life_id = _slot_entry_life_id(home)
    away_life_id = _slot_entry_life_id(away)
    if bool(home_life_id) != bool(away_life_id):
        raise PoolBracketValidationError(
            "A life-aware bracket match must identify both tournament lives."
        )
    if home_life_id and (
        not _two_lives_enabled(competition)
        or not _slot_life_belongs_to_entry(home)
        or not _slot_life_belongs_to_entry(away)
        or home_life_id == away_life_id
    ):
        raise PoolBracketValidationError(
            "A generated tournament life must belong to its distinct entrant."
        )
    if home.entry_id == away.entry_id:
        raise PoolBracketValidationError(
            "The same entrant cannot occupy both sides of a bracket tie."
        )
    if (
        competition.participant_mode == PoolCompetition.ParticipantMode.TEAMS
        and tie.race_target > 13
    ):
        # This is the one match-level rule not already implied by the
        # bracket graph.  Keep the normal model boundary's team score-sheet
        # ceiling when bulk materialising a trusted bracket projection.
        raise PoolBracketValidationError(
            "A team match race target cannot exceed 13: a 25-rack, no-draw "
            "schedule must be able to produce a winner."
        )
    match_kwargs = {
        "competition": competition,
        "stage": tie.bracket.stage,
        "home_entry_id": home.entry_id,
        "away_entry_id": away.entry_id,
        "race_target": tie.race_target,
        "round_label": tie.round_name,
        "venue": competition.venue,
    }
    match_kwargs.update(
        _life_field_kwargs(
            PoolMatch,
            home_entry_life_id=home_life_id,
            away_entry_life_id=away_life_id,
        )
    )
    return PoolMatch(**match_kwargs)


def _generated_audit_event(bracket, *, actor, action, message, payload):
    """Create an in-memory audit row for a trusted bracket transaction."""

    return PoolBracketAuditEvent(
        bracket=bracket,
        competition_id=bracket.competition_id,
        stage_id=bracket.stage_id,
        actor=actor,
        action=action,
        message=message,
        payload=payload,
    )


def _materialise_initial_bracket_state(bracket, ties_by_round, *, actor=None):
    """Resolve structural byes and create all initially playable fixtures.

    A generated bracket is private to this transaction until it commits.  The
    competition and stage roots are already locked by confirmation, so this
    narrowly scoped path intentionally does *not* reacquire the same bracket
    row for every just-created tie.  Normal correction paths still retain the
    complete ``_ensure_match_for_tie`` lock protocol; live result propagation
    supplies its own already-locked target batch.
    """

    regular_ties = [
        tie
        for round_number in sorted(ties_by_round)
        for tie in ties_by_round[round_number]
    ]
    ties_by_id = {tie.pk: tie for tie in regular_ties}
    slots_by_tie = _tie_slots_for_ties(regular_ties)
    dependent_slots_by_source = defaultdict(list)
    for tie_slots in slots_by_tie.values():
        for slot in tie_slots.values():
            if slot.source_tie_id:
                dependent_slots_by_source[slot.source_tie_id].append(slot)

    queue = deque(ties_by_round[1])
    queued_tie_ids = {tie.pk for tie in ties_by_round[1]}
    scheduled_match_tie_ids = set()
    match_projections = []
    changed_slots = {}
    changed_ties = {}
    audit_events = []
    champion_life_ids = set()
    eliminated_life_ids = set()
    now = timezone.now()
    has_tie_life = _model_has_field(PoolKnockoutTie, "resolved_winner_life")
    has_slot_life = _model_has_field(PoolKnockoutTieSlot, "entry_life")

    def enqueue(tie):
        if tie.pk in queued_tie_ids:
            return
        queued_tie_ids.add(tie.pk)
        queue.append(tie)

    def resolve_same_entry(tie, home, away):
        if _is_championship_final(tie):
            method = _same_entry_final_resolution_method()
            action = getattr(
                PoolBracketAuditEvent.Action,
                "SAME_ENTRY_FINAL",
                PoolBracketAuditEvent.Action.SLOT_UPDATED,
            )
            message = (
                "Both tournament lives reached the final; the shared "
                "competition entry is champion."
            )
            champion_life_ids.update(
                life_id
                for life_id in (_slot_entry_life_id(home), _slot_entry_life_id(away))
                if life_id
            )
        elif tie.is_third_place:
            method = _same_entry_third_resolution_method()
            action = getattr(
                PoolBracketAuditEvent.Action,
                "SAME_ENTRY_THIRD",
                PoolBracketAuditEvent.Action.SLOT_UPDATED,
            )
            message = (
                "Both tournament lives shared the third-place playoff."
            )
            eliminated_life_ids.update(
                life_id
                for life_id in (_slot_entry_life_id(home), _slot_entry_life_id(away))
                if life_id
            )
        else:
            raise PoolBracketValidationError(
                "The same entrant cannot occupy both sides of a bracket tie."
            )

        tie.resolved_winner_id = home.entry_id
        if has_tie_life:
            tie.resolved_winner_life_id = None
        tie.resolution_method = method
        tie.resolved_at = now
        tie.updated_at = now
        changed_ties[tie.pk] = tie
        audit_events.append(
            _generated_audit_event(
                bracket,
                actor=actor,
                action=action,
                message=message,
                payload={"tie_id": tie.pk, "entry_id": home.entry_id},
            )
        )

    def resolve_bye(tie, winner_slot):
        winner_life_id = _slot_entry_life_id(winner_slot)
        tie.resolved_winner_id = winner_slot.entry_id
        if has_tie_life:
            tie.resolved_winner_life_id = winner_life_id
        tie.resolution_method = PoolKnockoutTie.ResolutionMethod.BYE
        tie.resolved_at = now
        tie.updated_at = now
        changed_ties[tie.pk] = tie
        audit_events.append(
            _generated_audit_event(
                bracket,
                actor=actor,
                action=PoolBracketAuditEvent.Action.BYE_ADVANCED,
                message=f"{tie.round_name} {tie.position} advanced by bye.",
                payload={
                    "tie_id": tie.pk,
                    "entry_id": winner_slot.entry_id,
                    "life_id": winner_life_id,
                },
            )
        )
        if _is_championship_final(tie) and winner_life_id:
            champion_life_ids.add(winner_life_id)

        for dependent_slot in dependent_slots_by_source.get(tie.pk, ()):
            if dependent_slot.source_type == PoolKnockoutTieSlot.SourceType.TIE_WINNER:
                desired_entry_id = tie.resolved_winner_id
                desired_life_id = _tie_resolved_winner_life_id(tie)
            elif dependent_slot.source_type == PoolKnockoutTieSlot.SourceType.TIE_LOSER:
                # A bye has no competing loser.  The target slot is already
                # unresolved, so this is normally a no-op (for example a
                # third-place source from a bye semi-final).
                desired_entry_id = None
                desired_life_id = None
            else:  # pragma: no cover - topology validation prevents this.
                continue
            if (
                dependent_slot.entry_id == desired_entry_id
                and _slot_entry_life_id(dependent_slot) == desired_life_id
            ):
                continue
            dependent_slot.entry_id = desired_entry_id
            if has_slot_life:
                dependent_slot.entry_life_id = desired_life_id
            dependent_slot.updated_at = now
            changed_slots[dependent_slot.pk] = dependent_slot
            target_tie = ties_by_id.get(dependent_slot.tie_id)
            if target_tie is None:  # pragma: no cover - validated topology.
                raise PoolBracketValidationError(
                    "A generated bracket source points outside its topology."
                )
            audit_events.append(
                _generated_audit_event(
                    bracket,
                    actor=actor,
                    action=PoolBracketAuditEvent.Action.SLOT_UPDATED,
                    message=(
                        f"Updated {target_tie.round_name} {target_tie.position} "
                        f"{dependent_slot.get_slot_display()} source."
                    ),
                    payload={
                        "slot_id": dependent_slot.pk,
                        "entry_id": desired_entry_id,
                        "life_id": desired_life_id,
                        "source_tie_id": tie.pk,
                    },
                )
            )
            enqueue(target_tie)

    while queue:
        tie = queue.popleft()
        queued_tie_ids.discard(tie.pk)
        if tie.pk in scheduled_match_tie_ids or tie.resolved_winner_id:
            continue
        slots = slots_by_tie.get(tie.pk, {})
        home = slots.get(PoolKnockoutTieSlot.Slot.HOME)
        away = slots.get(PoolKnockoutTieSlot.Slot.AWAY)
        if not home or not away:
            raise PoolBracketValidationError("Bracket tie does not have two slots.")
        occupied = [slot for slot in (home, away) if slot.entry_id]
        sources = {home.source_type, away.source_type}
        if len(occupied) == 2:
            home_life_id = _slot_entry_life_id(home)
            away_life_id = _slot_entry_life_id(away)
            if home.entry_id == away.entry_id:
                if home_life_id and away_life_id and home_life_id != away_life_id:
                    resolve_same_entry(tie, home, away)
                else:
                    raise PoolBracketValidationError(
                        "The same entrant cannot occupy both sides of a bracket tie."
                    )
            else:
                match_projections.append((tie, _generated_match_projection(tie, home, away)))
                scheduled_match_tie_ids.add(tie.pk)
        elif (
            len(occupied) == 1
            and PoolKnockoutTieSlot.SourceType.BYE in sources
        ):
            resolve_bye(tie, occupied[0])
        elif not occupied and sources == {PoolKnockoutTieSlot.SourceType.BYE}:
            raise PoolBracketValidationError("A bracket tie cannot contain two byes.")

    if changed_slots:
        slot_fields = ["entry", "updated_at"]
        if has_slot_life:
            slot_fields.insert(1, "entry_life")
        PoolKnockoutTieSlot.objects.bulk_update(
            list(changed_slots.values()),
            slot_fields,
        )

    if match_projections:
        matches = [projection for _tie, projection in match_projections]
        PoolMatch.objects.bulk_create(matches)
        if any(match.pk is None for match in matches):
            # The stage was verified empty and is locked by confirmation, so
            # this fallback is deterministic on a backend without RETURNING.
            persisted_matches = list(
                PoolMatch.objects.filter(stage_id=bracket.stage_id).order_by("pk")
            )
            if len(persisted_matches) != len(matches):
                raise PoolBracketValidationError(
                    "The generated bracket fixtures could not be identified."
                )
            for match, persisted in zip(matches, persisted_matches):
                match.pk = persisted.pk
        for (tie, _match), match in zip(match_projections, matches):
            tie.match_id = match.pk
            tie.updated_at = now
            changed_ties[tie.pk] = tie

    if changed_ties:
        tie_fields = [
            "match",
            "resolved_winner",
            "resolution_method",
            "resolved_at",
            "updated_at",
        ]
        if has_tie_life:
            tie_fields.insert(2, "resolved_winner_life")
        PoolKnockoutTie.objects.bulk_update(
            list(changed_ties.values()),
            tie_fields,
        )

    # A non-terminal bye leaves its one real life ACTIVE, which is already
    # its persisted state.  Only terminal automatic outcomes need a lifecycle
    # write; using the existing lifecycle service retains its locked model
    # validation and never creates a duplicate entry or life.
    if champion_life_ids:
        Life = _life_model()
        _set_life_statuses(
            champion_life_ids,
            status=Life.Status.CHAMPION,
            eliminated=False,
        )
    if eliminated_life_ids:
        Life = _life_model()
        _set_life_statuses(
            eliminated_life_ids,
            status=Life.Status.ELIMINATED,
            eliminated=True,
        )

    if audit_events:
        PoolBracketAuditEvent.objects.bulk_create(audit_events)


def _generate_bracket_from_preview(
    preview: PoolBracketDrawPreview,
    *,
    actor=None,
    _allow_random=False,
) -> PoolKnockoutBracket:
    """Confirm a persisted draw while the caller owns the transaction.

    The private random capability is used only by
    :func:`generate_random_bracket`, which creates and confirms the order in
    one transaction before it is visible to an organiser.
    """
    preview_id = getattr(preview, "pk", preview)
    try:
        preview = (
            PoolBracketDrawPreview.objects.select_for_update(of=("self",))
            .select_related("competition", "stage", "handoff_from_round")
            .get(pk=preview_id)
        )
    except PoolBracketDrawPreview.DoesNotExist as exc:
        raise PoolBracketValidationError("The bracket draw preview no longer exists.") from exc
    if (
        preview.draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM
        and not _allow_random
    ):
        raise PoolBracketValidationError(
            "Random draws must be confirmed through the one-shot random draw workflow."
        )

    # Result services lock a bracket before the match and competition roots.
    # Keep bracket confirmation in that direction too.  In particular, reset
    # owns all stage previews before it locks this row; taking the competition
    # first here would produce a C <-> B deadlock with a reset or a live
    # result save that already owns its bracket fixture path.
    existing = (
        PoolKnockoutBracket.objects.select_for_update(of=("self",))
        .filter(stage_id=preview.stage_id)
        .first()
    )
    if existing:
        # Only the exact saved preview is idempotent.  Comparing just the
        # draw order would incorrectly accept a second preview that asked for
        # different third-place or per-round race settings.
        if existing.confirmed_preview_id == preview.pk:
            return existing
        raise PoolBracketValidationError("This knockout stage already has a bracket.")

    competition = _locked_competition(preview.competition)
    stage = _locked_stage(competition, preview.stage)
    # A missing bracket has no row to lock.  Recheck after the competition
    # root is acquired so two confirmations cannot both pass the first read.
    existing = (
        PoolKnockoutBracket.objects.select_for_update(of=("self",))
        .filter(stage=stage)
        .first()
    )
    if existing:
        if existing.confirmed_preview_id == preview.pk:
            return existing
        raise PoolBracketValidationError("This knockout stage already has a bracket.")
    handoff_field = None
    if preview.handoff_from_round_id:
        handoff_field = _handoff_round_field(
            competition,
            stage,
            preview.handoff_from_round,
            lock=True,
        )
    elif PoolMatch.objects.filter(stage=stage).exists():
        raise PoolBracketValidationError(
            "This knockout stage has fixtures that are outside the bracket. Remove untouched fixtures before confirming the draw."
        )

    entries = _entry_map(competition)
    if handoff_field is not None:
        expected_entry_ids = set(handoff_field["entry_ids"])
        entries = {
            entry_id: entry
            for entry_id, entry in entries.items()
            if entry_id in expected_entry_ids
        }
        if set(entries) != expected_entry_ids:
            raise PoolBracketValidationError(
                "The manual-round survivor field changed. Create a new bracket handoff preview."
            )
    life_enabled = _two_lives_enabled(competition)
    life_draw_order = _preview_life_draw_order(preview)
    if life_enabled:
        if preview.draw_method == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION:
            raise PoolBracketValidationError(
                "Group qualification draws do not support tournament lives."
            )
        if not life_draw_order:
            raise PoolBracketValidationError(
                "This two-life draw preview is missing its persisted tournament-life order."
            )
        lives = _competition_life_map(competition, lock=True)
        if handoff_field is not None:
            expected_life_ids = set(handoff_field["life_ids"])
            lives = {
                life_id: life
                for life_id, life in lives.items()
                if life_id in expected_life_ids
            }
            if set(lives) != expected_life_ids:
                raise PoolBracketValidationError(
                    "The manual-round survivor lives changed. Create a new bracket handoff preview."
                )
        life_draw_order = _validate_life_draw_order(
            life_draw_order,
            lives=lives,
            bracket_size=preview.bracket_size,
        )
        expected_entry_order = _life_draw_entry_order(life_draw_order, lives)
        if preview.draw_order != expected_entry_order:
            raise PoolBracketValidationError(
                "The preview entry order no longer matches its tournament-life order. Create a new draw preview."
            )
    else:
        entry_ids = [entry_id for entry_id in preview.draw_order if entry_id is not None]
        if any(entry_id not in entries for entry_id in entry_ids):
            raise PoolBracketValidationError(
                "The preview includes an entrant outside this competition."
            )
        if len(entry_ids) != len(set(entry_ids)):
            raise PoolBracketValidationError("The preview includes a duplicate entrant.")
        _validate_draw_size(preview.bracket_size, len(entry_ids))
        if preview.draw_method in {
            PoolBracketDrawPreview.DrawMethod.MANUAL,
            PoolBracketDrawPreview.DrawMethod.RANDOM,
            PoolBracketDrawPreview.DrawMethod.SEEDED,
        } and set(entry_ids) != set(entries):
            raise PoolBracketValidationError(
                "Competition entries changed after this preview. Create and confirm a new draw preview."
            )
    qualification_rules_by_slot = None
    if preview.draw_method == PoolBracketDrawPreview.DrawMethod.GROUP_QUALIFICATION:
        rules = _qualification_order(competition, stage, preview.bracket_size)
        resolved = _resolve_qualification_entries(rules)
        expected = [resolved.get(position) for position in range(1, preview.bracket_size + 1)]
        if expected != preview.draw_order:
            raise PoolBracketValidationError(
                "Group standings changed after this preview. Create and confirm a new draw preview."
            )
        qualification_rules_by_slot = {
            rule.destination_slot: rule for rule in rules
        }

    bracket_kwargs = {
        "competition": competition,
        "stage": stage,
        "size": preview.bracket_size,
        "draw_method": preview.draw_method,
        "draw_order": preview.draw_order,
        "third_place_enabled": preview.third_place_enabled,
        "confirmed_preview": preview,
        "generated_by": actor,
    }
    if handoff_field is not None:
        bracket_kwargs["handoff_from_round"] = handoff_field["source_round"]
    bracket_kwargs.update(
        _life_field_kwargs(
            PoolKnockoutBracket,
            life_draw_order=life_draw_order,
        )
    )
    bracket = PoolKnockoutBracket.objects.create(**bracket_kwargs)
    ties_by_round = _create_topology(
        bracket,
        preview,
        qualification_rules_by_slot=qualification_rules_by_slot,
    )
    validate_bracket(bracket)
    _audit(
        bracket,
        action=PoolBracketAuditEvent.Action.BRACKET_GENERATED,
        actor=actor,
        message=(
            "Confirmed the one-shot random pool bracket draw."
            if preview.draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM
            else "Confirmed the reviewed pool bracket draw."
        ),
        payload={
            "preview_token": str(preview.token),
            "draw_method": preview.draw_method,
            "draw_order": preview.draw_order,
            "life_draw_order": life_draw_order,
            "random_draw_confirmed": (
                preview.draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM
            ),
            "handoff_from_round_id": (
                handoff_field["source_round"].pk if handoff_field is not None else None
            ),
            "handoff_source_match_ids": (
                list(handoff_field["match_ids"]) if handoff_field is not None else []
            ),
            "handoff_survivor_entry_ids": (
                list(handoff_field["entry_ids"]) if handoff_field is not None else []
            ),
            "handoff_survivor_life_ids": (
                list(handoff_field["life_ids"]) if handoff_field is not None else []
            ),
        },
    )
    _materialise_initial_bracket_state(
        bracket,
        ties_by_round,
        actor=actor,
    )
    return bracket


@transaction.atomic
def generate_bracket_from_preview(
    preview: PoolBracketDrawPreview,
    *,
    actor=None,
) -> PoolKnockoutBracket:
    """Confirm exactly one already-reviewed non-random draw preview."""
    return _generate_bracket_from_preview(preview, actor=actor)


@transaction.atomic
def generate_random_bracket(
    competition: PoolCompetition,
    *,
    stage,
    bracket_size,
    third_place_enabled=False,
    round_race_targets=None,
    actor=None,
    confirmed=False,
) -> PoolKnockoutBracket:
    """Generate and reveal an official random bracket exactly once.

    All draw prerequisites are validated and the competition/stage state is
    locked before ``SystemRandom`` is used.  The temporary preview exists
    solely as immutable audit provenance inside this transaction; it is never
    returned or rendered before the matching bracket has been persisted.
    """
    if not confirmed:
        raise PoolBracketValidationError(
            "Confirm the random draw before generating the bracket."
        )
    preview = _create_draw_preview(
        competition,
        stage=stage,
        draw_method=PoolBracketDrawPreview.DrawMethod.RANDOM,
        bracket_size=bracket_size,
        third_place_enabled=third_place_enabled,
        round_race_targets=round_race_targets,
        actor=actor,
        _allow_random=True,
    )
    return _generate_bracket_from_preview(
        preview,
        actor=actor,
        _allow_random=True,
    )


@transaction.atomic
def generate_random_handoff_bracket(
    competition: PoolCompetition,
    *,
    stage,
    source_round,
    third_place_enabled=False,
    round_race_targets=None,
    actor=None,
    confirmed=False,
) -> PoolKnockoutBracket:
    """One-shot random bracket from a locked manual-round survivor field."""

    if not confirmed:
        raise PoolBracketValidationError(
            "Confirm the random draw before generating the bracket."
        )
    competition = _locked_competition(competition)
    stage = _locked_stage(competition, stage)
    handoff_field = _handoff_round_field(
        competition,
        stage,
        source_round,
        lock=True,
    )
    preview = _create_draw_preview(
        competition,
        stage=stage,
        draw_method=PoolBracketDrawPreview.DrawMethod.RANDOM,
        bracket_size=handoff_field["bracket_size"],
        third_place_enabled=third_place_enabled,
        round_race_targets=round_race_targets,
        actor=actor,
        _allow_random=True,
        _handoff_field=handoff_field,
    )
    return _generate_bracket_from_preview(preview, actor=actor, _allow_random=True)


def _late_bye_slot_position(slot):
    """Return the stable 1-based bracket position of a first-round slot."""

    return (slot.tie.position - 1) * 2 + (
        1 if slot.slot == PoolKnockoutTieSlot.Slot.HOME else 2
    )


def _late_bye_candidate_is_safe(tie):
    """Whether a provisional BYE can be retracted without rewinding play."""

    if tie.match_id or _tie_has_competitive_activity(tie, locked=True):
        return False
    if not tie.resolved_winner_id:
        return tie.resolution_method == PoolKnockoutTie.ResolutionMethod.PENDING
    if tie.resolution_method != PoolKnockoutTie.ResolutionMethod.BYE:
        return False
    try:
        dependent_slots = list(
            PoolKnockoutTieSlot.objects.select_for_update(of=("self",))
            .select_related("tie", "tie__match")
            .filter(source_tie=tie)
            .order_by("tie_id", "slot", "pk")
        )
        for dependent_slot in dependent_slots:
            _assert_safe_to_replace_slot(dependent_slot)
    except PoolBracketCorrectionBlocked:
        return False
    return True


def _late_open_bye_candidates(bracket, first_round_ties):
    """Return only direct, safely reversible first-round BYE sides.

    A group-qualification source is never a late-registration capacity slot:
    its occupant must continue to come from authoritative group standings.
    """

    slots_by_tie = _tie_slots_for_ties(first_round_ties, lock=True)
    candidates = []
    for tie in first_round_ties:
        slots = slots_by_tie.get(tie.pk, {})
        home = slots.get(PoolKnockoutTieSlot.Slot.HOME)
        away = slots.get(PoolKnockoutTieSlot.Slot.AWAY)
        if not home or not away:
            raise PoolBracketValidationError("Bracket tie does not have two slots.")
        bye_slot = next(
            (
                slot
                for slot in (home, away)
                if slot.source_type == PoolKnockoutTieSlot.SourceType.BYE
                and not slot.entry_id
                and not _slot_entry_life_id(slot)
                and not slot.source_entry_id
                and not _slot_source_entry_life_id(slot)
                and not slot.source_group_id
                and not slot.source_tie_id
            ),
            None,
        )
        companion = away if bye_slot is home else home if bye_slot is away else None
        if (
            bye_slot is None
            or companion is None
            or companion.source_type != PoolKnockoutTieSlot.SourceType.DIRECT_ENTRY
            or not companion.entry_id
            or companion.source_entry_id != companion.entry_id
        ):
            continue
        if not _late_bye_candidate_is_safe(tie):
            continue
        candidates.append(
            {
                "tie": tie,
                "bye_slot": bye_slot,
                "position": _late_bye_slot_position(bye_slot),
            }
        )
    return candidates


def _select_late_bye_candidates(
    bracket,
    candidates,
    *,
    requested_lives,
    preferred_bye_slot_ids=None,
):
    """Select no more than two safe BYE slots without redrawing entrants."""

    if not candidates:
        raise PoolBracketValidationError(
            "Late registration is closed because no safe open first-round BYE remains."
        )

    random_draw = bracket.draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM
    preferred_ids = []
    for value in preferred_bye_slot_ids or ():
        try:
            preferred_ids.append(int(value))
        except (TypeError, ValueError) as exc:
            raise PoolBracketValidationError("Choose valid open BYE slots.") from exc
    if len(set(preferred_ids)) != len(preferred_ids):
        raise PoolBracketValidationError("Choose each open BYE slot only once.")

    life_aware = bool(_bracket_life_draw_order(bracket))
    half_boundary = bracket.size // 2
    top_candidates = [
        candidate
        for candidate in candidates
        if candidate["position"] <= half_boundary
    ]
    bottom_candidates = [
        candidate
        for candidate in candidates
        if candidate["position"] > half_boundary
    ]

    if requested_lives == 1:
        # A late one-life entrant is always Life 1 in an existing life-aware
        # topology; it cannot be placed into the lower Life 2 path.
        eligible = top_candidates if life_aware else candidates
        if not eligible:
            raise PoolBracketValidationError(
                "Late registration is closed because no safe Life 1 BYE remains."
                if life_aware
                else "Late registration is closed because no safe open first-round BYE remains."
            )
        if preferred_ids and not random_draw:
            if len(preferred_ids) != 1:
                raise PoolBracketValidationError("Choose one open BYE for one life.")
            selected = next(
                (
                    candidate
                    for candidate in eligible
                    if candidate["bye_slot"].pk == preferred_ids[0]
                ),
                None,
            )
            if selected is None:
                raise PoolBracketValidationError(
                    "That first-round BYE is no longer open for the required life path."
                )
            return [selected]
        if random_draw:
            return [random.SystemRandom().choice(eligible)]
        return [min(eligible, key=lambda candidate: candidate["position"])]

    if requested_lives != 2:
        raise PoolBracketValidationError("Choose one or two tournament lives.")

    if life_aware:
        # Keep the return order aligned with assigned_lives below: Life 1 is
        # written to the top half and Life 2 to the bottom half.
        if not top_candidates or not bottom_candidates:
            raise PoolBracketValidationError(
                "Two lives require one safe Life 1 BYE in the top half and one "
                "safe Life 2 BYE in the bottom half."
            )
        if preferred_ids and not random_draw:
            if len(preferred_ids) != 2:
                raise PoolBracketValidationError("Choose two open BYEs for two lives.")
            by_slot_id = {
                candidate["bye_slot"].pk: candidate
                for candidate in [*top_candidates, *bottom_candidates]
            }
            selected = [by_slot_id.get(slot_id) for slot_id in preferred_ids]
            if None in selected:
                raise PoolBracketValidationError(
                    "Choose one safe top-half Life 1 BYE and one safe bottom-half Life 2 BYE."
                )
            top = next(
                (candidate for candidate in selected if candidate in top_candidates),
                None,
            )
            bottom = next(
                (candidate for candidate in selected if candidate in bottom_candidates),
                None,
            )
            if top is None or bottom is None:
                raise PoolBracketValidationError(
                    "Choose one safe top-half Life 1 BYE and one safe bottom-half Life 2 BYE."
                )
            return [top, bottom]
        if random_draw:
            rng = random.SystemRandom()
            return [rng.choice(top_candidates), rng.choice(bottom_candidates)]
        return [
            min(top_candidates, key=lambda candidate: candidate["position"]),
            min(bottom_candidates, key=lambda candidate: candidate["position"]),
        ]

    # A non-life-aware bracket cannot normally request two lives, but retain
    # the legacy defensive selection for a transitional historical bracket.
    pairs = [
        (left, right)
        for index, left in enumerate(candidates)
        for right in candidates[index + 1 :]
        if (left["position"] <= half_boundary) != (right["position"] <= half_boundary)
    ]
    if not pairs:
        raise PoolBracketValidationError(
            "Two lives require two safe open BYEs in opposite bracket halves."
        )
    if preferred_ids and not random_draw:
        if len(preferred_ids) != 2:
            raise PoolBracketValidationError("Choose two open BYEs for two lives.")
        preferred_pair = next(
            (
                pair
                for pair in pairs
                if {pair[0]["bye_slot"].pk, pair[1]["bye_slot"].pk}
                == set(preferred_ids)
            ),
            None,
        )
        if preferred_pair is None:
            raise PoolBracketValidationError(
                "Choose two safe open BYEs in opposite bracket halves."
            )
        return list(preferred_pair)
    if random_draw:
        return list(random.SystemRandom().choice(pairs))
    return list(
        min(
            pairs,
            key=lambda pair: tuple(
                sorted(candidate["position"] for candidate in pair)
            ),
        )
    )


@transaction.atomic
def create_handoff_draw_preview(
    competition: PoolCompetition,
    *,
    stage,
    source_round,
    draw_method=PoolBracketDrawPreview.DrawMethod.SEEDED,
    manual_entry_ids=None,
    manual_life_ids=None,
    third_place_enabled=False,
    round_race_targets=None,
    actor=None,
) -> PoolBracketDrawPreview:
    """Create a reviewable draw for only the survivors of manual rounds.

    This is a deliberately separate public service so ordinary bracket
    creation retains its longstanding ban on pre-existing knockout fixtures.
    Confirmation recomputes this exact field again before topology is saved.
    """

    competition = _locked_competition(competition)
    stage = _locked_stage(competition, stage)
    handoff_field = _handoff_round_field(
        competition,
        stage,
        source_round,
        lock=True,
    )
    return _create_draw_preview(
        competition,
        stage=stage,
        draw_method=draw_method,
        bracket_size=handoff_field["bracket_size"],
        manual_entry_ids=manual_entry_ids,
        manual_life_ids=manual_life_ids,
        third_place_enabled=third_place_enabled,
        round_race_targets=round_race_targets,
        actor=actor,
        _handoff_field=handoff_field,
    )


@transaction.atomic
def place_late_entry_in_open_bye(
    competition: PoolCompetition,
    *,
    player,
    requested_lives=1,
    preferred_bye_slot_ids=None,
    actor=None,
):
    """Create one direct singles entry by consuming safe first-round BYEs.

    This is intentionally the only post-confirmation entry path.  It never
    regenerates topology or moves a pre-existing participant; it only swaps a
    structural BYE source for a newly registered player and safely retracts a
    provisional BYE advancement when that untouched downstream work exists.
    """

    competition_id = getattr(competition, "pk", competition)
    player_id = getattr(player, "pk", player)
    if not competition_id or not player_id:
        raise PoolBracketValidationError("Choose a saved competition and active player.")
    try:
        requested_lives = int(requested_lives or 1)
    except (TypeError, ValueError) as exc:
        raise PoolBracketValidationError("Choose either one or two lives.") from exc
    if requested_lives not in {1, 2}:
        raise PoolBracketValidationError("Choose either one or two lives.")

    brackets = list(
        PoolKnockoutBracket.objects.select_for_update(of=("self",))
        .select_related("competition", "stage")
        .filter(competition_id=competition_id)
        .order_by("pk")
    )
    if len(brackets) != 1:
        raise PoolBracketValidationError(
            "Late BYE registration is available only for one generated direct knockout bracket."
        )
    bracket = brackets[0]
    locked_competition = PoolCompetition.objects.select_for_update().get(
        pk=bracket.competition_id
    )
    if (
        locked_competition.participant_mode
        != PoolCompetition.ParticipantMode.SINGLES
        or locked_competition.format_type != PoolCompetition.FormatType.KNOCKOUT
        or bracket.stage.stage_type != PoolCompetitionStage.StageType.KNOCKOUT
    ):
        raise PoolBracketValidationError(
            "Late BYE registration is available only for direct singles knockout competitions."
        )
    if requested_lives == 2 and not _two_lives_enabled(locked_competition):
        raise PoolBracketValidationError("Two lives are not available for this competition.")

    locked_player = PoolPlayer.objects.select_for_update().filter(pk=player_id).first()
    if locked_player is None or not locked_player.is_active:
        raise PoolBracketValidationError("Choose an active existing player.")
    if PoolCompetitionEntry.objects.filter(
        competition=locked_competition,
        player_id=locked_player.pk,
    ).exists():
        raise PoolBracketValidationError(
            "This player is already entered in this competition."
        )

    first_round_ties = list(
        PoolKnockoutTie.objects.select_for_update(of=("self",))
        .select_related("bracket__competition", "bracket__stage", "match")
        .filter(bracket=bracket, round_number=1, is_third_place=False)
        .order_by("position", "pk")
    )
    _lock_matches_for_ties(first_round_ties)
    if not first_round_ties:
        raise PoolBracketValidationError("The bracket has no first knockout round.")
    if all(tie.resolved_winner_id for tie in first_round_ties):
        raise PoolBracketValidationError(
            "Late registration is closed because the first knockout round has completed."
        )

    # Direct qualification slots must keep deriving from group standings; a
    # Group + Knockout bracket is never a late-registration capacity pool.
    if PoolKnockoutTieSlot.objects.filter(
        tie__bracket=bracket,
        source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
    ).exists():
        raise PoolBracketValidationError(
            "Late BYE registration cannot change group qualification slots."
        )
    candidates = _late_open_bye_candidates(bracket, first_round_ties)
    selected_candidates = _select_late_bye_candidates(
        bracket,
        candidates,
        requested_lives=requested_lives,
        preferred_bye_slot_ids=preferred_bye_slot_ids,
    )

    with _late_knockout_entry_creation_scope(locked_competition.pk):
        entry = PoolCompetitionEntry.objects.create(
            competition=locked_competition,
            player=locked_player,
            seed=None,
        )
        lives = {
            life.life_number: life
            for life in _life_model()
            .objects.select_for_update()
            .filter(competition_entry=entry)
            .order_by("life_number", "pk")
        }
        if 1 not in lives:
            raise PoolBracketValidationError("The new entry is missing its permanent Life 1.")
        if requested_lives == 2:
            lives[2] = _life_model().objects.create(
                competition_entry=entry,
                life_number=2,
            )

        # Every selected source is still guarded by the source/tie/descendant
        # locks held above.  A BYE resolution can only be retracted when none
        # of the downstream graph has started; otherwise the whole atomic
        # registration fails and no player/entry is left behind.
        for candidate in selected_candidates:
            tie = candidate["tie"]
            if tie.resolved_winner_id or _tie_resolved_winner_life_id(tie):
                _retract_tie_resolution(
                    tie,
                    actor=actor,
                    reason="Late entrant filled an open first-round BYE.",
                )

        assigned_lives = [
            lives[number]
            for number in range(1, requested_lives + 1)
        ]

        draw_order = list(bracket.draw_order or [])
        life_draw_order = list(_bracket_life_draw_order(bracket))

        # A competition may have PoolCompetitionEntryLife rows even when its
        # confirmed bracket is not life-aware. Only brackets with a persisted
        # life_draw_order should carry life identity on their tie slots/matches.
        #
        # Without this distinction a normal one-life knockout can become:
        #
        # existing player -> no entry_life
        # late player     -> Life 1
        #
        # and _ensure_match_for_tie() correctly rejects that mixed identity.
        life_aware = bool(life_draw_order)

        if len(draw_order) != bracket.size or (
            life_aware and len(life_draw_order) != bracket.size
        ):
            raise PoolBracketValidationError(
                "The saved bracket draw order is invalid."
            )
        for candidate, life in zip(selected_candidates, assigned_lives):
            slot = candidate["bye_slot"]
            _write_trusted_late_bye_slot(
                slot,
                tie=candidate["tie"],
                entry=entry,
                # Tournament-life identity belongs on bracket slots only when
                # the confirmed bracket itself is life-aware.  Ordinary
                # one-life brackets retain their historical null metadata.
                entry_life=life if life_aware else None,
            )
            position = candidate["position"]
            draw_order[position - 1] = entry.pk

            if life_aware:
                life_draw_order[position - 1] = life.pk
            _audit(
                bracket,
                action=PoolBracketAuditEvent.Action.SLOT_UPDATED,
                actor=actor,
                message=(
                    f"Late entrant filled open {tie.round_name} {tie.position} BYE "
                    "without redrawing the bracket."
                ),
                payload={
                    "late_entry": True,
                    "slot_id": slot.pk,
                    "entry_id": entry.pk,
                    "life_id": life.pk if life_aware else None,
                    "bracket_position": position,
                },
            )

        bracket.draw_order = draw_order
        update_fields = ["draw_order", "updated_at"]

        if life_aware:
            bracket.life_draw_order = life_draw_order
            update_fields.insert(1, "life_draw_order")

        bracket.save(update_fields=update_fields)
        validate_bracket(bracket)

        for candidate in selected_candidates:
            tie = _lock_tie_match_for_activity(candidate["tie"])
            _ensure_match_for_tie(tie, actor=actor, _locked_tie=True)

        _audit(
            bracket,
            action=PoolBracketAuditEvent.Action.SLOT_UPDATED,
            actor=actor,
            message="Late knockout entry was placed into an existing open BYE.",
            payload={
                "late_entry": True,
                "entry_id": entry.pk,
                "life_ids": [life.pk for life in assigned_lives],
                "slot_ids": [candidate["bye_slot"].pk for candidate in selected_candidates],
                "draw_method": bracket.draw_method,
            },
        )
    return entry


@transaction.atomic
def configure_group_qualification(rule: PoolGroupQualificationRule, *, actor=None):
    """Validate a stored mapping without accepting an entrant from POST data."""
    rule_id = getattr(rule, "pk", rule)
    try:
        rule = (
            PoolGroupQualificationRule.objects.select_for_update()
            .select_related("source_stage", "group", "knockout_stage")
            .get(pk=rule_id)
        )
    except PoolGroupQualificationRule.DoesNotExist as exc:
        raise PoolBracketValidationError("Qualification rule no longer exists.") from exc
    if rule.group.stage_id != rule.source_stage_id:
        raise PoolBracketValidationError("Qualification group is outside its source stage.")
    if rule.position > rule.group.memberships.count():
        raise PoolBracketValidationError(
            "Qualification position exceeds the group's current entries."
        )
    bracket = PoolKnockoutBracket.objects.filter(stage=rule.knockout_stage).first()
    if bracket:
        raise PoolBracketValidationError(
            "Qualification mappings cannot change after the destination bracket is generated."
        )
    return rule


def _match_tie(match, *, lock=False):
    queryset = PoolKnockoutTie.objects.select_related(
        "bracket__competition",
        "bracket__stage",
        "match",
    )
    if lock:
        # Result services take the bracket root and source fixture first.
        # Lock just this non-null topology row afterwards; PostgreSQL cannot
        # lock the nullable match side of the select-related join.
        queryset = queryset.select_for_update(of=("self",))
    try:
        return queryset.get(match=match)
    except PoolKnockoutTie.DoesNotExist:
        return None


def _winner_life_for_match(pool_match):
    """Resolve the winning life from the persisted home/away life identity."""

    if pool_match.winner_id == pool_match.home_entry_id:
        expected_life_id = _match_life_id(pool_match, "home")
    elif pool_match.winner_id == pool_match.away_entry_id:
        expected_life_id = _match_life_id(pool_match, "away")
    else:
        return None
    recorded_life_id = getattr(pool_match, "winner_entry_life_id", None)
    if recorded_life_id is not None and recorded_life_id != expected_life_id:
        raise PoolBracketValidationError(
            "The recorded winning tournament life does not match the fixture winner."
        )
    return expected_life_id


def _synchronise_match_winner_life(pool_match, winner_life_id):
    """Persist bracket-derived winner identity without changing the real winner."""

    if not _model_has_field(PoolMatch, "winner_entry_life"):
        return pool_match
    if pool_match.winner_entry_life_id == winner_life_id:
        return pool_match
    pool_match.winner_entry_life_id = winner_life_id
    pool_match.save(update_fields=["winner_entry_life", "updated_at"])
    return pool_match


@transaction.atomic
def refresh_bracket_progression_for_match(
    match,
    *,
    actor=None,
    reason="",
):
    """Synchronously advance or safely retract one bracket-linked result.

    This public boundary always takes the full bracket -> match lock ordering.
    Internal result services that already hold those locks use the private
    helper below instead; a public keyword must never weaken concurrency
    protection for an arbitrary caller.
    """

    match_id = getattr(match, "pk", match)
    # Direct callers need the same bracket -> match ordering as the result
    # services.  Re-read the fixture while it is locked so stale caller
    # instances cannot decide progression.
    lock_bracket_dependencies_for_match(match_id)
    try:
        match = (
            PoolMatch.objects.select_for_update(of=("self",))
            .select_related("competition", "stage", "home_entry", "away_entry", "winner")
            .get(pk=match_id)
        )
    except PoolMatch.DoesNotExist:
        return None
    return _refresh_bracket_progression_for_locked_match(
        match,
        actor=actor,
        reason=reason,
    )


def _refresh_bracket_progression_for_locked_match(match, *, actor=None, reason=""):
    """Advance one result while the caller owns its bracket roots and match.

    Private result-service helper.  The caller must already hold the bracket
    dependency rows followed by the current fixture row in an atomic block.
    Keeping that capability out of the public function signature prevents a
    future view/import caller from accidentally bypassing lock ordering.
    """

    if not isinstance(match, PoolMatch) or not match.pk:
        raise PoolBracketValidationError(
            "The locked progression path requires a saved pool match instance."
        )
    if not transaction.get_connection().in_atomic_block:
        raise PoolBracketValidationError(
            "The locked progression path requires an active transaction."
        )
    tie = _match_tie(match, lock=True)
    if tie is None:
        # A group result can still feed a generated qualification bracket.
        if match.stage.stage_type == PoolCompetitionStage.StageType.GROUP:
            refresh_group_qualification_for_match(match, actor=actor)
        return None
    eligible = (
        match.status == PoolMatch.Status.FINISHED
        and not match.is_in_correction_mode
        and match.winner_id in {match.home_entry_id, match.away_entry_id}
        and match.result_method
        in {
            PoolMatch.ResultMethod.PLAYED,
            PoolMatch.ResultMethod.WALKOVER,
            PoolMatch.ResultMethod.WITHDRAWAL,
            PoolMatch.ResultMethod.DISQUALIFICATION,
        }
    )
    if eligible:
        winner_life_id = _winner_life_for_match(match)
        if _bracket_life_draw_order(tie.bracket) and not winner_life_id:
            raise PoolBracketValidationError(
                "A life-aware knockout result must identify the winning tournament life."
            )
        _synchronise_match_winner_life(match, winner_life_id)
        # Re-run idempotent propagation even when this source already carries
        # the same resolution.  A transaction rollback normally prevents a
        # half-advanced graph, but retaining this repair boundary ensures an
        # otherwise untouched downstream fixture can be recreated safely if a
        # historic/imported graph is incomplete.  The target tie lock and
        # trusted materialiser keep this from producing a duplicate fixture.
        trusted_match_slots = _set_trusted_match_tie_resolution(
            tie,
            match,
            winner_life_id=winner_life_id,
        )
        if trusted_match_slots is None:
            _set_tie_resolution(
                tie,
                match.winner_id,
                life_id=winner_life_id,
                method=PoolKnockoutTie.ResolutionMethod.MATCH,
                actor=actor,
            )
        _propagate_tie_resolution(
            tie,
            actor=actor,
            _trusted_match_slots=trusted_match_slots,
        )
    else:
        _retract_tie_resolution(tie, actor=actor, reason=reason)
    return tie


def _retract_tie_resolution(tie, *, actor=None, reason=""):
    if not tie.resolved_winner_id and _tie_resolved_winner_life_id(tie) is None:
        return tie
    dependent_tie_ids = list(
        PoolKnockoutTieSlot.objects.filter(source_tie=tie)
        .order_by("tie_id")
        .values_list("tie_id", flat=True)
        .distinct()
    )
    locked_ties = _lock_ties(dependent_tie_ids)
    slots = list(
        PoolKnockoutTieSlot.objects.select_for_update(of=("self",))
        .select_related("tie", "tie__match")
        .filter(source_tie=tie)
        .order_by("tie_id", "slot", "pk")
    )
    for slot in slots:
        _assert_safe_to_replace_slot(slot)
    _clear_tie_resolution(tie)
    for slot in slots:
        if slot.entry_id is not None or _slot_entry_life_id(slot) is not None:
            _save_slot_entry(slot, entry_id=None, entry_life_id=None)
        dependent = locked_ties[slot.tie_id]
        _clear_tie_match_if_safe(dependent)
    return tie


@transaction.atomic
def preflight_result_change(
    match,
    *,
    prospective_winner=None,
    prospective_eligible=None,
    actor=None,
):
    """Block only result changes that would replace/retract consumed advancement.

    The result service calls this before opening a correction and before an
    actual changed score/result is persisted.  It is deliberately conservative
    when the exact prospective winner is not known: a linked completed result
    with consumed downstream work cannot be opened for a speculative rewrite.
    """
    match_id = getattr(match, "pk", match)
    lock_bracket_dependencies_for_match(match_id)
    try:
        locked_match = PoolMatch.objects.select_for_update(of=("self",)).get(
            pk=match_id
        )
    except PoolMatch.DoesNotExist:
        return None
    return _preflight_result_change_for_locked_match(
        locked_match,
        prospective_winner=prospective_winner,
        prospective_eligible=prospective_eligible,
        actor=actor,
    )


def _preflight_result_change_for_locked_match(
    match,
    *,
    prospective_winner=None,
    prospective_eligible=None,
    actor=None,
):
    """Preflight a result while the caller owns bracket roots and its match.

    This private counterpart is used by result services after ``_lock_match``.
    The public wrapper above deliberately has no lock-bypass option.
    """

    if not isinstance(match, PoolMatch) or not match.pk:
        raise PoolBracketValidationError(
            "The locked preflight path requires a saved pool match instance."
        )
    if not transaction.get_connection().in_atomic_block:
        raise PoolBracketValidationError(
            "The locked preflight path requires an active transaction."
        )
    tie = _match_tie(match, lock=True)
    if tie is None:
        # A handoff bracket is dependent on completed manual-round history,
        # even though those fixtures are not represented by PoolKnockoutTie.
        # Their source rows are locked by ``lock_bracket_dependencies_for_match``
        # before this method is reached, so refuse a correction rather than
        # allowing the generated survivor field to become historical fiction.
        if match.knockout_round_id:
            source_round = PoolKnockoutRoundDefinition.objects.filter(
                pk=match.knockout_round_id
            ).values("stage_id", "order").first()
            if source_round and PoolKnockoutBracket.objects.filter(
                handoff_from_round__stage_id=source_round["stage_id"],
                handoff_from_round__order__gte=source_round["order"],
            ).exists():
                raise PoolBracketCorrectionBlocked(
                    "Reset the untouched handoff bracket before correcting or reopening this manual knockout result."
                )
        # Group qualification is recomputed after the authoritative result
        # mutation.  That refresh compares each actual selected qualifier with
        # its materialised slot and only applies the dependency guard when a
        # slot would change.  Preflighting every group slot here would block
        # harmless/no-op corrections merely because another bracket match has
        # already started.
        return None
    winner_id = getattr(prospective_winner, "pk", prospective_winner)
    current = tie.resolved_winner_id
    if prospective_eligible is False:
        desired = None
    elif prospective_winner is not None:
        desired = winner_id
    else:
        # Opening a completed correction makes its source result temporarily
        # ineligible.  Until an explicit corrected winner is known, treat it
        # as a possible retraction so consumed downstream work is protected.
        desired = None if current else current
    if desired == current:
        return tie
    if current is None and desired is not None:
        # This is a first-time advancement, not a replacement.  Every direct
        # dependent slot still derives from an unresolved source tie, so no
        # downstream fixture can have been materialised through that slot.
        # The surrounding bracket-root lock prevents another completion from
        # filling it between this preflight and the authoritative result save.
        # Keep the full descendant activity guard for corrections/retractions,
        # where a previously consumed source really can invalidate live work.
        return tie
    for slot in PoolKnockoutTieSlot.objects.select_related("tie", "tie__match").filter(
        source_tie=tie
    ):
        _assert_safe_to_replace_slot(slot)
    return tie


def _qualification_slots_for_group_stage(stage):
    return PoolKnockoutTieSlot.objects.select_related(
        "tie", "tie__bracket", "tie__match", "source_group", "source_tie"
    ).filter(
        source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
        source_group__stage=stage,
    )


@transaction.atomic
def refresh_group_qualification_for_match(match, *, actor=None):
    """Refresh generated qualifier slots from the authoritative group tables.

    Every qualifier source in a bracket fed by the changed stage is resolved
    together.  That both prevents cross-stage duplicate entrants and avoids a
    transient duplicate fixture when two positions in one first-round tie
    swap following a result correction.
    """
    match_id = getattr(match, "pk", match)
    lock_bracket_dependencies_for_match(match_id)
    match = PoolMatch.objects.select_for_update(of=("self",)).select_related("stage", "group").get(
        pk=match_id
    )
    if match.stage.stage_type != PoolCompetitionStage.StageType.GROUP:
        return []

    affected_bracket_ids = list(
        _qualification_slots_for_group_stage(match.stage)
        .order_by("tie__bracket_id")
        .values_list("tie__bracket_id", flat=True)
        .distinct()
    )
    if not affected_bracket_ids:
        return []

    # Serialize at the target tie level before locking its nullable-source
    # slot rows.  This uses the same canonical lock order as normal knockout
    # advancement and is safe on PostgreSQL as well as SQLite tests.
    affected_tie_ids = list(
        PoolKnockoutTieSlot.objects.filter(
            tie__bracket_id__in=affected_bracket_ids,
            source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
        )
        .order_by("tie_id")
        .values_list("tie_id", flat=True)
        .distinct()
    )
    locked_ties = _lock_ties(affected_tie_ids)
    slots = list(
        PoolKnockoutTieSlot.objects.select_for_update(of=("self",))
        .select_related("tie", "tie__match", "source_group")
        .filter(
            tie__bracket_id__in=affected_bracket_ids,
            source_type=PoolKnockoutTieSlot.SourceType.GROUP_QUALIFIER,
        )
        .order_by("tie__bracket_id", "tie_id", "slot", "pk")
    )
    desired_by_slot = {}
    desired_by_bracket = defaultdict(list)
    for slot in slots:
        rows = standings_for_stage(slot.source_group.stage, group=slot.source_group)
        desired = (
            rows[slot.source_position - 1].entry_id
            if slot.source_position and len(rows) >= slot.source_position
            else None
        )
        desired_by_slot[slot.pk] = desired
        if desired is not None:
            desired_by_bracket[slot.tie.bracket_id].append(desired)

    for desired_entries in desired_by_bracket.values():
        if len(desired_entries) != len(set(desired_entries)):
            raise PoolBracketValidationError(
                "A participant cannot occupy more than one qualifying slot in a bracket."
            )

    changed = []
    changed_tie_ids = set()
    for slot in slots:
        desired = desired_by_slot[slot.pk]
        if slot.entry_id == desired and _slot_entry_life_id(slot) is None:
            continue
        _assert_safe_to_replace_slot(slot)

    # Apply every selected qualifier first.  Swapping two places in the same
    # first-round tie must never materialise an intermediate duplicate pairing
    # (for example A2 v A2) or briefly retain a stale fixture.
    for slot in slots:
        desired = desired_by_slot[slot.pk]
        if slot.entry_id == desired and _slot_entry_life_id(slot) is None:
            continue
        _save_slot_entry(slot, entry_id=desired, entry_life_id=None)
        changed.append(slot)
        changed_tie_ids.add(slot.tie_id)

    for tie_id in sorted(changed_tie_ids):
        tie = locked_ties[tie_id]
        target_slots = _tie_slots(tie, lock=True)
        if (
            not target_slots[PoolKnockoutTieSlot.Slot.HOME].entry_id
            or not target_slots[PoolKnockoutTieSlot.Slot.AWAY].entry_id
        ):
            _clear_tie_match_if_safe(tie)
            _resolve_bye_if_ready(tie, actor=actor)
        else:
            _ensure_match_for_tie(tie, actor=actor)
    return changed


@transaction.atomic
def update_bracket_round_targets(bracket, targets, *, actor=None):
    """Change generated-bracket round targets without re-saving each fixture.

    A bracket owns its rules, ties, and materialised fixtures.  Lock and
    validate that complete projection first, then write the target columns in
    one trusted lifecycle batch.  Regular PoolMatch edits still use the model
    validation boundary and cannot use this private path.
    """

    bracket_id = getattr(bracket, "pk", bracket)
    bracket = (
        PoolKnockoutBracket.objects.select_for_update(of=("self",))
        .select_related("competition", "stage")
        .get(pk=bracket_id)
    )
    rules = {
        rule.pk: rule
        for rule in PoolKnockoutRoundRule.objects.select_for_update(of=("self",))
        .filter(bracket=bracket)
        .order_by("round_number", "is_third_place", "pk")
    }
    if set(targets) != set(rules):
        raise PoolBracketValidationError("Round targets must cover this bracket exactly.")

    requested_targets = {}
    for rule_id, target in targets.items():
        if isinstance(target, bool):
            raise PoolBracketValidationError("Race targets must be whole numbers.")
        try:
            target = int(target)
        except (TypeError, ValueError) as exc:
            raise PoolBracketValidationError("Race targets must be whole numbers.") from exc
        if target < 1:
            raise PoolBracketValidationError("Race targets must be at least 1.")
        if (
            bracket.competition.participant_mode
            == PoolCompetition.ParticipantMode.TEAMS
            and target > 13
        ):
            # Keep the normal PoolMatch validation boundary's fixed 25-rack
            # team-score-sheet ceiling for both existing and future ties.
            raise PoolBracketValidationError(
                "A team match race target cannot exceed 13: a 25-rack, "
                "no-draw schedule must be able to produce a winner."
            )
        requested_targets[rule_id] = target

    rules_by_round = {
        (rule.round_number, rule.is_third_place): rule for rule in rules.values()
    }
    ties = list(
        PoolKnockoutTie.objects.select_for_update(of=("self",))
        .filter(bracket=bracket)
        .order_by("round_number", "is_third_place", "position", "pk")
    )
    _lock_matches_for_ties(ties)

    changed_ties = []
    changed_match_targets = {}
    for tie in ties:
        rule = rules_by_round.get((tie.round_number, tie.is_third_place))
        if rule is None:
            raise PoolBracketValidationError(
                "Every bracket tie must have a matching round rule."
            )
        target = requested_targets[rule.pk]
        pool_match = tie.match if tie.match_id else None
        if tie.race_target == target and (
            pool_match is None or pool_match.race_target == target
        ):
            continue
        changed_ties.append((tie, target))
        if pool_match is not None:
            if (
                pool_match.competition_id != bracket.competition_id
                or pool_match.stage_id != bracket.stage_id
            ):
                raise PoolBracketValidationError(
                    "Bracket fixtures must belong to the bracket competition and stage."
                )
            changed_match_targets[pool_match.pk] = target

    # The fixture rows were locked above, so one rack-presence query covers
    # the complete requested batch and preserves the existing activity
    # definition without a tie -> fixture lock/save cycle per row.
    rack_match_ids = _locked_rack_match_ids([tie for tie, _target in changed_ties])
    for tie, _target in changed_ties:
        if _tie_has_competitive_activity(
            tie,
            locked=True,
            rack_match_ids=rack_match_ids,
        ):
            raise PoolBracketValidationError(
                f"Cannot change {tie.round_name} race target after it has started."
            )

    # A team score sheet is a separate immutable operational contract even
    # before a rack begins.  The old PoolMatch.save() path enforced this via
    # clean(); retain it explicitly before taking the trusted batch write.
    if changed_match_targets and (
        bracket.competition.participant_mode
        == PoolCompetition.ParticipantMode.TEAMS
    ):
        scheduled_match_ids = set(
            PoolTeamMatchScheduledRack.objects.select_for_update(of=("self",))
            .filter(match_id__in=sorted(changed_match_targets))
            .values_list("match_id", flat=True)
        )
        if scheduled_match_ids:
            raise PoolBracketValidationError(
                "The race target cannot change after the team score sheet has been generated."
            )

    changed_rules = []
    for rule in rules.values():
        target = requested_targets[rule.pk]
        if rule.race_target != target:
            rule.race_target = target
            changed_rules.append(rule)
    if changed_rules:
        PoolKnockoutRoundRule.objects.bulk_update(changed_rules, ["race_target"])

    if changed_ties:
        now = timezone.now()
        changed_tie_rows = []
        for tie, target in changed_ties:
            if tie.race_target != target:
                tie.race_target = target
                tie.updated_at = now
                changed_tie_rows.append(tie)
        if changed_tie_rows:
            PoolKnockoutTie.objects.bulk_update(
                changed_tie_rows,
                ["race_target", "updated_at"],
            )
    if changed_match_targets:
        PoolMatch.objects._update_locked_bracket_race_targets(
            bracket_id=bracket.pk,
            targets_by_match_id=changed_match_targets,
        )
    return bracket


@transaction.atomic
def reset_bracket(bracket, *, actor=None, reason=""):
    """Remove a bracket only while every generated fixture is untouched."""
    bracket_id = getattr(bracket, "pk", bracket)
    # Lock every preview for this stage before its bracket root.  Apart from
    # preserving a single lock order with confirmation, this makes reset an
    # atomic lifecycle boundary for stale unconfirmed previews from other
    # organisers: none can survive and permanently block entry/life edits.
    try:
        bracket_scope = PoolKnockoutBracket.objects.select_related("competition").get(
            pk=bracket_id
        )
    except PoolKnockoutBracket.DoesNotExist as exc:
        raise PoolBracketValidationError(
            "This bracket has already been reset or no longer exists."
        ) from exc
    preview_ids = list(
        PoolBracketDrawPreview.objects.select_for_update()
        .filter(stage_id=bracket_scope.stage_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    # Result mutations use bracket -> match -> competition.  Taking the
    # bracket before the root means a reset either waits for that complete
    # result transaction or owns the path before a scorer can start one.
    try:
        bracket = (
            PoolKnockoutBracket.objects.select_for_update(of=("self",))
            .select_related("competition", "stage")
            .get(pk=bracket_id)
        )
    except PoolKnockoutBracket.DoesNotExist as exc:
        raise PoolBracketValidationError(
            "This bracket has already been reset or no longer exists."
        ) from exc
    PoolCompetition.objects.select_for_update().get(pk=bracket.competition_id)
    if bracket.draw_method == PoolBracketDrawPreview.DrawMethod.RANDOM:
        raise PoolBracketValidationError(
            "This random draw has already been confirmed and cannot be drawn again."
        )
    ties = list(
        PoolKnockoutTie.objects.select_for_update(of=("self",)).select_related("match").filter(
            bracket=bracket
        )
    )
    if any(_tie_has_reset_activity(tie) for tie in ties):
        raise PoolBracketValidationError(
            "This bracket cannot be reset after a fixture has started, recorded racks, or a result."
        )
    match_ids = [tie.match_id for tie in ties if tie.match_id]
    _audit(
        bracket,
        action=PoolBracketAuditEvent.Action.BRACKET_RESET,
        actor=actor,
        message=reason or "Bracket reset before competitive activity.",
        payload={"stage_id": bracket.stage_id, "match_ids": match_ids},
    )
    # Null the audit bracket FK before deletion so the reset record itself is
    # retained as a historical action rather than cascaded away.
    PoolBracketAuditEvent.objects.filter(bracket=bracket).update(bracket=None)
    PoolMatch.objects.filter(pk__in=match_ids).delete()
    # Slot provenance deliberately uses PROTECT so an incomplete/active graph
    # can never disappear as a side effect of a tie edit.  During this
    # explicitly authorised, preflighted reset every slot belongs to the same
    # untouched bracket, so remove the graph leaves before its ties/bracket.
    PoolKnockoutTieSlot.objects.filter(tie__bracket=bracket).delete()
    bracket.delete()
    if preview_ids:
        # The bracket is gone now, so the model guard intentionally permits
        # removal of the formerly confirmed audit source as well as every
        # stale, unconfirmed preview we locked above.
        PoolBracketDrawPreview.objects.filter(pk__in=preview_ids).delete()
    return True


def _slot_display_label(slot):
    """Return a life label only where the bracket actually carries a life."""

    if slot is None:
        return "TBD"
    life = getattr(slot, "entry_life", None)
    if life is not None:
        return life.display_name
    if slot.entry_id:
        return slot.entry.display_name
    return slot.source_label


def bracket_display(bracket):
    """Return a manager-friendly persistent graph without relying on ID order."""
    bracket_id = getattr(bracket, "pk", bracket)
    bracket = PoolKnockoutBracket.objects.get(pk=bracket_id)
    tie_relations = [
        "match",
        "resolved_winner__player",
        "resolved_winner__team",
    ]
    if _model_has_field(PoolKnockoutTie, "resolved_winner_life"):
        tie_relations.append("resolved_winner_life__competition_entry__player")
    slot_relations = [
        "slots__entry__player",
        "slots__entry__team",
        "slots__source_entry__player",
        "slots__source_entry__team",
        "slots__source_group",
        "slots__source_tie",
    ]
    if _model_has_field(PoolKnockoutTieSlot, "entry_life"):
        slot_relations.extend(
            [
                "slots__entry_life__competition_entry__player",
                "slots__source_entry_life__competition_entry__player",
            ]
        )
    ties = list(
        bracket.ties.select_related(*tie_relations)
        .prefetch_related(*slot_relations)
        .order_by("round_number", "is_third_place", "position", "id")
    )
    rounds = []
    groups = {}
    large_draw = bracket.size >= 64
    first_round_half_tie_count = bracket.size // 4
    for tie in ties:
        key = (tie.round_number, tie.is_third_place, tie.round_name)
        if key not in groups:
            groups[key] = {
                "number": tie.round_number,
                "name": tie.round_name,
                "is_third_place": tie.is_third_place,
                "ties": [],
            }
            rounds.append(groups[key])
        slots = {slot.slot: slot for slot in tie.slots.all()}
        home_slot = slots.get(PoolKnockoutTieSlot.Slot.HOME)
        away_slot = slots.get(PoolKnockoutTieSlot.Slot.AWAY)
        half_heading = ""
        draw_half = ""
        if large_draw and tie.round_number == 1 and not tie.is_third_place:
            draw_half = (
                "top"
                if tie.position <= first_round_half_tie_count
                else "bottom"
            )
            if tie.position == 1:
                half_heading = "Top half"
            elif tie.position == first_round_half_tie_count + 1:
                half_heading = "Bottom half"
        groups[key]["ties"].append(
            {
                "tie": tie,
                "home_slot": home_slot,
                "away_slot": away_slot,
                "home_label": _slot_display_label(home_slot),
                "away_label": _slot_display_label(away_slot),
                "same_entry_final": (
                    tie.resolution_method == _same_entry_final_resolution_method()
                ),
                "same_entry_third_place": (
                    tie.resolution_method == _same_entry_third_resolution_method()
                ),
                "champion_label": (
                    tie.resolved_winner.display_name
                    if tie.resolution_method == _same_entry_final_resolution_method()
                    and tie.resolved_winner_id
                    else ""
                ),
                "third_place_label": (
                    tie.resolved_winner.display_name
                    if tie.resolution_method == _same_entry_third_resolution_method()
                    and tie.resolved_winner_id
                    else ""
                ),
                "draw_half": draw_half,
                "half_heading": half_heading,
            }
        )
    return {
        "rounds": rounds,
        "ties": ties,
        "large_draw": large_draw,
        "first_round_half_tie_count": first_round_half_tie_count,
    }
